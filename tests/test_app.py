import os
import stat
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest import mock

from edith.app import ERROR_REPLY, App, build_app
from edith.broker.gateway import ProviderError
from edith.broker.killswitch import KillSwitch
from edith.channels.notifier import ChatNotifier, late_note
from edith.channels.telegram import Incoming
from edith.config import Config, ConfigError
from edith.scheduler.reminders import DeliveryError, Reminder
from edith.timeutil import UTC
from tests.helpers import NOW

OWNER = 777
GOOD_TOKEN = "123456789:" + "A" * 35


class FakeChannel:
    def __init__(self, batches=(), stop=None, blocked_sources=(), fail_with=None):
        self.batches, self.stop = list(batches), stop
        self.sent, self.offsets = [], []
        self.blocked_sources, self.fail_with = set(blocked_sources), fail_with

    def poll(self, offset=None):
        self.offsets.append(offset)
        if not self.batches:
            if self.stop:
                self.stop.set()
            return []
        b = self.batches.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    def send(self, text, source="user_chat", chat_id=None):
        if self.fail_with:
            raise self.fail_with
        if source in self.blocked_sources:
            return False
        self.sent.append((chat_id, text, source))
        return True


class FakeAssistant:
    def __init__(self):
        self.calls = []

    def handle(self, chat_id, text):
        self.calls.append(text)
        if text == "boom":
            raise RuntimeError("secret internal detail 482913")
        return "echo:" + text


class FakeScheduler:
    def __init__(self, fail=False):
        self.ticks, self.fail = 0, fail

    def tick(self):
        self.ticks += 1
        if self.fail:
            raise RuntimeError("db locked")
        return 0


def inc(text, uid=1, chat=OWNER, user=OWNER, kind="private"):
    return Incoming(uid, chat, user, kind, text)


class AppBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def make(self, owner=OWNER, channel=None, scheduler=None):
        cfg = Config(data_dir=self.dir, owner_chat_id=owner)
        self.channel = channel or FakeChannel()
        self.assistant = FakeAssistant()
        self.sched = scheduler or FakeScheduler()
        self.kill = KillSwitch(os.path.join(self.dir, "HALT"))
        return App(cfg, self.channel, self.assistant, self.sched, self.kill)


class OwnerLockTests(AppBase):
    def test_owner_is_served(self):
        app = self.make()
        app.handle_incoming(inc("hi"))
        self.assertEqual(self.channel.sent, [(OWNER, "echo:hi", "user_chat")])

    def test_everyone_else_is_silently_ignored(self):
        app = self.make()
        app.handle_incoming(inc("hi", chat=999, user=999))
        app.handle_incoming(inc("hi", user=999))                 # right chat, wrong user
        app.handle_incoming(inc("hi", kind="group"))             # owner in a group
        self.assertEqual((self.channel.sent, self.assistant.calls, app.ignored), ([], [], 3))

    def test_non_text(self):
        app = self.make()
        app.handle_incoming(inc(None))
        self.assertIn("Text messages only", self.channel.sent[0][1])
        self.assertEqual(self.assistant.calls, [])

    def test_assistant_failure_is_generic(self):
        app = self.make()
        with self.assertLogs("edith", level="ERROR"):
            app.handle_incoming(inc("boom"))
        self.assertEqual(self.channel.sent[0][1], ERROR_REPLY)
        self.assertNotIn("482913", self.channel.sent[0][1])

    def test_withheld_reply_notice(self):
        app = self.make(channel=FakeChannel(blocked_sources={"user_chat"}))
        app.handle_incoming(inc("hi"))
        self.assertEqual([(s[2]) for s in self.channel.sent], ["system"])
        self.assertIn("withheld", self.channel.sent[0][1])

    def test_delivery_error_does_not_crash(self):
        app = self.make(channel=FakeChannel(fail_with=ProviderError("net")))
        with self.assertLogs("edith", level="WARNING"):
            app.handle_incoming(inc("hi"))


