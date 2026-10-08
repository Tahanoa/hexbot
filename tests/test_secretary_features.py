import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from bot import ApiError, Config, Ollama
from business import BusinessBot, SecretaryConfig, State, REPLY_SCHEMA


class ImmediatePool:
    def submit(self, fn, *args):
        fn(*args)


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'owner_name': 'مالک', 'tone': 'معمولی'}))
        self.secretary = SecretaryConfig(10, profile, root / 'state.sqlite3', debounce_seconds=0)
        self.tg, self.ai = Mock(), Mock()
        self.conn = {'id': 'A', 'user': {'id': 10}, 'is_enabled': True,
                     'rights': {'can_reply': True, 'can_delete_sent_messages': True, 'can_delete_all_messages': True}}
        self.ids = iter(range(100000, 101000))
        def call(method, data, **kwargs):
            if method == 'getBusinessConnection':
                return dict(self.conn)
            if method == 'sendMessage':
                return {'message_id': next(self.ids)}
            return True
        self.tg.call.side_effect = call
        self.ai.chat.return_value = json.dumps({'reply': 'پاسخ', 'needs_owner': False, 'reason': ''})
        self.bot = BusinessBot(Config('test', history_turns=2), self.secretary, self.tg, self.ai)
        self.pool = ImmediatePool()

    def tearDown(self):
        self.bot.management_pool.shutdown(wait=True)
        self.bot.state.db.close()
        self.temp.cleanup()

    def msg(self, text='سلام', ident=1, user=20, chat=20):
        return {'business_connection_id': 'A', 'chat': {'id': chat, 'type': 'private'},
                'from': {'id': user, 'first_name': 'مخاطب'}, 'date': int(time.time()),
                'text': text, 'message_id': ident}

    def dispatch(self, message, pool=None):
        self.bot.dispatch_update({'business_message': message}, pool or self.pool)

    def replies(self):
        return [c.args[1] for c in self.tg.call.call_args_list if c.args[0] == 'sendMessage' and c.args[1]['text'] != '⏳']

    def control(self, text, user=10):
        self.bot.dispatch_update({'message': {'chat': {'id': user, 'type': 'private'},
            'from': {'id': user}, 'text': text}}, self.pool)

    def test_persistent_dialogue_summary_dedup_and_forget(self):
        self.dispatch(self.msg('اسمم علی است', 1))
        self.dispatch(self.msg('اسمم علی است', 1))
        self.assertEqual(self.ai.chat.call_count, 1)
        self.assertTrue(self.bot.state.save_summary(20, 'نام مخاطب علی است.', 0))
        self.bot.state.db.close()
        self.bot.state = State(self.secretary.database_path)
        self.bot.history.clear()
        self.dispatch(self.msg('اسم من چیست؟', 2))
        self.assertIn('علی', self.ai.chat.call_args.kwargs['system_prompt'])
        self.assertEqual(self.ai.chat.call_args.args[0][0]['content'], 'اسمم علی است')
        self.control('/forget 20')
        self.assertEqual(self.bot.state.dialogue(20)[:2], ([], ''))
        self.assertFalse(self.bot.state.recent_archive(20))
        self.dispatch(self.msg('سلام دوباره', 3))
        users = [m['content'] for m in self.ai.chat.call_args.args[0] if m['role'] == 'user']
        self.assertEqual(users, ['سلام دوباره'])

    def test_forget_prevents_late_dialogue_write_and_removes_draft_contents(self):
        key = ('A', 20)
        version = self.bot.state.snapshot(key)
        ident = self.bot.state.create_draft(key, version, 1, 'پاسخ خصوصی', [{'role': 'user', 'content': 'خصوصی'}])
        self.bot.state.forget(20)
        self.assertFalse(self.bot.state.save_dialogue(20, [{'role': 'user', 'content': 'قدیمی'}], key, version))
        self.assertEqual(self.bot.state.dialogue(20)[0], [])
        self.assertIsNone(self.bot.state.db.execute('SELECT answer FROM drafts WHERE id=?', (ident,)).fetchone())

    def test_batching_combines_burst_replies_to_last_message_cleans_waiting(self):
        self.bot.secretary = replace(self.secretary, debounce_seconds=0.02)
        class HoldingPool:
            def submit(inner, fn, *args):
                inner.job = (fn, args)
        pool = HoldingPool()
        self.dispatch(self.msg('سلام', 1), pool)
        self.dispatch(self.msg('یک سؤال', 2), pool)
        self.dispatch(self.msg('دارم', 3), pool)
        fn, args = pool.job
        fn(*args)
        self.assertEqual(self.ai.chat.call_count, 1)
        self.assertEqual(self.ai.chat.call_args.args[0][0]['content'], 'سلام\n\nیک سؤال\n\nدارم')
        self.assertEqual(self.replies()[0]['reply_parameters']['message_id'], 3)
        self.assertEqual(len([c for c in self.tg.call.call_args_list if c.args[0] == 'deleteBusinessMessages']), 3)

    def test_private_command_deletes_only_command_and_reports_to_owner(self):
        self.dispatch(self.msg('/stop', 10, user=10))
        self.assertTrue(self.bot.state.manually_stopped(20))
        deleted = [c.args[1] for c in self.tg.call.call_args_list if c.args[0] == 'deleteBusinessMessages']
        self.assertEqual(deleted, [{'business_connection_id': 'A', 'message_ids': [10]}])
        self.assertEqual(self.tg.send.call_args.args[0], 10)
        self.assertIn('حذف شد', self.tg.send.call_args.args[1])
        self.bot.dispatch_update({'deleted_business_messages': {'business_connection_id': 'A',
            'chat': {'id': 20}, 'message_ids': [10]}}, self.pool)
        self.dispatch(self.msg('/resume', 11, user=10))
        self.dispatch(self.msg('سلام', 12))
        self.assertEqual(self.ai.chat.call_count, 1)

    def test_delete_permission_failure_reported_but_stop_still_applied(self):
        self.conn['rights'].pop('can_delete_all_messages')
        self.dispatch(self.msg('/stop', 10, user=10))
        self.assertTrue(self.bot.state.manually_stopped(20))
        self.assertIn('دستور حذف نشد', self.tg.send.call_args.args[1])

    def test_approval_edit_and_exactly_once_send_survive_restart(self):
        self.control('/approval on')
        self.dispatch(self.msg('سؤال', 1))
        self.assertFalse(self.replies())
        ident = self.bot.state.pending_drafts()[0][0]
        self.bot.state.db.close()
        self.bot.state = State(self.secretary.database_path)
        self.control(f'/edit {ident} متن اصلاح‌شده')
        self.assertFalse(self.replies())
        self.control(f'/approve {ident}', user=20)
        self.assertFalse(self.replies())
        self.control(f'/approve {ident}')
        self.assertEqual(self.replies()[0]['text'], 'متن اصلاح‌شده')
        self.assertEqual(self.replies()[0]['reply_parameters']['message_id'], 1)
        self.control(f'/approve {ident}')
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.bot.state.dialogue(20)[0][-1]['content'], 'متن اصلاح‌شده')

    def test_stop_cancels_pending_draft_without_auto_cutoff(self):
        self.control('/approval on')
        self.dispatch(self.msg('سؤال', 1))
        ident = self.bot.state.pending_drafts()[0][0]
        self.dispatch(self.msg('/stop', 2, user=10))
        self.control(f'/approve {ident}')
        self.assertFalse(self.replies())
        self.assertFalse(self.bot.state.pending_drafts())

    def test_chat_review_overrides_default_and_draft_rejection(self):
        self.dispatch(self.msg('/review on', 1, user=10))
        self.dispatch(self.msg('سلام', 2))
        ident = self.bot.state.pending_drafts()[0][0]
        self.control(f'/reject {ident}')
        self.assertFalse(self.bot.state.pending_drafts())
        self.assertFalse(self.bot.state.manually_stopped(20))
        self.dispatch(self.msg('/review off', 3, user=10))
        self.dispatch(self.msg('سلام', 4))
        self.assertEqual(len(self.replies()), 1)

    def test_ambiguous_owner_question_registered_not_paused(self):
        self.ai.chat.return_value = json.dumps({'reply': 'برای بررسی ثبت شد.',
            'needs_owner': True, 'reason': 'قیمت در پروفایل موجود نیست', 'command': '/stop'})
        self.dispatch(self.msg('قیمت پروژه من چقدر است؟', 1))
        self.assertEqual(len(self.bot.state.inbox()), 1)
        self.assertIn('نیاز به بررسی شما', self.tg.send.call_args.args[1])
        self.assertFalse(self.bot.state.manually_stopped(20))
        self.assertEqual(self.replies()[0]['text'], 'برای بررسی ثبت شد.')

    def test_profile_changes_persist_reload_and_invalidate_generation(self):
        self.control('/profile set tone خیلی صمیمی')
        self.assertIn('خیلی صمیمی', self.bot.prompt)
        self.bot.state.db.close()
        self.bot.state = State(self.secretary.database_path)
        self.assertEqual(self.bot.state.setting('profile_override')['tone'], 'خیلی صمیمی')
        self.control('/profile set owner_name ""')
        self.assertEqual(self.bot.state.setting('profile_override')['owner_name'], 'مالک')
        self.control('/profile reload')
        self.assertIn('معمولی', self.bot.prompt)
        def generate(*args, **kwargs):
            self.control('/profile set tone جدید')
            return 'پاسخ قدیمی'
        self.ai.chat.side_effect = generate
        self.dispatch(self.msg())
        self.assertFalse(self.replies())

    def test_summary_uses_exactly_last_300_bounded_chunks(self):
        for ident in range(1, 305):
            self.bot.state.archive_message(20, ident, 'user', f'پیام شماره {ident}', ident)
        self.ai.chat.return_value = 'خلاصه کوچک'
        report = self.bot.summarize_chat(20)
        self.assertIn('300', report)
        self.assertEqual(self.bot.state.dialogue(20)[1], 'خلاصه کوچک')
        self.assertGreater(self.ai.chat.call_count, 1)
        for call in self.ai.chat.call_args_list:
            data = json.loads(call.args[0][0]['content'])
            self.assertLessEqual(len(data['conversation']), 3500)
        content = ''.join(json.loads(c.args[0][0]['content'])['conversation'] for c in self.ai.chat.call_args_list)
        self.assertNotIn('پیام شماره 1"', content)
        self.assertIn('پیام شماره 304', content)

    def test_forget_during_history_read_does_not_restore_archived_messages(self):
        reader = Mock()
        def read(*args, **kwargs):
            self.bot.forget_chat(20)
            return [{'message_id': 1, 'role': 'user', 'content': 'نباید برگردد', 'date': 1}]
        reader.read.side_effect = read
        report = self.bot.summarize_chat(20, reader)
        self.assertIn('لغو', report)
        self.assertFalse(self.bot.state.recent_archive(20))
        self.ai.chat.assert_not_called()

    def test_forget_during_summary_prevents_save(self):
        self.bot.state.archive_message(20, 1, 'user', 'متن', 1)
        def generate(*args, **kwargs):
            self.bot.forget_chat(20)
            return 'خلاصه قدیمی'
        self.ai.chat.side_effect = generate
        self.assertIn('ذخیره نشد', self.bot.summarize_chat(20))
        self.assertEqual(self.bot.state.dialogue(20)[1], '')

    def test_ollama_passes_json_schema_and_summary_token_limit(self):
        ollama = Ollama(Config('test'))
        ollama.client = Mock()
        ollama.client.post.return_value = {'message': {'content': 'ok'}}
        ollama.chat([{'role': 'user', 'content': 'سؤال'}], response_format=REPLY_SCHEMA, max_tokens=512)
        payload = ollama.client.post.call_args.args[1]
        self.assertEqual(payload['format'], REPLY_SCHEMA)
        self.assertEqual(payload['options']['num_predict'], 512)
