"""Persistent usage counters so free-tier budgets survive restarts."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from ..timeutil import utcnow


class UsageCounter:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = utcnow):
        self.conn, self.clock = conn, clock

    def _period(self, period: str) -> str:
        now = self.clock()
        if period == "day":
            return now.strftime("%Y-%m-%d")
        if period == "month":
            return now.strftime("%Y-%m")
        raise ValueError("bad_period")

    def used(self, key: str, period: str = "day") -> int:
        row = self.conn.execute("SELECT n FROM usage WHERE period=? AND key=?",
                                (self._period(period), key)).fetchone()
        return int(row["n"]) if row else 0

    def try_use(self, key: str, limit: int, period: str = "day") -> bool:
        """Consume one unit if the budget allows it. Returns False (and consumes nothing) when exhausted."""
        p = self._period(period)
        with self.conn:
            row = self.conn.execute("SELECT n FROM usage WHERE period=? AND key=?", (p, key)).fetchone()
            if row and row["n"] >= limit:
                return False
            if limit <= 0:
                return False
            self.conn.execute(
                "INSERT INTO usage (period, key, n) VALUES (?,?,1) "
                "ON CONFLICT(period, key) DO UPDATE SET n = n + 1", (p, key))
        return True
