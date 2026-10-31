"""Goals, tasks and progress monitoring. Progress is computed from task state, never by a model.

Status rules for a goal with a target date (documented so they can be tested and tuned):
  no_tasks  : nothing to measure yet
  done      : every task is done
  overdue   : target date passed and not done
  behind    : actual completion is more than 15 points below the straight-line expectation
  on_track  : otherwise
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Callable, List, Optional

from ..privacy.validate import clean_user_text
from ..timeutil import from_iso, to_iso, utcnow

BEHIND_MARGIN = 0.15


@dataclass(frozen=True)
class Goal:
    id: int
    title: str
    target: Optional[datetime]  # aware UTC
    status: str
    created: datetime


@dataclass(frozen=True)
class Task:
    id: int
    goal_id: Optional[int]
    title: str
    due: Optional[datetime]
    status: str
    done: Optional[datetime]


@dataclass(frozen=True)
class Progress:
    total: int
    done: int
    pct: int
    expected_pct: Optional[int]
    days_left: Optional[int]
    status: str


def _opt(s: Optional[str]) -> Optional[datetime]:
    return from_iso(s) if s else None


def _goal(r: sqlite3.Row) -> Goal:
    return Goal(r["id"], r["title"], _opt(r["target_utc"]), r["status"], from_iso(r["created_utc"]))


def _task(r: sqlite3.Row) -> Task:
    return Task(r["id"], r["goal_id"], r["title"], _opt(r["due_utc"]), r["status"], _opt(r["done_utc"]))


class GoalStore:
    def __init__(self, conn: sqlite3.Connection, tz: tzinfo, clock: Callable[[], datetime] = utcnow):
        self.conn, self.tz, self.clock = conn, tz, clock

    # --- goals ------------------------------------------------------------
    def add_goal(self, title: str, target: Optional[datetime] = None) -> int:
        title = clean_user_text(title, 200)
        if target is not None:
            if target.tzinfo is None:
                raise ValueError("naive_datetime")
            if target <= self.clock():
                raise ValueError("target_in_past")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO goals (title, target_utc, created_utc) VALUES (?,?,?)",
                (title, to_iso(target) if target else None, to_iso(self.clock())))
        return int(cur.lastrowid)

    def get_goal(self, goal_id: int) -> Optional[Goal]:
        r = self.conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return _goal(r) if r else None

    def list_goals(self, status: str = "active") -> List[Goal]:
        rows = self.conn.execute("SELECT * FROM goals WHERE status=? ORDER BY id", (status,)).fetchall()
        return [_goal(r) for r in rows]

    def close_goal(self, goal_id: int, status: str = "done") -> bool:
        if status not in ("done", "abandoned"):
            raise ValueError("bad_status")
        with self.conn:
            cur = self.conn.execute("UPDATE goals SET status=? WHERE id=? AND status='active'",
                                    (status, goal_id))
        return cur.rowcount > 0

    # --- tasks ------------------------------------------------------------
    def add_task(self, title: str, goal_id: Optional[int] = None, due: Optional[datetime] = None) -> int:
        title = clean_user_text(title, 200)
        if goal_id is not None:
            g = self.get_goal(goal_id)
            if g is None or g.status != "active":
                raise ValueError("no_such_goal")
        if due is not None and due.tzinfo is None:
            raise ValueError("naive_datetime")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tasks (goal_id, title, due_utc, created_utc) VALUES (?,?,?,?)",
                (goal_id, title, to_iso(due) if due else None, to_iso(self.clock())))
        return int(cur.lastrowid)

    def list_tasks(self, goal_id: Optional[int] = None, status: str = "open") -> List[Task]:
        sql, args = "SELECT * FROM tasks WHERE status=?", [status]
        if goal_id is not None:
            sql += " AND goal_id=?"
            args.append(goal_id)
        rows = self.conn.execute(sql + " ORDER BY (due_utc IS NULL), due_utc, id", args).fetchall()
        return [_task(r) for r in rows]

    def complete_task(self, task_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE tasks SET status='done', done_utc=? WHERE id=? AND status='open'",
                                    (to_iso(self.clock()), task_id))
        return cur.rowcount > 0

    # --- progress ---------------------------------------------------------
    def progress(self, goal: Goal) -> Progress:
        rows = self.conn.execute("SELECT status FROM tasks WHERE goal_id=?", (goal.id,)).fetchall()
        total = len(rows)
        done = sum(1 for r in rows if r["status"] == "done")
        now = self.clock()
        actual = done / total if total else 0.0
        expected: Optional[float] = None
        days_left: Optional[int] = None
        if goal.target is not None:
            span = (goal.target - goal.created).total_seconds()
            expected = min(1.0, max(0.0, (now - goal.created).total_seconds() / span)) if span > 0 else 1.0
            days_left = (goal.target.astimezone(self.tz).date() - now.astimezone(self.tz).date()).days
        if total == 0:
            status = "no_tasks"
        elif done == total:
            status = "done"
        elif goal.target is not None and now > goal.target:
            status = "overdue"
        elif expected is not None and actual + BEHIND_MARGIN < expected:
            status = "behind"
        else:
            status = "on_track"
        return Progress(total, done, round(actual * 100), None if expected is None else round(expected * 100),
                        days_left, status)

    def review_text(self) -> str:
        now = self.clock()
        local = now.astimezone(self.tz)
        lines = [f"Review - {local.strftime('%a %d %b')}"]
        goals = self.list_goals()
        if goals:
            lines.append("Goals:")
            for g in goals:
                p = self.progress(g)
                due = ""
                if p.days_left is not None:
                    due = f", {p.days_left} days left" if p.days_left >= 0 else f", {-p.days_left} days late"
                lines.append(f"G{g.id} {g.title}: {p.done}/{p.total} tasks ({p.pct}%){due} - "
                             f"{p.status.replace('_', ' ')}")
        open_tasks = self.list_tasks()
        overdue = [t for t in open_tasks if t.due and t.due < now]
        soon = [t for t in open_tasks if t.due and now <= t.due <= now + timedelta(days=7)]
        if overdue:
            lines.append("Overdue:")
            lines += [f"T{t.id} {t.title} (was due {t.due.astimezone(self.tz).strftime('%d %b')})" for t in overdue]
        if soon:
            lines.append("Due this week:")
            lines += [f"T{t.id} {t.title} ({t.due.astimezone(self.tz).strftime('%a %d %b')})" for t in soon]
        week_ago = to_iso(now - timedelta(days=7))
        finished = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND done_utc>=?", (week_ago,)).fetchone()[0]
        if finished:
            lines.append(f"Finished in the last 7 days: {finished}")
        if len(lines) == 1:
            return "Nothing tracked yet. Add a goal with /goal <title> by YYYY-MM-DD"
        return "\n".join(lines)
