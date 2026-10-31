import unittest

from edith.goals.tracker import GoalStore
from edith.store.db import connect
from tests.helpers import IST, Clock, ist

TARGET = ist(2026, 11, 30, 23, 59)


def make():
    clock = Clock()
    return clock, GoalStore(connect(), IST, clock)


def goal_with_tasks(gs, n=4, target=TARGET):
    gid = gs.add_goal("Learn guitar", target)
    return gid, [gs.add_task(f"step {i}", gid) for i in range(n)]


class ProgressTests(unittest.TestCase):
    def test_start_of_goal_is_on_track(self):
        _, gs = make()
        gid, tasks = goal_with_tasks(gs)
        gs.complete_task(tasks[0])
        p = gs.progress(gs.get_goal(gid))
        self.assertEqual((p.total, p.done, p.pct, p.days_left, p.status), (4, 1, 25, 28, "on_track"))

    def test_behind_on_track_done_overdue(self):
        clock, gs = make()
        gid, tasks = goal_with_tasks(gs)
        gs.complete_task(tasks[0])
        clock.set(ist(2026, 11, 23, 8, 0))               # ~73% of the time used, 25% of tasks done
        self.assertEqual(gs.progress(gs.get_goal(gid)).status, "behind")
        gs.complete_task(tasks[1])
        gs.complete_task(tasks[2])                        # 75% done
        self.assertEqual(gs.progress(gs.get_goal(gid)).status, "on_track")
        clock.set(ist(2026, 12, 2, 8, 0))
        self.assertEqual(gs.progress(gs.get_goal(gid)).status, "overdue")
        gs.complete_task(tasks[3])
        self.assertEqual(gs.progress(gs.get_goal(gid)).status, "done")

    def test_no_tasks_and_no_target(self):
        _, gs = make()
        empty = gs.add_goal("Read more")
        self.assertEqual(gs.progress(gs.get_goal(empty)).status, "no_tasks")
        gid = gs.add_goal("Open ended")
        gs.add_task("a", gid)
        p = gs.progress(gs.get_goal(gid))
        self.assertEqual((p.status, p.expected_pct, p.days_left), ("on_track", None, None))


class StoreTests(unittest.TestCase):
    def test_validation(self):
        _, gs = make()
        with self.assertRaises(ValueError) as ctx:
            gs.add_goal("old", ist(2026, 11, 1, 9, 0))
        self.assertEqual(str(ctx.exception), "target_in_past")
        with self.assertRaises(ValueError):
            gs.add_goal("x", TARGET.replace(tzinfo=None))
        with self.assertRaises(ValueError) as ctx:
            gs.add_goal("my OTP is 482913")
        self.assertEqual(str(ctx.exception), "secret_in_text")
        with self.assertRaises(ValueError) as ctx:
            gs.add_task("x", goal_id=99)
        self.assertEqual(str(ctx.exception), "no_such_goal")
        gid = gs.add_goal("g")
        gs.close_goal(gid)
        with self.assertRaises(ValueError):
            gs.add_task("x", goal_id=gid)

    def test_task_ordering_and_completion(self):
        _, gs = make()
        a = gs.add_task("no due")
        b = gs.add_task("due 5th", due=ist(2026, 11, 5, 9, 0))
        c = gs.add_task("due 3rd", due=ist(2026, 11, 3, 9, 0))
        self.assertEqual([t.id for t in gs.list_tasks()], [c, b, a])
        self.assertTrue(gs.complete_task(c))
        self.assertFalse(gs.complete_task(c))
        self.assertEqual([t.id for t in gs.list_tasks()], [b, a])
        self.assertEqual([t.id for t in gs.list_tasks(status="done")], [c])

    def test_close_goal(self):
        _, gs = make()
        gid = gs.add_goal("g")
        with self.assertRaises(ValueError):
            gs.close_goal(gid, "weird")
        self.assertTrue(gs.close_goal(gid, "abandoned"))
        self.assertFalse(gs.close_goal(gid))
        self.assertEqual(gs.list_goals(), [])
        self.assertEqual(len(gs.list_goals("abandoned")), 1)


class ReviewTests(unittest.TestCase):
    def test_review_contents(self):
        clock, gs = make()
        gid = gs.add_goal("Learn guitar", TARGET)
        gs.add_task("buy strings", gid, ist(2026, 11, 1, 9, 0))      # overdue
        gs.add_task("practice chords", gid, ist(2026, 11, 5, 9, 0))  # due this week
        done = gs.add_task("learn a song", gid)
        gs.complete_task(done)
        text = gs.review_text()
        for expected in ("Review - Mon 02 Nov", "G1 Learn guitar: 1/3 tasks (33%), 28 days left - on track",
                         "Overdue:", "buy strings", "Due this week:", "practice chords",
                         "Finished in the last 7 days: 1"):
            self.assertIn(expected, text)

    def test_empty_and_late(self):
        clock, gs = make()
        self.assertIn("Nothing tracked yet", gs.review_text())
        gs.add_goal("Learn guitar", TARGET)
        clock.set(ist(2026, 12, 2, 8, 0))
        self.assertIn("2 days late", gs.review_text())


if __name__ == "__main__":
    unittest.main()
