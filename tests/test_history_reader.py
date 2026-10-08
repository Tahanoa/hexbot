import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from history_reader import HistoryReader


class HistoryReaderTests(unittest.TestCase):
    def client(self, owner=10, private=True):
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=owner, bot=False))
        async def dialogs():
            yield SimpleNamespace(id=20, is_user=private, input_entity='peer')
        client.iter_dialogs.side_effect = dialogs
        date = datetime.datetime.now(datetime.timezone.utc)
        client.get_messages = AsyncMock(return_value=[
            SimpleNamespace(id=3, raw_text='پاسخ', out=True, via_business_bot_id=99, date=date),
            SimpleNamespace(id=2, raw_text='/stop', out=True, date=date),
            SimpleNamespace(id=1, raw_text='سلام', out=False, date=date)])
        return client

    def test_reads_at_most_300_chronologically_and_excludes_owner_controls(self):
        client = self.client()
        with patch('history_reader.make_client', return_value=client):
            rows = HistoryReader(10).read(20, 500)
        client.get_messages.assert_awaited_once_with('peer', limit=300)
        self.assertEqual([r['message_id'] for r in rows], [1, 3])
        self.assertEqual([r['role'] for r in rows], ['user', 'assistant'])
        client.disconnect.assert_awaited_once()
        client.send_message.assert_not_called()
        client.delete_messages.assert_not_called()

    def test_wrong_owner_cannot_read_history(self):
        client = self.client(owner=11)
        with patch('history_reader.make_client', return_value=client), self.assertRaises(ValueError):
            HistoryReader(10).read(20)
        client.get_messages.assert_not_awaited()
        client.disconnect.assert_awaited_once()

    def test_non_private_dialog_cannot_read_history(self):
        client = self.client(private=False)
        with patch('history_reader.make_client', return_value=client), self.assertRaises(ValueError):
            HistoryReader(10).read(20)
        client.get_messages.assert_not_awaited()

    def test_session_without_login_never_prompts_for_credentials(self):
        client = self.client()
        client.is_user_authorized.return_value = False
        with patch('history_reader.make_client', return_value=client), self.assertRaises(ValueError):
            HistoryReader(10).read(20)
        client.start.assert_not_called()
        client.get_messages.assert_not_awaited()
