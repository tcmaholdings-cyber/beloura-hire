"""OnlineJobs.ph for James: the employer-side client, the applicant ingest, the reply path and the notification-email intake.

Spec: ~/Documents/claudedocs/chatter-hiring-agent/olj-api-re.md (reverse-engineered 2026-11-04). Nothing here has run against the live
site yet; everything was built against recorded fixtures. OLJ's terms forbid automated use without permission, so the client is paced like a
person (2-5 s between calls, never more than one a second), pulls inbound applicants only, and is READ-ONLY unless OLJ_SEND=1.

  login(email, password)         GET /login -> csrf-token -> POST /authenticate -> GET v2 /myaccount -> accessToken; saves data/olj_session.json (0600).
                                 The login form carries a reCAPTCHA v2 checkbox, so this raises CaptchaRequired unless force_captcha=True; the
                                 fallback is import_cookies() with a Netscape cookie file exported from the browser (tools/olj_cli.py login --cookies).
  Api(session)                   httpx over https://api.onlinejobs.ph/api/v1 with the Bearer token, the cookie jar, X-ONLINEJOBSPH on /jobs calls,
                                 pacing, one automatic re-auth on 401 (cookie refresh of the token, then the reauth callable if given).
  sync_inbox(con, api)           unread inbox threads + threads of the open posts -> applicant (channel_code olj) + application (olj_* columns) +
                                 inbound message on channel 'olj' + olj_thread ledger. Dedupe on thread hash, then email / partner id. Sends nothing.
  send_reply(con, aid, text)     POST /message/add on the applicant's thread when OLJ_SEND=1, otherwise a 'held' message row with a note.
  move_to_telegram_text(...)     the first message: apply link + t.me deep link (links are fine in messages, not in posts).
  parse_olj_email(raw)           OLJ's "new application" notification email -> name, email, job title, message, profile URL. ingest_email() feeds the
                                 same upsert; install_email_hook() wraps channels.EmailChannel.handle_message so the IMAP poll routes them here.
  ensure_schema(con)             tables, columns and settings; called from db.init(). Settings: olj_sync_enabled (0), olj_mark_read (0).
  Worker: a thread started at import unless HIRE_NO_WORKER=1; syncs every OLJ_SYNC_SECONDS (600) while olj_sync_enabled=1 and a session exists.
"""
from __future__ import annotations

import email
import email.utils
import hashlib
import html as html_mod
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.message import Message

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import channels
import db
import engine
import texts

log = logging.getLogger("hire.olj")

HERE = os.path.dirname(os.path.abspath(__file__))
WWW = "https://www.onlinejobs.ph"
V2 = "https://v2.onlinejobs.ph"
API = "https://api.onlinejobs.ph"
API_PREFIX = "/api/v1"
JOBS_HEADER = ("X-ONLINEJOBSPH", "769cf2c8-b345-4bda-b848-68c2626b6416")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
CHANNEL = "olj"
DEFAULT_EMAIL = "employer@example.com"
KEYCHAIN_ITEM = "olj-login"
OUR_DOMAINS = ("onlinejobs.ph", "beloura.ai")
DEFAULT_SETTINGS = {"olj_sync_enabled": "0", "olj_mark_read": "0", "olj_last_sync_at": "", "olj_last_attempt_at": "", "olj_last_sync_notes": "",
                    "olj_last_sync_error": "", "olj_apply_code": "olj"}


def session_path() -> str:
    return os.environ.get("OLJ_SESSION_PATH") or os.path.join(HERE, "data", "olj_session.json")


def send_enabled() -> bool:
    return os.environ.get("OLJ_SEND", "0") == "1"


def sync_seconds() -> int:
    try:
        return max(60, int(os.environ.get("OLJ_SYNC_SECONDS", "600") or 600))
    except ValueError:
        return 600


class OljError(Exception):
    pass


class CaptchaRequired(OljError):
    pass


class LoginFailed(OljError):
    pass


class SessionExpired(OljError):
    pass


class ReadOnly(OljError):
    pass


# ---------------------------------------------------------------- session file

def load_session(path: str | None = None) -> dict | None:
    p = path or session_path()
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) and s.get("accessToken") else None


def save_session(sess: dict, path: str | None = None) -> str:
    p = path or session_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sess, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p


def session_status(path: str | None = None) -> dict:
    """What the staff page shows: never the token or cookie values."""
    p = path or session_path()
    s = load_session(p)
    if not s:
        return {"present": False, "path": p, "token_present": False, "fetched_at": "", "cookies": 0, "user": {}, "age_hours": None}
    fetched = db.parse_ts(s.get("fetched_at"))
    age = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600 if fetched else None
    u = s.get("user") or {}
    return {"present": True, "path": p, "token_present": bool(s.get("accessToken")), "fetched_at": s.get("fetched_at", ""),
            "cookies": len(s.get("cookies") or []), "user": {"name": u.get("name", ""), "email": u.get("email", "")}, "age_hours": age,
            "login_email": s.get("email", "")}


def _debug_dump(label: str, text: str) -> str:
    d = os.path.join(os.path.dirname(session_path()), "")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"olj_debug_{re.sub(r'[^a-z0-9]+', '-', label.lower())}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0o600)
    return p


# ---------------------------------------------------------------- HTML extractors (pure)

