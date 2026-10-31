import os
import sqlite3
import tempfile
import unittest

from edith.broker.egress import Approval, EgressConfig, EgressGuard, EgressRequest, Verdict
from edith.broker.ledger import Ledger, payload_hash
from edith.broker.policy import ActionProposal, Origin, PolicyConfig, PolicyEngine, RiskTier
from edith.privacy.classifier import classify, luhn_ok
from edith.privacy.redactor import redact, restore
from edith.privacy.sensitivity import Destination, Sensitivity

# Canary secrets: these exact strings must never be allowed out or stored in the ledger.
CANARIES = [
    "Your OTP is 482913, do not share it",
    "PAN ABCDE1234F belongs to me",
    "card 4111 1111 1111 1111 exp 12/29",
    "Aadhaar 2345 6789 0123",
    "my password: hunter2xyz",
    "a/c no 123456789012 IFSC HDFC0001234",
    "key sk-abcdefghijklmnopqrstuvwx123456",
]
CANARY_VALUES = ["482913", "ABCDE1234F", "4111 1111 1111 1111", "2345 6789 0123",
                 "hunter2xyz", "123456789012", "sk-abcdefghijklmnopqrstuvwx123456"]


class ClassifierTests(unittest.TestCase):
    def test_every_canary_is_s3(self):
        for text in CANARIES:
            self.assertEqual(classify(text, "user_chat").level, Sensitivity.S3_SECRET, text)

    def test_luhn(self):
        self.assertTrue(luhn_ok("4111111111111111"))
        self.assertFalse(luhn_ok("4111111111111112"))

    def test_non_luhn_16_digits_is_not_a_card(self):
        kinds = [f.kind for f in classify("ref 4111 1111 1111 1112", "web").findings]
        self.assertNotIn("card", kinds)

    def test_postal_pin_is_not_otp(self):
        self.assertEqual(classify("My pin code is 560001", "web").level, Sensitivity.S0_PUBLIC)

    def test_source_defaults(self):
        self.assertEqual(classify("hello", "web").level, Sensitivity.S0_PUBLIC)
        self.assertEqual(classify("hello", "reminder").level, Sensitivity.S1_PERSONAL)
        self.assertEqual(classify("hello", "gmail").level, Sensitivity.S2_SENSITIVE)
        self.assertEqual(classify("hello", None).level, Sensitivity.S2_SENSITIVE)
        self.assertEqual(classify("hello", "some_new_source").level, Sensitivity.S2_SENSITIVE)

    def test_pii_is_s2(self):
        self.assertEqual(classify("mail me at ravi@example.com", "web").level, Sensitivity.S2_SENSITIVE)
        self.assertEqual(classify("call +91 98765 43210", "web").level, Sensitivity.S2_SENSITIVE)


class RedactorTests(unittest.TestCase):
    def test_roundtrip_and_consistency(self):
        text = "Mail ravi@example.com and again ravi@example.com or call 9876543210"
        red, mapping = redact(text)
        self.assertNotIn("ravi@example.com", red)
        self.assertEqual(red.count("<EMAIL_1>"), 2)
        self.assertEqual(restore(red, mapping), text)

    def test_s3_is_destroyed_not_mapped(self):
        red, mapping = redact("Your OTP is 482913")
        self.assertNotIn("482913", red)
        self.assertNotIn("482913", " ".join(mapping.values()))
        self.assertIn("[REDACTED:OTP]", red)

    def test_known_names(self):
        red, mapping = redact("Lunch with Priya Sharma tomorrow", known_names=["Priya Sharma"])
        self.assertNotIn("Priya", red)
        self.assertEqual(restore(red, mapping), "Lunch with Priya Sharma tomorrow")

    def test_card_beats_overlapping_aadhaar_pattern(self):
        red, _ = redact("card 4111 1111 1111 1111 end")
        self.assertEqual(red, "card [REDACTED:CARD] end")


class EgressTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.guard = EgressGuard(self.ledger)

    def req(self, text, dest=Destination.CLOUD_FREE, host="generativelanguage.googleapis.com", source="web"):
        return EgressRequest(host=host, dest=dest, text=text, source=source)

    def test_canaries_never_allowed_anywhere(self):
        targets = [("localhost:11434", Destination.LOCAL),
                   ("generativelanguage.googleapis.com", Destination.CLOUD_FREE),
                   ("api.telegram.org", Destination.CHAT)]
        for text in CANARIES:
            for host, dest in targets:
                d = self.guard.check(EgressRequest(host, dest, text, "user_chat"))
                self.assertEqual(d.verdict, Verdict.DENY, (text, host))
                self.assertEqual(d.reason, "secret_in_payload")

    def test_canaries_never_in_ledger_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            guard = EgressGuard(Ledger(path))
            for text in CANARIES:
                guard.check(EgressRequest("api.telegram.org", Destination.CHAT, text, "user_chat"))
            guard.ledger.conn.close()
            with open(path, "rb") as fh:
                raw = fh.read().decode("latin-1")
            for value in CANARY_VALUES:
                self.assertNotIn(value, raw)

    def test_public_to_free_cloud_allowed(self):
        self.assertEqual(self.guard.check(self.req("best biryani places in Hyderabad")).verdict, Verdict.ALLOW)

    def test_email_to_free_cloud_needs_exact_approval(self):
        text = "Summarise: meeting moved to 4pm"
        r = self.req(text, source="gmail")
        self.assertEqual(self.guard.check(r).verdict, Verdict.ASK)
        good = Approval(payload_hash(text), r.host)
        self.assertEqual(self.guard.check(r, good).verdict, Verdict.ALLOW)
        wrong_payload = Approval(payload_hash(text + " extra"), r.host)
        self.assertEqual(self.guard.check(r, wrong_payload).verdict, Verdict.ASK)
        wrong_host = Approval(payload_hash(text), "other.example.com")
        self.assertEqual(self.guard.check(r, wrong_host).verdict, Verdict.ASK)

    def test_approval_cannot_override_secret(self):
        text = "my password: hunter2xyz"
        r = self.req(text, source="gmail")
        self.assertEqual(self.guard.check(r, Approval(payload_hash(text), r.host)).verdict, Verdict.DENY)

    def test_s1_to_free_cloud_depends_on_config(self):
        r = self.req("remind me to pay rent", source="reminder")
        self.assertEqual(self.guard.check(r).verdict, Verdict.ASK)
        relaxed = EgressGuard(Ledger(), EgressConfig(allow_s1_to_free_cloud=True))
        self.assertEqual(relaxed.check(r).verdict, Verdict.ALLOW)

    def test_email_to_local_allowed(self):
        r = self.req("Summarise this email from the bank", Destination.LOCAL, "localhost:11434", "gmail")
        self.assertEqual(self.guard.check(r).verdict, Verdict.ALLOW)

    def test_unknown_host_and_class_mismatch_denied(self):
        self.assertEqual(self.guard.check(self.req("hi", host="evil.example.com")).reason, "host_not_allowlisted")
        spoof = self.req("hi", Destination.LOCAL, "generativelanguage.googleapis.com")
        self.assertEqual(self.guard.check(spoof).reason, "destination_class_mismatch")

    def test_declared_level_can_only_raise(self):
        r = EgressRequest("generativelanguage.googleapis.com", Destination.CLOUD_FREE, "hello", "web",
                          declared_level=Sensitivity.S2_SENSITIVE)
        self.assertEqual(self.guard.check(r).verdict, Verdict.ASK)
        r2 = EgressRequest("generativelanguage.googleapis.com", Destination.CLOUD_FREE, "hello", "gmail",
                           declared_level=Sensitivity.S0_PUBLIC)
        self.assertEqual(self.guard.check(r2).verdict, Verdict.ASK)

    def test_email_in_chat_needs_approval(self):
        r = self.req("Your bank said X", Destination.CHAT, "api.telegram.org", "gmail")
        self.assertEqual(self.guard.check(r).verdict, Verdict.ASK)

    def test_every_decision_is_ledgered(self):
        self.guard.check(self.req("hello"))
        self.guard.check(self.req("hello", host="evil.example.com"))
        self.assertEqual(len(self.ledger.entries()), 2)


