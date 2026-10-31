import json
import unittest

from edith.agent.assistant import Assistant
from edith.broker.egress import Approval
from edith.broker.gateway import GatewayResult, Status
from edith.scheduler.reminders import ReminderStore
from edith.store.db import connect
from tests.helpers import IST, NOW, Clock, ist

CHAT = 42
HOST = "generativelanguage.googleapis.com"


def j(**over):
    base = dict(intent="reminder", text="pay rent", when="2026-11-03T09:00", recurrence=None,
                language="en", clarify=None)
    base.update(over)
    return json.dumps(base)


class FakeLLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, prompt, source=None, approval=None, only=None, declared_level=None):
        self.calls.append({"prompt": prompt, "source": source, "approval": approval, "only": only})
        r = self.replies.pop(0)
        return r if isinstance(r, GatewayResult) else GatewayResult(Status.OK, r, "fake")


def build(*replies):
    clock = Clock()
    store = ReminderStore(connect(), IST, clock)
    llm = FakeLLM(*replies)
    return clock, store, llm, Assistant(store, llm)


class NaturalLanguageTests(unittest.TestCase):
    def test_nothing_is_saved_until_confirmed(self):
        _, store, llm, a = build(j())
        reply = a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        self.assertIn("pay rent", reply)
        self.assertEqual(store.list_pending(), [])
        self.assertIn("Saved", a.handle(CHAT, "yes"))
        (r,) = store.list_pending()
        self.assertEqual((r.text, r.due.isoformat()), ("pay rent", "2026-11-03T03:30:00+00:00"))
        self.assertEqual(llm.calls[0]["source"], "user_chat")

    def test_telugu_round_trip(self):
        _, store, _, a = build(j(text="అమ్మకి ఫోన్ చేయాలి", language="te", when="2026-11-03T17:00"))
        self.assertIn("గుర్తు", a.handle(CHAT, "రేపు సాయంత్రం 5 కి అమ్మకి ఫోన్ చేయాలి"))
        self.assertIn("సేవ్", a.handle(CHAT, "అవును"))
        self.assertEqual(store.list_pending()[0].text, "అమ్మకి ఫోన్ చేయాలి")

    def test_decline_saves_nothing(self):
        _, store, _, a = build(j(), j(language="te"))
        a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        self.assertIn("Nothing saved", a.handle(CHAT, "no"))
        a.handle(CHAT, "repu 9 ki rent kattali")
        self.assertIn("రద్దు", a.handle(CHAT, "కాదు"))
        self.assertEqual(store.list_pending(), [])

    def test_other_message_abandons_pending(self):
        _, store, _, a = build(j(), j(intent="unclear", clarify="When?"))
        a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        self.assertEqual(a.handle(CHAT, "/list"), "Nothing pending.")
        self.assertEqual(a.handle(CHAT, "yes"), "When?")  # now just a new message
        self.assertEqual(store.list_pending(), [])

    def test_pending_is_per_chat(self):
        _, store, _, a = build(j(), j(intent="unclear", clarify="When?"))
        a.handle(1, "remind me to pay rent tomorrow at 9am")
        self.assertEqual(a.handle(2, "yes"), "When?")
        self.assertEqual(store.list_pending(), [])

    def test_confirmation_expires(self):
        clock, store, _, a = build(j(), j(intent="unclear", clarify="When?"))
        a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        clock.advance(minutes=11)
        self.assertEqual(a.handle(CHAT, "yes"), "When?")
        self.assertEqual(store.list_pending(), [])

    def test_past_time_is_refused_not_saved(self):
        _, store, _, a = build(j(when="2026-11-01T09:00"), j(intent="unclear", clarify="When?"))
        self.assertIn("passed", a.handle(CHAT, "remind me yesterday"))
        a.handle(CHAT, "yes")
        self.assertEqual(store.list_pending(), [])

    def test_invalid_output_retried_once(self):
        _, _, llm, a = build("sure, done!", j())
        self.assertIn("pay rent", a.handle(CHAT, "remind me to pay rent tomorrow at 9am"))
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("invalid", llm.calls[1]["prompt"])

    def test_invalid_twice_gives_up_without_saving(self):
        _, store, llm, a = build("nope", "still nope")
        self.assertIn("/remind", a.handle(CHAT, "remind me to pay rent tomorrow at 9am"))
        self.assertEqual(store.list_pending(), [])

    def test_deadline_flow(self):
        _, store, _, a = build(j(intent="deadline", text="Submit report", when="2026-11-05T17:00"))
        a.handle(CHAT, "report due thursday 5pm")
        self.assertIn("4 reminders", a.handle(CHAT, "yes"))
        self.assertEqual(len(store.list_pending()), 4)
        self.assertIn("Submit report", a.handle(CHAT, "/list"))

    def test_gateway_outcomes(self):
        _, _, _, a = build(GatewayResult(Status.DENIED, reason="secret_in_payload"),
                           GatewayResult(Status.UNAVAILABLE, reason="no_provider"),
                           GatewayResult(Status.DENIED, reason="host_not_allowlisted"))
        self.assertIn("secret", a.handle(CHAT, "remind me about OTP 482913"))
        self.assertIn("/remind", a.handle(CHAT, "remind me tomorrow 9am"))
        self.assertIn("safely", a.handle(CHAT, "remind me tomorrow 9am"))


