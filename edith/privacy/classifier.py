"""Deterministic pattern + provenance classifier.

Patterns err on the side of over-blocking (e.g. a 12-digit number starting with 2-9 is treated as a
possible Aadhaar). Person names are NOT detected here; pass known contact names to the Redactor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .sensitivity import SOURCE_DEFAULTS, UNKNOWN_SOURCE_LEVEL, Sensitivity


@dataclass(frozen=True)
class Finding:
    kind: str
    level: Sensitivity
    start: int
    end: int


@dataclass(frozen=True)
class Classification:
    level: Sensitivity
    findings: List[Finding]


def luhn_ok(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        flip = not flip
    return total % 10 == 0


_OTP_KW = r"(?:otp|one[- ]time (?:password|passcode|pin)|verification code|security code|passcode|upi[- ]?pin|atm[- ]?pin|mpin)"

# (kind, level, compiled regex, capture group that holds the sensitive span; 0 = whole match)
_PATTERNS = [
    ("otp", Sensitivity.S3_SECRET, re.compile(rf"\b{_OTP_KW}\b[^\d\n]{{0,25}}(\d{{4,8}})\b", re.I), 1),
    ("otp", Sensitivity.S3_SECRET, re.compile(rf"\b(\d{{4,8}})\b(?=[^\n]{{0,40}}\b{_OTP_KW}\b)", re.I), 1),
    ("password", Sensitivity.S3_SECRET, re.compile(r"\b(?:password|passwd|pwd|passphrase)\b\s*(?:is|=|:)\s*(\S+)", re.I), 1),
    ("api_key", Sensitivity.S3_SECRET, re.compile(
        r"(?:sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})"), 0),
    ("pan", Sensitivity.S3_SECRET, re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), 0),
    ("aadhaar", Sensitivity.S3_SECRET, re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b"), 0),
    ("bank_account", Sensitivity.S3_SECRET, re.compile(
        r"\b(?:a/c|acct?|account)(?:\s*(?:no|number|#))?\.?\s*[:\-]?\s*(\d{9,18})\b", re.I), 1),
    ("email", Sensitivity.S2_SENSITIVE, re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), 0),
    ("upi_id", Sensitivity.S2_SENSITIVE, re.compile(
        r"\b[\w.\-]{2,}@(?:ok(?:hdfcbank|icici|sbi|axis)|ybl|ibl|axl|paytm|upi|apl|sbi|hdfcbank|icici|axisbank)\b", re.I), 0),
    ("phone", Sensitivity.S2_SENSITIVE, re.compile(r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), 0),
]
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def find_sensitive(text: str, extra_names: Optional[List[str]] = None) -> List[Finding]:
    """All findings, overlaps resolved (higher level wins, then longer span), sorted by position."""
    raw: List[Finding] = []
    for kind, level, rx, grp in _PATTERNS:
        for m in rx.finditer(text):
            raw.append(Finding(kind, level, m.start(grp), m.end(grp)))
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            raw.append(Finding("card", Sensitivity.S3_SECRET, m.start(), m.end()))
    for name in extra_names or []:
        if name.strip():
            for m in re.finditer(rf"\b{re.escape(name)}\b", text, re.I):
                raw.append(Finding("person", Sensitivity.S2_SENSITIVE, m.start(), m.end()))

    raw.sort(key=lambda f: (-int(f.level), -(f.end - f.start), f.start))
    accepted: List[Finding] = []
    for f in raw:
        if all(f.end <= a.start or f.start >= a.end for a in accepted):
            accepted.append(f)
    accepted.sort(key=lambda f: f.start)
    return accepted


def classify(text: str, source: Optional[str] = None, extra_names: Optional[List[str]] = None) -> Classification:
    base = SOURCE_DEFAULTS.get(source, UNKNOWN_SOURCE_LEVEL) if source else UNKNOWN_SOURCE_LEVEL
    findings = find_sensitive(text, extra_names)
    level = max([base] + [f.level for f in findings])
    return Classification(Sensitivity(level), findings)
