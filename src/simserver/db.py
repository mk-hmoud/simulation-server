"""SQLite schema and connection helper (plan §2 storage layout, §8 schema).

WAL mode + busy_timeout so concurrent worker connections queue up on a write
lock instead of raising "database is locked"; the atomic claim query in
queue.py relies on BEGIN IMMEDIATE actually serializing writers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    model_id TEXT NOT NULL REFERENCES models(id),
    params_json TEXT NOT NULL,
    outputs_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_class TEXT,
    error_message TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    not_before TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs (status, priority, created_at);

CREATE TABLE IF NOT EXISTS results (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    output_name TEXT NOT NULL,
    sweep_index INTEGER NOT NULL DEFAULT 0,
    sweep_value REAL,
    value_real REAL,
    value_imag REAL
);

CREATE TABLE IF NOT EXISTS mode_selection (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    sweep_index INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL,
    core_fraction REAL,
    n_modes_considered INTEGER
);

CREATE TABLE IF NOT EXISTS artifacts (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER
);

-- small key/value store for cross-process admin state (plan §7 maintenance
-- mode); a table rather than a config file so every worker/supervisor
-- connection sees the same value immediately, no polling a separate file
CREATE TABLE IF NOT EXISTS admin_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- web client (researcher accounts, session cookies) — a separate auth layer
-- from the JSON API's static X-API-Key scheme, not merged with it
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # jobs/models already existed (with real data, including on the deployed
    # VM) before the web client added ownership — CREATE TABLE IF NOT EXISTS
    # can't retrofit a column onto an existing table, so migrate explicitly
    _add_column_if_missing(conn, "jobs", "owner_user_id", "owner_user_id INTEGER REFERENCES users(id)")
    _add_column_if_missing(conn, "models", "owner_user_id", "owner_user_id INTEGER REFERENCES users(id)")
