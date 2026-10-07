import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from bot import ApiError, Bot, Config, JsonClient, Ollama, Telegram, load_env


def message(chat=1, text="سلام", user=10):
    return {"chat": {"id": chat, "type": "private"}, "from": {"id": user}, "text": text}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tg, self.ai = Mock(), Mock()
        self.ai.chat.return_value = "پاسخ"
        self.bot = Bot(Config("test", history_turns=1, max_chats=2), self.tg, self.ai)

    def test_history_isolated_trimmed_and_reset(self):
        calls = []
        self.ai.chat.side_effect = lambda msgs: calls.append(list(msgs)) or "پاسخ"
        self.bot.respond(message(1, "اول"))
        self.bot.respond(message(2, "دیگر"))
        self.assertEqual(calls[1], [{"role": "user", "content": "دیگر"}])
        self.bot.respond(message(1, "دوم"))
        self.assertEqual(len(calls[2]), 3)
        self.assertEqual(self.bot.history[1][0]["content"], "دوم")
        self.bot.respond(message(1, "/reset"))
        self.assertNotIn(1, self.bot.history)
        self.assertIn(2, self.bot.history)

    def test_failed_generation_does_not_save_history(self):
        self.ai.chat.side_effect = ApiError("Ollama")
        self.bot.respond(message())
        self.assertFalse(self.bot.history)
        self.assertIn("Ollama", self.tg.send.call_args.args[1])

    def test_failed_delivery_releases_busy_without_saving(self):
        self.tg.send.side_effect = ApiError("Telegram")
        self.bot.busy.add(1)
        self.bot.work(message())
        self.assertFalse(self.bot.history)
        self.assertFalse(self.bot.busy)

    def test_memory_limit(self):
        for chat in (1, 2, 3):
            self.bot.respond(message(chat))
        self.assertEqual(list(self.bot.history), [2, 3])

    def test_commands_nontext_and_oversize_skip_model(self):
        for text in ("/start", "/help", "/reset", "/model", "/unknown", "", "a" * 8001):
            self.bot.respond(message(text=text))
        self.ai.chat.assert_not_called()

    def test_private_chat_and_allowlist(self):
        bot = Bot(Config("test", allowed_users=frozenset({10})), self.tg, self.ai)
        self.assertTrue(bot.accept(message()))
        self.assertFalse(bot.accept(message(user=11)))
        msg = message()
        msg["chat"]["type"] = "group"
        self.assertFalse(bot.accept(msg))
        self.assertFalse(bot.accept({}))

    def test_polling_routes_message_and_advances_offset(self):
        def call(method, payload, **kwargs):
            if method == "getMe":
                return {"username": "hexbot"}
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                if payload["offset"] == 0:
                    return [{"update_id": 21, "message": message()}]
                self.assertEqual(payload["offset"], 22)
                raise ApiError("Telegram", 401)
            return True
        self.tg.call.side_effect = call
        with self.assertRaises(ValueError):
            self.bot.run()
        self.ai.chat.assert_called_once()

    def test_webhook_is_not_deleted(self):
        self.tg.call.side_effect = [{"username": "hexbot"}, {"url": "https://example.com"}]
        with self.assertRaises(ValueError):
            self.bot.run()
        self.assertEqual([c.args[0] for c in self.tg.call.call_args_list], ["getMe", "getWebhookInfo"])

    def test_long_emoji_response(self):
        tg = Telegram(Config("test"))
        tg.call = Mock()
        text = "😀" * 4500
        tg.send(1, text)
        parts = [c.args[1]["text"] for c in tg.call.call_args_list]
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts))


class ConfigTests(unittest.TestCase):
    def test_missing_token_and_invalid_limits(self):
        for env in ({}, {"TELEGRAM_BOT_TOKEN": "test", "HISTORY_TURNS": "0"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                Config.from_env()

    def test_env_and_priority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OLLAMA_MODEL": "existing"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('# comment\nTELEGRAM_BOT_TOKEN="test"\nOLLAMA_MODEL=other\nALLOWED_USER_IDS=1, 2\n', encoding="utf-8")
            load_env(path)
            config = Config.from_env()
            self.assertEqual(config.model, "existing")
            self.assertEqual(config.allowed_users, frozenset({1, 2}))


class HttpTests(unittest.TestCase):
    def test_real_http_ollama_payload_and_errors(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200 if self.path == "/api/chat" else 429)
                self.end_headers()
                reply = {"message": {"content": "پاسخ محلی"}} if self.path == "/api/chat" else {"parameters": {"retry_after": 2}}
                self.wfile.write(json.dumps(reply).encode())
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            ai = Ollama(Config("secret", ollama_url=url))
            self.assertEqual(ai.chat([{"role": "user", "content": "سلام"}]), "پاسخ محلی")
            path, payload = captured[0]
            self.assertEqual(path, "/api/chat")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["model"], "qwen2.5:3b")
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["messages"][1]["content"], "سلام")
            with self.assertRaises(ApiError) as ctx:
                JsonClient(url, "Telegram").post("/secret", {})
            self.assertEqual(ctx.exception.retry_after, 2)
            self.assertNotIn("secret", str(ctx.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