def extract_csrf(html: str) -> str | None:
    for pat in (r'<input[^>]+name=["\']csrf-token["\'][^>]+value=["\']([^"\']+)["\']', r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']',
                r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None


def login_form(html: str) -> str:
    """The /authenticate form's HTML, or the whole page when the form is not found."""
    m = re.search(r'<form[^>]+action=["\'][^"\']*/authenticate["\'][^>]*>(.*?)</form>', html, re.I | re.S)
    return m.group(1) if m else html


def hidden_fields(html: str) -> dict:
    out = {}
    for tag in re.findall(r'<input[^>]*type=["\']hidden["\'][^>]*>', login_form(html), re.I):
        n = re.search(r'name=["\']([^"\']+)["\']', tag)
        v = re.search(r'value=["\']([^"\']*)["\']', tag)
        if n:
            out[n.group(1)] = html_mod.unescape(v.group(1)) if v else ""
    return out


def has_captcha(html: str) -> bool:
    return bool(re.search(r"g-recaptcha|h-captcha|cf-turnstile|recaptcha/api\.js|data-sitekey", login_form(html), re.I))


_FLIGHT = re.compile(r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:\\.|[^"\\])*)"\s*\]\)', re.S)


def extract_flight_text(html: str) -> str:
    """Decode every self.__next_f.push([1,"..."]) string of a Next.js RSC page into one text."""
    parts = []
    for raw in _FLIGHT.findall(html):
        try:
            parts.append(json.loads('"' + raw + '"'))
        except ValueError:
            try:
                parts.append(raw.encode("utf-8", "replace").decode("unicode_escape", "replace"))
            except Exception:  # noqa: BLE001
                parts.append(raw)
    return "\n".join(parts)


def extract_access_token(html: str) -> str | None:
    decoded = extract_flight_text(html)
    for text in (decoded, html):
        for pat in (r'"accessToken"\s*:\s*"([^"]+)"', r'accessToken\\"\s*:\s*\\"([^"\\]+)', r"accessToken['\"]?\s*[:=]\s*['\"]([A-Za-z0-9._\-|]{16,})"):
            m = re.search(pat, text)
            if m and m.group(1) not in ("", "null", "undefined"):
                return m.group(1)
    return None


def _balanced(text: str, start: int) -> str | None:
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def extract_user(html: str) -> dict:
    decoded = extract_flight_text(html)
    for m in re.finditer(r'"user"\s*:\s*\{', decoded):
        chunk = _balanced(decoded, m.end() - 1)
        if not chunk:
            continue
        try:
            d = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(d, dict) and ("email" in d or "name" in d):
            return {k: d.get(k) for k in ("id", "name", "email", "unreadMessagesCount", "context", "contextHashId", "employerType") if k in d}
    return {}


# ---------------------------------------------------------------- pacing

class Pacer:
    """A random 2-5 s gap between calls and never more than one call a second, whatever the env says."""

    def __init__(self, min_gap: float | None = None, max_gap: float | None = None, *, sleep=time.sleep, clock=time.monotonic, rand=random.uniform):
        lo = float(min_gap if min_gap is not None else (os.environ.get("OLJ_MIN_GAP") or 2))
        hi = float(max_gap if max_gap is not None else (os.environ.get("OLJ_MAX_GAP") or 5))
        self.min_gap = max(1.0, lo)
        self.max_gap = max(self.min_gap, hi)
        self.sleep, self.clock, self.rand = sleep, clock, rand
        self.last: float | None = None
        self.calls = 0

    def wait(self) -> float:
        waited = 0.0
        if self.last is not None:
            due = self.last + self.rand(self.min_gap, self.max_gap)
            now = self.clock()
            if now < due:
                waited = due - now
                self.sleep(waited)
        self.last = self.clock()
        self.calls += 1
        return waited


# ---------------------------------------------------------------- cookies

def cookies_to_list(jar: httpx.Cookies) -> list[dict]:
    out = []
    for c in jar.jar:
        out.append({"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/", "expires": c.expires, "secure": bool(c.secure)})
    return out


def cookies_from_list(items: list[dict] | None) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in items or []:
        if not c.get("name"):
            continue
        domain = str(c.get("domain") or "www.onlinejobs.ph")
        if domain.startswith("#HttpOnly_"):
            domain = domain[len("#HttpOnly_"):]
        jar.set(c["name"], str(c.get("value", "")), domain=domain, path=c.get("path") or "/")
    return jar


def parse_netscape_cookies(text: str) -> list[dict]:
    """A browser-exported cookies.txt (Netscape format, '#HttpOnly_' prefixes allowed)."""
    out = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        try:
            exp = int(expires) or None
        except ValueError:
            exp = None
        out.append({"name": name, "value": value, "domain": domain, "path": path or "/", "expires": exp, "secure": secure.upper() == "TRUE"})
    return out


# ---------------------------------------------------------------- login and token

def _client(transport=None, cookies: httpx.Cookies | None = None, follow: bool = True) -> httpx.Client:
    return httpx.Client(transport=transport, cookies=cookies, follow_redirects=follow, timeout=30.0,
                        headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})


def fetch_access_token(client: httpx.Client, pacer: Pacer) -> tuple[str | None, dict, str]:
    pacer.wait()
    r = client.get(V2 + "/myaccount", headers={"Accept": "text/html,application/xhtml+xml"})
    if "/login" in str(r.url):
        raise SessionExpired("v2 /myaccount redirected to the login page: the cookie session is not logged in")
    html = r.text
    return extract_access_token(html), extract_user(html), html


def login(email_addr: str, password: str, *, transport=None, pacer: Pacer | None = None, path: str | None = None, force_captcha: bool = False) -> dict:
    """Full login. Never logs the password or the token. Saves and returns the session."""
    pacer = pacer or Pacer()
    with _client(transport) as client:
        pacer.wait()
        r = client.get(WWW + "/login", headers={"Accept": "text/html,application/xhtml+xml"})
        page = r.text
        if has_captcha(page) and not force_captcha:
            p = _debug_dump("login-captcha", page)
            raise CaptchaRequired(f"the login form shows a captcha (saved to {p}). Log in with the browser, export cookies.txt and run "
                                  "tools/olj_cli.py login --cookies <file>; or retry with --force to post without a captcha answer.")
        csrf = extract_csrf(page)
        if not csrf:
            p = _debug_dump("login-nocsrf", page)
            raise LoginFailed(f"csrf-token not found on /login (saved to {p})")
        data = {**hidden_fields(page), "csrf-token": csrf, "info[email]": email_addr, "info[password]": password}
        pacer.wait()
        r2 = client.post(WWW + "/authenticate", data=data, headers={"Referer": WWW + "/login", "Origin": WWW,
                                                                     "Accept": "text/html,application/xhtml+xml"})
        still_login = "info[password]" in r2.text and "/authenticate" in r2.text
        if r2.status_code >= 400 or still_login:
            p = _debug_dump("authenticate", r2.text)
            raise LoginFailed(f"login did not succeed (status {r2.status_code}, still on the login form: {still_login}; saved to {p})")
        token, user, html = fetch_access_token(client, pacer)
        if not token:
            p = _debug_dump("myaccount-notoken", html)
            raise LoginFailed(f"accessToken not found in v2 /myaccount (saved to {p})")
        sess = {"cookies": cookies_to_list(client.cookies), "accessToken": token, "user": user, "email": email_addr, "fetched_at": db.now()}
    save_session(sess, path)
    return sess


def import_cookies(items: list[dict], *, transport=None, pacer: Pacer | None = None, path: str | None = None, email_addr: str = "") -> dict:
    """Browser cookies in, session out: fetches the accessToken from v2 /myaccount with them."""
    pacer = pacer or Pacer()
    with _client(transport, cookies_from_list(items)) as client:
        token, user, html = fetch_access_token(client, pacer)
        if not token:
            p = _debug_dump("myaccount-notoken", html)
            raise SessionExpired(f"accessToken not found in v2 /myaccount with these cookies (saved to {p})")
        sess = {"cookies": cookies_to_list(client.cookies), "accessToken": token, "user": user, "email": email_addr or (user or {}).get("email", ""),
                "fetched_at": db.now()}
    save_session(sess, path)
    return sess


def seed_session(access_token: str, *, cookies: list[dict] | None = None, email_addr: str = "", user: dict | None = None, path: str | None = None) -> dict:
    """Write a session from an already-captured Bearer token (+ optional cookie export), with no network call at all. The fully-offline seed.

    The OLJ reads are server-side, so a browser never shows the Bearer in a request; the clean seed is the cookie export (import_cookies), and the
    Api re-auths the token off the cookies on a 401. This path exists for when a token has already been captured (RSC payload, a server log, a prior
    login) and the owner wants to install it without any live call. Save then validate with `whoami` once the owner accepts the risk."""
    token = (access_token or "").strip()
    if not token:
        raise LoginFailed("seed_session needs a non-empty access token")
    sess = {"cookies": cookies or [], "accessToken": token, "user": user or {}, "email": email_addr or (user or {}).get("email", ""),
            "fetched_at": db.now(), "seeded": True}
    save_session(sess, path)
    return sess


