"""Recurrence rules: {"freq": "daily|weekly|monthly", "every": n, "weekdays": [0-6], "day": 1-31}.

Occurrences keep the same local wall-clock time. Monthly rules clamp to the month's last day
(31 Jan -> 28 Feb -> 31 Mar) using the remembered anchor "day".
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Any, Dict

FREQS = ("daily", "weekly", "monthly")
_ALLOWED_KEYS = {"freq", "every", "weekdays", "day"}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_recurrence(rec: Any) -> Dict[str, Any]:
    """Return a normalised copy or raise ValueError('bad_recurrence')."""
    bad = ValueError("bad_recurrence")
    if not isinstance(rec, dict) or set(rec) - _ALLOWED_KEYS or rec.get("freq") not in FREQS:
        raise bad
    every = rec.get("every", 1)
    if not _is_int(every) or not 1 <= every <= 365:
        raise bad
    out: Dict[str, Any] = {"freq": rec["freq"], "every": every}
    weekdays = rec.get("weekdays")
    if weekdays is not None:
        if (rec["freq"] != "weekly" or every != 1 or not isinstance(weekdays, list) or not weekdays
                or any(not _is_int(d) or not 0 <= d <= 6 for d in weekdays)):
            raise bad
        out["weekdays"] = sorted(set(weekdays))
    day = rec.get("day")
    if day is not None:
        if rec["freq"] != "monthly" or not _is_int(day) or not 1 <= day <= 31:
            raise bad
        out["day"] = day
    return out


def _step(occ: datetime, rec: Dict[str, Any]) -> datetime:
    every = rec["every"]
    if rec["freq"] == "daily":
        return occ + timedelta(days=every)
    if rec["freq"] == "weekly":
        weekdays = rec.get("weekdays")
        if not weekdays:
            return occ + timedelta(days=7 * every)
        nxt = occ + timedelta(days=1)
        while nxt.weekday() not in weekdays:
            nxt += timedelta(days=1)
        return nxt
    idx = occ.month - 1 + every
    year, month = occ.year + idx // 12, idx % 12 + 1
    day = min(rec.get("day", occ.day), calendar.monthrange(year, month)[1])
    return occ.replace(year=year, month=month, day=day)


def next_after(base_local: datetime, rec: Dict[str, Any], after: datetime) -> datetime:
    """First occurrence strictly after `after`, stepping from `base_local` (aware, user's timezone)."""
    occ = base_local
    for _ in range(100_000):
        if occ > after:
            return occ
        occ = _step(occ, rec)
    raise ValueError("recurrence_overflow")
