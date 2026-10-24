"""habeshamarketresearch.com: static pages plus two small endpoints.

GET  /api/posts    The newest three letters from the Substack RSS feed, newest
                   first, so the home page never goes stale. Cached for ten
                   minutes; if Substack can't be reached the last good copy is
                   served, and the page falls back to the letters baked into
                   index.html.
GET  /api/config   The Stripe Payment Links (the research deposit and the
                   Ethiopia report pre-orders), read from the environment, so
                   the buttons can be switched on in Railway without a code
                   change.
POST /api/request  A personal market research request from the form on the
                   home page. Emailed to REQUEST_TO over SMTP when SMTP_* is
                   set, and always written to the service log.
POST /api/subscribe  A sign-up for the letter from the form on the home page,
                   so nobody has to go through Substack's own sign-up or
                   sign-in pages (whose rate limit left readers on a bare
                   "Too Many Requests" page). Written to the service log and
                   emailed to REQUEST_TO when SMTP_* is set; the reader's
                   browser then hands the address to Substack, which sends the
                   letter, with the request Substack's own embed form makes.
                   The limits are generous (SIGNUP_IP_MAX a network in ten
                   minutes, SIGNUP_ALL_MAX in an hour) and a refusal says how
                   long to wait (Retry-After, retry_after), which the form
                   counts down.

Standard library only: python:3.12-alpine runs it with nothing installed.
"""

import collections
import html
import json
import math
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
    "other": "Something else",
}
FIELD_LIMITS = {"name": 120, "email": 200, "kind": 20, "topic": 4000, "deadline": 120, "budget": 120}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_last_post_by_ip = {}

def _env_count(name, default):
    """A positive whole number from the environment; the default when it isn't one."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


# Sign-ups for the letter. A whole office, a school or a family can share one
# address, so a network may sign up many times before it is asked to wait;
# the limit is there to keep a script from filling the owner's inbox.
SIGNUP_IP_MAX = _env_count("SIGNUP_IP_MAX", 20)     # one network, in SIGNUP_IP_WINDOW
SIGNUP_IP_WINDOW = 600                               # ten minutes
SIGNUP_ALL_MAX = _env_count("SIGNUP_ALL_MAX", 300)  # everyone, in SIGNUP_ALL_WINDOW
SIGNUP_ALL_WINDOW = 3600                             # an hour
SIGNUP_EMAILS_A_DAY = 50      # emails to the owner about sign-ups in a UTC day; the log keeps the rest
SIGNUP_REPEAT_SECONDS = 86400  # the same address again within a day is not emailed again


class Window:
    """At most `limit` events per `seconds` for each key (a sliding window)."""

    def __init__(self, limit, seconds):
        self.limit, self.seconds = limit, seconds
        self.hits = {}

    def wait(self, key, now):
        """Seconds until one more event for `key` fits; 0 when it fits now."""
        q = self.hits.get(key)
        if not q:
            return 0
        while q and now - q[0] >= self.seconds:
            q.popleft()
        if len(q) < self.limit:
            return 0
        return max(1, math.ceil(self.seconds - (now - q[0])))

    def add(self, key, now):
        self.hits.setdefault(key, collections.deque()).append(now)
        if len(self.hits) > 5000:  # forget the networks that have gone quiet
            for k in [k for k, q in self.hits.items() if not q or now - q[-1] >= self.seconds]:
                del self.hits[k]


class Signups:
    """The sign-up limits, and which addresses the owner has already been told about."""

    def __init__(self, ip_max=SIGNUP_IP_MAX, all_max=SIGNUP_ALL_MAX):
        self.lock = threading.Lock()
        self.by_ip = Window(ip_max, SIGNUP_IP_WINDOW)
        self.everyone = Window(all_max, SIGNUP_ALL_WINDOW)
        self.told = {}                      # address -> when the owner was emailed about it
        self.day, self.emails_today = "", 0

    def admit(self, ip, now=None):
        """Count a sign-up from `ip`: 0 when it may go ahead, else the seconds to wait (nothing counted)."""
        now = time.time() if now is None else now
        with self.lock:
            wait = max(self.by_ip.wait(ip, now), self.everyone.wait("*", now))
            if wait:
                return wait
            self.by_ip.add(ip, now)
            self.everyone.add("*", now)
            return 0

    def should_email(self, address, now=None):
        """Whether to email the owner about `address`: once a day an address, SIGNUP_EMAILS_A_DAY in all."""
        now = time.time() if now is None else now
        key = address.lower()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.lock:
            if day != self.day:
                self.day, self.emails_today = day, 0
                self.told = {k: t for k, t in self.told.items() if now - t < SIGNUP_REPEAT_SECONDS}
            if now - self.told.get(key, -SIGNUP_REPEAT_SECONDS) < SIGNUP_REPEAT_SECONDS:
                return False
            if self.emails_today >= SIGNUP_EMAILS_A_DAY:
                return False
            self.told[key] = now
            self.emails_today += 1
            return True


SIGNUPS = Signups()


def wait_words(seconds):
    """'45 seconds', '1 minute', '3 minutes': how long a refused reader waits."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


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