# ---------------------------------------------------------------- API client

class Api:
    """JSON API over httpx. GET only unless OLJ_SEND=1 (or read_only=False). Every call goes through the pacer."""

    def __init__(self, session: dict | None = None, *, transport=None, pacer: Pacer | None = None, read_only: bool | None = None, reauth=None,
                 path: str | None = None):
        self.path = path
        self.session = session or load_session(path)
        if not self.session:
            raise SessionExpired("no OLJ session: run tools/olj_cli.py login first")
        self.pacer = pacer or Pacer()
        self.read_only = (not send_enabled()) if read_only is None else read_only
        self.reauth_fn = reauth
        self.transport = transport
        self.client = _client(transport, cookies_from_list(self.session.get("cookies")), follow=False)
        self.client.headers.update({"Accept": "application/json", "Content-Type": "application/json", "Origin": V2, "Referer": V2 + "/message"})
        self._set_token(self.session.get("accessToken", ""))
        self.last: httpx.Response | None = None
        self.raw: list[dict] = []

    def _set_token(self, token: str) -> None:
        self.client.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.client.close()

    # -- auth
    def refresh_token(self) -> dict:
        """Cookie session -> new accessToken (the token expires before the cookies do)."""
        with _client(self.transport, self.client.cookies) as c:
            token, user, html = fetch_access_token(c, self.pacer)
            if not token:
                p = _debug_dump("refresh-notoken", html)
                raise SessionExpired(f"accessToken not found on refresh (saved to {p})")
            self.session.update({"accessToken": token, "user": user or self.session.get("user"), "cookies": cookies_to_list(c.cookies), "fetched_at": db.now()})
        self._set_token(token)
        self.client.cookies = cookies_from_list(self.session["cookies"])
        save_session(self.session, self.path)
        return self.session

    def reauth(self) -> None:
        try:
            self.refresh_token()
            return
        except OljError as e:
            log.warning("olj token refresh failed: %s", e)
        if not self.reauth_fn:
            raise SessionExpired("401 from the API and the cookie refresh failed: run tools/olj_cli.py login again")
        sess = self.reauth_fn()
        if not sess or not sess.get("accessToken"):
            raise SessionExpired("re-login returned no session")
        self.session = sess
        self._set_token(sess["accessToken"])
        self.client.cookies = cookies_from_list(sess.get("cookies"))

    # -- transport
    def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None, jobs: bool = False, _retry: bool = True):
        method = method.upper()
        if method != "GET" and self.read_only:
            raise ReadOnly(f"{method} {path} refused: OLJ_SEND is not 1 (read-only)")
        headers = {JOBS_HEADER[0]: JOBS_HEADER[1]} if jobs else {}
        self.pacer.wait()
        url = API + API_PREFIX + path
        r = self.client.request(method, url, params=params or None, json=body, headers=headers)
        self.last = r
        if r.status_code == 401 and _retry:
            log.info("olj 401 on %s %s: re-authenticating once", method, path)
            self.reauth()
            return self.request(method, path, params=params, body=body, jobs=jobs, _retry=False)
        if r.status_code >= 400:
            raise OljError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        self.raw.append({"method": method, "path": path, "status": r.status_code, "data": data})
        del self.raw[:-50]
        return data

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    # -- messages
    def inbox(self, page: int = 1, per_page: int = 20, unread_only: bool = False):
        params = {"page": page, "per_page": per_page}
        if unread_only:
            params["unread"] = "true"
        return self.get("/message/inbox", params=params)

    def job_threads_index(self, page: int = 1, per_page: int = 20):
        return self.get("/message/jobs", params={"page": page, "per_page": per_page})

    def job_threads(self, job_hash: str, page: int = 1, per_page: int = 20, include_archive: bool = False):
        return self.get(f"/message/jobs/{job_hash}", params={"page": page, "per_page": per_page, "include_archive": 1 if include_archive else 0})

    def thread(self, thread_hash: str, page: int = 1, per_page: int = 20):
        return self.get(f"/message/thread/{thread_hash}", params={"page": page, "per_page": per_page})

    def unread_count(self):
        return self.get("/message/unread-count")

    def me(self):
        return self.get("/users")

    def jobseeker_info(self, partner_id, viewer_key: str = ""):
        return self.get(f"/message/jobseeker-info/{partner_id}", params={"viewerKey": viewer_key} if viewer_key else None)

    def mark_thread_read(self, thread_hash: str):
        return self.request("PUT", f"/message/thread/{thread_hash}/read")

    def mark_job_read(self, job_hash: str):
        return self.request("PUT", f"/message/jobs/{job_hash}/read")

    def add_message(self, thread_hash: str, message: str):
        if not send_enabled():
            raise ReadOnly("add_message refused: OLJ_SEND is not 1")
        if not message or not message.strip():
            raise OljError("empty message")
        return self.request("POST", "/message/add", body={"thread_hash_id": thread_hash, "message": message})

    def forward_message(self, message_id):
        return self.request("POST", "/employers/forward-message", body={"message_id": message_id})

    # -- jobs
    def my_jobs(self, status: str | None = None):
        return self.get("/employers/me/jobs", params={"status": status} if status else None, jobs=True)

    def job(self, job_id):
        return self.get(f"/jobs/{job_id}", jobs=True)

    def job_edit_get(self, job_id):
        return self.get(f"/jobs/{job_id}/edit", jobs=True)

    def job_edit_put(self, job_id, job: dict, skill_tags: list):
        return self.request("PUT", f"/jobs/{job_id}/edit", body={"job": {**job, "id": job_id}, "skill_tags": list(skill_tags)}, jobs=True)

    def job_create(self, payload: dict, draft: bool = True):
        body = {**payload, "form_source": payload.get("form_source", "v2_job_form")}
        if draft:
            body["status"] = "draft"
        return self.request("POST", "/jobs", body=body, jobs=True)

    def job_delete(self, job_id):
        return self.request("DELETE", f"/jobs/{job_id}", jobs=True)


# ---------------------------------------------------------------- response mapping (pure)