class KillSwitchFlowTests(AppBase):
    def test_halt_and_resume(self):
        app = self.make()
        app.handle_incoming(inc("/halt"))
        self.assertTrue(self.kill.engaged)
        app.handle_incoming(inc("hi"))
        self.assertIn("halted", self.channel.sent[-1][1])
        self.assertEqual(self.assistant.calls, [])
        app.handle_incoming(inc("/resume"))
        self.assertFalse(self.kill.engaged)
        app.handle_incoming(inc("hi"))
        self.assertEqual(self.assistant.calls, ["hi"])

    def test_non_owner_cannot_halt_or_resume(self):
        app = self.make()
        app.handle_incoming(inc("/halt", chat=5, user=5))
        self.assertFalse(self.kill.engaged)

    def test_halted_scheduler_does_not_tick(self):
        stop = threading.Event()
        app = self.make(channel=FakeChannel(stop=stop))
        self.kill.engage()
        app.run(stop, retry_wait=0)
        self.assertEqual(self.sched.ticks, 0)


class PairingTests(AppBase):
    def test_pairing_flow(self):
        app = self.make(owner=None)
        app.handle_incoming(inc("hello"))
        with self.assertLogs("edith", level="WARNING"):
            app.handle_incoming(inc("/pair wrongcode"))
            app.handle_incoming(inc("/pair é€తెలుగు"))  # non-ASCII must not crash the comparison
        app.handle_incoming(inc(f"/pair {app.pair_code}", kind="group"))
        self.assertEqual(self.channel.sent, [])  # nothing is revealed to strangers
        app.handle_incoming(inc(f"/pair {app.pair_code}"))
        self.assertEqual(app.cfg.owner_chat_id, OWNER)
        self.assertIn("Paired", self.channel.sent[0][1])
        path = app.cfg.owner_path
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(Config(data_dir=self.dir)._load_owner(), OWNER)
        app.handle_incoming(inc("hi"))
        self.assertEqual(self.assistant.calls, ["hi"])

    def test_pairing_locks_after_repeated_failures(self):
        app = self.make(owner=None)
        with self.assertLogs("edith", level="WARNING"):
            for _ in range(5):
                app.handle_incoming(inc("/pair 00000000"))
        app.handle_incoming(inc(f"/pair {app.pair_code}"))
        self.assertIsNone(app.cfg.owner_chat_id)

    def test_scheduler_idle_until_paired(self):
        stop = threading.Event()
        app = self.make(owner=None, channel=FakeChannel(stop=stop))
        app.run(stop, retry_wait=0)
        self.assertEqual(self.sched.ticks, 0)


class RunLoopTests(AppBase):
    def test_offsets_errors_and_ticks(self):
        stop = threading.Event()
        batches = [[inc("a", uid=5), inc("b", uid=7)], ProviderError("net"), [inc("c", uid=9)]]
        app = self.make(channel=FakeChannel(batches, stop))
        with self.assertLogs("edith", level="WARNING"):
            app.run(stop, retry_wait=0)
        self.assertEqual(self.channel.offsets, [None, 8, 8, 10])
        self.assertEqual([s[1] for s in self.channel.sent], ["echo:a", "echo:b", "echo:c"])
        self.assertEqual(self.sched.ticks, 4)

    def test_handler_crash_does_not_stop_loop(self):
        stop = threading.Event()
        app = self.make(channel=FakeChannel([[inc("a", uid=1), inc("b", uid=2)]], stop))
        real = app.handle_incoming
        app.handle_incoming = lambda i: (_ for _ in ()).throw(RuntimeError("x")) if i.text == "a" else real(i)
        with self.assertLogs("edith", level="ERROR"):
            app.run(stop, retry_wait=0)
        self.assertEqual(self.assistant.calls, ["b"])

    def test_scheduler_failure_does_not_stop_loop(self):
        stop = threading.Event()
        app = self.make(channel=FakeChannel([[inc("a", uid=1)]], stop), scheduler=FakeScheduler(fail=True))
        with self.assertLogs("edith", level="ERROR"):
            app.run(stop, retry_wait=0)
        self.assertEqual(self.sched.ticks, 2)
        self.assertEqual(self.assistant.calls, ["a"])


