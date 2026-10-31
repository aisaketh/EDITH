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
    # v2: memory (facts with provenance), goals and tasks
    """
    CREATE TABLE facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        sensitivity INTEGER NOT NULL,
        source TEXT NOT NULL,                      -- provenance, e.g. 'user_command'
        status TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | candidate
        created_utc TEXT NOT NULL,
        last_confirmed_utc TEXT
    );
    CREATE TABLE goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        target_utc TEXT,
        status TEXT NOT NULL DEFAULT 'active',     -- active | done | abandoned
        created_utc TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER REFERENCES goals(id),
        title TEXT NOT NULL,
        due_utc TEXT,
        status TEXT NOT NULL DEFAULT 'open',       -- open | done
        created_utc TEXT NOT NULL,
        done_utc TEXT
    );
    CREATE INDEX idx_tasks_status ON tasks(status, due_utc);
    """,
    # v3: persistent usage counters for free-tier budgets (survive restarts)
    """
    CREATE TABLE usage (
        period TEXT NOT NULL,          -- '2026-11-02' (day) or '2026-11' (month)
        key TEXT NOT NULL,
        n INTEGER NOT NULL,
        PRIMARY KEY (period, key)
    );
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