def pick(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def items_of(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        d = resp.get("data", resp)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("data", "items", "threads", "jobs", "results"):
                if isinstance(d.get(k), list):
                    return d[k]
    return []


def meta_of(resp) -> dict:
    if not isinstance(resp, dict):
        return {}
    m = resp.get("meta")
    if not isinstance(m, dict) and isinstance(resp.get("data"), dict):
        m = resp["data"].get("meta")
    if isinstance(m, dict):
        return m
    return {k: resp[k] for k in ("current_page", "last_page", "total", "per_page") if k in resp}


def clean_text(s) -> str:
    if s is None:
        return ""
    t = str(s)
    t = re.sub(r"<br\s*/?>|</p>|</div>", "\n", t, flags=re.I)
    t = re.sub(r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(1) not in m.group(2) else m.group(2), t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = html_mod.unescape(t).replace("\r", "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def norm_ph_mobile(v) -> str:
    digits = re.sub(r"\D", "", str(v or ""))
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("09"):
        return "+63" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "+63" + digits
    if digits.startswith("63") and len(digits) == 12:
        return "+" + digits
    return str(v).strip() if str(v).strip().startswith("+") else "+" + digits


def _name(src: dict) -> str:
    n = pick(src, "name", "full_name", "fullname", "display_name")
    if not n:
        n = " ".join(x for x in (pick(src, "first_name", "firstname", default=""), pick(src, "last_name", "lastname", default="")) if x)
    return clean_text(n)[:120]


def message_direction(item: dict) -> str | None:
    """'in' (from the applicant), 'out' (ours) or None when the payload does not say."""
    m = item.get("message") or {}
    partner, owner = item.get("partner") or {}, item.get("owner") or {}
    sender = pick(m, "sender_id", "from_id", "user_id", "author_id", "sender")
    if sender is not None:
        if partner.get("id") is not None and str(sender) == str(partner.get("id")):
            return "in"
        if owner.get("id") is not None and str(sender) == str(owner.get("id")):
            return "out"
    for k in ("is_owner", "from_owner", "is_sender", "sent_by_me", "mine"):
        if k in m:
            return "out" if m[k] else "in"
    d = pick(m, "direction", "type")
    if isinstance(d, str):
        if d.lower() in ("in", "inbound", "received", "incoming"):
            return "in"
        if d.lower() in ("out", "outbound", "sent", "outgoing"):
            return "out"
    return None


def map_thread(item: dict, info: dict | None = None) -> dict:
    """One inbox / job-thread item (+ optional jobseeker-info) -> a flat applicant record. Tolerant of field names we have not seen."""
    thread = item.get("thread") or {}
    partner = item.get("partner") or {}
    message = item.get("message") or {}
    info = info or {}
    info_data = info.get("data") if isinstance(info.get("data"), dict) else info
    src = {**partner, **{k: v for k, v in (info_data or {}).items() if v not in (None, "")}}
    contact = src.get("contact") if isinstance(src.get("contact"), dict) else {}
    partner_id = pick(partner, "id", "user_id", "jobseeker_id") or pick(src, "id", "user_id", "jobseeker_id")
    mobile = pick(src, "mobile", "mobile_number", "phone", "phone_number", "contact_number", "cellphone") or pick(contact, "mobile", "phone", "number")
    rate = pick(src, "rate", "expected_salary", "salary", "asking_salary", "hourly_rate", "monthly_rate", "expected_rate")
    rec = {
        "thread_hash": str(pick(thread, "hash_id", "hash", "thread_hash_id", default="") or pick(item, "thread_hash_id", "hash_id", default="")),
        "thread_id": str(pick(thread, "id", default="") or ""),
        "job_hash": str(pick(thread, "job_hash_id", "job_hash", default="") or pick(item, "job_hash_id", default="")),
        "job_id": str(pick(thread, "job_id", default="") or pick(item, "job_id", default="")),
        "job_title": clean_text(pick(thread, "job_title", "title") or pick(item, "job_title") or pick(item.get("job") or {}, "title") or pick(info_data or {}, "job_title")),
        "partner_id": str(partner_id) if partner_id is not None else "",
        "partner_hash": str(pick(partner, "hash_id", default="") or ""),
        "name": _name(src),
        "email": (clean_text(pick(src, "email", "email_address") or pick(contact, "email")) or "").lower(),
        "mobile": norm_ph_mobile(mobile) if mobile else "",
        "viber": clean_text(pick(src, "viber", "viber_id", "viber_number") or pick(contact, "viber")),
        "headline": clean_text(pick(src, "headline", "tagline", "skill_summary", "title", "position"))[:300],
        "rate": clean_text(rate)[:60],
        "ap_score": clean_text(pick(src, "ap_score", "applicant_points", "points", "ap", "score"))[:20],
        "education": clean_text(pick(src, "education", "education_level"))[:120],
        "profile_url": clean_text(pick(src, "profile_url", "url", "link")) or (f"{WWW}/jobseekers/info/{partner_id}" if partner_id else ""),
        "message": clean_text(pick(message, "body", "message", "text", "content"))[:4000],
        "message_id": str(pick(message, "message_id", "id", "hash_id", default="") or ""),
        "created": clean_text(pick(message, "created", "created_at", "date", "sent_at")),
        "direction": message_direction(item),
        "unread": bool(pick(item, "unread_status", "unread", "is_unread", default=False)),
        "archived": bool(thread.get("is_archived")),
        "labels": item.get("labels") or [],
        "rating": item.get("rating"),
        "hired": item.get("hired"),
        "raw": item,
    }
    return rec


def job_record(j: dict) -> dict:
    jid = pick(j, "id", "job_id")
    status = str(pick(j, "status", "state", default="") or "")
    closed_flag = pick(j, "is_closed", "closed", "is_close")
    open_flag = pick(j, "is_open", "is_active", "active")
    clearly_closed = status.lower() in ("closed", "draft", "deleted", "expired", "inactive", "paused") or (closed_flag in (1, True, "1", "true"))
    if open_flag in (0, False, "0", "false"):
        clearly_closed = True
    return {"id": str(jid) if jid is not None else "", "hash_id": str(pick(j, "hash_id", "job_hash_id", "hash", default="") or ""),
            "title": clean_text(pick(j, "title", "job_title"))[:200], "status": status[:40], "is_open": 0 if clearly_closed else 1, "raw": j}


# ---------------------------------------------------------------- schema and ingest

SCHEMA = """
CREATE TABLE IF NOT EXISTS olj_thread (
  thread_hash TEXT PRIMARY KEY, thread_id TEXT NOT NULL DEFAULT '', job_hash TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
  job_title TEXT NOT NULL DEFAULT '', applicant_id INTEGER, partner_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '', mobile TEXT NOT NULL DEFAULT '', viber TEXT NOT NULL DEFAULT '', headline TEXT NOT NULL DEFAULT '',
  rate TEXT NOT NULL DEFAULT '', ap_score TEXT NOT NULL DEFAULT '', profile_url TEXT NOT NULL DEFAULT '', first_message TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL DEFAULT '', message_at TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'api', raw TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_olj_thread_applicant ON olj_thread (applicant_id);
CREATE INDEX IF NOT EXISTS ix_olj_thread_email ON olj_thread (lower(email));
CREATE TABLE IF NOT EXISTS olj_job (
  id TEXT PRIMARY KEY, hash_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  is_open INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL DEFAULT '{}', seen_at TEXT NOT NULL
);
"""
APPLICANT_COLS = {"olj_thread_hash": "TEXT", "olj_job_hash": "TEXT", "olj_partner_id": "TEXT"}
APPLICATION_COLS = {k: "TEXT NOT NULL DEFAULT ''" for k in ("olj_thread_hash", "olj_job_hash", "olj_job_title", "olj_mobile", "olj_viber", "olj_headline",
                                                             "olj_rate", "olj_ap_score", "olj_profile_url")}


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)
    for table, cols in (("applicant", APPLICANT_COLS), ("application", APPLICATION_COLS)):
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_applicant_olj_partner ON applicant (olj_partner_id)")
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))


def thread_seen(con, thread_hash: str) -> dict | None:
    return db.one(con, "SELECT * FROM olj_thread WHERE thread_hash=?", (thread_hash,))


def find_existing(con, rec: dict) -> dict | None:
    a = None
    if rec.get("email"):
        a = engine.find_applicant(con, {"email": rec["email"]})
    if not a and rec.get("partner_id"):
        a = db.one(con, "SELECT * FROM applicant WHERE olj_partner_id=?", (rec["partner_id"],))
    if not a and rec.get("mobile"):
        a = engine.find_applicant(con, {"phone": rec["mobile"]})
    return a


def import_thread(con, rec: dict, *, dry: bool = False, source: str = "api") -> dict:
    """One mapped thread -> applicant + application + inbound message + ledger row. Never sends."""
    th = rec.get("thread_hash") or ""
    short = {"thread_hash": th, "name": rec.get("name", ""), "email": rec.get("email", ""), "job_title": rec.get("job_title", "")}
    if not th:
        return {"status": "skipped", "why": "no thread hash", **short}
    seen = thread_seen(con, th)
    if seen:
        return {"status": "seen", "applicant_id": seen["applicant_id"], **short}
    existing = find_existing(con, rec)
    if dry:
        return {"status": "would_link" if existing else "would_create", "applicant_id": existing["id"] if existing else None, **short}
    aid, created = engine.upsert_applicant(con, {"name": rec.get("name", ""), "email": rec.get("email") or None, "phone": rec.get("mobile") or None,
                                                 "channel_code": CHANNEL, "timezone": db.DEFAULT_APPLICANT_TZ}, actor="olj")
    ts = db.now()
    cur = engine.applicant(con, aid) or {}
    con.execute("UPDATE applicant SET olj_thread_hash=?, olj_job_hash=?, olj_partner_id=?, updated_at=? WHERE id=?",
                (th, rec.get("job_hash") or cur.get("olj_job_hash") or "", rec.get("partner_id") or cur.get("olj_partner_id") or "", ts, aid))
    app_id = engine.save_application(con, aid, {"channel_code": CHANNEL, "heard_from": "onlinejobs", "timezone": db.DEFAULT_APPLICANT_TZ,
                                                "experience_text": rec.get("message", "")[:2000], "olj_thread_hash": th, "olj_job_hash": rec.get("job_hash", ""),
                                                "olj_job_title": rec.get("job_title", ""), "olj_profile_url": rec.get("profile_url", ""),
                                                "olj_headline": rec.get("headline", ""), "olj_rate": rec.get("rate", ""), "olj_ap_score": rec.get("ap_score", ""),
                                                "olj_source": source})
    con.execute("""UPDATE application SET olj_thread_hash=?, olj_job_hash=?, olj_job_title=?, olj_mobile=?, olj_viber=?, olj_headline=?, olj_rate=?, olj_ap_score=?,
                   olj_profile_url=? WHERE id=?""",
                (th, rec.get("job_hash", ""), rec.get("job_title", ""), rec.get("mobile", ""), rec.get("viber", ""), rec.get("headline", ""),
                 rec.get("rate", ""), rec.get("ap_score", ""), rec.get("profile_url", ""), app_id))
    body = rec.get("message") or ""
    if body and rec.get("direction") != "out":
        engine.record_inbound(con, aid, CHANNEL, body, external_id=f"{th}:{rec.get('message_id') or 'first'}", address=th)
    con.execute("""INSERT INTO olj_thread (thread_hash, thread_id, job_hash, job_id, job_title, applicant_id, partner_id, name, email, mobile, viber, headline, rate,
                   ap_score, profile_url, first_message, message_id, message_at, source, raw, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (th, rec.get("thread_id", ""), rec.get("job_hash", ""), rec.get("job_id", ""), rec.get("job_title", ""), aid, rec.get("partner_id", ""),
                 rec.get("name", ""), rec.get("email", ""), rec.get("mobile", ""), rec.get("viber", ""), rec.get("headline", ""), rec.get("rate", ""),
                 rec.get("ap_score", ""), rec.get("profile_url", ""), body[:4000], rec.get("message_id", ""), rec.get("created", ""), source,
                 json.dumps(rec.get("raw") or {}, default=str)[:20000], ts))
    return {"status": "created" if created else "linked", "applicant_id": aid, **short}


def _applicant_message(api: Api, rec: dict) -> dict:
    """When the latest message in the thread is ours, find the applicant's first message in the conversation."""
    if rec.get("direction") != "out":
        return rec
    try:
        resp = api.thread(rec["thread_hash"], page=1, per_page=50)
    except OljError as e:
        log.warning("olj thread %s fetch failed: %s", rec["thread_hash"], e)
        return rec
    items = items_of(resp)
    for it in reversed(items):  # oldest last in a newest-first list
        m = map_thread(it)
        if m["message"] and m["direction"] != "out":
            return {**rec, "message": m["message"], "message_id": m["message_id"], "created": m["created"], "direction": m["direction"]}
    return rec


def sync_inbox(con, api: Api, *, dry: bool = False, unread_only: bool = True, max_pages: int = 10, per_page: int = 50, enrich: bool = True,
               jobs: bool = True) -> dict:
    """Pull threads, import the new ones. Read-only against OLJ unless OLJ_SEND=1 and olj_mark_read=1."""
    out = {"dry": dry, "pages": 0, "threads": 0, "created": 0, "linked": 0, "seen": 0, "skipped": 0, "would_create": 0, "would_link": 0, "jobs": 0,
           "errors": 0, "plan": []}
    handled: set[str] = set()
    mark_read = (not dry) and (not api.read_only) and db.setting(con, "olj_mark_read", "0") == "1"

    def handle(item: dict) -> None:
        rec = map_thread(item)
        th = rec["thread_hash"]
        if not th or th in handled:
            return
        handled.add(th)
        out["threads"] += 1
        if thread_seen(con, th):
            out["seen"] += 1
            return
        if enrich and rec["partner_id"]:
            try:
                rec = map_thread(item, api.jobseeker_info(rec["partner_id"]))
            except OljError as e:
                out["errors"] += 1
                log.warning("olj jobseeker-info %s failed: %s", rec["partner_id"], e)
        rec = _applicant_message(api, rec)
        res = import_thread(con, rec, dry=dry)
        out[res["status"]] = out.get(res["status"], 0) + 1
        out["plan"].append({k: res.get(k) for k in ("status", "applicant_id", "thread_hash", "name", "email", "job_title")})
        if mark_read and res["status"] in ("created", "linked"):
            try:
                api.mark_thread_read(th)
            except OljError as e:
                log.warning("olj mark read %s failed: %s", th, e)

    def pages(fetch) -> None:
        page = 1
        while page <= max_pages:
            resp = fetch(page)
            items = items_of(resp)
            out["pages"] += 1
            for it in items:
                handle(it)
            meta = meta_of(resp)
            try:
                last, cur = int(meta.get("last_page") or 0), int(meta.get("current_page") or page)
            except (TypeError, ValueError):
                last, cur = 0, page
            if not items or not last or cur >= last:
                break
            page = cur + 1

    pages(lambda p: api.inbox(page=p, per_page=per_page, unread_only=unread_only))
    if jobs:
        try:
            for j in items_of(api.my_jobs()):
                jr = job_record(j)
                out["jobs"] += 1
                if jr["id"] and not dry:
                    con.execute("INSERT INTO olj_job (id, hash_id, title, status, is_open, raw, seen_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                                "hash_id=excluded.hash_id, title=excluded.title, status=excluded.status, is_open=excluded.is_open, raw=excluded.raw, seen_at=excluded.seen_at",
                                (jr["id"], jr["hash_id"], jr["title"], jr["status"], jr["is_open"], json.dumps(j, default=str)[:20000], db.now()))
                if jr["is_open"] and jr["hash_id"]:
                    pages(lambda p, h=jr["hash_id"]: api.job_threads(h, page=p, per_page=per_page))
        except OljError as e:
            out["errors"] += 1
            log.warning("olj jobs pull failed: %s", e)
    if not dry:
        db.set_setting(con, "olj_last_sync_at", db.now())
        db.set_setting(con, "olj_last_sync_notes", json.dumps({k: v for k, v in out.items() if k != "plan"}))
        db.set_setting(con, "olj_last_sync_error", "")
    return out


def run_sync(*, dry: bool = False, api: Api | None = None) -> dict:
    """One sync with its own connection, logged in worker_runs (kind olj_sync). Used by the worker and the staff button."""
    with db.db() as con:
        cur = con.execute("INSERT INTO worker_runs (kind, started_at) VALUES (?,?)", ("olj_sync", db.now()))
        rid = cur.lastrowid
        db.set_setting(con, "olj_last_attempt_at", db.now())
        try:
            api = api or Api()
            out = sync_inbox(con, api, dry=dry)
            con.execute("UPDATE worker_runs SET finished_at=?, ok=1, notes=? WHERE id=?",
                        (db.now(), json.dumps({k: v for k, v in out.items() if k != "plan"})[:500], rid))
            return out
        except Exception as e:  # noqa: BLE001
            log.exception("olj sync failed")
            db.set_setting(con, "olj_last_sync_error", f"{type(e).__name__}: {e}"[:400])
            con.execute("UPDATE worker_runs SET finished_at=?, ok=0, notes=? WHERE id=?", (db.now(), str(e)[:500], rid))
            return {"error": str(e)}


def sync_due(con, now: datetime | None = None) -> tuple[bool, str]:
    if db.setting(con, "olj_sync_enabled", "0") != "1":
        return False, "disabled"
    if not load_session():
        return False, "no session"
    now = now or datetime.now(timezone.utc)
    last = db.parse_ts(db.setting(con, "olj_last_attempt_at", "")) or db.parse_ts(db.setting(con, "olj_last_sync_at", ""))
    if last and (now - last).total_seconds() < sync_seconds():
        return False, "not due"
    return True, "due"


def worker_loop(interval: int = 30) -> None:
    while True:
        time.sleep(interval)
        try:
            with db.db() as con:
                due, _why = sync_due(con)
            if due:
                run_sync(dry=False)
        except Exception:  # noqa: BLE001
            log.exception("olj worker loop error")


_worker_started = False


def start_worker() -> bool:
    global _worker_started
    if _worker_started or os.environ.get("HIRE_NO_WORKER") == "1" or os.environ.get("OLJ_WORKER", "1") != "1":
        return False
    _worker_started = True
    threading.Thread(target=worker_loop, name="hire-olj", daemon=True).start()
    return True


# ---------------------------------------------------------------- outbound: the reply path

def apply_link(con=None) -> str:
    code = db.setting(con, "olj_apply_code", "olj") if con is not None else "olj"
    return channels.public_url(f"/apply/{code or 'olj'}")


def telegram_link_for(aid: int) -> str:
    bot = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    return f"https://t.me/{bot}?start={channels.start_token(aid)}" if bot else ""


def move_to_telegram_text(name: str, telegram_link: str, apply_link_url: str, job_title: str = "") -> str:
    """The first reply on OnlineJobs.ph: plain text, no emoji; links are allowed in messages (not in posts)."""
    first = (name or "").strip().split(" ")[0] if name else ""
    hi = f"Hi {first}," if first else "Hi,"
    about = f" for {job_title}" if job_title else ""
    steps = [f"1. Fill in our short application form (about three minutes): {apply_link_url}"]
    if telegram_link:
        steps.append(f"2. Then open {telegram_link} on Telegram and tap Start, so I can continue with you there.")
    else:
        steps.append("2. I will continue with you by email at the address you put on the form.")
    return (f"{hi} thanks for applying{about} on OnlineJobs.ph. {texts.DISCLOSURE}\n\n"
            "We run the rest of the process off OnlineJobs.ph so it moves faster. Two short steps:\n" + "\n".join(steps) + "\n\n"
            "Screening is automatic and takes a minute. After it there is a short text interview with me, five questions, then a 45-minute assessment "
            "and a 30-minute mock chat with one of our chat managers. Reply here if you have any questions.")


def send_reply(con, applicant_id: int, text: str, *, api: Api | None = None, actor: str = "sam", kind: str = "sam") -> int:
    """Post to the applicant's OLJ thread when OLJ_SEND=1; otherwise store the message as 'held' with a note. Returns the message id."""
    a = engine.applicant(con, applicant_id)
    if not a:
        raise ValueError("no such applicant")
    th = a.get("olj_thread_hash") or ""
    ts = db.now()
    if th.startswith("email:"):
        return engine.deliver(con, applicant_id, text, kind=kind, channel="email", actor=actor)
    if not th:
        cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, error, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (applicant_id, CHANNEL, "out", kind, text, "failed", "no OLJ thread on file", actor, ts))
        return cur.lastrowid
    if a.get("do_not_contact"):
        cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, error, actor, address, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (applicant_id, CHANNEL, "out", kind, text, "skipped", "do not contact", actor, th, ts))
        return cur.lastrowid
    if not send_enabled():
        cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, error, actor, address, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (applicant_id, CHANNEL, "out", kind, text, "held", "OLJ_SEND=0: read-only, not sent to OnlineJobs.ph", actor, th, ts))
        return cur.lastrowid
    status, pid, err = "sent", "", ""
    try:
        api = api or Api()
        resp = api.add_message(th, text)
        pid = str(pick(resp if isinstance(resp, dict) else {}, "message_id", "id", default="") or pick((resp or {}).get("data") or {}, "message_id", "id", default=""))
    except Exception as e:  # noqa: BLE001
        status, err = "failed", f"{type(e).__name__}: {e}"[:300]
        log.warning("olj send to %s failed: %s", applicant_id, err)
    cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, provider_id, error, actor, address, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (applicant_id, CHANNEL, "out", kind, text, status, pid, err, actor, th, ts))
    if status == "sent":
        con.execute("UPDATE applicant SET last_outbound_at=?, last_channel=?, updated_at=? WHERE id=?", (ts, CHANNEL, ts, applicant_id))
        if kind in engine.AWAIT_KINDS and not a.get("awaiting_since"):
            con.execute("UPDATE applicant SET awaiting_since=? WHERE id=?", (ts, applicant_id))
    return cur.lastrowid


