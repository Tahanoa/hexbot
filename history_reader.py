"""Optional, read-only access to the owner's private history via MTProto."""
import asyncio
import os
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent


def make_client():
    try:
        from telethon import TelegramClient
    except ImportError:
        raise ValueError('Install requirements-history.txt first') from None
    api_id, api_hash = os.getenv('TELEGRAM_API_ID', ''), os.getenv('TELEGRAM_API_HASH', '')
    if not api_id.isdigit() or not api_hash:
        raise ValueError('Set TELEGRAM_API_ID and TELEGRAM_API_HASH, then run history_login.py')
    session = ROOT / os.getenv('TELEGRAM_USER_SESSION', 'data/history-account')
    session.parent.mkdir(parents=True, exist_ok=True)
    proxy = None
    proxy_url = os.getenv('HISTORY_PROXY_URL', '').strip()
    if proxy_url:
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in ('socks5', 'socks4', 'http') or not parsed.hostname or not parsed.port:
            raise ValueError('HISTORY_PROXY_URL must be a socks5/socks4/http proxy with a port')
        proxy = {'proxy_type': parsed.scheme, 'addr': parsed.hostname, 'port': parsed.port,
                 'rdns': True, 'username': unquote(parsed.username or ''), 'password': unquote(parsed.password or '')}
    return TelegramClient(str(session), int(api_id), api_hash, proxy=proxy,
                          receive_updates=False, request_retries=2, connection_retries=2,
                          flood_sleep_threshold=0)


class HistoryReader:
    def __init__(self, owner_id):
        self.owner_id = owner_id
        self.lock = threading.Lock()

    def read(self, chat, limit=300):
        with self.lock:
            return asyncio.run(asyncio.wait_for(self._read(chat, min(300, limit)), timeout=90))

    async def _read(self, chat, limit):
        client = make_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('Run history_login.py on your computer first')
            me = await client.get_me()
            if me.id != self.owner_id or me.bot:
                raise ValueError('The history session must belong to OWNER_USER_ID')
            # Resolve only a private chat from the owner's own dialog list.
            peer = None
            async for dialog in client.iter_dialogs():
                if dialog.id == chat and dialog.is_user:
                    peer = dialog.input_entity
                    break
            if peer is None:
                raise ValueError('Private chat not found in the owner account')
            rows = []
            messages = await client.get_messages(peer, limit=limit)
            for message in reversed(messages):
                text = message.raw_text or '[پیام غیرمتنی؛ محتوای فایل خوانده نشده است]'
                automated = bool(getattr(message, 'via_business_bot_id', None))
                role = 'assistant' if automated else ('owner' if message.out else 'user')
                if role == 'owner' and text.strip().split(maxsplit=1)[:1] in (
                        ['/stop'], ['/resume'], ['/remember'], ['/forget'], ['/review']):
                    continue
                rows.append({'message_id': message.id, 'role': role, 'content': text,
                             'date': int(message.date.timestamp())})
            return rows
        finally:
            await client.disconnect()
