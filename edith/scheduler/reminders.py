"""Deterministic reminders, nags, deadlines and the scheduler tick. No LLM anywhere in this file.

Delivery is at-least-once: a reminder only changes state after the notifier succeeded. A failing
notifier never loses a reminder; it is retried with backoff.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from ..privacy.classifier import find_sensitive
from ..privacy.sensitivity import Sensitivity
from ..timeutil import from_iso, to_iso, utcnow
from .recurrence import next_after, validate_recurrence

MAX_TEXT = 500
MAX_BATCH = 50
# (days before the deadline, local time of day). The moment of the deadline itself is always added.
DEFAULT_DEADLINE_OFFSETS: Tuple[Tuple[int, time], ...] = ((3, time(9, 0)), (1, time(9, 0)), (0, time(8, 0)))


class DeliveryError(Exception):
    """Raised by a Notifier when a message could not be delivered."""


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due: datetime  # aware, UTC
    recurrence: Optional[Dict[str, Any]]
    status: str
    deadline_id: Optional[int]
    nags_left: int
    nag_minutes: int
    fail_count: int


class Notifier(Protocol):
    def send(self, reminder: Reminder, late_by: timedelta) -> None: ...


def _row_to_reminder(r: sqlite3.Row) -> Reminder:
    return Reminder(r["id"], r["text"], from_iso(r["due_utc"]),
                    json.loads(r["recurrence"]) if r["recurrence"] else None,
                    r["status"], r["deadline_id"], r["nags_left"], r["nag_minutes"], r["fail_count"])


def _clean_text(text: str) -> str:
    text = " ".join(str(text).split())
    if not text or len(text) > MAX_TEXT:
        raise ValueError("bad_text")
    if any(f.level >= Sensitivity.S3_SECRET for f in find_sensitive(text)):
        raise ValueError("secret_in_text")
    return text


class ReminderStore:
    def __init__(self, conn: sqlite3.Connection, tz: tzinfo, clock: Callable[[], datetime] = utcnow):
        self.conn, self.tz, self.clock = conn, tz, clock

    # --- reminders --------------------------------------------------------
    def add_reminder(self, text: str, due: datetime, recurrence: Optional[Dict[str, Any]] = None,
                     nags: int = 0, nag_minutes: int = 15, origin: str = "user",
                     deadline_id: Optional[int] = None) -> int:
        if due.tzinfo is None:
            raise ValueError("naive_datetime")
        text = _clean_text(text)
        rec = validate_recurrence(recurrence) if recurrence is not None else None
        if rec is not None and rec["freq"] == "monthly" and "day" not in rec:
            rec["day"] = due.astimezone(self.tz).day
        if not 0 <= nags <= 10 or not 1 <= nag_minutes <= 240 or (rec is not None and nags):
            raise ValueError("bad_nag_settings")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO reminders (text, due_utc, recurrence, nags_left, nag_minutes, deadline_id,"
                " created_utc, origin) VALUES (?,?,?,?,?,?,?,?)",
                (text, to_iso(due), json.dumps(rec) if rec else None, nags, nag_minutes, deadline_id,
                 to_iso(self.clock()), origin))
        return int(cur.lastrowid)

    def get(self, reminder_id: int) -> Optional[Reminder]:
        row = self.conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        return _row_to_reminder(row) if row else None

    def list_pending(self) -> List[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status='pending' ORDER BY due_utc, id").fetchall()
        return [_row_to_reminder(r) for r in rows]

    def done(self, reminder_id: int) -> bool:
        """Acknowledge. One-off: finished (stops nags). Recurring: series continues, acknowledgement only."""
        r = self.get(reminder_id)
        if r is None or r.status not in ("pending", "fired"):
            return False
        if r.recurrence:
            return True
        with self.conn:
            self.conn.execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))
        return True

    def cancel(self, reminder_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE reminders SET status='cancelled' WHERE id=? AND status IN ('pending','fired')",
                (reminder_id,))
        return cur.rowcount > 0

    def snooze(self, reminder_id: int, minutes: int) -> Optional[int]:
        """Return the id of the snoozed reminder (a new one-off for recurring ones), or None."""
        if not 1 <= minutes <= 1440:
            raise ValueError("bad_snooze")
        r = self.get(reminder_id)
        if r is None or r.status not in ("pending", "fired"):
            return None
        due = self.clock() + timedelta(minutes=minutes)
        if r.recurrence:
            return self.add_reminder(r.text, due, origin="snooze")
        with self.conn:
            self.conn.execute("UPDATE reminders SET due_utc=?, status='pending', fail_count=0 WHERE id=?",
                              (to_iso(due), reminder_id))
        return reminder_id

    # --- deadlines --------------------------------------------------------
    def add_deadline(self, title: str, due: datetime,
                     offsets: Sequence[Tuple[int, time]] = DEFAULT_DEADLINE_OFFSETS) -> Tuple[int, List[int]]:
        if due.tzinfo is None:
            raise ValueError("naive_datetime")
        title = _clean_text(title)
        if len(title) > 200:
            raise ValueError("bad_text")
        now = self.clock()
        if due <= now:
            raise ValueError("deadline_in_past")
        due_local = due.astimezone(self.tz)
        when = due_local.strftime("%a %d %b, %I:%M %p")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO deadlines (title, due_utc, created_utc) VALUES (?,?,?)",
                (title, to_iso(due), to_iso(now)))
        deadline_id = int(cur.lastrowid)
        plan: List[Tuple[datetime, str]] = []
        for days, at in offsets:
            moment = datetime.combine(due_local.date() - timedelta(days=days), at, tzinfo=self.tz)
            if moment <= now or moment >= due:
                continue
            label = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            plan.append((moment, f"Deadline {label}: {title} (due {when})"))
        plan.append((due, f"Deadline now: {title}"))
        ids = [self.add_reminder(text, moment, origin="deadline", deadline_id=deadline_id)
               for moment, text in plan]
        return deadline_id, ids

    def list_deadlines(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deadlines WHERE status='open' ORDER BY due_utc, id").fetchall()

    def close_deadline(self, deadline_id: int, status: str = "done") -> bool:
        if status not in ("done", "cancelled"):
            raise ValueError("bad_status")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE deadlines SET status=? WHERE id=? AND status='open'", (status, deadline_id))
            if cur.rowcount:
                self.conn.execute(
                    "UPDATE reminders SET status='cancelled' WHERE deadline_id=? AND status='pending'",
                    (deadline_id,))
        return cur.rowcount > 0


class Scheduler:
    def __init__(self, store: ReminderStore, notifier: Notifier):
        self.store, self.notifier = store, notifier

    def tick(self) -> int:
        """Deliver everything due. Returns the number delivered."""
        conn, now = self.store.conn, self.store.clock()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status='pending' AND due_utc<=? ORDER BY due_utc, id LIMIT ?",
            (to_iso(now), MAX_BATCH)).fetchall()
        delivered = 0
        for row in rows:
            r = _row_to_reminder(row)
            try:
                self.notifier.send(r, now - r.due)
            except DeliveryError:
                backoff = timedelta(minutes=min(2 ** r.fail_count, 30))
                with conn:
                    conn.execute("UPDATE reminders SET fail_count=fail_count+1, due_utc=? WHERE id=?",
                                 (to_iso(now + backoff), r.id))
                continue
            delivered += 1
            with conn:
                if r.recurrence:
                    nxt = next_after(r.due.astimezone(self.store.tz), r.recurrence, now)
                    conn.execute("UPDATE reminders SET due_utc=?, last_fired_utc=?, fail_count=0 WHERE id=?",
                                 (to_iso(nxt), to_iso(now), r.id))
                elif r.nags_left > 0:
                    conn.execute(
                        "UPDATE reminders SET due_utc=?, nags_left=nags_left-1, last_fired_utc=?,"
                        " fail_count=0 WHERE id=?",
                        (to_iso(now + timedelta(minutes=r.nag_minutes)), to_iso(now), r.id))
                else:
                    conn.execute("UPDATE reminders SET status='fired', last_fired_utc=?, fail_count=0 WHERE id=?",
                                 (to_iso(now), r.id))
        return delivered

    def run(self, stop: threading.Event, interval: float = 15.0) -> None:
        """Blocking loop for the long-running process; `stop.set()` ends it."""
        while not stop.is_set():
            try:
                self.tick()
            except sqlite3.Error:
                pass  # keep the loop alive; the next tick retries
            stop.wait(interval)
