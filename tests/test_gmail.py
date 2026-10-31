import base64
import unittest
from datetime import datetime

from edith.broker.egress import EgressConfig, EgressGuard
from edith.broker.gateway import ProviderError
from edith.broker.ledger import Ledger
from edith.connectors.gmail import GmailClient, html_to_text, parse_message
from edith.privacy.sensitivity import Destination
from edith.timeutil import UTC
from tests.test_providers import ok_json, serve


def b64(text, charset="utf-8"):
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def raw_msg(mid="m1", subject="Rent due", sender="Landlord <land@example.com>", parts=None, headers=None,
            internal="1793583000000"):
    hdrs = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Mon, 02 Nov 2026 07:00:00 +0530"}]
    return {"id": mid, "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "Please pay",
            "internalDate": internal,
            "payload": {"mimeType": "multipart/alternative", "headers": headers if headers is not None else hdrs,
                        "parts": parts if parts is not None else
                        [{"mimeType": "text/plain", "body": {"data": b64("Please pay 15000 by 5 Nov.")}}]}}


class ParseTests(unittest.TestCase):
    def test_plain_message(self):
        m = parse_message(raw_msg())
        self.assertEqual((m.id, m.sender, m.subject), ("m1", "Landlord <land@example.com>", "Rent due"))
        self.assertEqual(m.date, datetime(2026, 11, 2, 1, 30, tzinfo=UTC))
        self.assertEqual(m.body, "Please pay 15000 by 5 Nov.")
        self.assertEqual(m.labels, ["INBOX", "UNREAD"])

    def test_html_only_is_converted_and_scripts_dropped(self):
        html = ("<html><head><title>T</title><style>p{color:red}</style></head><body><p>Hello <b>world</b></p>"
                "<script>evil()</script><div>Second&nbsp;line</div></body></html>")
        m = parse_message(raw_msg(parts=[{"mimeType": "text/html", "body": {"data": b64(html)}}]))
        self.assertIn("Hello world", m.body)
        self.assertIn("Second line", m.body)
        for bad in ("evil", "color:red", "<b>"):
            self.assertNotIn(bad, m.body)

    def test_plain_preferred_and_attachments_ignored(self):
        parts = [{"mimeType": "text/html", "body": {"data": b64("<p>html version</p>")}},
                 {"mimeType": "text/plain", "body": {"data": b64("main body")}},
                 {"mimeType": "text/plain", "filename": "x.txt", "body": {"data": b64("attachment text")}}]
        self.assertEqual(parse_message(raw_msg(parts=parts)).body, "main body")

    def test_nested_multipart(self):
        inner = {"mimeType": "multipart/alternative",
                 "parts": [{"mimeType": "text/plain", "body": {"data": b64("deep body")}}]}
        outer = [{"mimeType": "multipart/mixed", "parts": [inner]}]
        self.assertEqual(parse_message(raw_msg(parts=outer)).body, "deep body")

    def test_charset_and_encoded_headers(self):
        part = {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
                "body": {"data": b64("café", "latin-1")}}
        enc = "=?utf-8?b?" + base64.b64encode("Rent due ₹15000".encode()).decode() + "?="
        m = parse_message(raw_msg(subject=enc, parts=[part]))
        self.assertEqual((m.body, m.subject), ("café", "Rent due ₹15000"))
        weird = {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=nonsense-9"}],
                 "body": {"data": b64("still readable")}}
        self.assertEqual(parse_message(raw_msg(parts=[weird])).body, "still readable")

    def test_header_injection_and_length_limits(self):
        m = parse_message(raw_msg(subject="Hi\r\nBcc: attacker@example.com " + "x" * 500))
        self.assertNotIn("\n", m.subject)
        self.assertLessEqual(len(m.subject), 200)
        long_body = [{"mimeType": "text/plain", "body": {"data": b64("a" * 10000)}}]
        self.assertEqual(len(parse_message(raw_msg(parts=long_body)).body), 4000)

    def test_date_fallbacks(self):
        no_date = [{"name": "Subject", "value": "s"}]
        self.assertEqual(parse_message(raw_msg(headers=no_date)).date,
                         datetime.fromtimestamp(1793583000, UTC))
        self.assertIsNone(parse_message(raw_msg(headers=no_date, internal="junk")).date)

    def test_malformed_messages(self):
        for bad in (None, [], {}, {"id": 5}, {"id": "../etc"}, {"id": "a b"}):
            with self.assertRaises(ProviderError, msg=repr(bad)):
                parse_message(bad)
        m = parse_message({"id": "ok1"})  # no payload at all
        self.assertEqual((m.body, m.subject, m.date), ("", "", None))
        junk_headers = parse_message(raw_msg(headers=["x", {"name": "Subject"}, {"name": "Subject", "value": "fine"}]))
        self.assertEqual(junk_headers.subject, "fine")

    def test_html_to_text_survives_garbage(self):
        self.assertEqual(html_to_text("<p>a<p>b"), "a\nb")
        self.assertIsInstance(html_to_text("<<<>>> <div"), str)


