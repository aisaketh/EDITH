import base64
import hashlib
import os
import stat
import tempfile
import threading
import unittest
import urllib.request
from urllib.parse import parse_qs, urlparse

from edith.broker.egress import EgressConfig, EgressGuard
from edith.broker.gateway import ProviderError
from edith.broker.ledger import Ledger
from edith.connectors.google_oauth import (GMAIL_READONLY, FileTokenStore, GoogleOAuth, ReauthRequired,
                                           pkce_pair)
from edith.privacy.sensitivity import Destination
from tests.test_providers import ok_json, serve


class Time:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class MemStore:
    def __init__(self, token=None):
        self.token = token

    def get(self):
        return self.token

    def set(self, t):
        self.token = t

    def delete(self):
        self.token = None


def make(url, store=None, clock=None):
    host = url.split("//", 1)[1]
    guard = EgressGuard(Ledger(), EgressConfig(allowlist={host: Destination.CONNECTOR}))
    oauth = GoogleOAuth("cid", "csecret", store if store is not None else MemStore(), guard,
                        auth_url="https://accounts.example/auth", token_url=url + "/token",
                        clock=clock or Time())
    return oauth, guard


def token_server(refresh="RT1", access="AT1", ttl=3600):
    def responder(path, headers, raw):
        form = parse_qs(raw.decode())
        if form.get("grant_type") == ["authorization_code"]:
            return ok_json({"access_token": access, "expires_in": ttl, "refresh_token": refresh})
        return ok_json({"access_token": access + "-r", "expires_in": ttl})
    return responder


