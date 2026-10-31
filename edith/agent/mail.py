"""Email digest: fetch (read-only), pre-filter secrets, triage on the LOCAL model, format.

Email is S2, so the gateway only lets a local model see it. If no local model is available the whole
digest is refused (error="no_local_model") instead of silently leaking to a cloud model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Callable, List, Optional

from ..broker.gateway import ProviderError, Status
from ..timeutil import utcnow
from . import email_triage as et


@dataclass(frozen=True)
class DeadlineProposal:
    title: str
    when: datetime
    message_id: str


@dataclass
class Digest:
    text: str = ""
    deadlines: List[DeadlineProposal] = field(default_factory=list)
    count: int = 0
    error: Optional[str] = None


class MailService:
    def __init__(self, gmail, gateway, tz: tzinfo, clock: Callable[[], datetime] = utcnow):
        self.gmail, self.gateway, self.tz, self.clock = gmail, gateway, tz, clock

    def digest(self, query: str = "in:inbox newer_than:2d", limit: int = 10) -> Digest:
        try:
            messages = self.gmail.fetch_recent(query, limit)
        except ProviderError:
            return Digest(error="gmail_unavailable")
        if not messages:
            return Digest(text="No matching emails.", count=0)

        now = self.clock().astimezone(self.tz)
        blocks: List[str] = []
        proposals: List[DeadlineProposal] = []
        for i, msg in enumerate(messages, 1):
            if et.has_secrets(msg):
                triage = et.SENSITIVE
            else:
                res = self.gateway.complete(et.build_prompt(msg, now), source="gmail")
                if res.status in (Status.NEEDS_APPROVAL, Status.UNAVAILABLE):
                    return Digest(error="no_local_model")
                triage = et.FALLBACK
                if res.status == Status.OK:
                    triage = et.parse_triage(res.text, now, self.tz) or et.FALLBACK
                elif res.status == Status.DENIED:
                    triage = et.SENSITIVE
            number = None
            if triage.deadline_when and triage.deadline_title:
                proposals.append(DeadlineProposal(triage.deadline_title, triage.deadline_when, msg.id))
                number = len(proposals)
            blocks.append(et.format_item(i, msg, triage, number))
        return Digest("\n".join(blocks), proposals, len(messages))