class ApprovalFlowTests(unittest.TestCase):
    def needs_approval(self):
        return GatewayResult(Status.NEEDS_APPROVAL, provider="gemini", reason="approval_required",
                             approval_host=HOST, payload_sha256="abc123")

    def test_approved_send_reuses_exact_prompt(self):
        clock, store, llm, a = build(self.needs_approval(), j())
        reply = a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        self.assertIn(HOST, reply)
        self.assertEqual(len(llm.calls), 1)
        clock.advance(minutes=1)  # a rebuilt prompt would differ (it contains the time)
        self.assertIn("pay rent", a.handle(CHAT, "yes"))
        first, second = llm.calls
        self.assertEqual(second["prompt"], first["prompt"])
        self.assertEqual(second["approval"], Approval("abc123", HOST))
        self.assertEqual(second["only"], ["gemini"])
        self.assertIn("Saved", a.handle(CHAT, "yes"))
        self.assertEqual(len(store.list_pending()), 1)

    def test_declined_send_calls_nothing(self):
        _, _, llm, a = build(self.needs_approval())
        a.handle(CHAT, "remind me to pay rent tomorrow at 9am")
        self.assertIn("won't send", a.handle(CHAT, "no"))
        self.assertEqual(len(llm.calls), 1)


class CommandTests(unittest.TestCase):
    def test_remind_list_snooze_done_cancel(self):
        _, store, llm, a = build()
        self.assertIn("Saved (#1)", a.handle(CHAT, "/remind 2026-11-03 09:00 buy milk"))
        self.assertIn("buy milk", a.handle(CHAT, "/list"))
        self.assertIn("Snoozed", a.handle(CHAT, "/snooze 1 2h"))
        self.assertEqual(store.get(1).due.astimezone(IST), ist(2026, 11, 2, 10, 0))
        self.assertEqual(a.handle(CHAT, "/done 1"), "Done.")
        self.assertIn("No active", a.handle(CHAT, "/done 1"))
        a.handle(CHAT, "/remind 2026-11-04 09:00 other")
        self.assertEqual(a.handle(CHAT, "/cancel 2"), "Cancelled.")
        self.assertEqual(llm.calls, [])  # commands never touch a model

    def test_deadline_command(self):
        _, store, _, a = build()
        self.assertIn("4 reminders", a.handle(CHAT, "/deadline 2026-11-05 17:00 Submit report"))

    def test_bad_input_is_rejected(self):
        _, store, _, a = build()
        self.assertIn("Usage", a.handle(CHAT, "/remind tomorrow 9am milk"))
        self.assertIn("Usage", a.handle(CHAT, "/remind 2026-13-40 09:00 milk"))
        self.assertIn("passed", a.handle(CHAT, "/remind 2026-11-01 09:00 milk"))
        self.assertIn("secret", a.handle(CHAT, "/remind 2026-11-03 09:00 OTP is 482913"))
        self.assertIn("Usage", a.handle(CHAT, "/snooze 1 banana"))
        self.assertIn("/list", a.handle(CHAT, "/whatever"))
        self.assertEqual(a.handle(CHAT, "   "), "")
        self.assertEqual(store.list_pending(), [])

    def test_commands_with_bot_suffix(self):
        _, _, _, a = build()
        self.assertEqual(a.handle(CHAT, "/list@edith_bot"), "Nothing pending.")


if __name__ == "__main__":
    unittest.main()
