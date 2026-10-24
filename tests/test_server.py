"""The request form's endpoint. Standard library only:

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

GOOD = {"name": "A reader", "email": "a@example.com", "kind": "research", "topic": "A topic long enough"}


class RequestTest(unittest.TestCase):
    def setUp(self):
        server._last_post_by_ip.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        conn.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw.startswith(b"{") else raw

    def test_a_request_is_taken_and_says_it_wasnt_emailed_without_smtp(self):
        self.assertEqual(self.post("/api/request", GOOD), (200, {"ok": True, "emailed": False}))

    def test_a_request_without_a_name_or_topic_is_refused_in_words(self):
        status, body = self.post("/api/request", {**GOOD, "name": ""})
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"])
        status, body = self.post("/api/request", {**GOOD, "topic": "short"})
        self.assertEqual(status, 400)

    def test_the_same_address_waits_a_moment_between_requests(self):
        self.assertEqual(self.post("/api/request", GOOD)[0], 200)
        self.assertEqual(self.post("/api/request", GOOD)[0], 429)

    def test_only_the_request_form_takes_a_post(self):
        self.assertEqual(self.post("/api/subscribe", {"email": "a@example.com"})[0], 404)


if __name__ == "__main__":
    unittest.main()
