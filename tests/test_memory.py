import os
import sqlite3
import tempfile
import unittest

from edith.memory.facts import FactStore, tokens
from edith.privacy.sensitivity import Sensitivity
from edith.store.db import MIGRATIONS, connect
from tests.helpers import Clock


def make():
    clock = Clock()
    return clock, FactStore(connect(), clock)


class FactTests(unittest.TestCase):
    def test_add_list_and_sensitivity(self):
        _, fs = make()
        fid = fs.add("I am vegetarian", "user_command")
        (f,) = fs.list("confirmed")
        self.assertEqual((f.id, f.text, f.source, f.status, f.sensitivity),
                         (fid, "I am vegetarian", "user_command", "confirmed", Sensitivity.S1_PERSONAL))
        pii = fs.add("my sister's number is 9876543210", "user_command")
        self.assertEqual(fs.get(pii).sensitivity, Sensitivity.S2_SENSITIVE)

    def test_duplicates_are_merged(self):
        _, fs = make()
        a = fs.add("I am vegetarian", "user_command")
        b = fs.add("  i AM   vegetarian ", "user_command")
        self.assertEqual(a, b)
        self.assertEqual(len(fs.list()), 1)

    def test_validation(self):
        _, fs = make()
        for text, code in [("", "bad_text"), ("x" * 301, "bad_text"), ("my OTP is 482913", "secret_in_text"),
                           ("card 4111 1111 1111 1111", "secret_in_text")]:
            with self.assertRaises(ValueError) as ctx:
                fs.add(text, "user_command")
            self.assertEqual(str(ctx.exception), code)
        with self.assertRaises(ValueError):
            fs.add("fact", "")                              # provenance is mandatory
        with self.assertRaises(ValueError):
            fs.add("fact", "user_command", status="weird")
        self.assertEqual(fs.list(), [])

    def test_candidates_are_inert_until_confirmed(self):
        _, fs = make()
        fid = fs.add("likes spicy food", "inferred", status="candidate")
        self.assertEqual(fs.search("spicy food"), [])
        self.assertEqual(fs.context_block("spicy", Sensitivity.S2_SENSITIVE), "")
        self.assertTrue(fs.confirm(fid))
        self.assertFalse(fs.confirm(fid))
        self.assertEqual([f.id for f in fs.search("spicy food")], [fid])

    def test_stating_a_candidate_confirms_it(self):
        _, fs = make()
        a = fs.add("likes biryani", "inferred", status="candidate")
        b = fs.add("likes biryani", "user_command")
        self.assertEqual(a, b)
        self.assertEqual(fs.get(a).status, "confirmed")

    def test_forget_is_a_hard_delete(self):
        _, fs = make()
        fid = fs.add("I am vegetarian", "user_command")
        self.assertTrue(fs.forget(fid))
        self.assertFalse(fs.forget(fid))
        self.assertIsNone(fs.get(fid))
        raw = fs.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(raw, 0)

    def test_search_ranking_and_telugu(self):
        _, fs = make()
        fs.add("I am vegetarian and avoid eggs", "user_command")
        fs.add("My favourite cuisine is Hyderabadi biryani", "user_command")
        fs.add("నాకు బిర్యానీ ఇష్టం", "user_command")
        self.assertEqual(fs.search("suggest vegetarian restaurants")[0].text, "I am vegetarian and avoid eggs")
        self.assertEqual(fs.search("బిర్యానీ")[0].text, "నాకు బిర్యానీ ఇష్టం")
        self.assertEqual(fs.search("completely unrelated"), [])

    def test_context_respects_destination_level(self):
        _, fs = make()
        fs.add("I am vegetarian", "user_command")
        fs.add("my sister's number is 9876543210 vegetarian", "user_command")
        s1 = fs.context_block("vegetarian", Sensitivity.S1_PERSONAL)
        self.assertIn("I am vegetarian", s1)
        self.assertNotIn("9876543210", s1)
        self.assertIn("9876543210", fs.context_block("vegetarian", Sensitivity.S2_SENSITIVE))
        self.assertEqual(fs.context_block("vegetarian", Sensitivity.S0_PUBLIC), "")

    def test_tokens(self):
        self.assertEqual(tokens("Hello, world! a"), {"hello", "world"})


class MigrationTests(unittest.TestCase):
    def test_fresh_database_has_all_tables(self):
        conn = connect()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"reminders", "deadlines", "facts", "goals", "tasks"} <= names)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))

    def test_upgrade_from_v1_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            old.executescript(MIGRATIONS[0])
            old.execute("INSERT INTO reminders (text, due_utc, created_utc) VALUES ('keep','2026-11-03T03:30:00Z','2026-11-02T00:00:00Z')")
            old.execute("PRAGMA user_version = 1")
            old.commit()
            old.close()
            conn = connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
            self.assertEqual(conn.execute("SELECT text FROM reminders").fetchone()[0], "keep")
            conn.execute("SELECT * FROM facts")  # table exists after upgrade
            conn.close()
            connect(path).close()  # idempotent


if __name__ == "__main__":
    unittest.main()
