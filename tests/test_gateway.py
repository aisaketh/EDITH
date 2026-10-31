import unittest

from edith.broker.egress import Approval, EgressGuard
from edith.broker.gateway import LLMGateway, ProviderError, RateLimited, Status
from edith.broker.ledger import Ledger
from edith.privacy.sensitivity import Destination


class FakeProvider:
    def __init__(self, name, host, dest, reply="ok", error=None):
        self.name, self.host, self.dest = name, host, dest
        self.reply, self.error = reply, error
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply(prompt) if callable(self.reply) else self.reply


def local(**kw):
    return FakeProvider("local", "localhost:11434", Destination.LOCAL, **kw)


def cloud(**kw):
    return FakeProvider("gemini", "generativelanguage.googleapis.com", Destination.CLOUD_FREE, **kw)


class GatewayTests(unittest.TestCase):
    def gw(self, providers, **kw):
        return LLMGateway(EgressGuard(Ledger()), providers, **kw)

    def test_public_query_prefers_first_provider(self):
        c, l = cloud(reply="cloud"), local(reply="local")
        r = self.gw([c, l]).complete("best biryani in Hyderabad", source="web")
        self.assertEqual((r.status, r.text), (Status.OK, "cloud"))
        self.assertEqual(l.prompts, [])

    def test_email_never_reaches_cloud_and_uses_local(self):
        c, l = cloud(), local(reply="local summary")
        r = self.gw([c, l]).complete("Summarise: lunch at 1pm", source="gmail")
        self.assertEqual((r.status, r.provider), (Status.OK, "local"))
        self.assertEqual(c.prompts, [])

    def test_email_with_local_down_asks_instead_of_calling_cloud(self):
        c, l = cloud(), local(error=ProviderError("down"))
        r = self.gw([l, c]).complete("Summarise: lunch at 1pm", source="gmail")
        self.assertEqual(r.status, Status.NEEDS_APPROVAL)
        self.assertEqual(r.approval_host, c.host)
        self.assertEqual(c.prompts, [])

    def test_secret_denied_and_no_provider_called(self):
        c, l = cloud(), local()
        r = self.gw([c, l]).complete("my password: hunter2xyz", source="user_chat")
        self.assertEqual((r.status, r.reason), (Status.DENIED, "secret_in_payload"))
        self.assertEqual(c.prompts + l.prompts, [])

    def test_failure_falls_through(self):
        a = cloud(error=RateLimited("429"))
        b = FakeProvider("groq", "generativelanguage.googleapis.com", Destination.CLOUD_FREE, reply="b")
        r = self.gw([a, b]).complete("capital of France", source="web")
        self.assertEqual((r.status, r.provider), (Status.OK, "groq"))

    def test_all_failed_is_unavailable(self):
        r = self.gw([cloud(error=ProviderError("x"))]).complete("hi", source="web")
        self.assertEqual(r.status, Status.UNAVAILABLE)

    def test_daily_budget_skips_provider(self):
        c, l = cloud(reply="cloud"), local(reply="local")
        gw = self.gw([c, l], daily_limits={"gemini": 1}, clock=lambda: 1_700_000_000)
        self.assertEqual(gw.complete("q1", source="web").provider, "gemini")
        self.assertEqual(gw.complete("q2", source="web").provider, "local")

    def test_escalation_sends_only_redacted_and_restores_reply(self):
        c = cloud(reply=lambda p: "Reply to <EMAIL_1> about the plan")
        gw = self.gw([local(), c])
        text = "Please reply to ravi@example.com about the plan"
        esc = gw.prepare_escalation(text, "gmail")
        self.assertNotIn("ravi@example.com", esc.redacted)
        # without approval nothing is sent
        self.assertEqual(gw.run_escalation(esc, "gemini", Approval("wrong", c.host)).status, Status.NEEDS_APPROVAL)
        self.assertEqual(c.prompts, [])
        res = gw.run_escalation(esc, "gemini", Approval(esc.payload_sha256, c.host))
        self.assertEqual(res.status, Status.OK)
        self.assertEqual(res.text, "Reply to ravi@example.com about the plan")
        self.assertNotIn("ravi@example.com", c.prompts[0])

    def test_escalation_only_to_cloud_providers(self):
        gw = self.gw([local()])
        esc = gw.prepare_escalation("hello", "gmail")
        r = gw.run_escalation(esc, "local", Approval(esc.payload_sha256, "localhost:11434"))
        self.assertEqual(r.status, Status.DENIED)


if __name__ == "__main__":
    unittest.main()