class LedgerTests(unittest.TestCase):
    def test_chain_verifies(self):
        led = Ledger()
        for i in range(3):
            led.append("egress", "local:x", 0, "allow", "ok", f"payload {i}")
        self.assertTrue(led.verify())

    def test_update_and_delete_blocked(self):
        led = Ledger()
        led.append("egress", "local:x", 0, "allow", "ok", "p")
        with self.assertRaises(sqlite3.DatabaseError):
            led.conn.execute("UPDATE entries SET decision='deny'")
        with self.assertRaises(sqlite3.DatabaseError):
            led.conn.execute("DELETE FROM entries")

    def test_tampering_detected(self):
        led = Ledger()
        led.append("egress", "local:x", 0, "deny", "r", "p1")
        led.append("egress", "local:x", 0, "allow", "ok", "p2")
        led.conn.execute("DROP TRIGGER entries_no_update")  # attacker with direct DB access
        led.conn.execute("UPDATE entries SET decision='allow' WHERE id=1")
        self.assertFalse(led.verify())


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = PolicyEngine(PolicyConfig(per_action_cap_inr=1000, daily_cap_inr=1500))

    def act(self, tier, origin=Origin.USER, tool="t", amount=None):
        return self.p.decide(ActionProposal(tool, tier, origin, amount)).verdict

    def test_tiers(self):
        self.assertEqual(self.act(RiskTier.READ_OWN), Verdict.ALLOW)
        self.assertEqual(self.act(RiskTier.REVERSIBLE_WRITE), Verdict.ALLOW)
        self.assertEqual(self.act(RiskTier.COMMUNICATE), Verdict.ASK)
        self.assertEqual(self.act(RiskTier.MONEY_OR_IRREVERSIBLE, amount=300), Verdict.ASK)

    def test_untrusted_origin_cannot_drive_tier3_or_4(self):
        self.assertEqual(self.act(RiskTier.COMMUNICATE, Origin.UNTRUSTED_CONTENT), Verdict.DENY)
        self.assertEqual(self.act(RiskTier.MONEY_OR_IRREVERSIBLE, Origin.UNTRUSTED_CONTENT, amount=10), Verdict.DENY)
        self.assertEqual(self.act(RiskTier.READ_EXTERNAL, Origin.UNTRUSTED_CONTENT), Verdict.ALLOW)

    def test_standing_approval_only_tier3(self):
        p = PolicyEngine(PolicyConfig(standing_approval_tools=frozenset({"send_msg", "order"})))
        self.assertEqual(p.decide(ActionProposal("send_msg", RiskTier.COMMUNICATE, Origin.USER)).verdict, Verdict.ALLOW)
        self.assertEqual(p.decide(ActionProposal("order", RiskTier.MONEY_OR_IRREVERSIBLE, Origin.USER, 10)).verdict, Verdict.ASK)

    def test_spend_caps(self):
        self.assertEqual(self.act(RiskTier.MONEY_OR_IRREVERSIBLE, amount=1200), Verdict.DENY)
        self.p.record_spend(900)
        self.assertEqual(self.act(RiskTier.MONEY_OR_IRREVERSIBLE, amount=700), Verdict.DENY)
        self.assertEqual(self.act(RiskTier.MONEY_OR_IRREVERSIBLE, amount=500), Verdict.ASK)

    def test_kill_switch(self):
        self.p.halted = True
        self.assertEqual(self.act(RiskTier.READ_OWN), Verdict.DENY)


if __name__ == "__main__":
    unittest.main()