def hit(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(url, timeout=5).read()
    except Exception:
        pass


class PkceAndUrlTests(unittest.TestCase):
    def test_pkce(self):
        v, c = pkce_pair()
        self.assertTrue(43 <= len(v) <= 128)
        expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(c, expected)
        self.assertNotEqual(pkce_pair()[0], v)

    def test_authorize_url(self):
        oauth, _ = make("http://127.0.0.1:9")
        url = oauth.authorize_url("http://127.0.0.1:5555/", "STATE", "CHAL")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual(q["client_id"], "cid")
        self.assertEqual(q["scope"], GMAIL_READONLY)          # read-only, nothing broader
        self.assertEqual((q["response_type"], q["code_challenge_method"], q["access_type"]),
                         ("code", "S256", "offline"))
        self.assertEqual((q["state"], q["code_challenge"], q["redirect_uri"]), ("STATE", "CHAL", "http://127.0.0.1:5555/"))
        self.assertNotIn("csecret", url)

    def test_requires_credentials(self):
        with self.assertRaises(ValueError):
            GoogleOAuth("", "x", MemStore(), EgressGuard(Ledger()))
        self.assertNotIn("csecret", repr(make("http://127.0.0.1:9")[0]))


class FileTokenStoreTests(unittest.TestCase):
    def test_roundtrip_permissions_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileTokenStore(os.path.join(tmp, "tok.json"))
            self.assertIsNone(store.get())
            store.set("RT1")
            self.assertEqual(store.get(), "RT1")
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            for junk in ("not json", "[]", '{"refresh_token": 5}', '{"refresh_token": ""}'):
                with open(store.path, "w") as fh:
                    fh.write(junk)
                self.assertIsNone(store.get(), junk)
            store.delete()
            store.delete()
            self.assertIsNone(store.get())


class TokenTests(unittest.TestCase):
    def test_exchange_caches_and_refreshes(self):
        clock = Time()
        with serve(token_server()) as (url, seen):
            store = MemStore()
            oauth, guard = make(url, store, clock)
            oauth.exchange_code("CODE", "VERIFIER", "http://127.0.0.1:1/")
            self.assertEqual(store.get(), "RT1")
            self.assertEqual(oauth.access_token(), "AT1")      # cached: no second request
            self.assertEqual(len(seen), 1)
            _, _, form = seen[0]
            self.assertEqual((form["grant_type"], form["code"], form["code_verifier"]),
                             ("authorization_code", "CODE", "VERIFIER"))
            clock.t += 3600                                      # expired
            self.assertEqual(oauth.access_token(), "AT1-r")
            self.assertEqual(seen[1][2]["grant_type"], "refresh_token")
            self.assertEqual(seen[1][2]["refresh_token"], "RT1")
        for e in guard.ledger.entries():                         # secrets never reach the ledger
            for secret in ("RT1", "AT1", "csecret", "CODE"):
                self.assertNotIn(secret, e.preview)
            self.assertTrue(e.preview.startswith("oauth:"))

    def test_not_logged_in_and_rejected_grants(self):
        oauth, _ = make("http://127.0.0.1:9", MemStore())
        with self.assertRaises(ReauthRequired):
            oauth.access_token()
        for status, exc, reauth in ((400, ReauthRequired, True), (401, ReauthRequired, True),
                                    (500, ProviderError, False)):
            with serve(lambda *a, s=status: (s, b"{}", {})) as (url, _):
                oauth, _ = make(url, MemStore("RT1"))
                with self.assertRaises(exc) as ctx:
                    oauth.access_token()
                self.assertEqual(isinstance(ctx.exception, ReauthRequired), reauth)

    def test_bad_token_responses(self):
        for body in ({"access_token": "A", "expires_in": 3600},                   # no refresh token
                     {"refresh_token": "R", "expires_in": 3600},                  # no access token
                     {"access_token": "A", "refresh_token": "R"},                 # no expiry
                     {"access_token": "", "refresh_token": "R", "expires_in": 1}):
            with serve(lambda *a, b=body: ok_json(b)) as (url, _):
                oauth, _ = make(url, MemStore())
                with self.assertRaises(ProviderError):
                    oauth.exchange_code("c", "v", "http://127.0.0.1:1/")
        with serve(lambda *a: ok_json([1, 2])) as (url, _):
            with self.assertRaises(ProviderError):
                make(url)[0].exchange_code("c", "v", "http://127.0.0.1:1/")


class LoginFlowTests(unittest.TestCase):
    def run_login(self, make_query, expect_error=None, timeout=10.0):
        with serve(token_server()) as (url, seen):
            store = MemStore()
            oauth, _ = make(url, store)
            captured = {}

            def on_url(auth_url):
                q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
                captured.update(q)
                threading.Thread(target=hit, args=(q["redirect_uri"] + "?" + make_query(q),), daemon=True).start()

            if expect_error:
                with self.assertRaises(ProviderError) as ctx:
                    oauth.login(on_url=on_url, timeout=timeout)
                self.assertEqual(str(ctx.exception), expect_error)
            else:
                oauth.login(on_url=on_url, timeout=timeout)
            return store, seen, captured

    def test_full_flow_with_pkce(self):
        store, seen, captured = self.run_login(lambda q: f"code=CODE1&state={q['state']}")
        self.assertEqual(store.get(), "RT1")
        form = seen[0][2]
        self.assertEqual((form["code"], form["grant_type"], form["redirect_uri"]),
                         ("CODE1", "authorization_code", captured["redirect_uri"]))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, captured["code_challenge"])      # verifier matches the challenge sent

    def test_stray_requests_before_the_redirect_are_ignored(self):
        with serve(token_server()) as (url, _):
            store = MemStore()
            oauth, _ = make(url, store)

            def on_url(auth_url):
                q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
                base = q["redirect_uri"]

                def go():
                    hit(base + "favicon.ico")                      # noise first
                    hit(base + f"?code=CODE2&state={q['state']}")  # then the real redirect
                threading.Thread(target=go, daemon=True).start()

            oauth.login(on_url=on_url, timeout=10.0)
        self.assertEqual(store.get(), "RT1")

    def test_forged_redirect_is_rejected_without_any_token_call(self):
        store, seen, _ = self.run_login(lambda q: "code=EVIL&state=WRONG", expect_error="state_mismatch")
        self.assertIsNone(store.get())
        self.assertEqual(seen, [])

    def test_user_denied(self):
        store, seen, _ = self.run_login(lambda q: f"error=access_denied&state={q['state']}", expect_error="login_denied")
        self.assertEqual((store.get(), seen), (None, []))

    def test_timeout(self):
        oauth, _ = make("http://127.0.0.1:9")
        with self.assertRaises(ProviderError) as ctx:
            oauth.login(on_url=lambda u: None, timeout=0.6)
        self.assertEqual(str(ctx.exception), "login_timeout")


if __name__ == "__main__":
    unittest.main()
