"""LLM gateway: every model call goes through the egress guard; providers are tried in order.

* Providers the guard ALLOWs are tried first-to-last; failures and exhausted daily budgets fall through.
* A provider the guard answers ASK for is never called. If nothing else succeeds the caller gets
  NEEDS_APPROVAL with the host and payload hash, so the UI can show the exact text to the user.
* Escalation (email -> free cloud, user-approved per item): prepare_escalation() redacts, the user
  approves the hash of the redacted text, run_escalation() sends it and restores placeholders locally.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

try:  # typing.Protocol exists from Python 3.8
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from ..privacy.redactor import redact, restore
from ..privacy.sensitivity import Destination, Sensitivity
from .egress import Approval, EgressGuard, EgressRequest, Verdict
from .ledger import payload_hash


class ProviderError(Exception):
    """Provider failed (network, bad response). The gateway falls through to the next provider."""


class RateLimited(ProviderError):
    pass


class Provider(Protocol):
    name: str
    host: str
    dest: Destination

    def complete(self, prompt: str) -> str: ...


class Status(Enum):
    OK = "ok"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class GatewayResult:
    status: Status
    text: str = ""
    provider: Optional[str] = None
    reason: str = ""
    approval_host: Optional[str] = None
    payload_sha256: Optional[str] = None


@dataclass(frozen=True)
class Escalation:
    redacted: str
    mapping: Dict[str, str]  # placeholder -> original; never leaves the device
    payload_sha256: str
    source: Optional[str]


class LLMGateway:
    def __init__(self, guard: EgressGuard, providers: List[Provider],
                 daily_limits: Optional[Dict[str, int]] = None,
                 clock: Callable[[], float] = time.time):
        self.guard = guard
        self.providers = list(providers)
        self.daily_limits = dict(daily_limits or {})
        self._clock = clock
        self._used: Dict[Tuple[str, str], int] = {}

    # --- free-tier budget -------------------------------------------------
    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def _budget_ok(self, p: Provider) -> bool:
        limit = self.daily_limits.get(p.name)
        return limit is None or self._used.get((p.name, self._day()), 0) < limit

    def _bump(self, p: Provider) -> None:
        key = (p.name, self._day())
        self._used[key] = self._used.get(key, 0) + 1

    # --- core call --------------------------------------------------------
    def complete(self, prompt: str, source: Optional[str] = None,
                 approval: Optional[Approval] = None,
                 only: Optional[List[str]] = None,
                 declared_level: Optional[Sensitivity] = None) -> GatewayResult:
        first_ask: Optional[Tuple[Provider, str]] = None
        denied_reason = ""
        last_failure = ""
        for p in self.providers:
            if only is not None and p.name not in only:
                continue
            if not self._budget_ok(p):
                last_failure = f"{p.name}:budget_exhausted"
                continue
            d = self.guard.check(EgressRequest(p.host, p.dest, prompt, source, declared_level), approval)
            if d.verdict == Verdict.ALLOW:
                self._bump(p)
                try:
                    return GatewayResult(Status.OK, p.complete(prompt), p.name, d.reason)
                except ProviderError as e:
                    last_failure = f"{p.name}:{type(e).__name__}"
            elif d.verdict == Verdict.ASK:
                if first_ask is None:
                    first_ask = (p, d.payload_sha256)
            else:
                denied_reason = d.reason

        if first_ask is not None:
            p, digest = first_ask
            return GatewayResult(Status.NEEDS_APPROVAL, provider=p.name, reason="approval_required",
                                 approval_host=p.host, payload_sha256=digest)
        if denied_reason:
            return GatewayResult(Status.DENIED, reason=denied_reason)
        return GatewayResult(Status.UNAVAILABLE, reason=last_failure or "no_provider")

    # --- user-approved escalation to a cloud provider ---------------------
    def prepare_escalation(self, text: str, source: Optional[str],
                           known_names: Optional[List[str]] = None) -> Escalation:
        redacted, mapping = redact(text, known_names)
        return Escalation(redacted, mapping, payload_hash(redacted), source)

    def run_escalation(self, esc: Escalation, provider_name: str, approval: Approval) -> GatewayResult:
        target = next((p for p in self.providers if p.name == provider_name), None)
        if target is None or target.dest != Destination.CLOUD_FREE:
            return GatewayResult(Status.DENIED, reason="not_a_cloud_provider")
        res = self.complete(esc.redacted, source=esc.source, approval=approval, only=[provider_name])
        if res.status == Status.OK:
            return replace(res, text=restore(res.text, esc.mapping))
        return res
