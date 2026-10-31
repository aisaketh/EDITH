"""Semantic memory: short facts about the user, each with provenance, sensitivity and a status.

* Facts the user states are 'confirmed'. Anything inferred later must enter as a 'candidate' and only
  becomes usable after the user confirms it, so a wrong inference can't silently shape behaviour.
* Every fact carries a sensitivity level, so prompt context can be limited to what a destination may see.
* Fully visible and editable by the user (/facts, /forget, /confirm).
* Retrieval is keyword overlap (no embeddings: fewer dependencies, works for Telugu, easy to audit).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from ..privacy.classifier import classify
from ..privacy.sensitivity import Sensitivity
from ..privacy.validate import clean_user_text
from ..timeutil import to_iso, utcnow

MAX_FACT = 300
_SPLIT = re.compile(r"[\s,.;:!?()\[\]{}\"'/\\|-]+")  # not \w: Telugu vowel signs are combining marks


def tokens(text: str) -> set:
    return {t.casefold() for t in _SPLIT.split(text) if len(t) >= 2}


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    sensitivity: Sensitivity
    source: str
    status: str


def _row(r: sqlite3.Row) -> Fact:
    return Fact(r["id"], r["text"], Sensitivity(r["sensitivity"]), r["source"], r["status"])


class FactStore:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = utcnow):
        self.conn, self.clock = conn, clock

    def add(self, text: str, source: str, status: str = "confirmed") -> int:
        """Store a fact (idempotent on identical text). Raises ValueError('bad_text'|'secret_in_text')."""
        if status not in ("confirmed", "candidate"):
            raise ValueError("bad_status")
        if not source:
            raise ValueError("provenance_required")
        text = clean_user_text(text, MAX_FACT)
        level = classify(text, "user_chat").level
        norm = " ".join(text.casefold().split())
        now = to_iso(self.clock())
        for r in self.conn.execute("SELECT id, text, status FROM facts").fetchall():
            if " ".join(r["text"].casefold().split()) == norm:
                if status == "confirmed" and r["status"] == "candidate":
                    self.confirm(r["id"])
                return int(r["id"])
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO facts (text, sensitivity, source, status, created_utc, last_confirmed_utc)"
                " VALUES (?,?,?,?,?,?)",
                (text, int(level), source, status, now, now if status == "confirmed" else None))
        return int(cur.lastrowid)

    def get(self, fact_id: int) -> Optional[Fact]:
        r = self.conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return _row(r) if r else None

    def list(self, status: Optional[str] = None) -> List[Fact]:
        if status:
            rows = self.conn.execute("SELECT * FROM facts WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM facts ORDER BY id").fetchall()
        return [_row(r) for r in rows]

    def confirm(self, fact_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE facts SET status='confirmed', last_confirmed_utc=? WHERE id=? AND status='candidate'",
                (to_iso(self.clock()), fact_id))
        return cur.rowcount > 0

    def forget(self, fact_id: int) -> bool:
        """Hard delete: forgetting means gone."""
        with self.conn:
            cur = self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        return cur.rowcount > 0

    def search(self, query: str, limit: int = 5,
               max_level: Sensitivity = Sensitivity.S2_SENSITIVE) -> List[Fact]:
        """Confirmed facts ranked by keyword overlap, newest first on ties."""
        q = tokens(query)
        scored = []
        for f in self.list("confirmed"):
            if f.sensitivity > max_level:
                continue
            score = len(q & tokens(f.text))
            if score:
                scored.append((score, f.id, f))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [f for _, _, f in scored[:limit]]

    def context_block(self, query: str, max_level: Sensitivity, limit: int = 5) -> str:
        """Prompt-ready text limited to facts the destination is allowed to see ('' when none)."""
        facts = self.search(query, limit, max_level)
        if not facts:
            return ""
        return "Known facts about the user:\n" + "\n".join(f"- {f.text}" for f in facts)
