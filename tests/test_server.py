"""The sign-up endpoint and its limits. Standard library only:

    python -m unittest discover -s tests
"""

import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "REQUEST_TO"):
    os.environ.pop(name, None)  # never send mail from a test

import server  # noqa: E402


class LimitsTest(unittest.TestCase):
    def test_a_network_may_sign_up_many_times_then_is_told_how_long_to_wait(self):
        s = server.Signups(ip_max=3, all_max=100)
        self.assertEqual([s.admit("1.1.1.1", now=1000 + i) for i in range(3)], [0, 0, 0])
        self.assertEqual(s.admit("1.1.1.1", now=1010), 600 - 10)  # the first one leaves the window at 1600
        self.assertEqual(s.admit("2.2.2.2", now=1010), 0)          # another network is not held up
        self.assertEqual(s.admit("1.1.1.1", now=1600), 0)          # the window has moved on

    def test_a_refusal_counts_nothing(self):
        s = server.Signups(ip_max=1, all_max=100)
        self.assertEqual(s.admit("1.1.1.1", now=0), 0)
        for t in range(1, 50):
            self.assertGreater(s.admit("1.1.1.1", now=t), 0)
        self.assertEqual(s.admit("1.1.1.1", now=600), 0)

    def test_everyone_together_has_a_ceiling(self):
        s = server.Signups(ip_max=100, all_max=2)
        self.assertEqual(s.admit("1.1.1.1", now=0), 0)
        self.assertEqual(s.admit("2.2.2.2", now=1), 0)
        self.assertEqual(s.admit("3.3.3.3", now=2), 3600 - 2)  # until the first leaves the hour

    def test_the_owner_hears_about_an_address_once_a_day(self):
        s = server.Signups()
        self.assertTrue(s.should_email("Reader@Example.com", now=0))
        self.assertFalse(s.should_email("reader@example.com", now=60))
        self.assertTrue(s.should_email("reader@example.com", now=server.SIGNUP_REPEAT_SECONDS + 1))

    def test_the_owner_gets_at_most_so_many_emails_a_day(self):
        s = server.Signups()
        sent = [s.should_email(f"r{i}@example.com", now=10 + i) for i in range(server.SIGNUP_EMAILS_A_DAY + 5)]
        self.assertEqual(sum(sent), server.SIGNUP_EMAILS_A_DAY)
        self.assertTrue(s.should_email("next-day@example.com", now=86400 + 10))

    def test_wait_words(self):
        self.assertEqual(server.wait_words(1), "1 second")
        self.assertEqual(server.wait_words(45), "45 seconds")
        self.assertEqual(server.wait_words(60), "1 minute")
        self.assertEqual(server.wait_words(61), "2 minutes")
        self.assertEqual(server.wait_words(600), "10 minutes")


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.saved = server.SIGNUPS
        server.SIGNUPS = server.Signups(ip_max=2, all_max=100)
        server._last_post_by_ip.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.SIGNUPS = self.saved

    def post(self, path, body, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        data = raw if raw is not None else json.dumps(body).encode()
        conn.request("POST", path, data, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def test_a_sign_up_is_kept(self):
        status, _, body = self.post("/api/subscribe", {"email": "reader@example.com"})
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_a_bad_address_is_refused_in_words(self):
        status, _, body = self.post("/api/subscribe", {"email": "not an email"})
        self.assertEqual(status, 400)
        self.assertIn("valid email", body["error"])
        self.assertEqual(self.post("/api/subscribe", None, raw=b"[1]")[0], 400)
        self.assertEqual(self.post("/api/subscribe", None, raw=b"x" * 5000)[0], 400)

    def test_the_honeypot_is_answered_but_not_counted(self):
        for _ in range(5):
            self.assertEqual(self.post("/api/subscribe", {"email": "bot@example.com", "website": "x"})[0], 200)
        self.assertEqual(self.post("/api/subscribe", {"email": "reader@example.com"})[0], 200)

    def test_too_many_says_when_to_try_again(self):
        for i in range(2):
            self.assertEqual(self.post("/api/subscribe", {"email": f"r{i}@example.com"})[0], 200)
        status, headers, body = self.post("/api/subscribe", {"email": "r3@example.com"})
        self.assertEqual(status, 429)
        self.assertFalse(body["ok"])
        self.assertGreater(body["retry_after"], 0)
        self.assertEqual(headers["Retry-After"], str(body["retry_after"]))
        self.assertIn("try again in", body["error"])

    def test_the_request_form_is_unchanged(self):
        status, _, body = self.post("/api/request", {
            "name": "A reader", "email": "a@example.com", "kind": "research", "topic": "A topic long enough"})
        self.assertEqual((status, body), (200, {"ok": True, "emailed": False}))


if __name__ == "__main__":
    unittest.main()
