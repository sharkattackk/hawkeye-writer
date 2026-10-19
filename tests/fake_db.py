"""In-memory SQLite stand-in for the MySQL connection, so runmode SQL runs in tests."""
import sqlite3

SCHEMA = """
CREATE TABLE test_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  content    VARCHAR(255) NOT NULL,
  created_by VARCHAR(64)  NOT NULL,
  created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class FakeConnection:
    def __init__(self):
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.execute(SCHEMA)

    def cursor(self):
        return _FakeCursor(self._db)


class _FakeCursor:
    def __init__(self, db):
        self._cursor = db.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def execute(self, sql, params=()):
        # pymysql uses %s placeholders; sqlite uses ?
        self._cursor.execute(sql.replace("%s", "?"), params)

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]
