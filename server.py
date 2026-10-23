"""habeshamarketresearch.com: static pages plus two small endpoints.

GET  /api/posts    The newest three letters from the Substack RSS feed, newest
                   first, so the home page never goes stale. Cached for ten
                   minutes; if Substack can't be reached the last good copy is
                   served, and the page falls back to the letters baked into
                   index.html.
GET  /api/config   The Stripe Payment Links, read from the environment, so the
                   deposit buttons can be switched on in Railway without a
                   code change.
POST /api/request  A research, article or report request from the form on the
                   home page. Emailed to REQUEST_TO over SMTP when SMTP_* is
                   set, and always written to the service log.

Standard library only: python:3.12-alpine runs it with nothing installed.
"""

import html
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
FEED_URL = os.environ.get("SUBSTACK_FEED", "https://thehabeshainvestor.substack.com/feed")
CACHE_SECONDS = 600
POSTS_SHOWN = 3

_cache = {"at": 0.0, "posts": None}
_cache_lock = threading.Lock()

# A request carries these fields; anything else is ignored.
REQUEST_KINDS = {
    "research": "Personal market research",
    "article": "An article written for you",
    "report": "Ethiopia report (waitlist)",
    "other": "Something else",
}
FIELD_LIMITS = {"name": 120, "email": 200, "kind": 20, "topic": 4000, "deadline": 120, "budget": 120, "report": 200}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_last_post_by_ip = {}


def _text(el, tag):
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _plain(s, limit=240):
    s = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def fetch_posts():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "habeshamarketresearch.com (+https://www.habeshamarketresearch.com)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        root = ET.fromstring(resp.read())
    posts = []
    for item in root.iter("item"):
        when = _text(item, "pubDate")
        try:
            dt = parsedate_to_datetime(when)
            iso, stamp = dt.date().isoformat(), dt.timestamp()
        except (TypeError, ValueError):
            iso, stamp = "", 0
        enclosure = item.find("enclosure")
        posts.append({
            "title": _plain(_text(item, "title"), 200),
            "link": _text(item, "link"),
            "date": iso,
            "ts": stamp,
            "description": _plain(_text(item, "description")),
            "image": enclosure.get("url", "") if enclosure is not None else "",
        })
    posts.sort(key=lambda p: p["ts"], reverse=True)
    for p in posts:
        p.pop("ts", None)
    return posts[:POSTS_SHOWN]


def latest_posts():
    with _cache_lock:
        fresh = _cache["posts"] is not None and time.time() - _cache["at"] < CACHE_SECONDS
        if fresh:
            return _cache["posts"]
    try:
        posts = fetch_posts()
    except Exception as exc:  # network, XML: keep serving what we had
        print(f"[posts] feed fetch failed: {exc!r}", file=sys.stderr, flush=True)
        with _cache_lock:
            return _cache["posts"]
    with _cache_lock:
        _cache.update(at=time.time(), posts=posts)
    return posts


def send_request_email(fields):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("REQUEST_TO", user or "")
    if not (host and user and password and to):
        return False
    msg = EmailMessage()
    kind = REQUEST_KINDS.get(fields.get("kind"), "Request")
    msg["Subject"] = f"[habeshamarketresearch.com] {kind}: {fields.get('name', '')}"
    msg["From"] = user
    msg["To"] = to
    msg["Reply-To"] = fields.get("email", user)
    lines = [f"{k}: {fields[k]}" for k in ("name", "email", "kind", "report", "deadline", "budget") if fields.get(k)]
    lines += ["", fields.get("topic", "")]
    msg.set_content("\n".join(lines))
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)
    return True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _json(self, status, payload, cache="no-store"):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/posts":
            posts = latest_posts()
            if posts is None:
                return self._json(502, {"posts": [], "error": "feed unavailable"})
            return self._json(200, {"posts": posts}, cache="public, max-age=300")
        if path == "/api/config":
            return self._json(200, {
                "researchDepositUrl": os.environ.get("STRIPE_RESEARCH_DEPOSIT_URL", ""),
                "articleDepositUrl": os.environ.get("STRIPE_ARTICLE_DEPOSIT_URL", ""),
                "requestsByEmail": bool(os.environ.get("SMTP_HOST")),
            })
        # Only the public files are served.
        if path.startswith("/.") or path.endswith((".py", "Dockerfile")):
            self.send_error(404)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/request":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 20000:
            return self._json(400, {"ok": False, "error": "Empty or too long."})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "Bad request."})
        if data.get("website"):  # the honeypot: people leave it empty
            return self._json(200, {"ok": True})
        fields = {k: str(data.get(k, "")).strip()[:n] for k, n in FIELD_LIMITS.items()}
        if not fields["name"] or not _EMAIL_RE.match(fields["email"]) or fields["kind"] not in REQUEST_KINDS:
            return self._json(400, {"ok": False, "error": "Please give your name, a valid email and what you need."})
        if fields["kind"] != "report" and len(fields["topic"]) < 10:
            return self._json(400, {"ok": False, "error": "Please describe the topic in a sentence or two."})
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        now = time.time()
        if now - _last_post_by_ip.get(ip, 0) < 20:
            return self._json(429, {"ok": False, "error": "Please wait a moment before sending again."})
        _last_post_by_ip[ip] = now
        print("[request] " + json.dumps(fields, ensure_ascii=False), flush=True)
        try:
            emailed = send_request_email(fields)
        except Exception as exc:
            print(f"[request] email failed: {exc!r}", file=sys.stderr, flush=True)
            emailed = False
        return self._json(200, {"ok": True, "emailed": emailed})

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving {ROOT} on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
