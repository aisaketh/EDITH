"""The EDITH process: owner-locked chat loop + scheduler, single-threaded (so SQLite needs no locking).

Security rules enforced here:
  * only the paired owner's private chat is served; everyone else is silently ignored
  * pairing needs a one-time code printed on the local terminal (/pair CODE), with attempt limits
  * /halt engages a persistent kill switch; /resume releases it
  * logs contain metadata only, never message text
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from typing import Optional

from .agent.assistant import Assistant, Reply
from .agent.factcheck import FactChecker
from .agent.mail import MailService
from .broker.egress import Approval, EgressConfig, EgressGuard
from .broker.gateway import LLMGateway, ProviderError
from .broker.killswitch import KillSwitch
from .broker.ledger import Ledger, payload_hash
from .channels.notifier import ChatNotifier
from .connectors.gmail import GmailClient
from .connectors.google_oauth import FileTokenStore, GoogleOAuth
from .connectors.places import GooglePlaces
from .connectors.search import FactCheckApi, TavilySearch
from .channels.telegram import Incoming, TelegramChannel
from .config import Config, ConfigError
from .goals.tracker import GoalStore
from .memory.facts import FactStore
from .providers.gemini import GeminiProvider
from .providers.ollama import OllamaProvider
from .scheduler.reminders import ReminderStore, Scheduler
from .store.db import connect
from .store.usage import UsageCounter
from .timeutil import get_tz

log = logging.getLogger("edith")
MAX_PAIR_ATTEMPTS = 5
ERROR_REPLY = "Something went wrong on my side. Nothing was changed."


class App:
    def __init__(self, cfg: Config, channel, assistant, scheduler: Scheduler, kill: KillSwitch):
        self.cfg, self.channel, self.assistant = cfg, channel, assistant
        self.scheduler, self.kill = scheduler, kill
        self.pair_code = secrets.token_hex(4)
        self._pair_failures = 0
        self.ignored = 0

    # --- one incoming message --------------------------------------------
    def handle_incoming(self, inc: Incoming) -> None:
        owner = self.cfg.owner_chat_id
        if owner is None:
            self._try_pair(inc)
            return
        if inc.chat_id != owner or inc.user_id != owner or inc.chat_type != "private":
            self.ignored += 1
            log.info("ignored message from non-owner (count=%d)", self.ignored)
            return
        if inc.text is None:
            self._reply(inc.chat_id, "Text messages only for now.")
            return
        cmd = inc.text.split()[0].lower().split("@")[0] if inc.text.split() else ""
        if cmd == "/halt":
            self.kill.engage()
            self._reply(inc.chat_id, "Halted. Nothing will run or be sent until you send /resume.")
            return
        if cmd == "/resume":
            self.kill.release()
            self._reply(inc.chat_id, "Resumed.")
            return
        if self.kill.engaged:
            self._reply(inc.chat_id, "I'm halted. Send /resume to continue.")
            return
        try:
            full = getattr(self.assistant, "handle_full", None)
            reply = full(inc.chat_id, inc.text) if full else Reply(self.assistant.handle(inc.chat_id, inc.text))
        except Exception:  # never leak internals or message text into the chat or the log
            log.exception("assistant failed (update %d)", inc.update_id)
            reply = Reply(ERROR_REPLY)
        if reply.text:
            self._reply(inc.chat_id, reply)

    def _reply(self, chat_id: int, reply) -> None:
        if isinstance(reply, str):
            reply = Reply(reply)
        kwargs = {}
        if reply.approved:  # the user said yes to exactly this text; bind the approval to its hash
            kwargs["approval"] = Approval(payload_hash(reply.text), getattr(self.channel, "host", ""))
        try:
            if not self.channel.send(reply.text, source=reply.source, chat_id=chat_id, **kwargs):
                self.channel.send("(Reply withheld: it contained sensitive content.)",
                                  source="system", chat_id=chat_id)
        except ProviderError:
            log.warning("could not deliver reply")

    def _try_pair(self, inc: Incoming) -> None:
        if self._pair_failures >= MAX_PAIR_ATTEMPTS or inc.chat_type != "private" or not inc.text:
            return
        parts = inc.text.split()
        if len(parts) != 2 or parts[0].lower().split("@")[0] != "/pair":
            return
        # Compare bytes: compare_digest raises TypeError on non-ASCII str, which a stranger could trigger.
        if hmac.compare_digest(parts[1].lower().encode("utf-8"), self.pair_code.encode("ascii")):
            self.cfg.save_owner(inc.chat_id)
            log.info("paired owner")
            self._reply(inc.chat_id, "Paired. Send /help to see what I can do.")
        else:
            self._pair_failures += 1
            log.warning("wrong pairing code (%d/%d)", self._pair_failures, MAX_PAIR_ATTEMPTS)

    # --- main loop ----------------------------------------------------------
    def run(self, stop: threading.Event, retry_wait: float = 5.0) -> None:
        offset: Optional[int] = None
        while not stop.is_set():
            try:
                updates = self.channel.poll(offset)
            except ProviderError as e:
                log.warning("poll failed: %s", e)
                updates = []
                stop.wait(retry_wait)
            for inc in updates:
                offset = max(offset or 0, inc.update_id + 1)
                try:
                    self.handle_incoming(inc)
                except Exception:  # one bad message must never take the whole process down
                    log.exception("failed to handle update %d", inc.update_id)
            if not self.kill.engaged and self.cfg.owner_chat_id is not None:
                try:
                    self.scheduler.tick()
                except Exception:
                    log.exception("scheduler tick failed")


def build_app(cfg: Config) -> App:
    if not cfg.telegram_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set (create a bot with @BotFather)")
    kill = KillSwitch(cfg.halt_path)
    ledger = Ledger(cfg.ledger_path)
    guard = EgressGuard(ledger, EgressConfig(allow_s1_to_free_cloud=cfg.allow_s1_cloud),
                        halted=lambda: kill.engaged)

    providers = []
    if cfg.local_model:
        providers.append(OllamaProvider(cfg.local_model))
    if cfg.gemini_model:
        try:
            providers.append(GeminiProvider(cfg.gemini_model))
        except ValueError as e:
            raise ConfigError(str(e)) from None
    conn = connect(cfg.db_path)
    usage = UsageCounter(conn)
    gateway = LLMGateway(guard, providers, daily_limits={"gemini": cfg.gemini_daily}, counter=usage)

    for path in (cfg.db_path, cfg.ledger_path):
        if os.path.exists(path):
            os.chmod(path, 0o600)
    tz = get_tz(cfg.tz)
    store = ReminderStore(conn, tz)
    facts, goals = FactStore(conn), GoalStore(conn, tz)
    try:
        channel = TelegramChannel(cfg.telegram_token, guard)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    app_holder = {}
    notifier = ChatNotifier(channel, lambda: app_holder["app"].cfg.owner_chat_id)
    mail = None
    if cfg.google_client_id and cfg.google_client_secret:
        oauth = GoogleOAuth(cfg.google_client_id, cfg.google_client_secret,
                            FileTokenStore(cfg.token_path), guard)
        mail = MailService(GmailClient(oauth.access_token, guard), gateway, tz)
    search = TavilySearch(cfg.tavily_api_key, guard, usage, cfg.search_monthly) if cfg.tavily_api_key else None
    fact_api = (FactCheckApi(cfg.google_api_key, guard, usage, cfg.factcheck_daily)
                if cfg.google_api_key else None)
    factcheck = FactChecker(gateway, search, fact_api) if (search or fact_api) else None
    places = (GooglePlaces(cfg.google_api_key, guard, usage, cfg.places_monthly)
              if cfg.google_api_key else None)
    assistant = Assistant(store, gateway, facts, goals, mail, search, factcheck, places)
    app = App(cfg, channel, assistant, Scheduler(store, notifier), kill)
    app_holder["app"] = app
    return app
