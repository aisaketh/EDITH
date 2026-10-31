"""Action policy: risk tiers, origin checks, spend caps, kill switch (deterministic, no LLM)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable, Dict, FrozenSet, Optional

from .egress import Verdict


class RiskTier(IntEnum):
    READ_OWN = 0               # read calendar, search memory
    READ_EXTERNAL = 1          # web search, fetch page
    REVERSIBLE_WRITE = 2       # create reminder, save draft
    COMMUNICATE = 3            # send email / message / invite
    MONEY_OR_IRREVERSIBLE = 4  # place order, pay, delete data


class Origin(Enum):
    USER = "user"
    SYSTEM = "system"                        # scheduler, routines the user configured
    UNTRUSTED_CONTENT = "untrusted_content"  # derived from an email, web page or incoming message


@dataclass(frozen=True)
class ActionProposal:
    tool: str
    tier: RiskTier
    origin: Origin
    amount_inr: Optional[float] = None


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    reason: str


@dataclass
class PolicyConfig:
    per_action_cap_inr: float = 1000.0
    daily_cap_inr: float = 2000.0
    # Tier-3 tools the user has granted standing approval. Tier 4 can never be listed here.
    standing_approval_tools: FrozenSet[str] = frozenset()


class PolicyEngine:
    def __init__(self, config: Optional[PolicyConfig] = None, clock: Callable[[], float] = time.time):
        self.config = config or PolicyConfig()
        self.halted = False  # kill switch
        self._clock = clock
        self._spend: Dict[str, float] = {}

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._clock()))

    def spent_today(self) -> float:
        return self._spend.get(self._today(), 0.0)

    def record_spend(self, amount_inr: float) -> None:
        self._spend[self._today()] = self.spent_today() + amount_inr

    def decide(self, p: ActionProposal) -> PolicyDecision:
        if self.halted:
            return PolicyDecision(Verdict.DENY, "kill_switch_engaged")
        if p.tier >= RiskTier.COMMUNICATE and p.origin == Origin.UNTRUSTED_CONTENT:
            return PolicyDecision(Verdict.DENY, "instruction_originates_from_untrusted_content")
        if p.tier == RiskTier.MONEY_OR_IRREVERSIBLE:
            if p.amount_inr is not None:
                if p.amount_inr > self.config.per_action_cap_inr:
                    return PolicyDecision(Verdict.DENY, "over_per_action_cap")
                if self.spent_today() + p.amount_inr > self.config.daily_cap_inr:
                    return PolicyDecision(Verdict.DENY, "over_daily_cap")
            return PolicyDecision(Verdict.ASK, "always_ask_tier4")
        if p.tier == RiskTier.COMMUNICATE:
            if p.tool in self.config.standing_approval_tools:
                return PolicyDecision(Verdict.ALLOW, "standing_approval")
            return PolicyDecision(Verdict.ASK, "approval_required")
        return PolicyDecision(Verdict.ALLOW, "tier_auto")
