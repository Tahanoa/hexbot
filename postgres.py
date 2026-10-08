"""PostgreSQL state with parameterized SQL and a dedicated hexbot schema."""
import re
import threading

from business import State


class Database:
    def __init__(self, connection):
        self.connection = connection
        self.transactions = []

    def __enter__(self):
        context = self.connection.transaction()
        context.__enter__()
        self.transactions.append(context)
        return self

    def __exit__(self, *args):
        return self.transactions.pop().__exit__(*args)

    def execute(self, query, params=()):
        replace = 'INSERT OR REPLACE INTO settings' in query
        ignore = 'INSERT OR IGNORE INTO' in query
        query = query.replace('INSERT OR REPLACE INTO settings', 'INSERT INTO settings')
        query = query.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
        query = query.replace('?', '%s')
        if replace:
            query += ' ON CONFLICT(key) DO UPDATE SET value=excluded.value'
        if ignore:
            query += ' ON CONFLICT DO NOTHING'
        if re.match(r'INSERT INTO settings', query.strip(), re.I):
            params = tuple(str(p) if p is not None else p for p in params)
        return self.connection.execute(query, params)

    def executemany(self, query, rows):
        for params in rows:
            self.execute(query, params)

    def close(self):
        self.connection.close()


class PostgresState(State):
    def __init__(self, url):
        try:
            import psycopg
        except ImportError:
            raise ValueError('Install requirements.txt to use PostgreSQL') from None
        try:
            connection = psycopg.connect(url, autocommit=True, connect_timeout=10)
            self.db = Database(connection)
            self.lock = threading.RLock()
            with self.db:
                self.db.execute('CREATE SCHEMA IF NOT EXISTS hexbot')
                self.db.execute('SET search_path TO hexbot')
                self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                self.db.execute('''CREATE TABLE IF NOT EXISTS chats (connection TEXT,chat BIGINT,
                    version BIGINT NOT NULL,has_replied INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(connection,chat))''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS requests (id BIGSERIAL PRIMARY KEY,
                    connection TEXT,chat BIGINT,name TEXT,message TEXT,created BIGINT)''')
                self.db.execute('CREATE TABLE IF NOT EXISTS manual_stops (chat BIGINT PRIMARY KEY)')
            self.initialize_features(postgres=True)
        except psycopg.Error:
            raise ValueError('PostgreSQL connection/schema initialization failed; check DATABASE_URL and database permissions') from None

    def enabled(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
            return row is None or bool(int(row[0]))

    def chats(self):
        with self.lock:
            return self.db.execute('SELECT chat FROM chats GROUP BY chat ORDER BY MAX(version) DESC,chat LIMIT 20').fetchall()