def send_invite(con, applicant_id: int, *, api: Api | None = None, actor: str = "staff") -> int:
    """The move-to-Telegram first message for an imported OLJ applicant (held unless OLJ_SEND=1)."""
    a = engine.applicant(con, applicant_id)
    if not a:
        raise ValueError("no such applicant")
    t = db.one(con, "SELECT job_title FROM olj_thread WHERE applicant_id=? ORDER BY imported_at DESC LIMIT 1", (applicant_id,)) or {}
    text = move_to_telegram_text(a.get("name", ""), telegram_link_for(applicant_id), apply_link(con), t.get("job_title", ""))
    return send_reply(con, applicant_id, text, api=api, actor=actor, kind="first")


# ---------------------------------------------------------------- email intake (option A: OLJ forwards each application to the mailbox)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PROFILE_RE = re.compile(r"https?://(?:www\.)?onlinejobs\.ph/(?:jobseekers?/(?:info|profile|worker)[^\s<>\"')]*|workers?/[^\s<>\"')]*)", re.I)
_SUBJECT_RE = re.compile(r"new (?:job )?applica(?:tion|nt)|applied (?:to|for)|application (?:for|received)|new message|applicant", re.I)
_FOOTER_RE = re.compile(r"^(?:to reply|reply to|log ?in|login|view (?:the )?(?:full )?(?:profile|application|message)|this (?:email|message) was|you (?:are )?receiv|"
                        r"unsubscribe|manage (?:your )?notifications|onlinejobs\.ph team|thanks,?$|regards,?$|-{3,}|_{3,}|={3,})", re.I)
