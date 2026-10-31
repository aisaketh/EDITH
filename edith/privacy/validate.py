"""Shared validation for any free text the user asks EDITH to store."""
from __future__ import annotations

from .classifier import find_sensitive
from .sensitivity import Sensitivity


def strip_secrets(text: str, placeholder: str = "[hidden]") -> str:
    """Replace S3 spans (OTP, card, PAN, Aadhaar, passwords, keys) with a placeholder; keep everything else."""
    out, pos = [], 0
    for f in find_sensitive(text):
        if f.level >= Sensitivity.S3_SECRET:
            out.append(text[pos:f.start])
            out.append(placeholder)
            pos = f.end
    out.append(text[pos:])
    return "".join(out)


def clean_user_text(text: str, max_len: int = 500) -> str:
    """Normalise whitespace; reject empty, too long, or secret-bearing text (ValueError with a short code)."""
    text = " ".join(str(text).split())
    if not text or len(text) > max_len:
        raise ValueError("bad_text")
    if any(f.level >= Sensitivity.S3_SECRET for f in find_sensitive(text)):
        raise ValueError("secret_in_text")
    return text
