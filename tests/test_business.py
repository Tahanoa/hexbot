import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot import Config
from business import BusinessBot, SecretaryConfig, State


class ImmediatePool:
    def submit(self, fn, *args):
        fn(*args)


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'owner_name': 'صاحب حساب', 'facts': ['زمان پاسخ مشخص نیست']}))
        self.config = SecretaryConfig(10, profile, root / 'state.sqlite3')
        self.tg, self.ai = Mock(), Mock()
        self.conn = {'id': 'A', 'user': {'id': 10}, 'is_enabled': True, 'rights': {'can_reply': True}}
        self.tg.call.side_effect = lambda method, data, **kw: dict(self.conn) if method == 'getBusinessConnection' else True
        self.ai.chat.return_value = 'سلام، من دستیار خودکار هستم.'
        self.bot = BusinessBot(Config('test'), self.config, self.tg, self.ai)
        self.pool = ImmediatePool()

    def tearDown(self):
        self.bot.state.db.close()
        self.temp.cleanup()

    def msg(self, text='سلام', user=20, chat=20, connection='A'):
        return {'business_connection_id': connection, 'chat': {'id': chat, 'type': 'private'},
                'from': {'id': user, 'first_name': 'مهمان'}, 'date': int(time.time()), 'text': text}

    def dispatch(self, msg):
        self.bot.dispatch_update({'business_message': msg}, self.pool)

    def sends(self):
        return [c.args[1] for c in self.tg.call.call_args_list if c.args[0] == 'sendMessage']

    def test_business_reply_has_connection_and_profile(self):
        self.dispatch(self.msg())
        self.assertEqual(self.sends()[0]['business_connection_id'], 'A')
        self.assertEqual(self.sends()[0]['chat_id'], 20)
        self.assertIn('صاحب حساب', self.ai.chat.call_args.kwargs['system_prompt'])

    def test_owner_manual_reply_pauses_and_resume_works(self):
        self.dispatch(self.msg(user=10))
        self.dispatch(self.msg())
        self.ai.chat.assert_not_called()
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/resume 20'})
        self.dispatch(self.msg())
        self.ai.chat.assert_called_once()

    def test_chats_lists_paused_chats_without_handoff_and_after_restart(self):
        self.dispatch(self.msg(user=10))
        self.assertEqual(self.bot.state.inbox(), [])
        reopened = State(self.config.database_path)
        try:
            self.assertEqual(reopened.chats(), [(20, 1)])
        finally:
            reopened.db.close()
        self.bot.dispatch_update({'message': {'chat': {'id': 30, 'type': 'private'},
            'from': {'id': 30}, 'text': '/chats'}}, self.pool)
        self.tg.send.assert_not_called()
        self.bot.dispatch_update({'message': {'chat': {'id': 10, 'type': 'private'},
            'from': {'id': 10}, 'text': '/chats'}}, self.pool)
        self.assertIn('20', self.tg.send.call_args.args[1])
        self.assertIn('متوقف', self.tg.send.call_args.args[1])

    def test_status_reports_missing_connection_then_reply_permission(self):
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/status'})
        self.assertIn('دریافت نشده', self.tg.send.call_args.args[1])
        self.bot.connections['A'] = dict(self.conn, rights={'can_reply': False})
        self.bot.state.set_enabled(False)
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/status'})
        reply = self.tg.send.call_args.args[1]
        self.assertIn('منشی: خاموش', reply)
        self.assertIn('اجازه پاسخ: خیر', reply)
        self.assertNotIn('facts', reply)

    def test_echo_and_disabled_wrong_owner_old_messages_ignored(self):
        msg = self.msg()
        msg['via_business_bot'] = {'id': 99}
        self.dispatch(msg)
        self.conn['is_enabled'] = False
        self.dispatch(self.msg())
        self.conn.update(is_enabled=True, user={'id': 11})
        self.dispatch(self.msg())
        self.conn['user'] = {'id': 10}
        msg = self.msg()
        msg['date'] = 1
        self.dispatch(msg)
        self.ai.chat.assert_not_called()
        self.assertFalse(self.sends())

    def test_permission_removed_before_generation_finishes(self):
        def generate(*args, **kwargs):
            self.conn['rights'] = {}
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertFalse(self.sends())

    def test_manual_intervention_during_generation_discards_reply(self):
        def generate(*args, **kwargs):
            self.dispatch(self.msg(user=10))
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertFalse(self.sends())
        self.assertFalse(self.bot.busy)

    def test_global_off_on_invalidates_running_reply(self):
        def generate(*args, **kwargs):
            self.bot.state.set_enabled(False)
            self.bot.state.set_enabled(True)
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertFalse(self.sends())

    def test_handoff_persists_and_pauses(self):
        self.dispatch(self.msg('/human لطفاً با من تماس بگیرید'))
        self.ai.chat.assert_not_called()
        self.assertEqual(self.bot.state.inbox()[0][3], 'لطفاً با من تماس بگیرید')
        self.assertEqual(self.bot.state.snapshot(('A', 20))[0], 1)
        second = State(self.config.database_path)
        self.assertEqual(len(second.inbox()), 1)
        self.assertEqual(second.snapshot(('A', 20))[0], 1)
        second.db.close()
        self.dispatch(self.msg())
        self.ai.chat.assert_not_called()

    def test_handoff_during_generation_cancels_generated_reply(self):
        def generate(*args, **kwargs):
            self.dispatch(self.msg('/human درخواست من'))
            return 'پاسخ دیرهنگام'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn('دیرهنگام', self.sends()[0]['text'])

    def test_private_controls_owner_only(self):
        for user in (20, 10):
            self.bot.dispatch_update({'message': {'chat': {'id': user, 'type': 'private'},
                'from': {'id': user}, 'text': '/secretary off'}}, self.pool)
            self.assertEqual(self.bot.state.enabled(), user != 10)

    def test_history_separate_for_connections(self):
        self.dispatch(self.msg(connection='A'))
        self.dispatch(self.msg(connection='B'))
        self.assertEqual(len(self.ai.chat.call_args.args[0]), 2)
        self.assertIn(('A', 20), self.bot.history)
        self.assertIn(('B', 20), self.bot.history)

    def test_pause_survives_restart_before_connection_is_cached(self):
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/pause 20'})
        second = State(self.config.database_path)
        self.assertEqual(second.snapshot(('NEW', 20))[0], 1)
        second.resume_chat(20)
        self.assertEqual(second.snapshot(('NEW', 20))[0], 0)
        second.db.close()

    def test_missing_profile_fails_clearly(self):
        with self.assertRaises(ValueError):
            SecretaryConfig(10, Path(self.temp.name) / 'absent').prompt()

    def test_followup_queued_while_model_running(self):
        answers = []
        def generate(msgs, **kwargs):
            answers.append([m['content'] for m in msgs])
            if len(answers) == 1:
                self.dispatch(self.msg('پیام دوم'))
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg('پیام اول'))
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[1][-1], 'پیام دوم')
        self.assertFalse(self.bot.busy)
        self.assertFalse(self.bot.pending)


if __name__ == '__main__':
    unittest.main()
