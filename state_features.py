"""Persistent secretary features shared by SQLite and PostgreSQL stores."""
import json
import time


class FeatureState:
    def initialize_features(self, postgres=False):
        ident = 'BIGSERIAL' if postgres else 'INTEGER'
        with self.lock, self.db:
            self.db.execute(f'''CREATE TABLE IF NOT EXISTS archive (
                seq {ident} PRIMARY KEY, chat BIGINT NOT NULL, message_id BIGINT,
                role TEXT NOT NULL, content TEXT NOT NULL, date BIGINT NOT NULL,
                UNIQUE(chat, message_id))''')
            self.db.execute('CREATE INDEX IF NOT EXISTS archive_chat_date ON archive(chat,date,seq)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS dialogue (
                chat BIGINT PRIMARY KEY, history TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '', memory_version BIGINT NOT NULL DEFAULT 0)''')
            self.db.execute(f'''CREATE TABLE IF NOT EXISTS drafts (
                id {ident} PRIMARY KEY, connection TEXT NOT NULL, chat BIGINT NOT NULL,
                version BIGINT NOT NULL, reply_to BIGINT, answer TEXT NOT NULL,
                messages TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created BIGINT NOT NULL)''')

    def setting(self, name, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (name,)).fetchone()
            return default if row is None else json.loads(str(row[0]))

    def set_setting(self, name, value):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (name, json.dumps(value, ensure_ascii=False)))

    def invalidate_all(self):
        with self.lock, self.db:
            self.db.execute('UPDATE chats SET version=version+1')
            self.db.execute("UPDATE drafts SET status='cancelled' WHERE status='pending'")

    def archive_message(self, chat, message_id, role, content, date=None):
        if not content:
            return True
        with self.lock, self.db:
            row = self.db.execute('''INSERT INTO archive(chat,message_id,role,content,date)
                VALUES (?,?,?,?,?) ON CONFLICT(chat,message_id) DO NOTHING RETURNING seq''',
                (chat, message_id, role, content[:16000], int(date or time.time()))).fetchone()
            return row is not None

    def recent_archive(self, chat, limit=300):
        with self.lock:
            rows = self.db.execute('''SELECT message_id,role,content,date FROM archive
                WHERE chat=? ORDER BY date DESC, seq DESC LIMIT ?''', (chat, min(300, limit))).fetchall()
            return [{'message_id': r[0], 'role': r[1], 'content': r[2], 'date': r[3]} for r in reversed(rows)]

    def dialogue(self, chat):
        with self.lock:
            row = self.db.execute('SELECT history,summary,memory_version FROM dialogue WHERE chat=?', (chat,)).fetchone()
            return (json.loads(row[0]), row[1], row[2]) if row else ([], '', 0)

    def save_dialogue(self, chat, history, key=None, version=None):
        with self.lock, self.db:
            if key is not None and self.snapshot(key) != version:
                return False
            self.db.execute('''INSERT INTO dialogue(chat,history) VALUES (?,?)
                ON CONFLICT(chat) DO UPDATE SET history=excluded.history''',
                (chat, json.dumps(history, ensure_ascii=False)))
            return True

    def save_summary(self, chat, summary, expected_version):
        with self.lock, self.db:
            self.db.execute('INSERT INTO dialogue(chat) VALUES (?) ON CONFLICT(chat) DO NOTHING', (chat,))
            row = self.db.execute('''UPDATE dialogue SET summary=?
                WHERE chat=? AND memory_version=? RETURNING chat''',
                (summary[:4000], chat, expected_version)).fetchone()
            return row is not None

    def forget(self, chat):
        with self.lock, self.db:
            self.db.execute('INSERT INTO dialogue(chat) VALUES (?) ON CONFLICT(chat) DO NOTHING', (chat,))
            self.db.execute("UPDATE dialogue SET history='[]', summary='', memory_version=memory_version+1 WHERE chat=?", (chat,))
            self.db.execute('DELETE FROM archive WHERE chat=?', (chat,))
            self.db.execute('UPDATE chats SET version=version+1 WHERE chat=?', (chat,))
            self.db.execute('DELETE FROM drafts WHERE chat=?', (chat,))

    def remove_archived(self, chat, message_ids):
        with self.lock, self.db:
            for ident in message_ids:
                self.db.execute('DELETE FROM archive WHERE chat=? AND message_id=?', (chat, ident))
            self.db.execute("UPDATE dialogue SET history='[]', summary='', memory_version=memory_version+1 WHERE chat=?", (chat,))

    def approval(self, chat):
        return bool(self.setting('approval_chat:' + str(chat), self.setting('approval', False)))

    def create_draft(self, key, version, reply_to, answer, messages):
        with self.lock, self.db:
            if not self.enabled() or self.manually_stopped(key[1]) or self.snapshot(key) != version:
                return None
            # Old pending replies are superseded explicitly, never sent automatically.
            self.db.execute("UPDATE drafts SET status='superseded' WHERE chat=? AND status='pending'", (key[1],))
            return self.db.execute('''INSERT INTO drafts(connection,chat,version,reply_to,answer,messages,created)
                VALUES (?,?,?,?,?,?,?) RETURNING id''',
                (*key, version, reply_to, answer, json.dumps(messages, ensure_ascii=False), int(time.time()))).fetchone()[0]

    def pending_drafts(self):
        with self.lock:
            return self.db.execute("SELECT id,chat,answer FROM drafts WHERE status='pending' ORDER BY id DESC LIMIT 10").fetchall()

    def claim_draft(self, ident, edit=None):
        with self.lock, self.db:
            row = self.db.execute("UPDATE drafts SET status='sending' WHERE id=? AND status='pending' RETURNING connection,chat,version,reply_to,answer,messages", (ident,)).fetchone()
            if not row:
                return None
            if edit is not None:
                self.db.execute('UPDATE drafts SET answer=? WHERE id=?', (edit, ident))
            return {'key': (row[0], row[1]), 'version': row[2], 'reply_to': row[3],
                    'answer': row[4] if edit is None else edit, 'messages': json.loads(row[5])}

    def finish_draft(self, ident, status):
        with self.lock, self.db:
            self.db.execute('UPDATE drafts SET status=? WHERE id=?', (status, ident))

    def reject_draft(self, ident):
        with self.lock, self.db:
            return self.db.execute("UPDATE drafts SET status='rejected' WHERE id=? AND status='pending' RETURNING id", (ident,)).fetchone() is not None

    def edit_draft(self, ident, answer):
        with self.lock, self.db:
            return self.db.execute("UPDATE drafts SET answer=? WHERE id=? AND status='pending' RETURNING id", (answer, ident)).fetchone() is not None

    def cancel_drafts(self, chat):
        with self.lock, self.db:
            self.db.execute("UPDATE drafts SET status='cancelled' WHERE chat=? AND status='pending'", (chat,))
