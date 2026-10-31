"""Data sensitivity classes and egress destinations (see docs/ARCHITECTURE.md, section 11)."""
from __future__ import annotations

from enum import Enum, IntEnum


class Sensitivity(IntEnum):
    S0_PUBLIC = 0      # web search queries, generic chat
    S1_PERSONAL = 1    # reminder text, preferences, routines
    S2_SENSITIVE = 2   # email bodies, private messages, contacts, finance, health
    S3_SECRET = 3      # OTPs, passwords, card numbers, Aadhaar, PAN, tokens: never leaves code


class Destination(Enum):
    LOCAL = "local"            # loopback services such as a local Ollama server
    CLOUD_FREE = "cloud_free"  # free-tier LLM providers (may train on inputs)
    CHAT = "chat"              # the Telegram bot chat (not end-to-end encrypted)


# Provenance defaults. Anything not listed (or with no source) is treated as S2: stricter by default.
SOURCE_DEFAULTS = {
    "web": Sensitivity.S0_PUBLIC,
    "news": Sensitivity.S0_PUBLIC,
    "user_chat": Sensitivity.S1_PERSONAL,
    "reminder": Sensitivity.S1_PERSONAL,
    "calendar": Sensitivity.S1_PERSONAL,
    "gmail": Sensitivity.S2_SENSITIVE,
    "whatsapp": Sensitivity.S2_SENSITIVE,
    "contacts": Sensitivity.S2_SENSITIVE,
}
UNKNOWN_SOURCE_LEVEL = Sensitivity.S2_SENSITIVE
