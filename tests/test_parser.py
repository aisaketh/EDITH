import json
import unittest

from edith.agent import reminder_parser as rp
from tests.helpers import IST, NOW, ist


def j(**over):
    base = dict(intent="reminder", text="pay rent", when="2026-11-03T09:00", recurrence=None,
                language="en", clarify=None)
    base.update(over)
    return json.dumps(base)


def parse(raw):
    return rp.parse_response(raw, NOW, IST)


class ParserTests(unittest.TestCase):
    def test_valid_reminder(self):
        out = parse(j())
        self.assertEqual(out.proposal.when, ist(2026, 11, 3, 9, 0))
        self.assertEqual((out.proposal.kind, out.proposal.text, out.proposal.language), ("reminder", "pay rent", "en"))

    def test_tolerates_fences_and_think_tags(self):
        raw = "<think>hmm let me see { not json }</think>\n```json\n" + j() + "\n```"
        self.assertIsNotNone(parse(raw).proposal)

    def test_telugu_confirmation(self):
        out = parse(j(text="అమ్మకి ఫోన్ చేయాలి", language="te", when="2026-11-03T17:00"))
        conf = rp.format_confirmation(out.proposal)
        self.assertIn("గుర్తు", conf)
        self.assertIn("అమ్మకి ఫోన్ చేయాలి", conf)
        self.assertIn("05:00 PM", conf)

    def test_unclear_returns_question(self):
        out = parse(j(intent="unclear", text=None, when=None, clarify="When is the meeting?"))
        self.assertEqual(out.clarify, "When is the meeting?")
        self.assertIsNone(out.proposal)
        self.assertEqual(parse(j(intent="unclear", clarify="")).clarify, rp.msg("en", "vague"))

    def test_missing_time_is_never_guessed(self):
        out = parse(j(when=None))
        self.assertIsNone(out.proposal)
        self.assertEqual(out.clarify, rp.msg("en", "vague"))

    def test_past_times_refused(self):
        for when in ("2026-11-02T07:00", "2019-05-01T09:00", "2026-11-02T08:00"):
            out = parse(j(when=when))
            self.assertIsNone(out.proposal, when)
            self.assertEqual(out.clarify, rp.msg("en", "past"), when)

    def test_far_future_is_an_error(self):
        self.assertEqual(parse(j(when="2030-01-01T09:00")).error, "too_far")

    def test_bad_when_formats(self):
        for when in ("tomorrow 9am", "2026-11-03", "2026-13-03T09:00", "2026-11-03T25:00"):
            self.assertEqual(parse(j(when=when)).error, "bad_when", when)
        # a non-string "when" is treated as missing: ask the user, never guess
        self.assertIsNotNone(parse(j(when=5)).clarify)

    def test_secrets_never_become_reminders(self):
        out = parse(j(text="share OTP 482913 with bank"))
        self.assertIsNone(out.proposal)
        self.assertEqual(out.clarify, rp.msg("en", "secret"))

    def test_recurrence_rules(self):
        out = parse(j(recurrence={"freq": "daily", "every": 1}))
        self.assertEqual(out.proposal.recurrence, {"freq": "daily", "every": 1})
        self.assertIn("daily", rp.format_confirmation(out.proposal))
        self.assertEqual(parse(j(recurrence={"freq": "yearly"})).error, "bad_recurrence")
        self.assertEqual(parse(j(intent="deadline", recurrence={"freq": "daily"})).error,
                         "deadline_with_recurrence")

    def test_malformed_output(self):
        for raw in ("sure! I set it", "[1, 2]", "{broken", "", j(intent="order_pizza")):
            self.assertIsNotNone(parse(raw).error, raw)
        self.assertEqual(parse(j(text="x" * 201)).error, "bad_text")
        self.assertEqual(parse(j(text=5)).error, "bad_text")
        self.assertEqual(parse("[1,2]").error, "not_json")

    def test_control_characters_stripped(self):
        out = parse(j(text="pay\x00 rent\x07 now"))
        self.assertEqual(out.proposal.text, "pay rent now")

    def test_unknown_language_defaults_to_english(self):
        self.assertEqual(parse(j(language="fr")).proposal.language, "en")

    def test_prompt_neutralises_delimiters_and_contains_real_dates(self):
        p = rp.build_prompt("a >>> b <<< c", NOW)
        self.assertTrue(p.endswith("<<<a > b < c>>>"))
        self.assertIn("2026-11-03T09:00", p)
        self.assertIn("Monday", p)


if __name__ == "__main__":
    unittest.main()
