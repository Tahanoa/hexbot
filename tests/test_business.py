import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot import ApiError, Config
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

    def test_introduction_state_survives_history_clear_and_restart(self):
        prompts = []
        def generate(*args, **kwargs):
            prompts.append(kwargs['system_prompt'])
            return 'answer'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertIn('این اولین پاسخ', prompts[-1])
        self.bot.history.clear()
        self.bot.state.db.close()
        self.bot.state = State(self.config.database_path)
        self.dispatch(self.msg(connection='NEW'))
        self.assertIn('خودت را دوباره معرفی نکن', prompts[-1])
        self.dispatch(self.msg(user=30, chat=30))
        self.assertIn('این اولین پاسخ', prompts[-1])

    def test_unsent_answer_does_not_mark_conversation_introduced(self):
        self.ai.chat.side_effect = ApiError('Ollama')
        self.dispatch(self.msg())
        self.assertFalse(self.bot.state.has_replied(('A', 20)))

    def enable_waiting(self):
        self.conn['rights']['can_delete_sent_messages'] = True
        counter = iter(range(100, 1000))
        def call(method, data, **kwargs):
            if method == 'getBusinessConnection':
                return dict(self.conn)
            if method == 'sendMessage':
                return {'message_id': next(counter)}
            return True
        self.tg.call.side_effect = call
        return call

    def test_waiting_precedes_ai_and_deleted_after_reply_without_cancelling_next(self):
        self.enable_waiting()
        def generate(*args, **kwargs):
            self.assertEqual(self.sends()[-1]['text'], '⏳')
            return 'answer'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        methods = [c.args[0] for c in self.tg.call.call_args_list]
        self.assertEqual(methods, ['getBusinessConnection', 'sendMessage',
            'getBusinessConnection', 'sendMessage', 'deleteBusinessMessages'])
        self.assertEqual(self.tg.call.call_args.args[1]['message_ids'], [100])
        version = self.bot.state.snapshot(('A', 20))
        self.bot.dispatch_update({'deleted_business_messages': {'business_connection_id': 'A',
            'chat': {'id': 20}, 'message_ids': [100]}}, self.pool)
        self.assertEqual(self.bot.state.snapshot(('A', 20)), version)
        self.dispatch(self.msg())
        self.assertEqual(self.ai.chat.call_count, 2)

    def test_waiting_deleted_when_ai_fails(self):
        self.enable_waiting()
        self.ai.chat.side_effect = ApiError('Ollama')
        self.dispatch(self.msg())
        self.assertEqual(self.tg.call.call_args.args[0], 'deleteBusinessMessages')
        self.assertEqual(len(self.sends()), 2)

    def test_waiting_deletion_update_does_not_cancel_queued_reply(self):
        base = self.enable_waiting()
        generated = []
        def generate(messages, **kwargs):
            generated.append(messages[-1]['content'])
            if len(generated) == 1:
                self.dispatch(self.msg('next'))
            return 'answer'
        def call(method, data, **kwargs):
            if method == 'deleteBusinessMessages':
                self.bot.dispatch_update({'deleted_business_messages': {
                    'business_connection_id': 'A', 'chat': {'id': 20},
                    'message_ids': data['message_ids']}}, self.pool)
            return base(method, data, **kwargs)
        self.tg.call.side_effect = call
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg('first'))
        self.assertEqual(generated, ['first', 'next'])
        self.assertEqual([s['text'] for s in self.sends()].count('answer'), 2)

    def test_premium_waiting_saved_with_utf16_entity_and_falls_back(self):
        base = self.enable_waiting()
        emoji = '🕰️'
        self.bot.dispatch_update({'message': {'chat': {'id': 10, 'type': 'private'},
            'from': {'id': 10}, 'text': emoji, 'entities': [{'type': 'custom_emoji',
                'offset': 0, 'length': 3, 'custom_emoji_id': '12345'}]}}, self.pool)
        reopened = State(self.config.database_path)
        try:
            self.assertEqual(reopened.waiting_emoji(), ('12345', emoji))
        finally:
            reopened.db.close()
        def call(method, data, **kwargs):
            if method == 'sendMessage' and data.get('entities'):
                self.assertEqual(data['entities'][0]['length'], 3)
                raise ApiError('Telegram', 400)
            return base(method, data, **kwargs)
        self.tg.call.side_effect = call
        self.dispatch(self.msg())
        self.assertEqual(self.sends()[1]['text'], '⏳')
        self.assertNotIn('entities', self.sends()[1])
        self.assertEqual(self.tg.call.call_args.args[0], 'deleteBusinessMessages')

    def test_waiting_delivery_failure_does_not_block_ai(self):
        base = self.enable_waiting()
        def call(method, data, **kwargs):
            if method == 'sendMessage' and data['text'] == '⏳':
                raise ApiError('Telegram', 500)
            return base(method, data, **kwargs)
        self.tg.call.side_effect = call
        self.dispatch(self.msg())
        self.ai.chat.assert_called_once()
        self.assertEqual(self.sends()[-1]['text'], self.ai.chat.return_value)

    def test_waiting_cleanup_failure_does_not_lose_answer_or_history(self):
        base = self.enable_waiting()
        def call(method, data, **kwargs):
            if method == 'deleteBusinessMessages':
                raise ApiError('Telegram', 403)
            return base(method, data, **kwargs)
        self.tg.call.side_effect = call
        self.dispatch(self.msg())
        self.assertEqual(self.sends()[-1]['text'], self.ai.chat.return_value)
        self.assertIn(('A', 20), self.bot.history)

    def test_manual_reply_does_not_disable_other_or_same_chat(self):
        self.dispatch(self.msg(user=10))
        self.dispatch(self.msg())
        self.dispatch(self.msg(user=30, chat=30))
        self.assertEqual(self.ai.chat.call_count, 2)
        self.assertEqual({m['chat_id'] for m in self.sends()}, {20, 30})

    def test_chats_lists_ids_after_restart_and_is_owner_only(self):
        self.dispatch(self.msg(user=10))
        reopened = State(self.config.database_path)
        try:
            self.assertEqual(reopened.chats(), [(20,)])
        finally:
            reopened.db.close()
        self.bot.dispatch_update({'message': {'chat': {'id': 30, 'type': 'private'},
            'from': {'id': 30}, 'text': '/chats'}}, self.pool)
        self.tg.send.assert_not_called()
        self.bot.dispatch_update({'message': {'chat': {'id': 10, 'type': 'private'},
            'from': {'id': 10}, 'text': '/chats'}}, self.pool)
        self.assertIn('20', self.tg.send.call_args.args[1])

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

    def test_manual_intervention_during_generation_keeps_reply(self):
        def generate(*args, **kwargs):
            self.dispatch(self.msg(user=10))
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertEqual(len(self.sends()), 1)
        self.assertFalse(self.bot.busy)

    def test_global_off_on_invalidates_running_reply(self):
        def generate(*args, **kwargs):
            self.bot.state.set_enabled(False)
            self.bot.state.set_enabled(True)
            return 'پاسخ'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertFalse(self.sends())

    def test_handoff_persists_and_keeps_reply_enabled(self):
        self.dispatch(self.msg('/human لطفاً با من تماس بگیرید'))
        self.ai.chat.assert_not_called()
        self.assertEqual(self.bot.state.inbox()[0][3], 'لطفاً با من تماس بگیرید')
        second = State(self.config.database_path)
        self.assertEqual(len(second.inbox()), 1)
        second.db.close()
        self.dispatch(self.msg())
        self.ai.chat.assert_called_once()

    def test_handoff_during_generation_keeps_generated_reply(self):
        def generate(*args, **kwargs):
            self.dispatch(self.msg('/human درخواست من'))
            return 'پاسخ دیرهنگام'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertEqual(len(self.sends()), 2)
        self.assertIn('دیرهنگام', self.sends()[1]['text'])

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

    def test_legacy_pauses_removed_without_losing_global_state_or_requests(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
            db.executemany('INSERT INTO settings VALUES (?,?)', [('enabled', 1), ('pause:20', 1)])
            db.execute('CREATE TABLE chats (connection TEXT, chat INTEGER, paused INTEGER NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(connection,chat))')
            db.execute("INSERT INTO chats VALUES ('A',20,1,3)")
            db.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, connection TEXT, chat INTEGER, name, message, created INTEGER)')
            db.execute("INSERT INTO requests VALUES (1,'A',20,'visitor','saved request',0)")
        self.bot.state.db.close()
        self.bot.state = State(path)
        migrated = self.bot.state
        self.assertTrue(migrated.enabled())
        self.assertEqual(migrated.snapshot(('A', 20)), 3)
        self.assertEqual(migrated.inbox()[0][3], 'saved request')
        self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'pause:%'").fetchone()[0], 0)
        self.dispatch(self.msg())
        self.ai.chat.assert_called_once()

    def test_global_switch_controls_all_chats(self):
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/secretary off'})
        for chat in (20, 30):
            self.dispatch(self.msg(user=chat, chat=chat))
        self.ai.chat.assert_not_called()
        self.bot.owner_command({'chat': {'id': 10}, 'text': '/secretary on'})
        for chat in (20, 30):
            self.dispatch(self.msg(user=chat, chat=chat))
        self.assertEqual(self.ai.chat.call_count, 2)

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
