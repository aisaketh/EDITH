import contextlib
import io
import json
import unittest
from datetime import datetime

from edith.evals import runner
from edith.timeutil import get_tz

IDEAL = {"intent": "reminder", "text": "pay rent", "when": "2026-11-03T09:00", "recurrence": None,
         "language": "en", "clarify": None}
EXPECT = {"outcome": "proposal", "kind": "reminder", "when": "2026-11-03T09:00", "language": "en"}


def doc(reply, expect=EXPECT):
    return {"now": "2026-11-02T08:00", "timezone": "Asia/Kolkata",
            "cases": [{"id": "t", "category": "c", "text": "remind me", "fake_reply": reply, "expect": dict(expect)}]}


def verdict(reply, expect=EXPECT):
    (r,) = runner.run_cases(doc(reply, expect), runner.fake_complete_for)
    return r.verdict


class RecordedCasesTests(unittest.TestCase):
    def test_all_recorded_cases_pass_with_zero_wrong_actions(self):
        results = runner.run_cases(runner.load_cases(), runner.fake_complete_for)
        self.assertEqual([r for r in results if r.verdict != "pass"], [])
        self.assertEqual(runner.summarize(results)["wrong_action"], 0)

    def test_case_file_is_well_formed_and_covers_what_matters(self):
        d = runner.load_cases()
        ids = [c["id"] for c in d["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 20)
        cats = {c["category"] for c in d["cases"]}
        self.assertTrue({"te_script", "te_roman", "secrets", "injection", "ambiguous", "past"} <= cats)
        for c in d["cases"]:
            self.assertIn(c["expect"]["outcome"], ("proposal", "proposal_or_clarify", "clarify", "no_proposal"))
            self.assertTrue(c["text"] and "fake_reply" in c)


class JudgeTests(unittest.TestCase):
    def test_verdict_matrix(self):
        unclear = {"intent": "unclear", "text": None, "when": None, "recurrence": None, "language": "en",
                   "clarify": "When?"}
        self.assertEqual(verdict(IDEAL), "pass")
        self.assertEqual(verdict(unclear), "safe_miss")
        self.assertEqual(verdict("not json at all"), "error")
        self.assertEqual(verdict(dict(IDEAL, when="2026-11-03T21:00")), "wrong_action")   # subtly wrong hour
        self.assertEqual(verdict(dict(IDEAL, language="te")), "wrong_action")
        self.assertEqual(verdict(dict(IDEAL, recurrence={"freq": "daily", "every": 1})), "wrong_action")
        self.assertEqual(verdict(IDEAL, {"outcome": "clarify"}), "wrong_action")          # answered instead of asking
        self.assertEqual(verdict(unclear, {"outcome": "clarify"}), "pass")
        self.assertEqual(verdict(IDEAL, {"outcome": "no_proposal"}), "wrong_action")
        self.assertEqual(verdict(unclear, {"outcome": "no_proposal"}), "pass")
        both = dict(EXPECT, outcome="proposal_or_clarify")
        self.assertEqual(verdict(unclear, both), "pass")
        self.assertEqual(verdict("garbage", both), "safe_miss")
        self.assertEqual(verdict(dict(IDEAL, when="2026-11-03T10:00"), both), "wrong_action")

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            verdict(IDEAL, {"outcome": "whatever"})


class InterpretTests(unittest.TestCase):
    def test_one_retry_on_unusable_output(self):
        tz = get_tz()
        now = datetime(2026, 11, 2, 8, 0, tzinfo=tz)
        replies = ["sure!", json.dumps(IDEAL)]
        calls = []

        def complete(prompt):
            calls.append(prompt)
            return replies.pop(0)
        out = runner.interpret(complete, "remind me", now, tz)
        self.assertEqual(out.kind, "proposal")
        self.assertEqual(len(calls), 2)
        self.assertIn("invalid", calls[1])

    def test_unavailable_model_is_an_error_not_a_guess(self):
        tz = get_tz()
        out = runner.interpret(lambda p: None, "remind me", datetime(2026, 11, 2, 8, 0, tzinfo=tz), tz)
        self.assertEqual((out.kind, out.detail), ("error", "model_unavailable"))


class ReportTests(unittest.TestCase):
    def test_report_and_exit_codes(self):
        results = runner.run_cases(doc(dict(IDEAL, when="2026-11-03T21:00")), runner.fake_complete_for)
        report = runner.format_report(results)
        self.assertIn("wrong-action rate", report)
        self.assertIn("not passing", report)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(runner.main([]), 0)                       # recorded cases are clean
            self.assertEqual(runner.main(["--max-wrong", "-1"]), 1)    # the gate trips when over the limit
        self.assertIn("overall pass rate : 100%", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
