"""Telegram text bot backed exclusively by a local Ollama server (Python 3.10+)."""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

LOG = logging.getLogger("hexbot")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            raise ValueError("Invalid .env entry")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    token: str
    model: str = "qwen2.5:3b"
    ollama_url: str = "http://127.0.0.1:11434"
    timeout: int = 180
    history_turns: int = 8
    max_chats: int = 100
    workers: int = 2
    system_prompt: str = "You are a helpful assistant. Reply in the user's language, clearly and concisely."
    telegram_proxy: str = ""
    context_length: int = 8192

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token == "your_bot_token_here":
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env")
        url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("OLLAMA_BASE_URL must be an HTTP(S) server URL")
        proxy = os.getenv("TELEGRAM_PROXY_URL", "").strip()
        if proxy and urlsplit(proxy).scheme not in ("http", "https"):
            raise ValueError("TELEGRAM_PROXY_URL must be an HTTP(S) proxy")
        values = {name: int(os.getenv(env, str(default))) for name, env, default in (
            ("timeout", "OLLAMA_TIMEOUT", 180), ("history_turns", "HISTORY_TURNS", 8),
            ("max_chats", "MAX_CHATS", 100), ("workers", "MAX_CONCURRENT_CHATS", 2),
            ("context_length", "OLLAMA_NUM_CTX", 8192))}
        if any(value <= 0 for value in values.values()):
            raise ValueError("Numeric settings must be positive")
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b").strip()
        if not model:
            raise ValueError("OLLAMA_MODEL cannot be empty")
        return cls(token=token, model=model, ollama_url=url, telegram_proxy=proxy,
                   system_prompt=os.getenv("SYSTEM_PROMPT", cls.system_prompt), **values)


class ApiError(Exception):
    def __init__(self, service: str, status: int = 0, retry_after: int = 0, reason: str = ""):
        self.service, self.status, self.retry_after = service, status, retry_after
        self.reason = reason
        detail = f"; {reason}" if reason else ""
        super().__init__(f"{service} request failed (status {status}{detail})")


