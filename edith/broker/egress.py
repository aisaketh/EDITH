"""Egress guard: the single checkpoint every outbound payload must pass (deterministic, no LLM).

Rules (see docs/ARCHITECTURE.md, section 11):
  * host must be on the allow-list, and the claimed destination class must match the allow-list entry
  * any S3 pattern in the final payload is denied everywhere, no approval can override it
  * LOCAL accepts S0-S2
  * CLOUD_FREE: S0 allowed, S1 allowed only if configured, S2 (and S1 otherwise) needs an approval
    bound to the exact payload hash and host
  * CHAT: S0/S1 allowed, S2 needs an approval
Every decision is written to the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional

from ..privacy.classifier import classify
from ..privacy.sensitivity import USER_ORIGIN_SOURCES, Destination, Sensitivity
from .ledger import Ledger, payload_hash


class Verdict(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


DEFAULT_ALLOWLIST: Dict[str, Destination] = {
    "localhost:11434": Destination.LOCAL,
    "127.0.0.1:11434": Destination.LOCAL,
    "generativelanguage.googleapis.com": Destination.CLOUD_FREE,
    "api.telegram.org": Destination.CHAT,
    "api.tavily.com": Destination.CLOUD_FREE,
    "factchecktools.googleapis.com": Destination.CLOUD_FREE,
    "places.googleapis.com": Destination.CLOUD_FREE,
    "gmail.googleapis.com": Destination.CONNECTOR,
    "oauth2.googleapis.com": Destination.CONNECTOR,
}


@dataclass(frozen=True)
class EgressRequest:
    host: str
    dest: Destination
    text: str
    source: Optional[str] = None             # provenance of the content; None/unknown means S2
    declared_level: Optional[Sensitivity] = None  # caller may only raise the level, never lower it


@dataclass(frozen=True)
class Approval:
    """A user approval for one exact payload to one exact host."""
    payload_sha256: str
    host: str


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    level: Sensitivity
    payload_sha256: str


@dataclass
class EgressConfig:
    allow_s1_to_free_cloud: bool = False
    chat_allows_user_origin_pii: bool = True
    allowlist: Dict[str, Destination] = field(default_factory=lambda: dict(DEFAULT_ALLOWLIST))


class EgressGuard:
    def __init__(self, ledger: Ledger, config: Optional[EgressConfig] = None,
                 halted: Optional[Callable[[], bool]] = None):
        self.ledger = ledger
        self.config = config or EgressConfig()
        self.halted = halted

    def check(self, req: EgressRequest, approval: Optional[Approval] = None) -> Decision:
        host = req.host.strip().lower()
        digest = payload_hash(req.text)
        cls = classify(req.text, req.source)
        level = cls.level
        if req.declared_level is not None:
            level = Sensitivity(max(level, req.declared_level))

        verdict, reason = self._decide(host, req, level, cls, approval, digest)
        self.ledger.append("egress", f"{req.dest.value}:{host}", int(level), verdict.value, reason, req.text)
        return Decision(verdict, reason, level, digest)

    def _decide(self, host, req, level, cls, approval, digest):
        allowed_dest = self.config.allowlist.get(host)
        if allowed_dest is None:
            return Verdict.DENY, "host_not_allowlisted"
        if allowed_dest != req.dest:
            return Verdict.DENY, "destination_class_mismatch"
        if self.halted is not None and self.halted() and req.dest != Destination.CHAT:
            return Verdict.DENY, "kill_switch_engaged"  # only the owner chat stays open, to say "halted"
        if any(f.level >= Sensitivity.S3_SECRET for f in cls.findings) or level >= Sensitivity.S3_SECRET:
            return Verdict.DENY, "secret_in_payload"

        if req.dest == Destination.LOCAL:
            return Verdict.ALLOW, "local_ok"
        if req.dest == Destination.CONNECTOR:
            # Requests to the user's own account (search queries, ids). Never pass secrets as `text`:
            # token requests are described, not included, e.g. "oauth:refresh".
            return Verdict.ALLOW, "connector_ok"

        needs_approval = False
        if req.dest == Destination.CLOUD_FREE:
            if level >= Sensitivity.S2_SENSITIVE:
                needs_approval = True
            elif level == Sensitivity.S1_PERSONAL and not self.config.allow_s1_to_free_cloud:
                needs_approval = True
        elif req.dest == Destination.CHAT:
            if level >= Sensitivity.S2_SENSITIVE:
                # PII that the user typed themselves (a phone number in a reminder) may be echoed back;
                # anything derived from email/messages (S2 by provenance) may not.
                user_origin = (self.config.chat_allows_user_origin_pii
                               and req.source in USER_ORIGIN_SOURCES
                               and (req.declared_level is None or req.declared_level < Sensitivity.S2_SENSITIVE))
                needs_approval = not user_origin

        if not needs_approval:
            return Verdict.ALLOW, "policy_ok"
        if approval and approval.payload_sha256 == digest and approval.host.strip().lower() == host:
            return Verdict.ALLOW, "user_approved_exact_payload"
        return Verdict.ASK, "approval_required"
