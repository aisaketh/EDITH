import json
import os
import socket
import threading
import unittest
from contextlib import contextmanager
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from edith.broker.egress import EgressConfig, EgressGuard
from edith.broker.gateway import LLMGateway, ProviderError, RateLimited, Status
from edith.broker.ledger import Ledger
from edith.privacy.sensitivity import Destination
from edith.providers.gemini import GeminiProvider
from edith.providers.ollama import OllamaProvider

SECRET_KEY = "AIzaSyFAKEKEYFORTESTING0123456789abcde"


@contextmanager
def serve(responder):
    """Run a throwaway local HTTP server. responder(path, headers, body) -> (status, body_bytes, headers)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, dict(self.headers), json.loads(raw or b"{}")))
            status, body, headers = responder(self.path, self.headers, raw)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def ok_json(obj):
    return 200, json.dumps(obj).encode(), {"Content-Type": "application/json"}


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class OllamaTests(unittest.TestCase):
    def test_success_and_request_shape(self):
        with serve(lambda *a: ok_json({"message": {"content": "namaste"}})) as (url, seen):
            p = OllamaProvider("qwen3:8b", base_url=url, system="be brief")
            self.assertEqual(p.complete("hello"), "namaste")
        path, _, body = seen[0]
        self.assertEqual(path, "/api/chat")
        self.assertFalse(body["stream"])
        self.assertEqual(body["model"], "qwen3:8b")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_rejects_non_loopback_and_bad_model(self):
        with self.assertRaises(ValueError):
            OllamaProvider("m", base_url="http://example.com:11434")
        with self.assertRaises(ValueError):
            OllamaProvider("m", base_url="https://localhost:11434")
        with self.assertRaises(ValueError):
            OllamaProvider("bad model; rm -rf")

    def test_http_errors_map_to_provider_errors(self):
        with serve(lambda *a: (429, b"{}", {})) as (url, _):
            with self.assertRaises(RateLimited):
                OllamaProvider("m", base_url=url).complete("x")
        with serve(lambda *a: (500, b"boom", {})) as (url, _):
            with self.assertRaises(ProviderError) as ctx:
                OllamaProvider("m", base_url=url).complete("x")
            self.assertNotIsInstance(ctx.exception, RateLimited)

    def test_redirect_is_not_followed(self):
        with serve(lambda *a: (302, b"", {"Location": "http://127.0.0.1:1/steal"})) as (url, _):
            with self.assertRaises(ProviderError) as ctx:
                OllamaProvider("m", base_url=url).complete("x")
            self.assertEqual(str(ctx.exception), "redirect_blocked")

    def test_connection_refused_bad_json_unexpected_shape(self):
        with self.assertRaises(ProviderError):
            OllamaProvider("m", base_url=free_port_url(), timeout=2).complete("x")
        with serve(lambda *a: (200, b"not json", {})) as (url, _):
            with self.assertRaises(ProviderError):
                OllamaProvider("m", base_url=url).complete("x")
        with serve(lambda *a: ok_json({"unexpected": 1})) as (url, _):
            with self.assertRaises(ProviderError):
                OllamaProvider("m", base_url=url).complete("x")


class GeminiTests(unittest.TestCase):
    def test_success_sends_key_in_header_and_joins_parts(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "hel"}, {"text": "lo"}]}}]}
        with serve(lambda *a: ok_json(reply)) as (url, seen):
            p = GeminiProvider("test-model", api_key=SECRET_KEY, base_url=url)
            self.assertEqual(p.complete("hi"), "hello")
        path, headers, body = seen[0]
        self.assertEqual(path, "/v1beta/models/test-model:generateContent")
        lowered = {k.lower(): v for k, v in headers.items()}  # urllib capitalises header names
        self.assertEqual(lowered.get("x-goog-api-key"), SECRET_KEY)
        self.assertNotIn(SECRET_KEY, path)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "hi")

    def test_config_validation(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                GeminiProvider("m", api_key="")
        with self.assertRaises(ValueError):
            GeminiProvider("m", api_key="k", base_url="http://generativelanguage.googleapis.com")
        with self.assertRaises(ValueError):
            GeminiProvider("../evil", api_key="k")

    def test_key_not_leaked_in_repr_or_errors(self):
        with serve(lambda *a: (500, b"oops " + SECRET_KEY.encode(), {})) as (url, _):
            p = GeminiProvider("m", api_key=SECRET_KEY, base_url=url)
            self.assertNotIn(SECRET_KEY, repr(p))
            with self.assertRaises(ProviderError) as ctx:
                p.complete("x")
            self.assertNotIn(SECRET_KEY, str(ctx.exception))

    def test_rate_limit_and_blocked_or_empty_replies(self):
        with serve(lambda *a: (429, b"{}", {})) as (url, _):
            with self.assertRaises(RateLimited):
                GeminiProvider("m", api_key="k", base_url=url).complete("x")
        with serve(lambda *a: ok_json({"promptFeedback": {"blockReason": "SAFETY"}})) as (url, _):
            with self.assertRaises(ProviderError):
                GeminiProvider("m", api_key="k", base_url=url).complete("x")
        with serve(lambda *a: ok_json({"candidates": [{"content": {"parts": [{"text": ""}]}}]})) as (url, _):
            with self.assertRaises(ProviderError):
                GeminiProvider("m", api_key="k", base_url=url).complete("x")


class GatewayIntegrationTests(unittest.TestCase):
    def test_email_flows_to_local_server_through_guard_and_is_ledgered(self):
        with serve(lambda *a: ok_json({"message": {"content": "summary: lunch at 1pm"}})) as (url, seen):
            local = OllamaProvider("m", base_url=url)
            ledger = Ledger()
            guard = EgressGuard(ledger, EgressConfig(allowlist={local.host: Destination.LOCAL}))
            r = LLMGateway(guard, [local]).complete("Summarise: lunch at 1pm", source="gmail")
            self.assertEqual((r.status, r.text), (Status.OK, "summary: lunch at 1pm"))
            self.assertEqual(len(seen), 1)
            self.assertEqual(len(ledger.entries()), 1)
            self.assertTrue(ledger.verify())

    def test_secret_never_hits_the_wire(self):
        with serve(lambda *a: ok_json({"message": {"content": "x"}})) as (url, seen):
            local = OllamaProvider("m", base_url=url)
            guard = EgressGuard(Ledger(), EgressConfig(allowlist={local.host: Destination.LOCAL}))
            r = LLMGateway(guard, [local]).complete("Your OTP is 482913", source="gmail")
            self.assertEqual(r.status, Status.DENIED)
            self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
