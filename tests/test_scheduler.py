import os
import tempfile
import unittest
from datetime import datetime, time

from edith.scheduler.recurrence import validate_recurrence
from edith.scheduler.reminders import DeliveryError, ReminderStore, Scheduler
from edith.store.db import connect
from edith.timeutil import UTC, from_iso
from tests.helpers import IST, NOW, Clock, ist


class FakeNotifier:
    def __init__(self):
        self.sent, self.fail = [], False

    def send(self, reminder, late_by):
        if self.fail:
            raise DeliveryError("down")
        self.sent.append((reminder.id, reminder.text, late_by))


def make(start=NOW):
    clock = Clock(start)
    store = ReminderStore(connect(), IST, clock)
    notifier = FakeNotifier()
    return clock, store, Scheduler(store, notifier), notifier


def due_local(store, rid):
    return store.get(rid).due.astimezone(IST)


class ReminderTests(unittest.TestCase):
    def test_one_off_fires_once_at_due_time(self):
        clock, store, sched, n = make()
        rid = store.add_reminder("pay rent", ist(2026, 11, 2, 9, 0))
        self.assertEqual(sched.tick(), 0)
        clock.advance(hours=1)
        self.assertEqual(sched.tick(), 1)
        self.assertEqual(sched.tick(), 0)
        self.assertEqual(store.get(rid).status, "fired")
        self.assertEqual([s[1] for s in n.sent], ["pay rent"])

    def test_daily_recurrence_skips_missed_days_and_fires_once(self):
        clock, store, sched, n = make()
        rid = store.add_reminder("take medicine", ist(2026, 11, 2, 9, 0), {"freq": "daily"})
        clock.set(ist(2026, 11, 5, 10, 0))  # laptop was off for 3 days
        self.assertEqual(sched.tick(), 1)
        self.assertEqual(len(n.sent), 1)
        self.assertGreater(n.sent[0][2].days, 2)  # told how late it is
        self.assertEqual(due_local(store, rid), ist(2026, 11, 6, 9, 0))
        self.assertEqual(store.get(rid).status, "pending")

    def test_monthly_clamps_to_month_end_and_returns(self):
        clock, store, sched, _ = make(ist(2027, 1, 31, 8, 0))
        rid = store.add_reminder("rent", ist(2027, 1, 31, 9, 0), {"freq": "monthly"})
        clock.set(ist(2027, 1, 31, 9, 0))
        sched.tick()
        self.assertEqual(due_local(store, rid), ist(2027, 2, 28, 9, 0))
        clock.set(ist(2027, 2, 28, 9, 0))
        sched.tick()
        self.assertEqual(due_local(store, rid), ist(2027, 3, 31, 9, 0))

    def test_weekly_with_weekdays(self):
        clock, store, sched, _ = make()
        rid = store.add_reminder("gym", ist(2026, 11, 2, 9, 0), {"freq": "weekly", "weekdays": [0, 4]})
        clock.set(ist(2026, 11, 2, 9, 0))
        sched.tick()
        self.assertEqual(due_local(store, rid), ist(2026, 11, 6, 9, 0))  # Friday
        clock.set(ist(2026, 11, 6, 9, 0))
        sched.tick()
        self.assertEqual(due_local(store, rid), ist(2026, 11, 9, 9, 0))  # next Monday

    def test_nags_repeat_until_done(self):
        clock, store, sched, n = make()
        rid = store.add_reminder("call mom", ist(2026, 11, 2, 9, 0), nags=2, nag_minutes=15)
        for hh, mm, expect in [(9, 0, 1), (9, 15, 1), (9, 30, 1), (9, 45, 0)]:
            clock.set(ist(2026, 11, 2, hh, mm))
            self.assertEqual(sched.tick(), expect, (hh, mm))
        self.assertEqual(store.get(rid).status, "fired")

    def test_done_stops_nags(self):
        clock, store, sched, n = make()
        rid = store.add_reminder("call mom", ist(2026, 11, 2, 9, 0), nags=3)
        clock.set(ist(2026, 11, 2, 9, 0))
        sched.tick()
        self.assertTrue(store.done(rid))
        clock.set(ist(2026, 11, 2, 12, 0))
        self.assertEqual(sched.tick(), 0)
        self.assertEqual(len(n.sent), 1)

    def test_delivery_failure_keeps_reminder_and_retries_with_backoff(self):
        clock, store, sched, n = make()
        rid = store.add_reminder("important", ist(2026, 11, 2, 9, 0))
        clock.set(ist(2026, 11, 2, 9, 0))
        n.fail = True
        self.assertEqual(sched.tick(), 0)
        r = store.get(rid)
        self.assertEqual((r.status, r.fail_count), ("pending", 1))
        n.fail = False
        self.assertEqual(sched.tick(), 0)  # backoff: not due yet
        clock.advance(minutes=2)
        self.assertEqual(sched.tick(), 1)
        self.assertEqual(store.get(rid).status, "fired")

    def test_cancel_and_done_on_recurring(self):
        _, store, _, _ = make()
        rid = store.add_reminder("x", ist(2026, 11, 3, 9, 0), {"freq": "daily"})
        self.assertTrue(store.done(rid))
        self.assertEqual(store.get(rid).status, "pending")  # ack only, series continues
        self.assertTrue(store.cancel(rid))
        self.assertFalse(store.cancel(rid))
        self.assertEqual(store.list_pending(), [])

    def test_snooze(self):
        clock, store, sched, _ = make()
        one = store.add_reminder("a", ist(2026, 11, 2, 9, 0))
        self.assertEqual(store.snooze(one, 30), one)
        self.assertEqual(due_local(store, one), ist(2026, 11, 2, 8, 30))
        rec = store.add_reminder("b", ist(2026, 11, 3, 9, 0), {"freq": "daily"})
        new = store.snooze(rec, 10)
        self.assertNotEqual(new, rec)
        self.assertIsNone(store.get(new).recurrence)
        self.assertEqual(due_local(store, rec), ist(2026, 11, 3, 9, 0))
        with self.assertRaises(ValueError):
            store.snooze(one, 0)
        self.assertIsNone(store.snooze(9999, 5))

    def test_input_validation(self):
        _, store, _, _ = make()
        with self.assertRaises(ValueError):
            store.add_reminder("x", datetime(2026, 11, 3, 9, 0))  # naive
        with self.assertRaises(ValueError):
            store.add_reminder("   ", ist(2026, 11, 3, 9, 0))
        with self.assertRaises(ValueError) as ctx:
            store.add_reminder("Your OTP is 482913", ist(2026, 11, 3, 9, 0))
        self.assertEqual(str(ctx.exception), "secret_in_text")
        with self.assertRaises(ValueError):
            store.add_reminder("x", ist(2026, 11, 3, 9, 0), {"freq": "daily"}, nags=2)

    def test_recurrence_validation(self):
        for bad in [None, [], {"freq": "hourly"}, {"freq": "daily", "every": 0}, {"freq": "daily", "every": True},
                    {"freq": "weekly", "weekdays": [7]}, {"freq": "daily", "weekdays": [1]},
                    {"freq": "daily", "evil": 1}, {"freq": "monthly", "day": 32}]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_recurrence(bad)
        self.assertEqual(validate_recurrence({"freq": "weekly", "weekdays": [4, 0, 4]})["weekdays"], [0, 4])

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "edith.db")
            conn = connect(path)
            ReminderStore(conn, IST, Clock()).add_reminder("keep me", ist(2026, 11, 3, 9, 0))
            conn.close()
            conn2 = connect(path)
            pending = ReminderStore(conn2, IST, Clock()).list_pending()
            conn2.close()
            self.assertEqual([r.text for r in pending], ["keep me"])


