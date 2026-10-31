from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from edith.timeutil import UTC

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=IST)  # a Monday


class Clock:
    def __init__(self, start=NOW):
        self.now = start.astimezone(UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)

    def set(self, dt):
        self.now = dt.astimezone(UTC)


def ist(*args):
    return datetime(*args, tzinfo=IST)
