"""Chat-facing assistant for Phase 1: reminders, deadlines, confirmations, commands.

Flow for natural language:
    message -> LLM (through the gateway/egress guard) proposes JSON -> code validates ->
    echo interpretation -> user says yes/no -> only then is anything written.
If the privacy policy needs approval to use a cloud model, the user is asked first and nothing is sent.
Explicit /commands never touch a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Optional, Union

from ..broker.egress import Approval
from ..broker.gateway import ProviderError, Status
from ..connectors.places import format_places, is_vegetarian, rank_places
from ..connectors.search import BudgetExhausted, QueryBlocked, format_results
from . import factcheck as fc
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
    "/deadline YYYY-MM-DD HH:MM title - exact deadline (no AI)\n"
    "/show N - read a reminder\n"
    "Email: /mail [gmail search]  /mailadd N   (read-only; summarised on this machine)\n"
    "Web: /search <query>   /factcheck <claim or headline>   /food <what and where>\n"
    "Memory: /remember <fact>  /facts  /forget N  /confirm N\n"
    "Goals: /goal <title> by YYYY-MM-DD  /goals  /task [goal N] <title>  /tasks  /task done N  /closegoal N\n"
    "/review - progress summary     /review weekly - schedule it every Sunday 6 PM"
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


@dataclass
class _DigestReady:
    digest: object   # agent.mail.Digest, already triaged on the local model
    expires: datetime


@dataclass(frozen=True)
class Reply:
    """What to send back. `source` tells the egress guard where the content came from (e.g. 'gmail');
    `approved` means the user said yes to sending exactly this text despite its sensitivity."""
    text: str
    source: str = "user_chat"
    approved: bool = False


_Pending = Union[_Confirm, _ApprovalNeeded, _DigestReady]


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
    def __init__(self, store: ReminderStore, llm, facts=None, goals=None, mail=None,
                 search=None, factcheck=None, places=None):
        """`llm` needs complete(prompt, source=..., approval=..., only=...) -> GatewayResult (the gateway).
        `facts`, `goals`, `mail`, `search`, `factcheck`, `places` are optional services; their commands
        say so when absent."""
        self.store, self.llm = store, llm
        self.facts, self.goals, self.mail = facts, goals, mail
        self.search, self.factcheck, self.places = search, factcheck, places
        self._pending: Dict[int, _Pending] = {}
        self._last_deadlines: list = []

    # --- helpers ----------------------------------------------------------
    def _now_local(self) -> datetime:
        return self.store.clock().astimezone(self.store.tz)

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.store.tz)

    # --- entry point ------------------------------------------------------
    def handle(self, chat_id: int, text: str) -> str:
        return self.handle_full(chat_id, text).text

    def handle_full(self, chat_id: int, text: str) -> "Reply":
        """Like handle(), but also says where the text came from and whether the user approved sending it."""
        result = self._dispatch(chat_id, text)
        return result if isinstance(result, Reply) else Reply(result)

    def _dispatch(self, chat_id: int, text: str) -> Union[str, "Reply"]:
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
            return self._command(text, chat_id)
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

    def _accept(self, chat_id: int, pending: _Pending) -> Union[str, "Reply"]:
        if isinstance(pending, _DigestReady):
            self._last_deadlines = pending.digest.deadlines
            footer = "\n\nAdd a suggested deadline with /mailadd N." if pending.digest.deadlines else ""
            return Reply(pending.digest.text + footer, source="gmail", approved=True)
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
        if isinstance(pending, _DigestReady):
            return "Okay, I won't show it here. Nothing was sent anywhere."
        if isinstance(pending, _Confirm):
            return rp.msg(pending.proposal.language, "dropped")
        return "Okay, I won't send it. You can use /remind YYYY-MM-DD HH:MM text instead."

    # --- commands (no model involved) ------------------------------------
    def _command(self, text: str, chat_id: int = 0) -> str:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/list":
            return self._list()
        if cmd == "/show" and len(parts) == 2 and parts[1].isdigit():
            r = self.store.get(int(parts[1]))
            if r is None:
                return f"No reminder #{parts[1]}."
            return f"#{r.id} {rp.format_when(self._local(r.due))} - {r.text}"
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
        if cmd in ("/mail", "/mailadd"):
            return self._mail_command(chat_id, cmd, parts, text)
        if cmd in ("/search", "/factcheck", "/food"):
            return self._web_command(cmd, text)
        extra = self._memory_and_goals(cmd, parts, text)
        return extra if extra is not None else HELP

    # --- web: search, fact-check, places (only the typed query leaves the machine) -----
    def _web_command(self, cmd: str, text: str) -> str:
        pieces = text.split(None, 1)
        rest = pieces[1].strip() if len(pieces) > 1 else ""
        try:
            if cmd == "/search":
                if self.search is None:
                    return "Search isn't set up. Set TAVILY_API_KEY (free tier at tavily.com)."
                return format_results(self.search.search(rest)) if rest else "Usage: /search <query>"
            if cmd == "/factcheck":
                if self.factcheck is None:
                    return fc.error_text("not_configured")
                return fc.format_report(self.factcheck.check(rest)) if rest else \
                    "Usage: /factcheck <the claim or headline>"
            if self.places is None:
                return "Places isn't set up. Set GOOGLE_API_KEY with the Places API (New) enabled."
            if not rest:
                return "Usage: /food <what and where>, e.g. /food biryani near Banjara Hills"
            facts = [f.text for f in self.facts.list("confirmed")] if self.facts else []
            body = format_places(rank_places(self.places.search(rest), facts), is_vegetarian(facts))
            return body + ("\n\nRanked using your saved preferences, on this machine only." if facts else "")
        except QueryBlocked as e:
            return fc.error_text(f"query_blocked:{e}")
        except BudgetExhausted:
            return fc.error_text("budget_exhausted")
        except ValueError:
            return "That query isn't usable. Try different words."
        except ProviderError:
            return "That service didn't respond. Try again later."

    # --- email (read-only; digest is built on the local model) -------------
    def _mail_command(self, chat_id: int, cmd: str, parts, text: str) -> str:
        if self.mail is None:
            return ("Email is not set up. Create a Google OAuth client, set GOOGLE_CLIENT_ID and "
                    "GOOGLE_CLIENT_SECRET, then run: python3 -m edith gmail-login")
        if cmd == "/mailadd":
            if len(parts) != 2 or not parts[1].isdigit():
                return "Usage: /mailadd N   (after /mail shows a suggested deadline)"
            n = int(parts[1])
            if not 1 <= n <= len(self._last_deadlines):
                return "No such suggestion. Run /mail first."
            d = self._last_deadlines[n - 1]
            try:
                did, ids = self.store.add_deadline(d.title, d.when)
            except ValueError as e:
                return rp.msg("en", "past") if str(e) == "deadline_in_past" else "I couldn't save that."
            return f"Deadline saved (#{did}) with {len(ids)} reminders."
        query = text.split(None, 1)[1].strip() if len(parts) > 1 else "in:inbox newer_than:2d"
        digest = self.mail.digest(query)
        if digest.error == "no_local_model":
            return ("Email is only read by a local model on your machine, and none is available. "
                    "Start Ollama and set EDITH_LOCAL_MODEL.")
        if digest.error:
            return ("I couldn't read Gmail. If this keeps happening, run: python3 -m edith gmail-login")
        if digest.count == 0:
            return digest.text
        self._pending[chat_id] = _DigestReady(digest, self.store.clock() + CONFIRM_TTL)
        return (f"I read {digest.count} emails on this machine. The digest contains email content, and "
                "Telegram chats are not end-to-end encrypted, so Telegram could read it. "
                "Show it here? (yes / no)")

    # --- memory and goals (all deterministic, no model) -------------------
    def _end_of_day(self, date_str: str) -> datetime:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise ValueError("bad_date") from None
        return d.replace(hour=23, minute=59, tzinfo=self.store.tz)

    def _title_and_date(self, rest: str):
        m = re.match(r"^(.*?)\s+by\s+(\d{4}-\d{2}-\d{2})$", rest, re.I)
        if not m:
            return rest, None
        return m.group(1), self._end_of_day(m.group(2))  # ValueError on an impossible date

    def _memory_and_goals(self, cmd: str, parts, text: str) -> Optional[str]:
        rest = text.split(None, 1)[1].strip() if len(parts) > 1 else ""
        if cmd in ("/remember", "/facts", "/forget", "/confirm"):
            if self.facts is None:
                return "Memory is not enabled."
            if cmd == "/remember":
                try:
                    return f"Remembered (#{self.facts.add(rest, 'user_command')})."
                except ValueError as e:
                    return rp.msg("en", "secret") if str(e) == "secret_in_text" else \
                        "Usage: /remember <short fact> (up to 300 characters)"
            if cmd == "/facts":
                lines = [f"#{f.id} {f.text}" for f in self.facts.list("confirmed")]
                lines += [f"(unconfirmed) #{f.id} {f.text} - /confirm {f.id} or /forget {f.id}"
                          for f in self.facts.list("candidate")]
                return "\n".join(lines) or "I don't know anything yet. Tell me with /remember <fact>."
            if len(parts) == 2 and parts[1].isdigit():
                ok = self.facts.forget(int(parts[1])) if cmd == "/forget" else self.facts.confirm(int(parts[1]))
                if ok:
                    return "Forgotten." if cmd == "/forget" else "Confirmed."
                return f"No such fact #{parts[1]}."
            return f"Usage: {cmd} N"

        if cmd in ("/goal", "/goals", "/task", "/tasks", "/closegoal", "/review"):
            if self.goals is None:
                return "Goal tracking is not enabled."
            try:
                return self._goals_command(cmd, parts, rest)
            except ValueError as e:
                code = str(e)
                if code == "secret_in_text":
                    return rp.msg("en", "secret")
                if code in ("target_in_past",):
                    return rp.msg("en", "past")
                if code == "no_such_goal":
                    return "No such active goal."
                return f"I couldn't do that ({code}). Dates look like 2026-12-31."
        return None

    def _goals_command(self, cmd: str, parts, rest: str) -> str:
        g = self.goals
        if cmd == "/goal":
            if not rest:
                return "Usage: /goal <title> [by YYYY-MM-DD]"
            title, target = self._title_and_date(rest)
            return f"Goal G{g.add_goal(title, target)} saved. Add steps with /task goal N <title>."
        if cmd == "/goals":
            active = g.list_goals()
            if not active:
                return "No active goals. Add one with /goal <title> by YYYY-MM-DD"
            out = []
            for goal in active:
                p = g.progress(goal)
                out.append(f"G{goal.id} {goal.title}: {p.done}/{p.total} ({p.pct}%) - {p.status.replace('_', ' ')}")
            return "\n".join(out)
        if cmd == "/tasks":
            tasks = g.list_tasks()
            if not tasks:
                return "No open tasks."
            return "\n".join(
                f"T{t.id}{f' [G{t.goal_id}]' if t.goal_id else ''} {t.title}"
                + (f" (due {rp.format_when(self._local(t.due))})" if t.due else "") for t in tasks)
        if cmd == "/task":
            m = re.match(r"^done\s+T?(\d+)$", rest, re.I)
            if m:
                return "Task done." if g.complete_task(int(m.group(1))) else f"No open task #{m.group(1)}."
            m = re.match(r"^goal\s+G?(\d+)\s+(.+)$", rest, re.I)
            goal_id, body = (int(m.group(1)), m.group(2)) if m else (None, rest)
            if not body:
                return "Usage: /task [goal N] <title> [by YYYY-MM-DD]   or   /task done N"
            title, due = self._title_and_date(body)
            return f"Task T{g.add_task(title, goal_id, due)} saved."
        if cmd == "/closegoal":
            m = re.match(r"^G?(\d+)(\s+abandon)?$", rest, re.I)
            if not m:
                return "Usage: /closegoal N [abandon]"
            ok = g.close_goal(int(m.group(1)), "abandoned" if m.group(2) else "done")
            return "Goal closed." if ok else f"No active goal #{m.group(1)}."
        # /review
        if rest.lower() == "weekly":
            now = self._now_local()
            first = datetime.combine(now.date() + timedelta(days=(6 - now.weekday()) % 7), time(18, 0),
                                     tzinfo=self.store.tz)
            if first <= now:
                first += timedelta(days=7)
            rid = self.store.add_reminder("Weekly review time - send /review", first,
                                          {"freq": "weekly", "weekdays": [6]}, origin="review")
            return f"Weekly review scheduled for Sundays 6 PM (#{rid}). Cancel with /cancel {rid}."
        return g.review_text()

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