_LABEL_RE = re.compile(r"^\s*(?P<k>applicant(?:'s)? name|applicant|worker name|worker|name|from|email(?: address)?|e-mail|job(?: title| post)?|position|"
                       r"profile(?: link| url)?|view profile|message|cover letter|application message|applicant'?s message)\s*[:\-]\s*(?P<v>.*)$", re.I)


def _as_message(raw) -> Message:
    if isinstance(raw, Message):
        return raw
    if isinstance(raw, bytes):
        return email.message_from_bytes(raw)
    return email.message_from_string(str(raw))


def _body_text(msg: Message) -> str:
    text = ""
    if msg.is_multipart():
        html_part = ""
        for part in msg.walk():
            ct = part.get_content_type()
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            try:
                payload = part.get_payload(decode=True)
                s = payload.decode(part.get_content_charset() or "utf-8", "replace") if payload else ""
            except Exception:  # noqa: BLE001
                continue
            if ct == "text/plain" and not text:
                text = s
            elif ct == "text/html" and not html_part:
                html_part = s
        if not text and html_part:
            text = clean_text(html_part)
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else str(msg.get_payload())
        except Exception:  # noqa: BLE001
            text = str(msg.get_payload())
        if msg.get_content_type() == "text/html":
            text = clean_text(text)
    return text.replace("\r", "")


