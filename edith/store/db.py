"""SQLite connection + tiny forward-only migration runner (PRAGMA user_version)."""
from __future__ import annotations

import sqlite3

MIGRATIONS = [
    # v1: reminders and deadlines
    """
    CREATE TABLE deadlines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        due_utc TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_utc TEXT NOT NULL
    );
    CREATE TABLE reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        due_utc TEXT NOT NULL,
        recurrence TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        deadline_id INTEGER REFERENCES deadlines(id),
        nags_left INTEGER NOT NULL DEFAULT 0,
        nag_minutes INTEGER NOT NULL DEFAULT 15,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_fired_utc TEXT,
        created_utc TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'user'
    );
    CREATE INDEX idx_reminders_due ON reminders(status, due_utc);
    """,
]


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version > current:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
