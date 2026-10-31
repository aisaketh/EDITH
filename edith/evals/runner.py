"""Evaluation harness: how often does the reminder pipeline do the right thing, and, more importantly,
how often does it do the WRONG thing?

Verdicts per case:
  pass          behaved as expected
  safe_miss     asked a question / produced nothing where a proposal was expected (annoying, harmless)
  error         the model's output was unusable even after one retry (harmless, nothing is saved)
  wrong_action  produced a proposal that differs from the expectation, or one where none was allowed
                (the number that must stay at zero)

Default mode replays recorded "model replies" from cases.json (ideal and adversarial) through the real
parser/validators: it tests the safety net, not a model. `--live` sends the real user texts to the configured
providers (use fake data only; add --allow-cloud to let the cloud provider see them).

    python3 -m edith.evals [--live] [--allow-cloud] [--max-wrong 0.0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from ..agent import reminder_parser as rp
from ..timeutil import get_tz

CASES_PATH = os.path.join(os.path.dirname(__file__), "cases.json")
Complete = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class Outcome:
    kind: str                       # proposal | clarify | error
    proposal: Optional[rp.Proposal]
    detail: str = ""


@dataclass(frozen=True)
class CaseResult:
    id: str
    category: str
    verdict: str
    detail: str


def load_cases(path: str = CASES_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def interpret(complete: Complete, user_text: str, now_local: datetime, tz, attempts: int = 2) -> Outcome:
    """Mirror of the assistant's flow: prompt -> model -> validate, with one retry on unusable output."""
    base = rp.build_prompt(user_text, now_local)
    prompt, last = base, "no_attempt"
    for _ in range(attempts):
        raw = complete(prompt)
        if raw is None:
            return Outcome("error", None, "model_unavailable")
        out = rp.parse_response(raw, now_local, tz)
        if out.error:
            last = out.error
            prompt = base + f"\n\nYour previous reply was invalid ({out.error}). Reply with ONLY the JSON object."
            continue
        if out.clarify:
            return Outcome("clarify", None, out.clarify)
        return Outcome("proposal", out.proposal)
    return Outcome("error", None, last)


def _matches(p: rp.Proposal, expect: dict) -> bool:
    return (p.kind == expect.get("kind")
            and p.when.strftime("%Y-%m-%dT%H:%M") == expect.get("when")
            and p.language == expect.get("language", p.language)
            and p.recurrence == expect.get("recurrence"))


def judge(expect: dict, out: Outcome) -> str:
    want = expect["outcome"]
    if want == "no_proposal":
        return "wrong_action" if out.kind == "proposal" else "pass"
    if want == "clarify":
        return {"clarify": "pass", "proposal": "wrong_action"}.get(out.kind, "error")
    if want in ("proposal", "proposal_or_clarify"):
        if out.kind == "proposal":
            return "pass" if _matches(out.proposal, expect) else "wrong_action"
        if out.kind == "clarify":
            return "pass" if want == "proposal_or_clarify" else "safe_miss"
        return "safe_miss" if want == "proposal_or_clarify" else "error"
    raise ValueError(f"unknown outcome {want!r}")


def run_cases(doc: dict, complete_for: Callable[[dict], Complete]) -> List[CaseResult]:
    tz = get_tz(doc.get("timezone", "Asia/Kolkata"))
    now = datetime.strptime(doc["now"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    results = []
    for case in doc["cases"]:
        out = interpret(complete_for(case), case["text"], now, tz)
        verdict = judge(case["expect"], out)
        results.append(CaseResult(case["id"], case["category"], verdict, out.detail or out.kind))
    return results


def fake_complete_for(case: dict) -> Complete:
    reply = case["fake_reply"]
    text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
    return lambda prompt: text


def summarize(results: List[CaseResult]) -> Dict[str, object]:
    total = len(results) or 1
    count = lambda v: sum(1 for r in results if r.verdict == v)  # noqa: E731
    return {"total": len(results), "pass": count("pass"), "safe_miss": count("safe_miss"),
            "error": count("error"), "wrong_action": count("wrong_action"),
            "pass_rate": count("pass") / total, "wrong_action_rate": count("wrong_action") / total}


def format_report(results: List[CaseResult]) -> str:
    by_cat: "OrderedDict[str, List[CaseResult]]" = OrderedDict()
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    lines = ["category          pass/total"]
    for cat, rs in by_cat.items():
        lines.append(f"{cat:<17} {sum(1 for r in rs if r.verdict == 'pass')}/{len(rs)}")
    s = summarize(results)
    lines.append(f"\noverall pass rate : {s['pass_rate']:.0%}  ({s['pass']}/{s['total']})")
    lines.append(f"wrong-action rate : {s['wrong_action_rate']:.0%}  ({s['wrong_action']})   <- must stay 0")
    lines.append(f"safe misses       : {s['safe_miss']}    unusable outputs: {s['error']}")
    bad = [r for r in results if r.verdict != "pass"]
    if bad:
        lines.append("\nnot passing:")
        lines += [f"  {r.id:<8} {r.verdict:<13} {r.detail}" for r in bad]
    return "\n".join(lines)


def _live_complete(allow_cloud: bool) -> Complete:
    from ..broker.egress import EgressConfig, EgressGuard
    from ..broker.gateway import LLMGateway, Status
    from ..broker.ledger import Ledger
    from ..config import Config
    from ..providers.gemini import GeminiProvider
    from ..providers.ollama import OllamaProvider

    cfg = Config.from_env()
    providers = []
    if cfg.local_model:
        providers.append(OllamaProvider(cfg.local_model))
    if cfg.gemini_model:
        providers.append(GeminiProvider(cfg.gemini_model))
    if not providers:
        raise SystemExit("No model configured: set EDITH_LOCAL_MODEL and/or EDITH_GEMINI_MODEL.")
    gateway = LLMGateway(EgressGuard(Ledger(), EgressConfig(allow_s1_to_free_cloud=allow_cloud)), providers)

    def complete(prompt: str) -> Optional[str]:
        res = gateway.complete(prompt, source="user_chat")
        return res.text if res.status == Status.OK else None
    return complete


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m edith.evals", description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", action="store_true", help="query the configured model(s) with the case texts")
    ap.add_argument("--allow-cloud", action="store_true", help="with --live, let the free cloud model see the (fake) texts")
    ap.add_argument("--max-wrong", type=float, default=0.0, help="fail if wrong-action rate exceeds this (default 0)")
    args = ap.parse_args(argv)

    doc = load_cases()
    if args.live:
        complete = _live_complete(args.allow_cloud)
        results = run_cases(doc, lambda case: complete)
        mode = "LIVE"
    else:
        results = run_cases(doc, fake_complete_for)
        mode = "replay (validators only)"
    print(f"mode: {mode}\n")
    print(format_report(results))
    return 1 if summarize(results)["wrong_action_rate"] > args.max_wrong else 0


if __name__ == "__main__":
    sys.exit(main())
