"""In-memory SQLite stand-in for the MySQL connection, so runmode SQL runs in tests."""
import sqlite3

import pymysql

# Mirrors hawkeye-database/sql/, in SQLite syntax
SCHEMA = """
CREATE TABLE test_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  content    VARCHAR(255) NOT NULL,
  created_by VARCHAR(64)  NOT NULL,
  created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  cognito_sub          VARCHAR(64)  NOT NULL UNIQUE,
  email                VARCHAR(255) NOT NULL,
  first_name           VARCHAR(100),
  last_name            VARCHAR(100),
  phone                VARCHAR(32),
  marina_id            INTEGER REFERENCES marinas(id) ON DELETE SET NULL,
  profile_completed_at TIMESTAMP,
  created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE marinas (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          VARCHAR(255) NOT NULL,
  address_line1 VARCHAR(255),
  address_line2 VARCHAR(255),
  city          VARCHAR(100),
  region        VARCHAR(100),
  postal_code   VARCHAR(20),
  country       CHAR(2),
  timezone      VARCHAR(64),
  latitude      DECIMAL(9,6),
  longitude     DECIMAL(9,6),
  phone         VARCHAR(32),
  email         VARCHAR(255),
  created_by    INTEGER NOT NULL REFERENCES users(id),
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class FakeConnection:
    def __init__(self):
        # autocommit like db.py; begin()/commit()/rollback() open explicit transactions
        self._db = sqlite3.connect(":memory:", isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def cursor(self):
        return _FakeCursor(self._db)

    def begin(self):
        self._db.execute("BEGIN")

    def commit(self):
        self._db.execute("COMMIT")

    def rollback(self):
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")


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

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def execute(self, sql, params=()):
        # pymysql uses %s placeholders; sqlite uses ?
        try:
            self._cursor.execute(sql.replace("%s", "?"), params)
        except sqlite3.IntegrityError as e:
            raise pymysql.err.IntegrityError(1062, str(e)) from e

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]