def network_reason(exc) -> str:
    """Return fixed diagnostic labels without URLs, tokens or proxy credentials."""
    cause = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(cause, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed"
    if isinstance(cause, ssl.SSLError):
        return "TLS connection failed"
    if isinstance(cause, socket.gaierror):
        return "DNS resolution failed"
    if isinstance(cause, TimeoutError) or getattr(cause, "winerror", None) == 10060:
        return "Connection timed out"
    if isinstance(cause, ConnectionRefusedError) or getattr(cause, "winerror", None) == 10061:
        return "Connection refused; check VPN/proxy address and port"
    if isinstance(cause, ConnectionResetError):
        return "Connection reset by peer"
    if isinstance(cause, ValueError):
        return "Invalid server response or proxy configuration"
    return "Network connection failed; check API connectivity and VPN/proxy routing"


class JsonClient:
    def __init__(self, base_url: str, service: str, proxy: str | None = None):
        self.base_url, self.service = base_url, service
        # None: use OS/environment proxy settings. Empty string: force direct.
        proxies = None if proxy is None else ({"http": proxy, "https": proxy} if proxy else {})
        self.opener = build_opener(ProxyHandler(proxies))

    def post(self, path: str, payload: dict, timeout: int = 30) -> dict:
        request = Request(self.base_url + path, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            retry_after = 0
            try:
                retry_after = int(json.load(exc).get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError, AttributeError):
                pass
            raise ApiError(self.service, exc.code, retry_after) from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            # Never log raw URLs/errors: Telegram URLs contain the bot token.
            raise ApiError(self.service, reason=network_reason(exc)) from None
        if not isinstance(data, dict):
            raise ApiError(self.service)
        return data


class Telegram:
    def __init__(self, config: Config):
        self.client = JsonClient(f"https://api.telegram.org/bot{config.token}/", "Telegram", config.telegram_proxy or None)

    def call(self, method: str, payload: dict, timeout: int = 30):
        data = self.client.post(method, payload, timeout)
        if not data.get("ok"):
            raise ApiError("Telegram", data.get("error_code", 0), data.get("parameters", {}).get("retry_after", 0))
        return data["result"]

    def send(self, chat_id: int, text: str):
        # 2,000 code points also fit within 4,096 UTF-16 units for emoji-only replies.
        for start in range(0, len(text), 2000):
            started = time.perf_counter()
            LOG.info("Telegram sending; chat ID: %s; part: %s", chat_id, start // 2000 + 1)
            self.call("sendMessage", {"chat_id": chat_id, "text": text[start:start + 2000]})
            LOG.info("Telegram accepted reply; chat ID: %s; send: %.2fs", chat_id, time.perf_counter() - started)


class Ollama:
    def __init__(self, config: Config):
        self.config = config
        self.client = JsonClient(config.ollama_url, "Ollama", proxy="")

    def chat(self, messages: list[dict], system_prompt: str | None = None) -> str:
        data = self.client.post("/api/chat", {"model": self.config.model,
            "messages": [{"role": "system", "content": self.config.system_prompt if system_prompt is None else system_prompt}] + messages,
            "stream": False, "options": {"num_predict": 2048,
                "num_ctx": self.config.context_length}}, self.config.timeout)
        answer = data.get("message", {}).get("content", "")
        if data.get("error") or not isinstance(answer, str) or not answer.strip():
            raise ApiError("Ollama")
        def metric(name):
            value = data.get(name, 0)
            return value if isinstance(value, (int, float)) and value >= 0 else 0
        duration = metric("eval_duration") / 1e9
        LOG.info("Ollama timings; load: %.2fs; prompt: %.2fs; generation: %.2fs; output tokens: %s; tokens/s: %.1f",
                 metric("load_duration") / 1e9, metric("prompt_eval_duration") / 1e9,
                 duration, metric("eval_count"), metric("eval_count") / duration if duration else 0)
        return answer.strip()


class Bot:
    def __init__(self, config: Config, telegram=None, ollama=None):
        self.config = config
        self.telegram = telegram or Telegram(config)
        self.ollama = ollama or Ollama(config)
        self.history: OrderedDict[int, list[dict]] = OrderedDict()
        self.lock = threading.Lock()
        self.busy: set[int] = set()
        self.stop = threading.Event()

    def respond(self, message: dict):
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if command in ("/start", "/help"):
            self.telegram.send(chat_id, "سلام! پیام متنی بفرست تا با هوش مصنوعی محلی پاسخ بدهم.\n/reset — پاک‌کردن حافظهٔ گفتگو\n/model — نمایش مدل")
            return
        if command == "/whoami":
            self.telegram.send(chat_id, f"شناسه شما: {message.get('from', {}).get('id')}")
            return
        if command == "/reset":
            with self.lock:
                self.history.pop(chat_id, None)
            self.telegram.send(chat_id, "حافظهٔ گفتگو پاک شد.")
            return
        if command == "/model":
            self.telegram.send(chat_id, f"مدل محلی: {self.config.model}")
            return
        if not text:
            self.telegram.send(chat_id, "فعلاً فقط پیام متنی پشتیبانی می‌شود.")
            return
        if command.startswith("/"):
            self.telegram.send(chat_id, "دستور ناشناخته؛ /help را بفرست.")
            return
        if len(text) > 8000:
            self.telegram.send(chat_id, "لطفاً پیام را کوتاه‌تر از ۸۰۰۰ نویسه بفرست.")
            return
        with self.lock:
            messages = list(self.history.get(chat_id, []))
        messages.append({"role": "user", "content": text})
        try:
            self.telegram.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except ApiError:
            pass
        try:
            answer = self.ollama.chat(messages)
        except ApiError as exc:
            LOG.warning("%s", exc)
            self.telegram.send(chat_id, "پاسخ از Ollama دریافت نشد. اجرا بودن Ollama، نصب مدل و تنظیمات اتصال را بررسی کن و دوباره پیام بده.")
            return
        self.telegram.send(chat_id, answer)
        messages.append({"role": "assistant", "content": answer})
        with self.lock:
            self.history[chat_id] = messages[-self.config.history_turns * 2:]
            self.history.move_to_end(chat_id)
            while len(self.history) > self.config.max_chats:
                self.history.popitem(last=False)

    def work(self, message: dict):
        try:
            self.respond(message)
        except ApiError as exc:
            LOG.warning("%s", exc)
        except Exception:
            LOG.error("Unexpected message processing error")
        finally:
            with self.lock:
                self.busy.discard(message["chat"]["id"])

    def accept(self, message: dict) -> bool:
        return (message.get("chat", {}).get("type") == "private"
                and not message.get("from", {}).get("is_bot", False))

    @property
    def update_types(self):
        return ["message"]

    def dispatch_update(self, update: dict, pool):
        message = update.get("message", {})
        if not self.accept(message):
            return
        chat_id = message["chat"]["id"]
        with self.lock:
            occupied = chat_id in self.busy or len(self.busy) >= self.config.workers
            if not occupied:
                self.busy.add(chat_id)
        if occupied:
            self.telegram.send(chat_id, "در حال پاسخ‌دادن هستم؛ لطفاً کمی بعد دوباره پیام بده.")
        else:
            pool.submit(self.work, message)

    def run(self):
        me = self.telegram.call("getMe", {})
        info = self.telegram.call("getWebhookInfo", {})
        if info.get("url"):
            raise ValueError("An active Telegram webhook exists. Remove it before using polling.")
        LOG.info("@%s running; local model: %s", me.get("username", "bot"), self.config.model)
        offset = 0
        with ThreadPoolExecutor(max_workers=self.config.workers) as pool:
            while not self.stop.is_set():
                try:
                    updates = self.telegram.call("getUpdates", {"offset": offset, "timeout": 25,
                        "allowed_updates": self.update_types}, timeout=35)
                    for update in updates:
                        if self.stop.is_set():
                            break
                        self.dispatch_update(update, pool)
                        offset = update["update_id"] + 1
                except ApiError as exc:
                    LOG.warning("%s", exc)
                    if exc.status in (401, 409):
                        raise ValueError("Invalid bot token or another polling instance is running") from None
                    self.stop.wait(max(3, exc.retry_after))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_env(Path(__file__).resolve().with_name(".env"))
        config = Config.from_env()
        if os.getenv("BUSINESS_MODE", "false").lower() == "true":
            from business import BusinessBot, SecretaryConfig
            bot = BusinessBot(config, SecretaryConfig.from_env())
        else:
            bot = Bot(config)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: bot.stop.set())
        bot.run()
    except (ValueError, ApiError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    # business imports bot; reuse this module so ApiError has one identity.
    sys.modules["bot"] = sys.modules[__name__]
    raise SystemExit(main())