def make_client(url, halted=None, token="tok123"):
    host = url.split("//", 1)[1]
    guard = EgressGuard(Ledger(), EgressConfig(allowlist={host: Destination.CONNECTOR}), halted=halted)
    return GmailClient(lambda: token, guard, base_url=url), guard


class ClientTests(unittest.TestCase):
    def responder(self, path, headers, raw):
        if path.startswith("/gmail/v1/users/me/messages?"):
            return ok_json({"messages": [{"id": "m1", "threadId": "t"}, {"id": "m2", "threadId": "t"},
                                         {"id": "../x"}, "junk"]})
        if path.startswith("/gmail/v1/users/me/messages/m1?"):
            return ok_json(raw_msg("m1", "First"))
        if path.startswith("/gmail/v1/users/me/messages/m2?"):
            return ok_json(raw_msg("m2", "Second"))
        return 404, b"{}", {}

    def test_fetch_recent(self):
        with serve(self.responder) as (url, seen):
            client, guard = make_client(url)
            msgs = client.fetch_recent("in:inbox newer_than:2d", 2)
        self.assertEqual([m.subject for m in msgs], ["First", "Second"])
        list_path, headers, _ = seen[0]
        self.assertIn("q=in%3Ainbox+newer_than%3A2d", list_path)
        self.assertIn("maxResults=2", list_path)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["authorization"], "Bearer tok123")
        self.assertTrue(guard.ledger.verify())
        self.assertEqual(len(guard.ledger.entries()), 3)
        for e in guard.ledger.entries():
            self.assertNotIn("tok123", e.preview)

    def test_limit_is_clamped_and_query_cleaned(self):
        with serve(self.responder) as (url, seen):
            client, _ = make_client(url)
            client.list_ids("from:bank\r\nbcc:x", 1000)
        self.assertIn("maxResults=25", seen[0][0])
        self.assertNotIn("%0D", seen[0][0])

    def test_input_validation(self):
        client, _ = make_client("http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            client.get("../etc/passwd")
        with self.assertRaises(ValueError):
            GmailClient(lambda: "t", EgressGuard(Ledger()), base_url="http://gmail.googleapis.com")

    def test_policy_block_makes_no_request(self):
        with serve(self.responder) as (url, seen):
            client, _ = make_client(url, halted=lambda: True)   # kill switch engaged
            with self.assertRaises(ProviderError) as ctx:
                client.list_ids()
        self.assertEqual(str(ctx.exception), "blocked_by_policy")
        self.assertEqual(seen, [])

    def test_auth_failure_is_a_provider_error(self):
        with serve(lambda *a: (401, b"{}", {})) as (url, _):
            client, _ = make_client(url)
            with self.assertRaises(ProviderError) as ctx:
                client.list_ids()
        self.assertEqual(str(ctx.exception), "http_401")


if __name__ == "__main__":
    unittest.main()
