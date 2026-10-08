import os
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from business import State
from postgres import PostgresState
from migrate_postgres import migrate


@unittest.skipUnless(os.getenv('POSTGRES_TEST_URL'), 'A disposable PostgreSQL test database is required')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.url = os.environ['POSTGRES_TEST_URL']
        with psycopg.connect(self.url, autocommit=True) as db:
            db.execute('DROP SCHEMA IF EXISTS hexbot CASCADE')
        self.state = PostgresState(self.url)

    def tearDown(self):
        self.state.db.close()

    def test_settings_big_ids_stops_and_archive_roundtrip(self):
        key = ('A', 8984804633)
        version = self.state.snapshot(key)
        self.state.set_enabled(False)
        self.assertFalse(self.state.enabled())
        self.state.set_enabled(True)
        self.state.set_manual_stop(key, True)
        self.assertTrue(self.state.manually_stopped(key[1]))
        self.state.set_manual_stop(key, False)
        self.state.set_reply_emoji('123', '✨')
        self.assertEqual(self.state.reply_emoji(), ('123', '✨'))
        self.assertTrue(self.state.archive_message(key[1], 12, 'user', 'سلام', 1))
        self.assertFalse(self.state.archive_message(key[1], 12, 'user', 'تکراری', 1))
        self.state.save_dialogue(key[1], [{'role': 'user', 'content': 'سلام'}])
        self.state.save_summary(key[1], 'خلاصه', 0)
        reopened = PostgresState(self.url)
        try:
            self.assertEqual(reopened.dialogue(key[1])[1], 'خلاصه')
            self.assertEqual(reopened.chats(), [(key[1],)])
        finally:
            reopened.db.close()

    def test_atomic_draft_claim_and_forget(self):
        key = ('A', 8984804633)
        version = self.state.snapshot(key)
        self.state.set_setting('approval', True)
        ident = self.state.create_draft(key, version, 12, 'پاسخ', [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.state.claim_draft, [ident, ident]))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.state.finish_draft(ident, 'sent')
        self.state.forget(key[1])
        self.assertFalse(self.state.save_summary(key[1], 'قدیمی', 0))
        self.assertEqual(self.state.dialogue(key[1])[1], '')

    def test_sqlite_migration_preserves_requests_flags_and_sequences(self):
        with tempfile.TemporaryDirectory() as root:
            source = State(Path(root) / 'old.sqlite3')
            try:
                key = ('A', 8984804633)
                version = source.snapshot(key)
                source.request(key, 'مخاطب', 'درخواست', version)
                source.set_enabled(False)
                source.set_manual_stop(key, True)
                source.archive_message(key[1], 1, 'user', 'سلام', 1)
                source.set_setting('profile_override', {'owner_name': 'مالک'})
                migrate(source, self.state)
                migrate(source, self.state)
                self.assertFalse(self.state.enabled())
                self.assertTrue(self.state.manually_stopped(key[1]))
                self.assertEqual(len(self.state.inbox()), 1)
                self.assertEqual(self.state.setting('profile_override')['owner_name'], 'مالک')
                self.state.set_manual_stop(key, False)
                self.state.set_enabled(True)
                self.state.request(key, 'دیگر', 'جدید', self.state.snapshot(key))
                self.assertEqual(len(self.state.inbox()), 2)
            finally:
                source.db.close()