class DeadlineTests(unittest.TestCase):
    def test_escalation_schedule(self):
        _, store, _, _ = make()
        did, ids = store.add_deadline("Submit report", ist(2026, 11, 5, 17, 0))
        got = [(due_local(store, i), store.get(i).text) for i in ids]
        self.assertEqual([g[0] for g in got], [ist(2026, 11, 2, 9, 0), ist(2026, 11, 4, 9, 0),
                                               ist(2026, 11, 5, 8, 0), ist(2026, 11, 5, 17, 0)])
        self.assertIn("in 3 days", got[0][1])
        self.assertIn("tomorrow", got[1][1])
        self.assertIn("today", got[2][1])
        self.assertIn("now", got[3][1])

    def test_past_offsets_are_skipped(self):
        _, store, _, _ = make(ist(2026, 11, 4, 10, 0))
        _, ids = store.add_deadline("Submit report", ist(2026, 11, 5, 17, 0))
        self.assertEqual(len(ids), 2)  # morning-of + due moment

    def test_morning_after_due_is_skipped(self):
        _, store, _, _ = make(ist(2026, 11, 5, 6, 0))
        _, ids = store.add_deadline("early thing", ist(2026, 11, 5, 7, 0))
        self.assertEqual(len(ids), 1)

    def test_past_deadline_rejected(self):
        _, store, _, _ = make()
        with self.assertRaises(ValueError):
            store.add_deadline("old", ist(2026, 11, 1, 9, 0))

    def test_closing_deadline_cancels_pending_reminders(self):
        _, store, _, _ = make()
        did, ids = store.add_deadline("Submit report", ist(2026, 11, 5, 17, 0))
        self.assertTrue(store.close_deadline(did))
        self.assertEqual(store.list_pending(), [])
        self.assertEqual(store.list_deadlines(), [])
        self.assertFalse(store.close_deadline(did))


if __name__ == "__main__":
    unittest.main()
