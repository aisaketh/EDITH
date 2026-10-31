"""Time helpers. Everything is stored as UTC ISO strings; the user's timezone is applied at the edges."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_TZ = "Asia/Kolkata"


def get_tz(name: str = DEFAULT_TZ) -> ZoneInfo:
    return ZoneInfo(name)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime(ISO_FMT)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=UTC)
