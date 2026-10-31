import unittest

from edith.broker.egress import EgressConfig, EgressGuard
from edith.broker.gateway import ProviderError
from edith.broker.killswitch import KillSwitch
from edith.broker.ledger import Ledger
from edith.channels.telegram import Incoming, TelegramChannel, split_message
from edith.privacy.sensitivity import Destination, Sensitivity
from tests.test_providers import ok_json, serve

TOKEN = "123456789:" + "A" * 35
OWNER = 777


def make_channel(url, halted=None):
    host = url.split("//", 1)[1]
    guard = EgressGuard(Ledger(), EgressConfig(allowlist={host: Destination.CHAT}), halted=halted)
    return TelegramChannel(TOKEN, guard, base_url=url, poll_timeout=1), guard


def tg(result):
    return {"ok": True, "result": result}


class TelegramChannelTests(unittest.TestCase):
    def test_token_validation_never_echoes_token(self):
        guard = EgressGuard(Ledger())
        for bad in ("", "nope", "123:short", None):
            with self.assertRaises(ValueError) as ctx:
                TelegramChannel(bad, guard)
            self.assertNotIn("short", str(ctx.exception))
        with self.assertRaises(ValueError):
            TelegramChannel(TOKEN, guard, base_url="http://api.telegram.org")
        self.assertNotIn(TOKEN, repr(TelegramChannel(TOKEN, guard)))

    def test_poll_parses_updates_and_skips_malformed(self):
        updates = [
            {"update_id": 10, "message": {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "text": "hi"}},
            {"update_id": 11, "message": {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}},
            {"update_id": 12, "message": {"from": {"id": 1}}},
            {"update_id": True, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}},
            "garbage",
        ]
        with serve(lambda *a: ok_json(tg(updates))) as (url, seen):
            ch, _ = make_channel(url)
            got = ch.poll(offset=10)
        self.assertEqual(got, [Incoming(10, OWNER, OWNER, "private", "hi"),
                               Incoming(11, OWNER, OWNER, "private", None)])
        path, _, body = seen[0]
        self.assertEqual(path, f"/bot{TOKEN}/getUpdates")
        self.assertEqual((body["offset"], body["allowed_updates"]), (10, ["message"]))

    def test_api_not_ok_is_provider_error(self):
        with serve(lambda *a: ok_json({"ok": False, "description": "nope"})) as (url, _):
            ch, _ = make_channel(url)
            with self.assertRaises(ProviderError):
                ch.poll()

    def test_send_goes_through_guard_and_ledger(self):
        with serve(lambda *a: ok_json(tg({}))) as (url, seen):
            ch, guard = make_channel(url)
            self.assertTrue(ch.send("Reminder: pay rent", source="reminder", chat_id=OWNER))
        path, _, body = seen[0]
        self.assertEqual(path, f"/bot{TOKEN}/sendMessage")
        self.assertEqual((body["chat_id"], body["text"]), (OWNER, "Reminder: pay rent"))
        self.assertEqual(len(guard.ledger.entries()), 1)
        self.assertNotIn(TOKEN, guard.ledger.entries()[0].preview)

    def test_secrets_are_never_sent(self):
        with serve(lambda *a: ok_json(tg({}))) as (url, seen):
            ch, _ = make_channel(url)
            self.assertFalse(ch.send("Your OTP is 482913", source="user_chat", chat_id=OWNER))
        self.assertEqual(seen, [])

    def test_email_derived_content_blocked_but_user_typed_pii_allowed(self):
        with serve(lambda *a: ok_json(tg({}))) as (url, seen):
            ch, _ = make_channel(url)
            self.assertFalse(ch.send("From the bank: call 9876543210", source="gmail", chat_id=OWNER))
            self.assertTrue(ch.send("Remind: call 9876543210", source="reminder", chat_id=OWNER))
        self.assertEqual(len(seen), 1)

    def test_long_messages_are_split(self):
        text = "line of text\n" * 1000
        chunks = split_message(text)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))
        self.assertEqual(split_message("short"), ["short"])
        with serve(lambda *a: ok_json(tg({}))) as (url, seen):
            ch, _ = make_channel(url)
            ch.send(text, chat_id=OWNER)
        self.assertEqual(len(seen), len(chunks))

    def test_chat_id_required(self):
        ch, _ = make_channel("http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            ch.send("hi")


class GuardExtrasTests(unittest.TestCase):
    def guard(self, **kw):
        cfg = EgressConfig(**kw.pop("cfg", {}))
        return EgressGuard(Ledger(), cfg, **kw)

    def check(self, g, host, dest, text, source="user_chat", declared=None):
        from edith.broker.egress import EgressRequest
        return g.check(EgressRequest(host, dest, text, source, declared)).verdict.value

    def test_kill_switch_denies_everything_except_owner_chat(self):
        g = self.guard(halted=lambda: True)
        self.assertEqual(self.check(g, "localhost:11434", Destination.LOCAL, "hi", "web"), "deny")
        self.assertEqual(self.check(g, "generativelanguage.googleapis.com", Destination.CLOUD_FREE, "hi", "web"), "deny")
        self.assertEqual(self.check(g, "api.telegram.org", Destination.CHAT, "Halted.", "system"), "allow")

    def test_chat_pii_rules(self):
        g = self.guard()
        tel = "api.telegram.org"
        self.assertEqual(self.check(g, tel, Destination.CHAT, "call 9876543210", "reminder"), "allow")
        self.assertEqual(self.check(g, tel, Destination.CHAT, "call 9876543210", "gmail"), "ask")
        self.assertEqual(self.check(g, tel, Destination.CHAT, "hello", "user_chat", Sensitivity.S2_SENSITIVE), "ask")
        self.assertEqual(self.check(g, tel, Destination.CHAT, "static help text", "system"), "allow")
        strict = self.guard(cfg={"chat_allows_user_origin_pii": False})
        self.assertEqual(self.check(strict, tel, Destination.CHAT, "call 9876543210", "reminder"), "ask")


class KillSwitchTests(unittest.TestCase):
    def test_engage_release_persist(self):
        import os
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "HALT")
            ks = KillSwitch(path)
            self.assertFalse(ks.engaged)
            ks.engage()
            self.assertTrue(KillSwitch(path).engaged)  # a fresh object (a restart) still sees it
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            ks.release()
            ks.release()  # idempotent
            self.assertFalse(ks.engaged)


if __name__ == "__main__":
    unittest.main()
