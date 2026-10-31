"""Telegram Bot API channel (long polling, so no inbound ports are opened).

Reminder: Telegram bot chats are NOT end-to-end encrypted. Outgoing text passes the egress guard
(destination class CHAT), which blocks secrets and email-derived content.

getUpdates carries no user content (only an offset), so it is not run through the guard or ledgered;
sendMessage always is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import urlparse

from ..broker.egress import Approval, EgressGuard, EgressRequest, Verdict
from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from ..providers.http import LOOPBACK_HOSTS, post_json

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MAX_MESSAGE = 4000  # Telegram's hard limit is 4096 characters


@dataclass(frozen=True)
class Incoming:
    update_id: int
    chat_id: int
    user_id: int
    chat_type: str
    text: Optional[str]


def _as_int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def split_message(text: str, limit: int = MAX_MESSAGE) -> List[str]:
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)
    return chunks


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, guard: EgressGuard, base_url: str = "https://api.telegram.org",
                 poll_timeout: int = 10):
        if not _TOKEN_RE.match(token or ""):
            raise ValueError("invalid Telegram bot token format")  # never echo the token
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError("Telegram base_url must be https")
        self.host = parsed.netloc.lower()
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._token = token
        self.guard = guard
        self.poll_timeout = poll_timeout

    def __repr__(self) -> str:
        return f"TelegramChannel(host={self.host!r})"

    def _api(self, method: str, payload: dict, timeout: float) -> Any:
        data = post_json(f"{self._base}/bot{self._token}/{method}", payload, timeout=timeout)
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ProviderError("telegram_not_ok")
        return data.get("result")

    def poll(self, offset: Optional[int] = None) -> List[Incoming]:
        payload: dict = {"timeout": self.poll_timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._api("getUpdates", payload, timeout=self.poll_timeout + 10)
        out: List[Incoming] = []
        for u in result if isinstance(result, list) else []:
            try:
                msg = u.get("message") or {}
                chat, sender = msg.get("chat") or {}, msg.get("from") or {}
                update_id, chat_id, user_id = _as_int(u.get("update_id")), _as_int(chat.get("id")), _as_int(sender.get("id"))
                if update_id is None or chat_id is None or user_id is None:
                    continue
                text = msg.get("text")
                out.append(Incoming(update_id, chat_id, user_id, str(chat.get("type", "")),
                                    text if isinstance(text, str) else None))
            except AttributeError:
                continue
        return out

    def send(self, text: str, source: str = "user_chat", chat_id: Optional[int] = None,
             approval: Optional[Approval] = None) -> bool:
        """Send through the egress guard. Returns False (and sends nothing) if the guard did not ALLOW.
        `approval` must be bound to exactly this text and this host (see Approval)."""
        if chat_id is None:
            raise ValueError("chat_id required")
        decision = self.guard.check(EgressRequest(self.host, Destination.CHAT, text, source), approval)
        if decision.verdict != Verdict.ALLOW:
            return False
        for chunk in split_message(text):
            self._api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                      timeout=30)
        return True