def parse_olj_email(raw) -> dict | None:
    """OLJ's 'new application' notification -> {name, email, job_title, message, profile_url, subject, message_id, from}. None when it is not one."""
    msg = _as_message(raw)
    from_addr = (email.utils.parseaddr(channels._hdr(msg, "From"))[1] or "").lower()
    subject = channels._hdr(msg, "Subject") or ""
    body = _body_text(msg)
    from_olj = from_addr.endswith("onlinejobs.ph") or from_addr.endswith("@onlinejobs.ph")
    mentions = "onlinejobs.ph" in body.lower() or "onlinejobs.ph" in subject.lower()
    if not (from_olj or (mentions and _SUBJECT_RE.search(subject))):
        return None
    if not _SUBJECT_RE.search(subject) and not re.search(r"applied|application|applicant", body, re.I):
        return None
    out = {"name": "", "email": "", "job_title": "", "message": "", "profile_url": "", "subject": subject.strip(),
           "message_id": (msg.get("Message-ID") or "").strip(), "from": from_addr}
    lines = body.split("\n")
    message_lines: list[str] = []
    in_message = False
    for line in lines:
        s = line.strip()
        if in_message:
            if _FOOTER_RE.match(s) or _PROFILE_RE.search(s) and not s[:1].isalnum():
                in_message = False
                continue
            message_lines.append(line.rstrip())
            continue
        m = _LABEL_RE.match(line)
        if not m:
            continue
        k, v = m.group("k").lower(), m.group("v").strip()
        if k in ("name", "applicant", "applicant name", "applicant's name", "worker", "worker name", "from") and not out["name"] and v and "@" not in v:
            out["name"] = clean_text(v)[:120]
        elif k.startswith("e") and not out["email"]:
            em = _EMAIL_RE.search(v)
            out["email"] = em.group(0).lower() if em else ""
        elif k in ("job", "job title", "job post", "position") and not out["job_title"]:
            out["job_title"] = clean_text(v.strip('"“”')).strip()[:200]
        elif k.startswith("profile") or k == "view profile":
            pm = _PROFILE_RE.search(v)
            out["profile_url"] = pm.group(0) if pm else out["profile_url"]
        elif k in ("message", "cover letter", "application message", "applicant's message", "applicants message"):
            in_message = True
            if v:
                message_lines.append(v)
    if not out["name"]:
        m = re.search(r"(?:new application from|application from|applicant)\s+([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})", body) or \
            re.search(r"^\s*([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})\s+(?:has )?applied\b", body, re.M)
        if m:
            out["name"] = m.group(1).strip()[:120]
    if not out["email"]:
        for em in _EMAIL_RE.findall(body):
            low = em.lower()
            if not any(low.endswith(d) for d in OUR_DOMAINS) and low != from_addr:
                out["email"] = low
                break
    if not out["job_title"]:
        m = re.search(r"(?:for|to) (?:your|the) (?:job(?: post(?:ing)?)?|position)[:\s]*[\"“]?([^\"”\n]+?)[\"”]?\s*(?:\.|$)", body, re.I | re.M) or \
            re.search(r"(?:application|applicant)(?: for)?[:\s]+[\"“]?([^\"”\n]+?)[\"”]?\s*$", subject, re.I)
        if m:
            out["job_title"] = clean_text(m.group(1)).strip()[:200]
    if not out["profile_url"]:
        pm = _PROFILE_RE.search(body)
        out["profile_url"] = pm.group(0) if pm else ""
    if message_lines:
        out["message"] = clean_text("\n".join(message_lines))[:4000]
    else:
        kept = [ln for ln in lines if ln.strip() and not _LABEL_RE.match(ln) and not _FOOTER_RE.match(ln.strip()) and not _PROFILE_RE.search(ln)
                and not re.match(r"^\s*(hi|hello|dear)\b", ln, re.I)]
        out["message"] = clean_text("\n".join(kept))[:4000]
    if not (out["email"] or out["name"]):
        return None
    return out


