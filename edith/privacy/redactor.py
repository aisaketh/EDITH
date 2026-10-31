"""Pseudonymiser: S2 values become reversible placeholders; S3 values are destroyed (not reversible)."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .classifier import find_sensitive
from .sensitivity import Sensitivity

Mapping = Dict[str, str]  # placeholder -> original


def redact(text: str, known_names: Optional[List[str]] = None) -> Tuple[str, Mapping]:
    """Return (redacted_text, mapping). Mapping holds only S2 values and must stay on the device."""
    findings = find_sensitive(text, known_names)
    out: List[str] = []
    mapping: Mapping = {}
    reverse: Dict[Tuple[str, str], str] = {}
    counters: Dict[str, int] = {}
    pos = 0
    for f in findings:
        out.append(text[pos:f.start])
        value = text[f.start:f.end]
        if f.level >= Sensitivity.S3_SECRET:
            out.append(f"[REDACTED:{f.kind.upper()}]")
        else:
            key = (f.kind, value.lower())
            if key not in reverse:
                counters[f.kind] = counters.get(f.kind, 0) + 1
                reverse[key] = f"<{f.kind.upper()}_{counters[f.kind]}>"
                mapping[reverse[key]] = value
            out.append(reverse[key])
        pos = f.end
    out.append(text[pos:])
    return "".join(out), mapping


def restore(text: str, mapping: Mapping) -> str:
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text