def reminder(text="pay rent", rid=7):
    return Reminder(rid, text, NOW.astimezone(UTC), None, "pending", None, 0, 15, 0)


class NotifierTests(unittest.TestCase):
    def test_normal_delivery(self):
        ch = FakeChannel()
        ChatNotifier(ch, lambda: OWNER).send(reminder(), timedelta(seconds=20))
        self.assertEqual(ch.sent, [(OWNER, "Reminder: pay rent", "reminder")])

    def test_late_notes(self):
        self.assertEqual(late_note(timedelta(minutes=2)), "")
        self.assertEqual(late_note(timedelta(minutes=30)), " (due 30 min ago)")
        self.assertEqual(late_note(timedelta(hours=3)), " (due 3 h ago)")
        self.assertEqual(late_note(timedelta(days=3)), " (due 3 days ago)")

    def test_guard_refusal_falls_back_to_content_free_nudge(self):
        ch = FakeChannel(blocked_sources={"reminder"})
        ChatNotifier(ch, lambda: OWNER).send(reminder("secret-ish"), timedelta(0))
        self.assertEqual(len(ch.sent), 1)
        self.assertIn("/show 7", ch.sent[0][1])
        self.assertNotIn("secret-ish", ch.sent[0][1])

    def test_failures_become_delivery_errors(self):
        for ch in (FakeChannel(blocked_sources={"reminder", "system"}), FakeChannel(fail_with=ProviderError("x"))):
            with self.assertRaises(DeliveryError):
                ChatNotifier(ch, lambda: OWNER).send(reminder(), timedelta(0))
        with self.assertRaises(DeliveryError):
            ChatNotifier(FakeChannel(), lambda: None).send(reminder(), timedelta(0))


class ConfigTests(unittest.TestCase):
    def test_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            cfg = Config.from_env({"EDITH_HOME": home, "TELEGRAM_BOT_TOKEN": GOOD_TOKEN,
                                   "EDITH_LOCAL_MODEL": "qwen3:8b", "EDITH_GEMINI_DAILY": "42",
                                   "EDITH_ALLOW_S1_CLOUD": "1"})
            self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
            self.assertEqual((cfg.local_model, cfg.gemini_daily, cfg.allow_s1_cloud, cfg.tz),
                             ("qwen3:8b", 42, True, "Asia/Kolkata"))
            self.assertIsNone(cfg.owner_chat_id)
            with self.assertRaises(ConfigError):
                Config.from_env({"EDITH_HOME": home, "EDITH_GEMINI_DAILY": "lots"})

    def test_corrupt_owner_file_means_unpaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(data_dir=tmp)
            for junk in ("not json", '{"chat_id": "abc"}', "[1]", '{"chat_id": true}'):
                with open(cfg.owner_path, "w") as fh:
                    fh.write(junk)
                self.assertIsNone(cfg._load_owner(), junk)

    def test_build_app_validates_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                build_app(Config(data_dir=tmp))
            with self.assertRaises(ConfigError) as ctx:
                build_app(Config(data_dir=tmp, telegram_token="bad-token"))
            self.assertNotIn("bad-token", str(ctx.exception))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ConfigError):
                    build_app(Config(data_dir=tmp, telegram_token=GOOD_TOKEN, gemini_model="some-model"))
            app = build_app(Config(data_dir=tmp, telegram_token=GOOD_TOKEN, local_model="qwen3:8b"))
            self.assertEqual([p.name for p in app.assistant.llm.providers], ["local"])
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tmp, "edith.db")).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tmp, "ledger.db")).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
