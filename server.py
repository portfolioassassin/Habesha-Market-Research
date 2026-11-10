"""habeshamarketresearch.com: static pages plus two small endpoints.

GET  /              The home page, with the newest three letters written into
                   it before it is sent (between the "letters" marks in
                   index.html), so a visitor sees them the moment the page
                   opens, with or without scripts.
GET  /api/posts    The same three letters as JSON, newest first. The page asks
                   for them once it has loaded, in case a browser is holding
                   an older copy of the page.

                   The letters are read from the Substack RSS feed by a thread
                   of their own: at start, then every five minutes, and a
                   minute after a read that failed. No request waits on
                   Substack. If Substack can't be reached the last good copy
                   stands, and before any read has worked the page shows the
                   letters written into index.html.
GET  /api/config   The Stripe Payment Links (the research deposit and the
                   Ethiopia report pre-orders), read from the environment, so
                   the buttons can be switched on in Railway without a code
                   change.
POST /api/request  A personal market research request from the form on the
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
REFRESH_SECONDS = 300          # how often the feed is read
RETRY_SECONDS = 60             # how soon after a read that failed
POSTS_SHOWN = 3
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

_cache = {"at": 0.0, "posts": None, "tried": 0.0, "failed": ""}
_cache_lock = threading.Lock()
_page = {"mtime": None, "html": ""}

# A request carries these fields; anything else is ignored.
REQUEST_KINDS = {
    "research": "Personal market research",
    "other": "Something else",
}
FIELD_LIMITS = {"name": 120, "email": 200, "kind": 20, "topic": 4000, "deadline": 120, "budget": 120}
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
        if not _text(item, "link").startswith("https://"):
            continue                       # only a letter that can be linked to is shown
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


def refresh_posts():
    """Read the feed once. True when the letters on file are now fresh."""
    try:
        posts = fetch_posts()
        if not posts:
            raise ValueError("the feed has no letters")
    except Exception as exc:  # network, XML: keep serving what we had
        print(f"[posts] feed read failed: {exc!r}", file=sys.stderr, flush=True)
        with _cache_lock:
            _cache.update(tried=time.time(), failed=type(exc).__name__)
        return False
    with _cache_lock:
        changed = posts != _cache["posts"]
        _cache.update(at=time.time(), tried=time.time(), failed="", posts=posts)
    if changed:
        print("[posts] newest: " + json.dumps([p["title"] for p in posts], ensure_ascii=False), flush=True)
    return True


def keep_posts_fresh():
    """The feed's own thread: no request ever waits on Substack."""
    while True:
        ok = refresh_posts()
        time.sleep(REFRESH_SECONDS if ok else RETRY_SECONDS)


def latest_posts():
    with _cache_lock:
        return _cache["posts"]


def long_date(iso):
    parts = (iso or "").split("-")
    if len(parts) != 3:
        return ""
    try:
        return f"{MONTHS[int(parts[1]) - 1]} {int(parts[2])}, {parts[0]}"
    except (ValueError, IndexError):
        return ""


def _between(page, name, inner):
    """Replace what stands between <!-- name:start --> and <!-- name:end -->."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    a, b = page.find(start), page.find(end)
    if a < 0 or b < a:
        return page
    return page[: a + len(start)] + inner + page[b:]


def letters_html(posts):
    rows = []
    for p in posts:
        link = p.get("link", "")
        if not link.startswith("https://"):
            continue
        esc = lambda v: html.escape(v or "", quote=True)
        desc = f'\n          <span class="post-desc">{esc(p.get("description"))}</span>' if p.get("description") else ""
        rows.append(
            f'\n      <li class="post">\n        <a href="{esc(link)}" target="_blank" rel="noopener">'
            f'\n          <time class="post-kind" datetime="{esc(p.get("date"))}">{esc(long_date(p.get("date")))}</time>'
            f'\n          <span class="post-title">{esc(p.get("title"))}</span>'
            f'\n          <span class="post-go">Read on Substack</span>{desc}'
            f'\n        </a>\n      </li>')
    return "".join(rows) + "\n      " if rows else ""


def home_page():
    """index.html with the newest letters written in. The file is read again when it changes."""
    path = os.path.join(ROOT, "index.html")
    mtime = os.path.getmtime(path)
    if _page["mtime"] != mtime:
        with open(path, encoding="utf-8") as f:
            _page.update(mtime=mtime, html=f.read())
    page, posts = _page["html"], latest_posts()
    rows = letters_html(posts or [])
    if not rows:
        return page                       # nothing read yet: the letters in the file stand
    first = next(p for p in posts if p.get("link", "").startswith("https://"))
    latest = (f'<a class="text-link" id="latestLink" href="{html.escape(first["link"], quote=True)}" '
              f'target="_blank" rel="noopener">{html.escape(first.get("title", ""))}</a>')
    return _between(_between(page, "letters", rows), "latest", latest)


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
    lines = [f"{k}: {fields[k]}" for k in ("name", "email", "kind", "deadline", "budget") if fields.get(k)]
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

    def _home(self, head=False):
        body = home_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")     # the letters in it change
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            return self._home(head=True)
        return super().do_HEAD()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._home()
        if path == "/api/posts":
            posts = latest_posts()
            if posts is None:
                return self._json(502, {"posts": [], "error": "feed unavailable"})
            return self._json(200, {"posts": posts}, cache="public, max-age=60")
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

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main():
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=keep_posts_fresh, name="letters", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"serving {ROOT} on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