def send_mail(subject, body, reply_to=""):
    """Email the owner (REQUEST_TO, else SMTP_USER) over SMTP; False when SMTP_* isn't set."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("REQUEST_TO", user or "")
    if not (host and user and password and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Reply-To"] = reply_to or user
    msg.set_content(body)
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


def send_request_email(fields):
    kind = REQUEST_KINDS.get(fields.get("kind"), "Request")
    lines = [f"{k}: {fields[k]}" for k in ("name", "email", "kind", "deadline", "budget") if fields.get(k)]
    lines += ["", fields.get("topic", "")]
    return send_mail(f"[habeshamarketresearch.com] {kind}: {fields.get('name', '')}", "\n".join(lines),
                     reply_to=fields.get("email", ""))


def send_signup_email(address, when):
    body = "\n".join([
        f"{address} signed up for the letter on habeshamarketresearch.com ({when} UTC).",
        "",
        "Their browser also sent the address to Substack, the way Substack's own sign-up form does,",
        "so they should be in your subscriber list in a minute. If they aren't, add them by hand in",
        "Substack: Subscribers, then Add subscribers.",
    ])
    return send_mail(f"[habeshamarketresearch.com] New subscriber: {address}", body, reply_to=address)


def tell_owner_about_signup(address, when):
    """Email the owner in the background, so the reader isn't kept waiting on SMTP."""
    def run():
        try:
            send_signup_email(address, when)
        except Exception as exc:
            print(f"[subscribe] email failed: {exc!r}", file=sys.stderr, flush=True)
    threading.Thread(target=run, name="signup-email", daemon=True).start()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _json(self, status, payload, cache="no-store", headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", cache)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        """The address Railway's edge saw: X-Real-IP, else the last X-Forwarded-For entry (the
        first ones are whatever the client sent, so they can't key a limit), else the socket's."""
        real = (self.headers.get("X-Real-IP") or "").strip()
        if real:
            return real
        hops = [h.strip() for h in (self.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
        return hops[-1] if hops else self.client_address[0]

    def _read_json(self, limit):
        """The JSON body as a dict, or None after answering 400."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            self._json(400, {"ok": False, "error": "Empty or too long."})
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "Bad request."})
            return None
        return data

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
                "ethiopiaBusinessUrl": os.environ.get("STRIPE_ETHIOPIA_BUSINESS_URL", ""),
                "ethiopiaRealEstateUrl": os.environ.get("STRIPE_ETHIOPIA_REAL_ESTATE_URL", ""),
                "requestsByEmail": bool(os.environ.get("SMTP_HOST")),
            })
        # Only the public files are served.
        if path.startswith("/.") or path.endswith((".py", "Dockerfile")):
            self.send_error(404)
            return
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/subscribe":
            return self._subscribe()
        if path != "/api/request":
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
        if len(fields["topic"]) < 10:
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

    def _subscribe(self):
        data = self._read_json(4000)
        if data is None:
            return
        if data.get("website"):  # the honeypot: people leave it empty
            return self._json(200, {"ok": True})
        address = str(data.get("email", "")).strip()
        if len(address) > 200 or not _EMAIL_RE.match(address):
            return self._json(400, {"ok": False, "error": "Please enter a valid email address, like name@example.com."})
        wait = SIGNUPS.admit(self._client_ip())
        if wait:
            return self._json(429, {
                "ok": False,
                "retry_after": wait,
                "error": f"Too many sign-ups from your network just now. Please try again in {wait_words(wait)}.",
            }, headers={"Retry-After": str(wait)})
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        print("[subscribe] " + json.dumps({"email": address, "at": when}, ensure_ascii=False), flush=True)
        if SIGNUPS.should_email(address):
            tell_owner_about_signup(address, when)
        return self._json(200, {"ok": True})

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
