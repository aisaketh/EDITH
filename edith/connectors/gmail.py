"""Read-only Gmail client (scope gmail.readonly). It can list and read messages; it cannot send, modify or delete.

Everything returned is untrusted S2 data. Callers must never treat email text as instructions.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlparse

from ..broker.egress import EgressGuard, EgressRequest, Verdict
from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from ..providers.http import LOOPBACK_HOSTS, get_json
from ..timeutil import UTC

MAX_BODY = 4000
MAX_LIMIT = 25
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class EmailMessage:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: Optional[datetime]   # aware
    snippet: str
    body: str                  # plain text, truncated to MAX_BODY
    labels: List[str]


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # malformed HTML must never break triage
        pass
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _header_text(value: Any, limit: int = 200) -> str:
    try:
        decoded = str(make_header(decode_header(str(value))))
    except Exception:
        decoded = str(value)
    return _CTRL_RE.sub(" ", decoded).strip()[:limit]


def _decode_part(part: Dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data") or ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    charset = "utf-8"
    for h in part.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == "content-type":
            m = re.search(r"charset=\"?([\w.-]+)", str(h.get("value", "")), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _collect(part: Any, plain: List[str], markup: List[str], depth: int = 0) -> None:
    if not isinstance(part, dict) or depth > 10 or part.get("filename"):
        return  # attachments are ignored
    mime = str(part.get("mimeType", ""))
    if mime == "text/plain":
        plain.append(_decode_part(part))
    elif mime == "text/html":
        markup.append(_decode_part(part))
    for sub in part.get("parts") or []:
        _collect(sub, plain, markup, depth + 1)


def parse_message(raw: Any) -> EmailMessage:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID_RE.match(raw["id"]):
        raise ProviderError("bad_message")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = {str(h["name"]).lower(): h["value"] for h in payload.get("headers") or []
               if isinstance(h, dict) and "name" in h and "value" in h}
    plain: List[str] = []
    markup: List[str] = []
    _collect(payload, plain, markup)
    body = "\n".join(p for p in plain if p.strip()) or html_to_text("\n".join(markup))
    body = "\n".join(" ".join(line.split()) for line in body.splitlines() if line.strip())[:MAX_BODY]

    name, addr = parseaddr(_header_text(headers.get("from", "")))
    sender = _header_text(f"{name} <{addr}>" if name and addr else (addr or name))
    date: Optional[datetime] = None
    try:
        date = parsedate_to_datetime(str(headers["date"]))
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        try:
            date = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            date = None
    labels = [str(x) for x in raw.get("labelIds") or [] if isinstance(x, str)][:20]
    return EmailMessage(raw["id"], str(raw.get("threadId", "")), sender,
                        _header_text(headers.get("subject", "")), date,
                        _CTRL_RE.sub(" ", str(raw.get("snippet", ""))).strip()[:300], body, labels)


class GmailClient:
    def __init__(self, token_provider: Callable[[], str], guard: EgressGuard,
                 base_url: str = "https://gmail.googleapis.com", timeout: float = 30.0):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError("Gmail base_url must be https")
        self.host = parsed.netloc.lower()
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._token, self.guard, self._timeout = token_provider, guard, timeout

    def __repr__(self) -> str:
        return f"GmailClient(host={self.host!r})"

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        query = urlencode(params)
        decision = self.guard.check(EgressRequest(self.host, Destination.CONNECTOR,
                                                  f"GET {path}?{query}", "system"))
        if decision.verdict != Verdict.ALLOW:
            raise ProviderError("blocked_by_policy")
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        return get_json(url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=self._timeout)

    def list_ids(self, query: str = "in:inbox newer_than:2d", limit: int = 10) -> List[str]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        query = _CTRL_RE.sub(" ", query).strip()[:200]
        data = self._get("/gmail/v1/users/me/messages", {"q": query, "maxResults": limit})
        ids = [m.get("id") for m in (data.get("messages") or []) if isinstance(m, dict)] if isinstance(data, dict) else []
        return [i for i in ids if isinstance(i, str) and _ID_RE.match(i)][:limit]

    def get(self, message_id: str) -> EmailMessage:
        if not _ID_RE.match(message_id):
            raise ValueError("bad_message_id")
        return parse_message(self._get(f"/gmail/v1/users/me/messages/{message_id}", {"format": "full"}))

    def fetch_recent(self, query: str = "in:inbox newer_than:2d", limit: int = 10) -> List[EmailMessage]:
        return [self.get(i) for i in self.list_ids(query, limit)]
