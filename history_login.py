"""Run locally once; credentials and session never pass through the bot chat."""
import asyncio
from bot import load_env
from history_reader import ROOT, make_client
import os


async def login():
    client = make_client()
    try:
        await client.start()
        me = await client.get_me()
        if me.bot or me.id != int(os.getenv('OWNER_USER_ID', '0')):
            raise ValueError('Login must use the account matching OWNER_USER_ID')
        print('History account authorized. You can now use /remember in a private chat.')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    load_env(ROOT / '.env')
    try:
        asyncio.run(login())
    except Exception:
        print('History login failed. Check API credentials, owner account and network/proxy; no credentials were printed.')
        raise SystemExit(1)