def ingest_email(con, parsed: dict, *, dry: bool = False) -> dict:
    """A parsed notification -> the same upsert as the API path. Ledger key 'email:<id>' so the reply path knows to answer by email."""
    key_src = parsed.get("message_id") or f"{parsed.get('email', '')}|{parsed.get('job_title', '')}|{(parsed.get('message') or '')[:80]}"
    th = "email:" + hashlib.sha1(key_src.encode("utf-8", "replace")).hexdigest()[:24]
    rec = {"thread_hash": th, "thread_id": "", "job_hash": "", "job_id": "", "job_title": parsed.get("job_title", ""), "partner_id": "", "partner_hash": "",
           "name": parsed.get("name", ""), "email": (parsed.get("email") or "").lower(), "mobile": "", "viber": "", "headline": "", "rate": "", "ap_score": "",
           "education": "", "profile_url": parsed.get("profile_url", ""), "message": parsed.get("message", ""), "message_id": parsed.get("message_id", ""),
           "created": "", "direction": "in", "unread": True, "archived": False, "labels": [], "rating": None, "hired": None,
           "raw": {"subject": parsed.get("subject", ""), "from": parsed.get("from", ""), "message_id": parsed.get("message_id", "")}}
    return import_thread(con, rec, dry=dry, source="email")


def install_email_hook() -> None:
    """Wrap channels.EmailChannel.handle_message: OLJ notifications are ingested here, everything else goes on as before."""
    cls = channels.EmailChannel
    if getattr(cls, "_olj_hooked", False):
        return
    orig = cls.handle_message

    def handle_message(self, con, msg) -> bool:
        try:
            parsed = parse_olj_email(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("olj email parse failed: %s", e)
            parsed = None
        if parsed:
            res = ingest_email(con, parsed)
            log.info("olj notification email -> %s applicant %s", res.get("status"), res.get("applicant_id"))
            return res.get("status") in ("created", "linked", "seen")
        return orig(self, con, msg)

    cls.handle_message = handle_message
    cls._olj_hooked = True
    cls._olj_orig_handle_message = orig


# ---------------------------------------------------------------- staff page

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _ago(ts: str | None) -> str:
    d = db.parse_ts(ts)
    if not d:
        return "never"
    s = int((datetime.now(timezone.utc) - d).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


templates.env.filters["ago"] = _ago
templates.env.globals.update(APP_NAME="Beloura Hire", AGENT=texts.AGENT_NAME, STAGES=db.STAGES, PIPELINE=db.PIPELINE, SIDE=db.SIDE, STAGE_LABELS=db.STAGE_LABELS,
                             SHIFT_LABELS=db.SHIFT_LABELS)


def _ctx(request: Request, **extra) -> dict:
    with db.db() as con:
        esc_n = (db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'") or {}).get("n", 0)
        worker = db.one(con, "SELECT * FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1")
    return {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path, "query": dict(request.query_params),
            "esc_n": esc_n, "worker": worker, **extra}


def _is_admin(request: Request) -> bool:
    return ((getattr(request.state, "user", None) or {}).get("role")) == "admin"


def _who(request: Request) -> str:
    return ((getattr(request.state, "user", None) or {}).get("user")) or "staff"


def staff_counts(con) -> dict:
    return {
        "threads": (db.one(con, "SELECT COUNT(*) n FROM olj_thread") or {}).get("n", 0),
        "threads_email": (db.one(con, "SELECT COUNT(*) n FROM olj_thread WHERE source='email'") or {}).get("n", 0),
        "applicants": (db.one(con, "SELECT COUNT(*) n FROM applicant WHERE channel_code=?", (CHANNEL,)) or {}).get("n", 0),
        "inbound": (db.one(con, "SELECT COUNT(*) n FROM message WHERE channel=? AND direction='in'", (CHANNEL,)) or {}).get("n", 0),
        "held": (db.one(con, "SELECT COUNT(*) n FROM message WHERE channel=? AND status='held'", (CHANNEL,)) or {}).get("n", 0),
        "sent": (db.one(con, "SELECT COUNT(*) n FROM message WHERE channel=? AND direction='out' AND status='sent'", (CHANNEL,)) or {}).get("n", 0),
        "jobs": (db.one(con, "SELECT COUNT(*) n FROM olj_job") or {}).get("n", 0),
        "jobs_open": (db.one(con, "SELECT COUNT(*) n FROM olj_job WHERE is_open=1") or {}).get("n", 0),
    }


@router.get("/staff/olj", response_class=HTMLResponse)
def staff_olj(request: Request):
    with db.db() as con:
        s = {k: db.setting(con, k, v) for k, v in DEFAULT_SETTINGS.items()}
        counts = staff_counts(con)
        threads = db.q(con, """SELECT t.*, a.stage, a.name AS applicant_name FROM olj_thread t LEFT JOIN applicant a ON a.id=t.applicant_id
                               ORDER BY t.imported_at DESC LIMIT 100""")
        jobs = db.q(con, "SELECT * FROM olj_job ORDER BY is_open DESC, title")
        runs = db.q(con, "SELECT * FROM worker_runs WHERE kind='olj_sync' ORDER BY id DESC LIMIT 10")
        held = db.q(con, """SELECT m.*, a.name FROM message m JOIN applicant a ON a.id=m.applicant_id WHERE m.channel=? AND m.direction='out'
                            ORDER BY m.id DESC LIMIT 20""", (CHANNEL,))
        due, why = sync_due(con)
    status = session_status()
    return templates.TemplateResponse(request, "olj.html", _ctx(request, status=status, s=s, counts=counts, threads=threads, jobs=jobs, runs=runs, held=held,
                                                               send_enabled=send_enabled(), sync_seconds=sync_seconds(), due=due, due_why=why,
                                                               is_admin=_is_admin(request), msg=request.query_params.get("msg", "")))


@router.post("/staff/olj/toggle")
def staff_olj_toggle(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "admin only")
    with db.db() as con:
        cur = db.setting(con, "olj_sync_enabled", "0")
        db.set_setting(con, "olj_sync_enabled", "0" if cur == "1" else "1")
    return RedirectResponse("/staff/olj?msg=toggled", status_code=303)


@router.post("/staff/olj/sync")
def staff_olj_sync(request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "admin only")
    if not load_session():
        return RedirectResponse("/staff/olj?msg=no-session", status_code=303)
    threading.Thread(target=run_sync, kwargs={"dry": False}, name="hire-olj-sync", daemon=True).start()
    return RedirectResponse("/staff/olj?msg=sync-started", status_code=303)


@router.post("/staff/olj/applicant/{aid}/invite")
def staff_olj_invite(aid: int, request: Request):
    if not _is_admin(request):
        raise HTTPException(403, "admin only")
    with db.db() as con:
        if not engine.applicant(con, aid):
            raise HTTPException(404)
        send_invite(con, aid, actor=_who(request))
    return RedirectResponse(f"/staff/olj?msg=invite-{'sent' if send_enabled() else 'held'}", status_code=303)


install_email_hook()
start_worker()
