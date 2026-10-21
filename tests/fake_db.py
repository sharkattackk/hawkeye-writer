"""In-memory SQLite stand-in for the MySQL connection, so runmode SQL runs in tests."""
import sqlite3

import pymysql

# Mirrors hawkeye-database/sql/, in SQLite syntax
SCHEMA = """
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
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reference_image_id INTEGER REFERENCES marina_images(id),
  calibrated_at TIMESTAMP
);

CREATE TABLE marina_images (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  marina_id       INTEGER NOT NULL REFERENCES marinas(id),
  s3_key          VARCHAR(512) NOT NULL,
  annotated_s3_key VARCHAR(512),
  content_type    VARCHAR(64)  NOT NULL,
  file_size_bytes INTEGER,
  width_px        INTEGER,
  height_px       INTEGER,
  captured_at     TIMESTAMP,
  gps_latitude    DECIMAL(9,6),
  gps_longitude   DECIMAL(9,6),
  gps_altitude_m  DECIMAL(7,2),
  status          TEXT NOT NULL DEFAULT 'pending_upload'
                  CHECK (status IN ('pending_upload', 'uploaded', 'failed')),
  homography      JSON,
  uploaded_by     INTEGER NOT NULL REFERENCES users(id),
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE docks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  marina_id  INTEGER NOT NULL REFERENCES marinas(id),
  name       VARCHAR(64) NOT NULL,
  sort_order SMALLINT NOT NULL DEFAULT 0,
  UNIQUE (marina_id, name)
);

CREATE TABLE slips (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  marina_id  INTEGER NOT NULL REFERENCES marinas(id),
  dock_id    INTEGER REFERENCES docks(id) ON DELETE SET NULL,
  label      VARCHAR(32) NOT NULL COLLATE NOCASE,  -- MySQL's collation is case-insensitive
  kind       TEXT NOT NULL DEFAULT 'slip' CHECK (kind IN ('slip', 't_head', 'side_tie', 'mooring')),
  polygon    JSON NOT NULL,
  center_x   DECIMAL(7,6) NOT NULL,
  center_y   DECIMAL(7,6) NOT NULL,
  length_ft  DECIMAL(6,2),
  width_ft   DECIMAL(6,2),
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (marina_id, label)
);

CREATE TABLE analysis_jobs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  marina_id    INTEGER NOT NULL REFERENCES marinas(id),
  image_id     INTEGER NOT NULL UNIQUE REFERENCES marina_images(id),
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
  progress     INTEGER,
  message      VARCHAR(255),
  worker_id    VARCHAR(64),
  attempts     INTEGER NOT NULL DEFAULT 0,
  queued_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at   TIMESTAMP,
  heartbeat_at TIMESTAMP,
  finished_at  TIMESTAMP
);

CREATE TABLE slip_observations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES analysis_jobs(id),
  image_id    INTEGER NOT NULL REFERENCES marina_images(id),
  slip_id     INTEGER REFERENCES slips(id) ON DELETE SET NULL,
  label       VARCHAR(32),
  dock        VARCHAR(64),
  kind        TEXT,
  polygon     JSON,
  crop_s3_key VARCHAR(512),
  occupied    BOOLEAN NOT NULL,
  confidence  DECIMAL(4,3),
  model_version VARCHAR(64),
  observed_at TIMESTAMP NOT NULL,
  UNIQUE (image_id, slip_id)
);

CREATE TABLE analysis_job_steps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
  attempt     INTEGER NOT NULL,
  step        VARCHAR(32) NOT NULL,
  status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
  started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TIMESTAMP,
  message     VARCHAR(255),
  detail      JSON,
  artifacts   JSON,
  UNIQUE (job_id, attempt, step)
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
