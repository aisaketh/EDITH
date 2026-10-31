"""Minimal JSON-over-HTTP helpers for providers and connectors.

Security properties:
  * redirects are never followed (a redirect could send the payload to a host outside the allow-list)
  * errors carry only a short code, never URLs, headers or bodies (API keys travel in headers)
  * loopback targets bypass any configured proxy
  * responses are size-capped
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..broker.gateway import ProviderError, RateLimited

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_RESPONSE_BYTES = 8_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # makes urllib raise HTTPError for the 3xx instead of following it


def _call(req: urllib.request.Request, url: str, timeout: float) -> Any:
    handlers = [_NoRedirect()]
    if urlparse(url).hostname in LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code == 429:
            raise RateLimited("http_429") from None
        if 300 <= code < 400:
            raise ProviderError("redirect_blocked") from None
        raise ProviderError(f"http_{code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ProviderError(f"network_error:{type(e).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderError("response_too_large")
    try:
        return json.loads(body)
    except ValueError:
        raise ProviderError("bad_json") from None


def post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None,
              timeout: float = 60.0) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    return _call(req, url, timeout)


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 60.0) -> Any:
    req = urllib.request.Request(url, method="GET", headers=dict(headers or {}))
    return _call(req, url, timeout)


def post_form(url: str, fields: Dict[str, str], timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _call(req, url, timeout)
