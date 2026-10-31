"""Google OAuth for an installed app: loopback redirect + PKCE, refresh-token storage, access-token cache.

Only the read-only Gmail scope is requested. The refresh token is kept in a file readable only by you
(0600, inside the 0700 data dir); client id/secret come from the environment. Token request bodies are
never passed to the egress guard or ledger (they are described as "oauth:..." instead).

One-time setup (by you, in Google Cloud Console): create a project, enable the Gmail API, configure the OAuth
consent screen, set publishing status to "In production" (an unverified personal app shows a warning but its
refresh tokens do not expire after 7 days like "Testing" ones), and create an OAuth client of type
"Desktop app". Put its id/secret in GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET, then run:
    python3 -m edith gmail-login
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

from ..broker.egress import EgressGuard, EgressRequest, Verdict
from ..broker.gateway import ProviderError
from ..privacy.sensitivity import Destination
from ..providers.http import post_form

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class ReauthRequired(ProviderError):
    """No usable refresh token: run `python3 -m edith gmail-login` again."""


class FileTokenStore:
    def __init__(self, path: str):
        self.path = path

    def get(self) -> Optional[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                value = json.load(fh).get("refresh_token")
        except (FileNotFoundError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value else None

    def set(self, refresh_token: str) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"refresh_token": refresh_token}, fh)
        os.chmod(self.path, 0o600)

    def delete(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def pkce_pair() -> "tuple[str, str]":
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, store, guard: EgressGuard,
                 scopes: Sequence[str] = (GMAIL_READONLY,), auth_url: str = AUTH_URL,
                 token_url: str = TOKEN_URL, clock: Callable[[], float] = time.time):
        if not client_id or not client_secret:
            raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required")
        self._id, self._secret, self.store, self.guard = client_id, client_secret, store, guard
        self.scopes, self.auth_url, self.token_url, self._clock = list(scopes), auth_url, token_url, clock
        self._access: Optional[str] = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return "GoogleOAuth(...)"

    # --- authorisation URL --------------------------------------------------
    def authorize_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        return self.auth_url + "?" + urlencode({
            "client_id": self._id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(self.scopes), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})

    # --- token calls --------------------------------------------------------
    def _token_call(self, description: str, fields: Dict[str, str]) -> Dict[str, object]:
        host = urlparse(self.token_url).netloc.lower()
        decision = self.guard.check(EgressRequest(host, Destination.CONNECTOR, description, "system"))
        if decision.verdict != Verdict.ALLOW:
            raise ProviderError("blocked_by_policy")
        data = post_form(self.token_url, fields)
        if not isinstance(data, dict):
            raise ProviderError("bad_token_response")
        return data

    def _remember_access(self, data: Dict[str, object]) -> None:
        token, ttl = data.get("access_token"), data.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(ttl, (int, float)):
            raise ProviderError("bad_token_response")
        self._access, self._expires_at = token, self._clock() + float(ttl)

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> None:
        data = self._token_call("oauth:code_exchange", {
            "client_id": self._id, "client_secret": self._secret, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ProviderError("no_refresh_token")  # happens if consent was not granted offline
        self._remember_access(data)
        self.store.set(refresh)

    def access_token(self) -> str:
        if self._access and self._clock() < self._expires_at - 60:
            return self._access
        refresh = self.store.get()
        if not refresh:
            raise ReauthRequired("not_logged_in")
        try:
            data = self._token_call("oauth:refresh", {
                "client_id": self._id, "client_secret": self._secret, "refresh_token": refresh,
                "grant_type": "refresh_token"})
        except ProviderError as e:
            if str(e) in ("http_400", "http_401"):  # revoked or expired grant
                raise ReauthRequired("grant_rejected") from None
            raise
        self._remember_access(data)
        return self._access  # type: ignore[return-value]

    # --- interactive login --------------------------------------------------
    def login(self, on_url: Optional[Callable[[str], None]] = None, timeout: float = 300.0) -> None:
        """Run the browser flow on a loopback port; blocks until finished or timed out."""
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        result: Dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                if "code" not in q and "error" not in q:
                    # favicon, browser preconnect, port scan...: not the OAuth redirect, keep waiting
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                result["code"] = (q.get("code") or [""])[0]
                result["state"] = (q.get("state") or [""])[0]
                result["error"] = (q.get("error") or [""])[0]
                ok = bool(result["code"]) and not result["error"]
                body = ("EDITH is connected. You can close this tab." if ok
                        else "Sign-in was not completed. You can close this tab.").encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 0.5
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
        url = self.authorize_url(redirect_uri, state, challenge)
        (on_url or _open_in_browser)(url)
        deadline = time.monotonic() + timeout
        try:
            while not result and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            raise ProviderError("login_timeout")
        if result.get("error") or not result.get("code"):
            raise ProviderError("login_denied")
        if not hmac.compare_digest(result.get("state", "").encode("utf-8"), state.encode("ascii")):
            raise ProviderError("state_mismatch")  # possible forged redirect
        self.exchange_code(result["code"], verifier, redirect_uri)


def _open_in_browser(url: str) -> None:
    print("Open this link in your browser to connect Gmail (read-only):\n\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
