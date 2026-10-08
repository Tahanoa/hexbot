"""Copy existing SQLite secretary data to PostgreSQL without overwriting rows."""
import os
from bot import load_env
from business import ROOT, State
from postgres import PostgresState


def migrate(source, target):
    tables = ('settings', 'chats', 'requests', 'manual_stops', 'archive', 'dialogue', 'drafts')
    with target.lock, target.db:
        for table in tables:
            cursor = source.db.execute('SELECT * FROM ' + table)
            columns = [c[0] for c in cursor.description]
            query = ('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES ('
                     + ','.join('?' for _ in columns) + ') ON CONFLICT DO NOTHING')
            for row in cursor.fetchall():
                target.db.execute(query, row)
        for table, column in (('requests', 'id'), ('archive', 'seq'), ('drafts', 'id')):
            target.db.execute(f"SELECT setval(pg_get_serial_sequence('hexbot.{table}','{column}'), COALESCE(MAX({column}),1), COUNT(*)>0) FROM {table}")


if __name__ == '__main__':
    load_env(ROOT / '.env')
    url = os.getenv('DATABASE_URL', '').strip()
    if not url.startswith(('postgresql://', 'postgres://')):
        raise SystemExit('Set DATABASE_URL to a PostgreSQL URL first')
    source = State(ROOT / os.getenv('SECRETARY_DATABASE', 'data/secretary.sqlite3'))
    target = PostgresState(url)
    try:
        migrate(source, target)
        print('SQLite data copied to PostgreSQL. Source file retained.')
    finally:
        source.db.close()
        target.db.close()
