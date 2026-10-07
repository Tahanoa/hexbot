import json
import os
import socket
import ssl
import subprocess
import sys
from urllib.error import URLError
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

    def test_all_private_users_accepted(self):
        bot = Bot(Config("test"), self.tg, self.ai)
        self.assertTrue(bot.accept(message()))
        self.assertTrue(bot.accept(message(user=11)))
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
        for env in ({}, {"TELEGRAM_BOT_TOKEN": "test", "HISTORY_TURNS": "0"},
                    {"TELEGRAM_BOT_TOKEN": "test", "OLLAMA_NUM_CTX": "0"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                Config.from_env()

    def test_env_and_priority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OLLAMA_MODEL": "existing"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('# comment\nTELEGRAM_BOT_TOKEN="test"\nOLLAMA_MODEL=other\nALLOWED_USER_IDS=1, 2\n', encoding="utf-8")
            load_env(path)
            config = Config.from_env()
            self.assertEqual(config.model, "existing")
            self.assertEqual(config.context_length, 8192)

    def test_custom_context_length(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "OLLAMA_NUM_CTX": "16384"}, clear=True):
            self.assertEqual(Config.from_env().context_length, 16384)


class NetworkTests(unittest.TestCase):
    def test_secretary_script_startup_network_error_is_caught(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile.json'
            profile.write_text(json.dumps({'owner_name': 'owner'}))
            env = dict(os.environ, TELEGRAM_BOT_TOKEN='test', BUSINESS_MODE='true',
                OWNER_USER_ID='10', SECRETARY_PROFILE=str(profile),
                SECRETARY_DATABASE=str(Path(directory) / 'state.sqlite3'))
            script = """import runpy
from unittest.mock import Mock, patch
from urllib.error import URLError
opener = Mock()
opener.open.side_effect = URLError(ConnectionRefusedError('refused'))
with patch('urllib.request.build_opener', return_value=opener):
    runpy.run_path('bot.py', run_name='__main__')
"""
            result = subprocess.run([sys.executable, '-c', script], env=env,
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn('ERROR Telegram request failed', result.stderr)
            self.assertIn('Connection refused', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_telegram_uses_system_proxy_and_ollama_bypasses_it(self):
        with patch('bot.ProxyHandler') as handler, patch('bot.build_opener'):
            Telegram(Config('secret'))
            handler.assert_called_with(None)
            Ollama(Config('secret'))
            handler.assert_called_with({})
            Telegram(Config('secret', telegram_proxy='http://localhost:8080'))
            handler.assert_called_with({'http': 'http://localhost:8080', 'https': 'http://localhost:8080'})

    def test_safe_error_categories_without_sensitive_details(self):
        cases = [
            (socket.gaierror('https://example.com/botSECRET'), 'DNS resolution failed'),
            (TimeoutError('https://example.com/botSECRET'), 'Connection timed out'),
            (ConnectionRefusedError('password SECRET'), 'Connection refused'),
            (ssl.SSLCertVerificationError('token SECRET'), 'TLS certificate verification failed'),
            (ConnectionResetError('SECRET'), 'Connection reset'),
            (ValueError('SECRET'), 'Invalid server response'),
        ]
        for cause, label in cases:
            for exc in (cause, URLError(cause)):
                client = JsonClient('https://example.com/botSECRET/', 'Telegram', proxy='')
                client.opener = Mock()
                client.opener.open.side_effect = exc
                with self.assertRaises(ApiError) as ctx:
                    client.post('getMe', {})
                self.assertIn(label, str(ctx.exception))
                self.assertNotIn('SECRET', str(ctx.exception))
                self.assertNotIn('https://', str(ctx.exception))


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
            ai = Ollama(Config("secret", ollama_url=url, context_length=16384))
            self.assertEqual(ai.chat([{"role": "user", "content": "سلام"}]), "پاسخ محلی")
            path, payload = captured[0]
            self.assertEqual(path, "/api/chat")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["options"]["num_ctx"], 16384)
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
