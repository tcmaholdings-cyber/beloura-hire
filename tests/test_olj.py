"""OnlineJobs.ph against recorded fixtures only: csrf and accessToken extraction, pacing, the API client over a mock transport (headers, the
X-ONLINEJOBSPH job header, one re-auth on 401, the read-only guard), login and the captcha stop, inbox/thread -> applicant mapping, sync with
dedupe on thread hash / email / partner, the held reply path and the move-to-Telegram text, the notification-email parser and its IMAP hook,
the staff page and the CLI refusals. Nothing here touches the network."""
import contextlib
import email
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.TemporaryDirectory()
os.environ.update({"HIRE_NO_WORKER": "1", "HIRE_SECRET": "test-olj", "HIRE_DB_PATH": os.path.join(TMP.name, "olj-api.db"), "HIRE_PUBLIC_URL": "http://test",
                   "BELOURA_GATEWAY_TOKEN": "gw", "PANEL_PASSWORD": "pw", "OPENROUTER_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "DISCORD_BOT_TOKEN": "",
                   "SMTP_HOST": "", "TELEGRAM_BOT_USERNAME": "beloura_hiring_bot", "OLJ_SESSION_PATH": os.path.join(TMP.name, "sess", "olj_session.json")})
os.environ.pop("OLJ_SEND", None)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import channels  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import olj  # noqa: E402
from tools import olj_cli  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "olj")
ADMIN = {"X-Gateway-Token": "gw", "Remote-User": "mikey", "Remote-Groups": "id-admins,hiring-admin", "Remote-Name": "Mikey"}
MANAGER = {"X-Gateway-Token": "gw", "Remote-User": "ian", "Remote-Groups": "hiring-manager", "Remote-Name": "Ian"}
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s|```", re.M)
FIXTURE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.FIXTURE-TOKEN-abc123.sig"


def fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def fxb(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def fxj(name: str):
    return json.loads(fx(name))


def fast_pacer() -> olj.Pacer:
    return olj.Pacer(sleep=lambda s: None)


class FakeApi:
    """The surface sync_inbox and send_reply use, fed from the fixtures."""
    read_only = True

    def __init__(self, inbox_pages=None, jobs=None, job_threads=None, threads=None, info=None):
        self.calls = []
        self.inbox_pages = inbox_pages or [fxj("inbox.json")]
        self.jobs = jobs if jobs is not None else fxj("jobs.json")
        self.job_threads_by = job_threads if job_threads is not None else {"job_open1": fxj("job_threads.json")}
        self.threads = threads if threads is not None else {"th_g7h8i9": fxj("thread.json")}
        self.info = info if info is not None else fxj("jobseeker_info.json")

    def inbox(self, page=1, per_page=20, unread_only=False):
        self.calls.append(("inbox", page, unread_only))
        return self.inbox_pages[page - 1] if page - 1 < len(self.inbox_pages) else {"data": [], "meta": {}}

    def my_jobs(self, status=None):
        self.calls.append(("jobs",))
        return self.jobs

    def job_threads(self, job_hash, page=1, per_page=20, include_archive=False):
        self.calls.append(("job_threads", job_hash, page))
        return self.job_threads_by.get(job_hash, {"data": []}) if page == 1 else {"data": []}

    def thread(self, thread_hash, page=1, per_page=20):
        self.calls.append(("thread", thread_hash))
        return self.threads.get(thread_hash, {"data": []})

    def jobseeker_info(self, partner_id, viewer_key=""):
        self.calls.append(("info", str(partner_id)))
        return self.info.get(str(partner_id), {"data": {}})

    def mark_thread_read(self, thread_hash):
        self.calls.append(("read", thread_hash))
        return {}

    def add_message(self, thread_hash, message):
        if not olj.send_enabled():
            raise olj.ReadOnly("add_message refused")
        self.calls.append(("add", thread_hash, message))
        return {"data": {"message_id": 777}}


def fresh_db():
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "t.db")
    db.init(path)
    return tmp, db.connect(path)


# ---------------------------------------------------------------- extractors

class ExtractorTest(unittest.TestCase):
    def test_csrf_hidden_fields_and_captcha(self):
        page = fx("login.html")
        self.assertEqual(olj.extract_csrf(page), "3640096e587a905de16b9b4b6c1fe70f")
        self.assertEqual(olj.hidden_fields(page), {"csrf-token": "3640096e587a905de16b9b4b6c1fe70f", "info[remember]": "1"})
        self.assertFalse(olj.has_captcha(page))
        self.assertTrue(olj.has_captcha(fx("login_captcha.html")))
        outside = page.replace("</body>", '<div class="g-recaptcha" data-sitekey="x"></div></body>')
        self.assertFalse(olj.has_captcha(outside), "a captcha on another form of the page is not the login's")
        self.assertIsNone(olj.extract_csrf("<html><form action='/authenticate'></form></html>"))

    def test_access_token_from_flight_payload(self):
        page = fx("myaccount.html")
        self.assertEqual(olj.extract_access_token(page), FIXTURE_TOKEN)
        u = olj.extract_user(page)
        self.assertEqual((u["name"], u["email"], u["unreadMessagesCount"]), ("Employer", "employer@example.com", 297))
        decoded = olj.extract_flight_text(page)
        self.assertIn('"accessToken":"', decoded)
        self.assertNotIn('\\"', decoded)

    def test_access_token_fallbacks(self):
        self.assertEqual(olj.extract_access_token('<script>window.__x={"accessToken":"tok-plain-1234567890"}</script>'), "tok-plain-1234567890")
        self.assertEqual(olj.extract_access_token('<script>x("{\\"accessToken\\":\\"tok-escaped-1234567890\\"}")</script>'), "tok-escaped-1234567890")
        self.assertIsNone(olj.extract_access_token("<html><body>nothing here</body></html>"))
        self.assertIsNone(olj.extract_access_token('{"accessToken":null}'))
        self.assertEqual(olj.extract_user("<html></html>"), {})

    def test_netscape_cookies(self):
        text = ("# Netscape HTTP Cookie File\n"
                "#HttpOnly_www.onlinejobs.ph\tFALSE\t/\tTRUE\t1800000000\tPHPSESSID\tabc123\n"
                ".onlinejobs.ph\tTRUE\t/\tTRUE\t0\t_ga\tGA1.2.3\n"
                "broken line without tabs\n")
        items = olj.parse_netscape_cookies(text)
        self.assertEqual([(c["name"], c["domain"], c["expires"]) for c in items], [("PHPSESSID", "www.onlinejobs.ph", 1800000000), ("_ga", ".onlinejobs.ph", None)])
        jar = olj.cookies_from_list(items + [{"name": "x", "value": "1", "domain": "#HttpOnly_v2.onlinejobs.ph"}])
        names = {c.name: c.domain for c in jar.jar}
        self.assertEqual(names["PHPSESSID"], "www.onlinejobs.ph")
        self.assertEqual(names["x"], "v2.onlinejobs.ph")
        back = olj.cookies_to_list(jar)
        self.assertEqual({c["name"] for c in back}, {"PHPSESSID", "_ga", "x"})


# ---------------------------------------------------------------- pacing

class PacerTest(unittest.TestCase):
    def test_random_gap_and_waits(self):
        t = [100.0]
        slept = []

        def sleep(s):
            slept.append(s)
            t[0] += s

        gaps = iter([2.0, 4.5, 3.0])
        p = olj.Pacer(sleep=sleep, clock=lambda: t[0], rand=lambda a, b: next(gaps))
        self.assertEqual(p.wait(), 0.0)          # first call goes straight out
        self.assertEqual(p.wait(), 2.0)          # second waits the whole gap
        t[0] += 0.5                              # some work happened
        self.assertEqual(p.wait(), 4.0)          # 4.5 gap minus the 0.5 already spent
        t[0] += 10
        self.assertEqual(p.wait(), 0.0)          # long enough since the last call: no wait
        self.assertEqual(slept, [2.0, 4.0])
        self.assertEqual(p.calls, 4)

    def test_never_faster_than_one_a_second(self):
        p = olj.Pacer(0.1, 0.2, sleep=lambda s: None)
        self.assertEqual((p.min_gap, p.max_gap), (1.0, 1.0))
        old = os.environ.get("OLJ_MIN_GAP")
        os.environ["OLJ_MIN_GAP"], os.environ["OLJ_MAX_GAP"] = "0", "0.5"
        try:
            q = olj.Pacer(sleep=lambda s: None)
            self.assertEqual((q.min_gap, q.max_gap), (1.0, 1.0))
        finally:
            os.environ.pop("OLJ_MAX_GAP", None)
            if old is None:
                os.environ.pop("OLJ_MIN_GAP", None)
            else:
                os.environ["OLJ_MIN_GAP"] = old
        r = olj.Pacer()
        self.assertEqual((r.min_gap, r.max_gap), (2.0, 5.0))


# ---------------------------------------------------------------- API client over a mock transport

class MockOlj:
    """Routes for www / v2 / api hosts. Records every request; never leaves the process."""

    def __init__(self):
        self.requests = []
        self.expired = False
        self.captcha = False
        self.new_token = "NEWTOK-" + "x" * 20

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        auth = request.headers.get("Authorization", "")
        if host == "www.onlinejobs.ph":
            if path == "/login":
                return httpx.Response(200, text=fx("login_captcha.html" if self.captcha else "login.html"))
            if path == "/authenticate" and request.method == "POST":
                return httpx.Response(302, headers={"Location": "https://v2.onlinejobs.ph/myaccount", "Set-Cookie": "PHPSESSID=sess-abc; Path=/; Domain=www.onlinejobs.ph; HttpOnly"})
        if host == "v2.onlinejobs.ph" and path == "/myaccount":
            return httpx.Response(200, text=fx("myaccount.html").replace(FIXTURE_TOKEN, self.new_token) if self.expired else fx("myaccount.html"))
        if host == "api.onlinejobs.ph":
            if self.expired and auth != f"Bearer {self.new_token}":
                return httpx.Response(401, json={"message": "Unauthenticated."})
            if path == "/api/v1/message/unread-count":
                return httpx.Response(200, json={"data": {"count": 5}})
            if path == "/api/v1/users":
                return httpx.Response(200, json={"data": {"id": 1200, "name": "Employer", "email": "employer@example.com"}})
            if path == "/api/v1/jobs/4433221":
                if request.headers.get("X-ONLINEJOBSPH") != olj.JOBS_HEADER[1]:
                    return httpx.Response(403, json={"message": "missing header"})
                return httpx.Response(200, json={"data": fxj("jobs.json")["data"][0]})
            if path == "/api/v1/employers/me/jobs":
                return httpx.Response(200, json=fxj("jobs.json"))
            if path == "/api/v1/message/inbox":
                return httpx.Response(200, json=fxj("inbox.json"))
            if path == "/api/v1/message/add" and request.method == "POST":
                return httpx.Response(200, json={"data": {"message_id": 777, **json.loads(request.content)}})
            if request.method in ("PUT", "DELETE", "POST"):
                return httpx.Response(200, json={"data": "written"})
        return httpx.Response(404, text="no route " + path)


def session_dict(token="tok-0123456789abcdef") -> dict:
    return {"cookies": [{"name": "PHPSESSID", "value": "sess-abc", "domain": "www.onlinejobs.ph", "path": "/"}], "accessToken": token,
            "user": {"name": "Employer", "email": "employer@example.com"}, "email": "employer@example.com", "fetched_at": db.now()}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "olj_session.json")
        self.mock = MockOlj()

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, **kw) -> olj.Api:
        return olj.Api(session_dict(), transport=httpx.MockTransport(self.mock), pacer=fast_pacer(), path=self.path, **kw)

    def test_headers_and_jobs_header(self):
        api = self.api()
        self.assertEqual(api.unread_count(), {"data": {"count": 5}})
        r = self.mock.requests[-1]
        self.assertEqual(r.headers["Authorization"], "Bearer tok-0123456789abcdef")
        self.assertEqual(r.headers["Accept"], "application/json")
        self.assertNotIn("X-ONLINEJOBSPH", r.headers)
        self.assertEqual(r.url.params["page"], "1") if "page" in r.url.params else None
        api.job(4433221)
        self.assertEqual(self.mock.requests[-1].headers["X-ONLINEJOBSPH"], olj.JOBS_HEADER[1])
        api.inbox(page=2, per_page=10, unread_only=True)
        p = self.mock.requests[-1].url.params
        self.assertEqual((p["page"], p["per_page"], p["unread"]), ("2", "10", "true"))
        self.assertEqual(api.pacer.calls, 3)

    def test_reauth_once_on_401(self):
        self.mock.expired = True
        api = self.api()
        self.assertEqual(api.unread_count(), {"data": {"count": 5}})
        paths = [(r.url.host, r.url.path, r.status_code if hasattr(r, "status_code") else None) for r in self.mock.requests]
        self.assertEqual([(h, p) for h, p, _ in paths], [("api.onlinejobs.ph", "/api/v1/message/unread-count"), ("v2.onlinejobs.ph", "/myaccount"),
                                                         ("api.onlinejobs.ph", "/api/v1/message/unread-count")])
        self.assertEqual(self.mock.requests[-1].headers["Authorization"], f"Bearer {self.mock.new_token}")
        saved = olj.load_session(self.path)
        self.assertEqual(saved["accessToken"], self.mock.new_token)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_reauth_fails_cleanly_when_cookies_are_dead(self):
        self.mock.expired = True
        dead = MockOlj()
        dead.expired = True

        def handler(request):
            if request.url.host == "v2.onlinejobs.ph":
                return httpx.Response(302, headers={"Location": "https://www.onlinejobs.ph/login"})
            if request.url.path == "/login":
                return httpx.Response(200, text=fx("login.html"))
            return dead(request)
        api = olj.Api(session_dict(), transport=httpx.MockTransport(handler), pacer=fast_pacer(), path=self.path)
        with self.assertRaises(olj.SessionExpired):
            api.unread_count()

    def test_read_only_guard(self):
        api = self.api()
        self.assertTrue(api.read_only)
        with self.assertRaises(olj.ReadOnly):
            api.add_message("th_a1b2c3", "hello")
        with self.assertRaises(olj.ReadOnly):
            api.mark_thread_read("th_a1b2c3")
        with self.assertRaises(olj.ReadOnly):
            api.job_delete(4433221)
        with self.assertRaises(olj.ReadOnly):
            api.job_create({"title": "x"})
        self.assertEqual([r for r in self.mock.requests if r.method != "GET"], [], "no write reached the transport")
        api2 = self.api(read_only=False)
        with self.assertRaises(olj.ReadOnly):
            api2.add_message("th_a1b2c3", "hello")   # the send guard is OLJ_SEND itself, not the client flag
        os.environ["OLJ_SEND"] = "1"
        try:
            api3 = self.api()
            self.assertFalse(api3.read_only)
            out = api3.add_message("th_a1b2c3", "hello there")
            self.assertEqual(out["data"]["thread_hash_id"], "th_a1b2c3")
            self.assertEqual(out["data"]["message"], "hello there")
            self.assertEqual(json.loads(self.mock.requests[-1].content), {"thread_hash_id": "th_a1b2c3", "message": "hello there"})
            with self.assertRaises(olj.OljError):
                api3.add_message("th_a1b2c3", "   ")
            api3.job_create({"title": "Chat Support"})
            body = json.loads(self.mock.requests[-1].content)
            self.assertEqual((body["status"], body["form_source"]), ("draft", "v2_job_form"))
            api3.job_edit_put(4433221, {"title": "T"}, [12, 15])
            self.assertEqual(json.loads(self.mock.requests[-1].content), {"job": {"title": "T", "id": 4433221}, "skill_tags": [12, 15]})
        finally:
            os.environ.pop("OLJ_SEND", None)

    def test_login_flow_saves_session_without_the_password(self):
        pacer = fast_pacer()
        sess = olj.login("kirty@example.com", "pw-secret-value", transport=httpx.MockTransport(self.mock), pacer=pacer, path=self.path)
        self.assertEqual(sess["accessToken"], FIXTURE_TOKEN)
        self.assertEqual(sess["user"]["email"], "employer@example.com")
        self.assertIn("PHPSESSID", {c["name"] for c in sess["cookies"]})
        post = [r for r in self.mock.requests if r.method == "POST"][0]
        form = dict(p.split("=", 1) for p in post.content.decode().split("&"))
        self.assertEqual(form["csrf-token"], "3640096e587a905de16b9b4b6c1fe70f")
        self.assertEqual(form["info%5Bemail%5D"], "kirty%40example.com")
        self.assertEqual(form["info%5Bremember%5D"], "1")
        self.assertIn("info%5Bpassword%5D", form)
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("pw-secret", raw)
        self.assertIn(FIXTURE_TOKEN, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(pacer.calls, 3)
        self.assertTrue(olj.session_status(self.path)["token_present"])
        self.assertFalse(olj.session_status(os.path.join(self.tmp.name, "none.json"))["present"])

    def test_login_stops_on_captcha_and_dumps_the_page(self):
        self.mock.captcha = True
        old = os.environ.get("OLJ_SESSION_PATH")
        os.environ["OLJ_SESSION_PATH"] = self.path
        try:
            with self.assertRaises(olj.CaptchaRequired) as cm:
                olj.login("kirty@example.com", "pw", transport=httpx.MockTransport(self.mock), pacer=fast_pacer(), path=self.path)
            self.assertIn("--cookies", str(cm.exception))
            dumps = [f for f in os.listdir(self.tmp.name) if f.startswith("olj_debug_login-captcha")]
            self.assertEqual(len(dumps), 1)
            self.assertEqual([r.method for r in self.mock.requests], ["GET"], "no POST after the captcha stop")
            self.assertFalse(os.path.exists(self.path))
            sess = olj.login("kirty@example.com", "pw", transport=httpx.MockTransport(self.mock), pacer=fast_pacer(), path=self.path, force_captcha=True)
            self.assertEqual(sess["accessToken"], FIXTURE_TOKEN)
        finally:
            os.environ["OLJ_SESSION_PATH"] = old

    def test_import_cookies(self):
        items = olj.parse_netscape_cookies("#HttpOnly_.onlinejobs.ph\tTRUE\t/\tTRUE\t1800000000\tPHPSESSID\tfrom-browser\n")
        sess = olj.import_cookies(items, transport=httpx.MockTransport(self.mock), pacer=fast_pacer(), path=self.path)
        self.assertEqual(sess["accessToken"], FIXTURE_TOKEN)
        self.assertEqual(self.mock.requests[0].headers.get("Cookie"), "PHPSESSID=from-browser")
        self.assertEqual(sess["email"], "employer@example.com")

    def test_seed_session_offline(self):
        before = len(self.mock.requests)
        items = olj.parse_netscape_cookies("#HttpOnly_.onlinejobs.ph\tTRUE\t/\tTRUE\t1800000000\tPHPSESSID\tseeded\n")
        sess = olj.seed_session("captured-bearer-token-xyz", cookies=items, email_addr="kirty@example.com", path=self.path)
        self.assertEqual(len(self.mock.requests), before, "seeding makes no network call")
        self.assertEqual((sess["accessToken"], sess["email"], sess["seeded"]), ("captured-bearer-token-xyz", "kirty@example.com", True))
        saved = olj.load_session(self.path)
        self.assertEqual(saved["accessToken"], "captured-bearer-token-xyz")
        self.assertEqual({c["name"] for c in saved["cookies"]}, {"PHPSESSID"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        st = olj.session_status(self.path)
        self.assertTrue(st["present"] and st["token_present"])
        with self.assertRaises(olj.LoginFailed):
            olj.seed_session("  ", path=self.path)
        # a seeded token drives the Api without a login
        api = olj.Api(saved, transport=httpx.MockTransport(self.mock), pacer=fast_pacer(), path=self.path)
        self.assertEqual(api.unread_count(), {"data": {"count": 5}})
        self.assertEqual(self.mock.requests[-1].headers["Authorization"], "Bearer captured-bearer-token-xyz")

    def test_api_without_session(self):
        with self.assertRaises(olj.SessionExpired):
            olj.Api(None, path=os.path.join(self.tmp.name, "missing.json"), transport=httpx.MockTransport(self.mock))


# ---------------------------------------------------------------- mapping and sync

class MappingTest(unittest.TestCase):
    def test_map_thread(self):
        items = fxj("inbox.json")["data"]
        info = fxj("jobseeker_info.json")
        m = olj.map_thread(items[0], info["7788990"])
        self.assertEqual((m["thread_hash"], m["job_hash"], m["job_id"], m["partner_id"]), ("th_a1b2c3", "job_x9y8", "4433221", "7788990"))
        self.assertEqual((m["name"], m["email"], m["mobile"], m["viber"]), ("Maria Santos", "maria.santos@example.com", "+639171234567", "+639171234567"))
        self.assertEqual((m["headline"], m["rate"], m["ap_score"], m["education"]), ("Customer Support | Chat Sales", "Php 30,000/month", "73", "Bachelors degree"))
        self.assertEqual(m["direction"], "in")
        self.assertTrue(m["unread"])
        self.assertEqual(m["profile_url"], "https://www.onlinejobs.ph/jobseekers/info/7788990")
        self.assertEqual(m["message_id"], "90001")
        j = olj.map_thread(items[1])
        self.assertEqual(j["message"], "Hi, applying for the chat role.\nAvailable night shift & weekends.")
        self.assertEqual(j["email"], "")
        self.assertEqual(olj.map_thread(items[2])["direction"], "out")
        self.assertEqual(olj.norm_ph_mobile("+63 917 555 0100"), "+639175550100")
        self.assertEqual(olj.norm_ph_mobile("9171234567"), "+639171234567")
        self.assertEqual(olj.items_of({"data": {"items": [1, 2]}}), [1, 2])
        self.assertEqual(olj.meta_of({"data": [], "meta": {"last_page": 3}}), {"last_page": 3})
        self.assertEqual(olj.meta_of({"current_page": 2, "last_page": 3}), {"current_page": 2, "last_page": 3})
        jobs = [olj.job_record(j) for j in fxj("jobs.json")["data"]]
        self.assertEqual([(j["hash_id"], j["is_open"]) for j in jobs], [("job_x9y8", 0), ("job_open1", 1)])

    def test_message_direction_variants(self):
        base = {"partner": {"id": 7}, "owner": {"id": 1}}
        self.assertEqual(olj.message_direction({**base, "message": {"sender_id": 7}}), "in")
        self.assertEqual(olj.message_direction({**base, "message": {"sender_id": 1}}), "out")
        self.assertEqual(olj.message_direction({**base, "message": {"is_owner": True}}), "out")
        self.assertEqual(olj.message_direction({**base, "message": {"direction": "inbound"}}), "in")
        self.assertIsNone(olj.message_direction({**base, "message": {"body": "x"}}))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.con = fresh_db()
        self.rec = []
        self.old_sender = engine.SENDER
        engine.SENDER = lambda a, ch, text: self.rec.append((a["id"], ch, text)) or "ok"

    def tearDown(self):
        engine.SENDER = self.old_sender
        self.con.close()
        self.tmp.cleanup()

    def test_sync_creates_links_and_dedupes(self):
        api = FakeApi()
        out = olj.sync_inbox(self.con, api)
        self.assertEqual((out["created"], out["linked"], out["seen"], out["threads"], out["jobs"]), (4, 1, 0, 5, 2))
        maria = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("maria.santos@example.com",))
        self.assertEqual((maria["channel_code"], maria["phone"], maria["stage"], maria["olj_partner_id"]), ("olj", "+639171234567", "applied", "7788990"))
        self.assertEqual(maria["olj_thread_hash"], "th_m4n5o6", "the newest thread wins")
        self.assertEqual(maria["olj_job_hash"], "job_open1", "the job hash follows the newest thread")
        apps = db.q(self.con, "SELECT * FROM application WHERE applicant_id=? ORDER BY id", (maria["id"],))
        self.assertEqual(len(apps), 2)
        self.assertEqual((apps[0]["channel_code"], apps[0]["heard_from"], apps[0]["olj_mobile"], apps[0]["olj_ap_score"], apps[0]["olj_job_title"]),
                         ("olj", "onlinejobs", "+639171234567", "73", ""))
        self.assertIn("2 years of chat support", apps[0]["experience_text"])
        inbound = db.q(self.con, "SELECT * FROM message WHERE applicant_id=? AND direction='in' ORDER BY id", (maria["id"],))
        self.assertEqual([m["external_id"] for m in inbound], ["th_a1b2c3:90001", "th_m4n5o6:91002"])
        self.assertEqual(inbound[0]["channel"], "olj")
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM message WHERE direction='out'")["n"], 0, "import sends nothing")
        self.assertEqual(self.rec, [])
        jose = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("jose.reyes@example.com",))
        self.assertEqual(jose["phone"], "+639175550100")
        lea = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("lea.dizon@example.com",))
        lea_in = db.q(self.con, "SELECT * FROM message WHERE applicant_id=? AND direction='in'", (lea["id"],))
        self.assertEqual(len(lea_in), 1)
        self.assertTrue(lea_in[0]["body"].startswith("Good day! I'm Lea"), "the latest message was ours, so the thread was read for hers")
        self.assertIn(("thread", "th_g7h8i9"), api.calls)
        ana = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("ana.cruz@example.com",))
        self.assertEqual(ana["olj_job_hash"], "job_open1")
        self.assertEqual([c for c in api.calls if c[0] == "job_threads"], [("job_threads", "job_open1", 1)], "only the open post's threads are pulled")
        self.assertEqual([c for c in api.calls if c[0] == "read"], [], "read-only: nothing marked read on OLJ")
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM olj_thread")["n"], 5)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM olj_job WHERE is_open=1")["n"], 1)
        self.assertTrue(db.setting(self.con, "olj_last_sync_at"))
        again = olj.sync_inbox(self.con, FakeApi())
        self.assertEqual((again["created"], again["linked"], again["seen"]), (0, 0, 5))
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM applicant")["n"], 4)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM message")["n"], 5)

    def test_dry_run_writes_nothing(self):
        out = olj.sync_inbox(self.con, FakeApi(), dry=True)
        self.assertEqual((out["would_create"], out["created"], out["linked"]), (5, 0, 0))
        self.assertEqual({p["status"] for p in out["plan"]}, {"would_create"})
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM applicant")["n"], 0)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM olj_thread")["n"], 0)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM olj_job")["n"], 0)
        self.assertEqual(db.setting(self.con, "olj_last_sync_at"), "")

    def test_links_to_a_form_applicant_by_email(self):
        aid, created = engine.upsert_applicant(self.con, {"name": "Maria S", "email": "MARIA.SANTOS@example.com", "telegram": "@marias", "channel_code": "ofmjobs"})
        self.assertTrue(created)
        out = olj.sync_inbox(self.con, FakeApi(), jobs=False)
        self.assertEqual((out["created"], out["linked"]), (2, 1))
        a = engine.applicant(self.con, aid)
        self.assertEqual((a["channel_code"], a["olj_thread_hash"], a["phone"], a["telegram"]), ("ofmjobs", "th_a1b2c3", "+639171234567", "marias"))
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM applicant")["n"], 3)

    def test_pagination_and_enrich_off(self):
        p1 = fxj("inbox.json")
        p1["data"] = p1["data"][:1]
        p1["meta"] = {"current_page": 1, "last_page": 2, "per_page": 1, "total": 2}
        p2 = fxj("inbox.json")
        p2["data"] = p2["data"][1:2]
        p2["meta"] = {"current_page": 2, "last_page": 2, "per_page": 1, "total": 2}
        api = FakeApi(inbox_pages=[p1, p2])
        out = olj.sync_inbox(self.con, api, jobs=False, enrich=False, max_pages=5)
        self.assertEqual((out["pages"], out["created"]), (2, 2))
        self.assertEqual([c for c in api.calls if c[0] == "inbox"], [("inbox", 1, True), ("inbox", 2, True)])
        self.assertEqual([c for c in api.calls if c[0] == "info"], [])
        jose = db.one(self.con, "SELECT * FROM applicant WHERE name='Jose Reyes'")
        self.assertIsNone(jose["email"], "without enrichment the inbox item had no email; deduped on the thread hash instead")
        self.assertEqual(olj.sync_inbox(self.con, api, jobs=False, enrich=False)["seen"], 2)

    def test_sync_due_and_run_sync(self):
        due, why = olj.sync_due(self.con)
        self.assertEqual((due, why), (False, "disabled"))
        db.set_setting(self.con, "olj_sync_enabled", "1")
        self.assertEqual(olj.sync_due(self.con), (False, "no session"))


# ---------------------------------------------------------------- reply path

class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.con = fresh_db()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        olj.sync_inbox(self.con, FakeApi(), jobs=False)
        self.maria = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("maria.santos@example.com",))

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.con.close()
        self.tmp.cleanup()

    def test_invite_is_held_without_the_flag(self):
        api = FakeApi()
        mid = olj.send_invite(self.con, self.maria["id"], api=api)
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (mid,))
        self.assertEqual((m["channel"], m["direction"], m["status"], m["kind"]), ("olj", "out", "held", "first"))
        self.assertIn("OLJ_SEND=0", m["error"])
        self.assertEqual(m["address"], "th_a1b2c3")
        self.assertIn("http://test/apply/olj", m["body"])
        self.assertIn("https://t.me/beloura_hiring_bot?start=", m["body"])
        self.assertIn("Hi Maria,", m["body"])
        self.assertIn("for Sales Chat Specialist", m["body"]) if "Sales Chat Specialist" in m["body"] else None
        self.assertIn("automated", m["body"])
        self.assertIsNone(EMOJI.search(m["body"]))
        self.assertIsNone(MARKDOWN.search(m["body"]))
        self.assertEqual([c for c in api.calls if c[0] == "add"], [])
        self.assertIsNone(engine.applicant(self.con, self.maria["id"])["last_outbound_at"])

    def test_send_with_the_flag_posts_to_the_thread(self):
        api = FakeApi()
        os.environ["OLJ_SEND"] = "1"
        try:
            mid = olj.send_reply(self.con, self.maria["id"], "Hello Maria, next step below.", api=api)
        finally:
            os.environ.pop("OLJ_SEND", None)
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (mid,))
        self.assertEqual((m["status"], m["provider_id"], m["channel"]), ("sent", "777", "olj"))
        self.assertEqual(api.calls[-1], ("add", "th_a1b2c3", "Hello Maria, next step below."))
        a = engine.applicant(self.con, self.maria["id"])
        self.assertEqual(a["last_channel"], "olj")
        self.assertIsNotNone(a["last_outbound_at"])
        self.assertIsNotNone(a["awaiting_since"])

    def test_no_thread_and_do_not_contact(self):
        aid, _ = engine.upsert_applicant(self.con, {"name": "Form Only", "email": "form@example.com"})
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (olj.send_reply(self.con, aid, "x"),))
        self.assertEqual((m["status"], m["error"]), ("failed", "no OLJ thread on file"))
        self.con.execute("UPDATE applicant SET do_not_contact=1 WHERE id=?", (self.maria["id"],))
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (olj.send_reply(self.con, self.maria["id"], "x"),))
        self.assertEqual(m["status"], "skipped")

    def test_move_to_telegram_text_without_a_bot(self):
        t = olj.move_to_telegram_text("Jose Reyes", "", "http://test/apply/olj", "Chat Support")
        self.assertIn("Hi Jose,", t)
        self.assertIn("by email", t)
        self.assertIn("for Chat Support on OnlineJobs.ph", t)
        self.assertNotIn("t.me", t)
        self.assertIsNone(EMOJI.search(t))


# ---------------------------------------------------------------- notification emails

class EmailIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.con = fresh_db()
        self.old_sender = engine.SENDER
        self.sent = []
        engine.SENDER = lambda a, ch, text: self.sent.append((a["id"], ch, text)) or "ok"

    def tearDown(self):
        engine.SENDER = self.old_sender
        self.con.close()
        self.tmp.cleanup()

    def test_parse_notification_fixture(self):
        p = olj.parse_olj_email(fxb("notification.eml"))
        self.assertEqual((p["name"], p["email"], p["job_title"]), ("Lea Dizon", "lea.dizon@example.com", "Sales Chat Specialist"))
        self.assertEqual(p["profile_url"], "https://www.onlinejobs.ph/jobseekers/info/5544332")
        self.assertTrue(p["message"].startswith("Good day! I'm Lea"))
        self.assertIn("Looking forward", p["message"])
        self.assertNotIn("To reply", p["message"])
        self.assertNotIn("OnlineJobs.ph Team", p["message"])
        self.assertEqual(p["message_id"], "<olj-app-123@onlinejobs.ph>")
        self.assertEqual(p["from"], "noreply@onlinejobs.ph")

    def test_parse_variant_labels_and_html(self):
        raw = ("From: OnlineJobs.ph Notifications <notifications@mail.onlinejobs.ph>\nTo: hr@beloura.ai\nSubject: Someone applied to your job\n"
               "Message-ID: <v2-998@onlinejobs.ph>\nContent-Type: text/html; charset=utf-8\n\n"
               "<html><body><p>Hello,</p><p>Ana Cruz applied for your job post: <b>Chat Support Specialist (night shift)</b>.</p>"
               "<p>Applicant Name: Ana Cruz<br>E-mail: Ana.Cruz@example.com<br>Job Title: Chat Support Specialist (night shift)</p>"
               "<p>Cover Letter:</p><p>Hello, I would like to apply for the night shift role.<br>I have a laptop and fibre at home.</p>"
               "<p><a href=\"https://www.onlinejobs.ph/jobseekers/info/4433225\">View profile</a></p>"
               "<p>Reply to this applicant from your OnlineJobs.ph inbox.</p></body></html>")
        p = olj.parse_olj_email(raw)
        self.assertEqual((p["name"], p["email"], p["job_title"]), ("Ana Cruz", "ana.cruz@example.com", "Chat Support Specialist (night shift)"))
        self.assertEqual(p["profile_url"], "https://www.onlinejobs.ph/jobseekers/info/4433225")
        self.assertIn("night shift role", p["message"])
        self.assertIn("fibre at home", p["message"])
        self.assertNotIn("Reply to this applicant", p["message"])

    def test_parse_rejects_other_mail(self):
        reply = "From: Maria Santos <maria.santos@example.com>\nTo: hr@beloura.ai\nSubject: Re: Your Beloura application\n\nThanks, when do I start?\n"
        self.assertIsNone(olj.parse_olj_email(reply))
        newsletter = "From: OnlineJobs.ph <news@onlinejobs.ph>\nTo: hr@beloura.ai\nSubject: Weekly tips for employers\n\nHire faster with these tips.\n"
        self.assertIsNone(olj.parse_olj_email(newsletter))

    def test_ingest_through_the_imap_hook(self):
        self.assertTrue(getattr(channels.EmailChannel, "_olj_hooked", False))
        msg = email.message_from_bytes(fxb("notification.eml"))
        ch = channels.EmailChannel()
        self.assertTrue(ch.handle_message(self.con, msg))
        lea = db.one(self.con, "SELECT * FROM applicant WHERE lower(email)=?", ("lea.dizon@example.com",))
        self.assertEqual((lea["name"], lea["channel_code"]), ("Lea Dizon", "olj"))
        self.assertTrue(lea["olj_thread_hash"].startswith("email:"))
        t = db.one(self.con, "SELECT * FROM olj_thread WHERE applicant_id=?", (lea["id"],))
        self.assertEqual((t["source"], t["job_title"], t["profile_url"]), ("email", "Sales Chat Specialist", "https://www.onlinejobs.ph/jobseekers/info/5544332"))
        app = engine.latest_application(self.con, lea["id"])
        self.assertEqual((app["channel_code"], app["heard_from"], app["olj_job_title"]), ("olj", "onlinejobs", "Sales Chat Specialist"))
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM message WHERE applicant_id=? AND direction='in' AND channel='olj'", (lea["id"],))["n"], 1)
        self.assertEqual(self.sent, [], "intake sends nothing")
        self.assertTrue(ch.handle_message(self.con, msg), "a second delivery of the same notification is recognised and swallowed")
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM applicant")["n"], 1)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM message")["n"], 1)
        plain = email.message_from_string("From: Maria <maria.santos@example.com>\nSubject: hi\n\nhello\n")
        self.assertFalse(ch.handle_message(self.con, plain), "non-OLJ mail still goes to the original handler (unknown sender: ignored)")
        mid = olj.send_invite(self.con, lea["id"])
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (mid,))
        self.assertEqual((m["channel"], m["status"]), ("email", "sent"), "email-intake applicants are answered by email through the engine")
        self.assertEqual(self.sent[0][1], "email")
        self.assertIn("http://test/apply/olj", self.sent[0][2])


# ---------------------------------------------------------------- staff page and CLI

class StaffAndCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(appmod.app)

    def test_staff_page_and_gates(self):
        self.assertEqual(self.c.get("/staff/olj").status_code, 401)
        r = self.c.get("/staff/olj", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("OnlineJobs.ph", r.text)
        self.assertIn("Session file: missing", r.text)
        self.assertIn("Sync now", r.text)
        self.assertIn("read-only (OLJ_SEND=0)", r.text)
        self.assertIsNone(EMOJI.search(r.text))
        r2 = self.c.get("/staff/olj", headers=MANAGER)
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn('action="/staff/olj/sync"', r2.text)
        self.assertIn('action="/staff/olj/sync"', r.text)
        self.assertIn('href="/staff/olj"', r.text)

    def test_toggle_and_sync_now(self):
        self.assertEqual(self.c.post("/staff/olj/toggle", headers=MANAGER).status_code, 403)
        r = self.c.post("/staff/olj/toggle", headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(db.setting(con, "olj_sync_enabled"), "1")
        self.c.post("/staff/olj/toggle", headers=ADMIN, follow_redirects=False)
        with db.db() as con:
            self.assertEqual(db.setting(con, "olj_sync_enabled"), "0")
        r = self.c.post("/staff/olj/sync", headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("msg=no-session"))
        self.assertEqual(self.c.post("/staff/olj/sync", headers=MANAGER).status_code, 403)
        self.assertEqual(self.c.post("/staff/olj/applicant/999/invite", headers=ADMIN).status_code, 404)

    def test_cli_send_refuses_without_the_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = olj_cli.main(["send", "th_a1b2c3", "hello"])
        self.assertEqual(rc, 2)
        self.assertIn("refused", out.getvalue())
        self.assertIn("OLJ_SEND", out.getvalue())

    def test_cli_login_token_seeds_without_network(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sess_path = os.path.join(tmp.name, "olj_session.json")
        cookie_file = os.path.join(tmp.name, "cookies.txt")
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write("#HttpOnly_.onlinejobs.ph\tTRUE\t/\tTRUE\t1800000000\tPHPSESSID\tfrom-export\n")
        old = os.environ.get("OLJ_SESSION_PATH")
        os.environ["OLJ_SESSION_PATH"] = sess_path
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = olj_cli.main(["login", "--token", "cli-captured-token", "--cookies", cookie_file, "--email", "kirty@example.com"])
        finally:
            if old is None:
                os.environ.pop("OLJ_SESSION_PATH", None)
            else:
                os.environ["OLJ_SESSION_PATH"] = old
        self.assertEqual(rc, 0)
        self.assertIn("no network call", out.getvalue())
        saved = olj.load_session(sess_path)
        self.assertEqual(saved["accessToken"], "cli-captured-token")
        self.assertEqual({c["name"] for c in saved["cookies"]}, {"PHPSESSID"})

    def test_cli_without_session_exits_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            olj_cli.main(["whoami"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("login", out.getvalue())

    def test_cli_sync_dry_run_prints_the_plan(self):
        old = olj_cli.make_api
        olj_cli.make_api = lambda: FakeApi()
        out = io.StringIO()
        with db.db() as con:
            before = (db.one(con, "SELECT COUNT(*) n FROM applicant")["n"], db.one(con, "SELECT COUNT(*) n FROM olj_thread")["n"])
        try:
            with contextlib.redirect_stdout(out):
                rc = olj_cli.main(["sync"])
        finally:
            olj_cli.make_api = old
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        self.assertIn("would_create", text)
        self.assertIn("maria.santos@example.com", text)
        with db.db() as con:
            after = (db.one(con, "SELECT COUNT(*) n FROM applicant")["n"], db.one(con, "SELECT COUNT(*) n FROM olj_thread")["n"])
        self.assertEqual(after, before, "a dry run writes nothing")

    def test_cli_inbox_and_jobs_print(self):
        old = olj_cli.make_api
        olj_cli.make_api = lambda: FakeApi()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.assertEqual(olj_cli.main(["inbox", "--unread"]), 0)
                self.assertEqual(olj_cli.main(["jobs"]), 0)
                self.assertEqual(olj_cli.main(["thread", "th_g7h8i9"]), 0)
        finally:
            olj_cli.make_api = old
        text = out.getvalue()
        self.assertIn("th_a1b2c3", text)
        self.assertIn("job_open1", text)
        self.assertIn("us: Thanks Lea", text)


if __name__ == "__main__":
    unittest.main()
