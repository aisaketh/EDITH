"""Append-only, hash-chained local ledger of everything that left (or tried to leave) the device.

The ledger never stores raw payloads: only a redacted, truncated preview plus a SHA-256 of the payload.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List

from ..privacy.redactor import redact

GENESIS = "0" * 64
PREVIEW_CHARS = 60


@dataclass(frozen=True)
class Entry:
    id: int
    ts: str
    kind: str
    destination: str
    sensitivity: int
    decision: str
    reason: str
    preview: str
    payload_sha256: str
    prev_hash: str
    hash: str


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_preview(text: str) -> str:
    redacted, _ = redact(text)
    return redacted[:PREVIEW_CHARS]


def _chain_hash(prev: str, fields: List[object]) -> str:
    blob = json.dumps([prev] + fields, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self.conn = sqlite3.connect(path)
        self._clock = clock
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, kind TEXT NOT NULL, destination TEXT NOT NULL,
                sensitivity INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                preview TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                prev_hash TEXT NOT NULL, hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
                BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
                BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
            """
        )

    def _last_hash(self) -> str:
        row = self.conn.execute("SELECT hash FROM entries ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def append(self, kind: str, destination: str, sensitivity: int, decision: str,
               reason: str, payload: str) -> int:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock()))
        preview = safe_preview(payload)
        digest = payload_hash(payload)
        prev = self._last_hash()
        h = _chain_hash(prev, [ts, kind, destination, int(sensitivity), decision, reason, preview, digest])
        cur = self.conn.execute(
            "INSERT INTO entries (ts, kind, destination, sensitivity, decision, reason, preview,"
            " payload_sha256, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, kind, destination, int(sensitivity), decision, reason, preview, digest, prev, h),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def entries(self) -> List[Entry]:
        rows = self.conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
        return [Entry(*r) for r in rows]

    def verify(self) -> bool:
        prev = GENESIS
        for e in self.entries():
            expect = _chain_hash(prev, [e.ts, e.kind, e.destination, e.sensitivity, e.decision,
                                        e.reason, e.preview, e.payload_sha256])
            if e.prev_hash != prev or e.hash != expect:
                return False
            prev = e.hash
        return True
