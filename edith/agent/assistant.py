"""Chat-facing assistant for Phase 1: reminders, deadlines, confirmations, commands.

Flow for natural language:
    message -> LLM (through the gateway/egress guard) proposes JSON -> code validates ->
    echo interpretation -> user says yes/no -> only then is anything written.
If the privacy policy needs approval to use a cloud model, the user is asked first and nothing is sent.
Explicit /commands never touch a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Union

from ..broker.egress import Approval
from ..broker.gateway import Status
from ..scheduler.reminders import ReminderStore
from ..timeutil import from_iso
from . import reminder_parser as rp

YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "avunu", "avnu", "sare", "అవును", "సరే"}
NO = {"no", "n", "nope", "cancel", "kadu", "kaadu", "vaddu", "కాదు", "వద్దు"}
MAX_INPUT = 1000
CONFIRM_TTL = timedelta(minutes=10)

HELP = (
    "I can set reminders and deadlines. Just tell me, in English or Telugu, e.g. "
    "\"remind me to pay rent tomorrow at 9am\".\n"
    "/list - pending reminders and deadlines\n"
    "/done N - mark done      /cancel N - cancel      /snooze N 30m - snooze (m or h)\n"
    "/remind YYYY-MM-DD HH:MM text - exact reminder (no AI)\n"
    "/deadline YYYY-MM-DD HH:MM title - exact deadline (no AI)"
)


@dataclass
class _Confirm:
    proposal: rp.Proposal
    expires: datetime


@dataclass
class _ApprovalNeeded:
    user_text: str
    prompt: str
    approval: Approval
    provider: str
    expires: datetime


_Pending = Union[_Confirm, _ApprovalNeeded]


def _norm(text: str) -> str:
    return text.strip().lower().strip(".!?,")


def _parse_minutes(token: str) -> Optional[int]:
    token = token.lower().strip()
    try:
        if token.endswith("h"):
            return int(token[:-1]) * 60
        if token.endswith("m"):
            return int(token[:-1])
        return int(token)
    except ValueError:
        return None


class Assistant:
    def __init__(self, store: ReminderStore, llm):
        """`llm` needs complete(prompt, source=..., approval=..., only=...) -> GatewayResult (the gateway)."""
        self.store, self.llm = store, llm
        self._pending: Dict[int, _Pending] = {}

    # --- helpers ----------------------------------------------------------
    def _now_local(self) -> datetime:
        return self.store.clock().astimezone(self.store.tz)

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.store.tz)

    # --- entry point ------------------------------------------------------
    def handle(self, chat_id: int, text: str) -> str:
        text = (text or "").strip()[:MAX_INPUT]
        if not text:
            return ""
        pending = self._pending.pop(chat_id, None)
        if pending is not None and pending.expires > self.store.clock():
            word = _norm(text)
            if word in YES:
                return self._accept(chat_id, pending)
            if word in NO:
                return self._reject(pending)
            # any other message abandons the pending question and is handled as new input
        if text.startswith("/"):
            return self._command(text)
        return self._interpret(chat_id, text)

    # --- natural language -------------------------------------------------
    def _interpret(self, chat_id: int, user_text: str, approval: Optional[Approval] = None,
                   only=None, approved_prompt: Optional[str] = None) -> str:
        now = self._now_local()
        # An approval is bound to the exact bytes the user agreed to send, so reuse that prompt verbatim.
        base = approved_prompt or rp.build_prompt(user_text, now)
        prompt = base
        for attempt in range(2):
            res = self.llm.complete(prompt, source="user_chat",
                                    approval=approval if attempt == 0 else None,
                                    only=only if attempt == 0 else None)
            if res.status == Status.NEEDS_APPROVAL:
                self._pending[chat_id] = _ApprovalNeeded(
                    user_text, prompt, Approval(res.payload_sha256, res.approval_host), res.provider,
                    self.store.clock() + CONFIRM_TTL)
                return (f"To understand this I need to send your message to {res.approval_host}. "
                        f"Send it? (yes / no)\n> {user_text[:200]}")
            if res.status == Status.DENIED:
                if res.reason == "secret_in_payload":
                    return rp.msg("en", "secret")
                return "I can't process that safely."
            if res.status == Status.UNAVAILABLE:
                return ("I can't reach a model right now. You can still use "
                        "/remind YYYY-MM-DD HH:MM text")
            out = rp.parse_response(res.text, now, self.store.tz)
            if out.error:
                prompt = (base + f"\n\nYour previous reply was invalid ({out.error}). "
                                 "Reply with ONLY the JSON object.")
                continue
            if out.clarify:
                return out.clarify
            self._pending[chat_id] = _Confirm(out.proposal, self.store.clock() + CONFIRM_TTL)
            return rp.format_confirmation(out.proposal)
        return ("I couldn't understand that reliably. Please rephrase, or use "
                "/remind YYYY-MM-DD HH:MM text")

    def _accept(self, chat_id: int, pending: _Pending) -> str:
        if isinstance(pending, _ApprovalNeeded):
            return self._interpret(chat_id, pending.user_text, approval=pending.approval,
                                   only=[pending.provider], approved_prompt=pending.prompt)
        p = pending.proposal
        try:
            if p.kind == "deadline":
                did, ids = self.store.add_deadline(p.text, p.when)
                return rp.msg(p.language, "saved_deadline", id=did, n=len(ids))
            rid = self.store.add_reminder(p.text, p.when, p.recurrence)
            return rp.msg(p.language, "saved", id=rid)
        except ValueError as e:
            if str(e) in ("deadline_in_past",):
                return rp.msg(p.language, "past")
            return rp.msg(p.language, "secret") if str(e) == "secret_in_text" else "I couldn't save that."

    def _reject(self, pending: _Pending) -> str:
        if isinstance(pending, _Confirm):
            return rp.msg(pending.proposal.language, "dropped")
        return "Okay, I won't send it. You can use /remind YYYY-MM-DD HH:MM text instead."

    # --- commands (no model involved) ------------------------------------
    def _command(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/list":
            return self._list()
        if cmd in ("/done", "/cancel") and len(parts) == 2 and parts[1].isdigit():
            rid = int(parts[1])
            ok = self.store.done(rid) if cmd == "/done" else self.store.cancel(rid)
            return ("Done." if cmd == "/done" else "Cancelled.") if ok else f"No active reminder #{rid}."
        if cmd == "/snooze" and len(parts) == 3 and parts[1].isdigit():
            minutes = _parse_minutes(parts[2])
            if minutes is None or not 1 <= minutes <= 1440:
                return "Usage: /snooze N 30m (1 minute to 24 hours)"
            new_id = self.store.snooze(int(parts[1]), minutes)
            return f"Snoozed #{new_id} for {minutes} min." if new_id else f"No active reminder #{parts[1]}."
        if cmd in ("/remind", "/deadline"):
            return self._explicit(cmd, text)
        return HELP

    def _explicit(self, cmd: str, text: str) -> str:
        parts = text.split(None, 3)
        usage = f"Usage: {cmd} YYYY-MM-DD HH:MM text"
        if len(parts) < 4:
            return usage
        try:
            naive = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M")
        except ValueError:
            return usage
        when = naive.replace(tzinfo=self.store.tz)
        if when <= self.store.clock() + rp.MIN_LEAD:
            return rp.msg("en", "past")
        try:
            if cmd == "/remind":
                return f"Saved (#{self.store.add_reminder(parts[3], when)}) for {rp.format_when(when)}."
            did, ids = self.store.add_deadline(parts[3], when)
            return f"Deadline saved (#{did}) with {len(ids)} reminders."
        except ValueError as e:
            return rp.msg("en", "secret") if str(e) == "secret_in_text" else "I couldn't save that."

    def _list(self) -> str:
        lines = []
        for r in self.store.list_pending()[:20]:
            rec = f" [{r.recurrence['freq']}]" if r.recurrence else ""
            lines.append(f"#{r.id} {rp.format_when(self._local(r.due))} - {r.text}{rec}")
        deadlines = self.store.list_deadlines()
        if deadlines:
            lines.append("Deadlines:")
            for d in deadlines[:10]:
                lines.append(f"D{d['id']} {rp.format_when(self._local(from_iso(d['due_utc'])))} - {d['title']}")
        return "\n".join(lines) if lines else "Nothing pending."
