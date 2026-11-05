"""Job-post renderers obey each channel's constraints (no links on OLJ / ofmjobs, apply link elsewhere, code word present, OLJ title length,
OLJ wage and hours fields), the paste-queue cadence logic, code-word attribution, the staff page and its actions, and no emoji or AI mentions."""
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.TemporaryDirectory()
os.environ.update({"HIRE_NO_WORKER": "1", "HIRE_SECRET": "test-posts", "HIRE_DB_PATH": os.path.join(TMP.name, "posts.db"), "HIRE_PUBLIC_URL": "http://test",
                   "BELOURA_GATEWAY_TOKEN": "gw", "PANEL_PASSWORD": "pw", "OPENROUTER_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_USERNAME": "beloura_hiring_bot",
                   "DISCORD_BOT_TOKEN": "", "SMTP_HOST": ""})

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import posts  # noqa: E402

ADMIN = {"X-Gateway-Token": "gw", "Remote-User": "mikey", "Remote-Groups": "id-admins,hiring-admin", "Remote-Name": "Mikey"}
MANAGER = {"X-Gateway-Token": "gw", "Remote-User": "ian", "Remote-Groups": "hiring-manager", "Remote-Name": "Ian"}
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
URL = re.compile(r"https?://|www\.|t\.me/|\.ai\b|\.com\b|\.ph\b", re.I)
AI = re.compile(r"\b(AI|artificial intelligence|chatbot|bot|automated|assistant|GPT|LLM)\b")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.sp = dict(posts.SPEC_DEFAULTS)
        self.r = {c: posts.render_spec(self.sp, c, "http://test", "beloura_hiring_bot") for c in posts.CHANNELS}

    def test_every_channel_passes_its_own_check(self):
        for c, r in self.r.items():
            self.assertEqual(r["violations"], [], c)
            self.assertTrue(r["title"] and r["body"], c)

    def test_code_word_present_everywhere(self):
        for c, r in self.r.items():
            self.assertIn(r["code_word"], r["body"], c)
            self.assertEqual(r["code_word"], posts.SPEC_DEFAULTS[f"post_code_{c}"])
        self.assertEqual(len({r["code_word"] for r in self.r.values()}), len(posts.CHANNELS), "code words must differ per channel")

    def test_olj_has_no_links_and_asks_for_telegram_username(self):
        r = self.r["olj"]
        self.assertIsNone(URL.search(r["body"]), r["body"])
        self.assertLessEqual(len(r["title"]), 80)
        self.assertIn("telegram username", r["body"].lower())
        self.assertIn("FAST FINGERS", r["body"])
        self.assertIn("Salary per month", r["fields"])
        self.assertIn("Hours per week", r["fields"])
        self.assertEqual(r["fields"]["Salary per month"], "USD 208-416/month base + 2% commission")
        self.assertEqual(r["fields"]["Hours per week"], "24-48")
        self.assertEqual(r["apply_url"], "http://test/apply/olj")

    def test_olj_title_is_cut_at_80_and_long_titles_are_flagged(self):
        sp = {**self.sp, "post_title": "A" * 95}
        r = posts.render_spec(sp, "olj", "http://test")
        self.assertEqual(len(r["title"]), 80)
        self.assertEqual(r["violations"], [])
        self.assertIn("title over 80 characters", posts.check("olj", {**r, "title": "A" * 81}))

    def test_olj_check_catches_a_link(self):
        r = self.r["olj"]
        v = posts.check("olj", {**r, "body": r["body"] + "\nApply at hire.beloura.ai/apply/olj"})
        self.assertTrue(any("link" in x for x in v), v)
        v = posts.check("olj", {**r, "body": r["body"] + "\nhttps://example.com"})
        self.assertTrue(any("link" in x for x in v), v)

    def test_olj_wage_from_hours(self):
        self.assertEqual(posts.olj_wage_per_month({"post_olj_hours_per_week": "40"}), "USD 347/month base + 2% commission")
        self.assertEqual(posts.olj_wage_per_month({"post_olj_hours_per_week": "24 to 48 hours"}), "USD 208-416/month base + 2% commission")
        self.assertIn("USD", posts.olj_wage_per_month({}))

    def test_ofmjobs_has_no_links_or_contact_details(self):
        r = self.r["ofmjobs"]
        self.assertIsNone(URL.search(r["body"]), r["body"])
        self.assertNotRegex(r["body"], r"@[a-z0-9_]{3,}")
        self.assertNotIn("telegram", r["body"].lower())
        self.assertIn("CLOSER", r["body"])
        self.assertIn("Discord", r["body"], "naming the tool is fine; a handle is not")
        v = posts.check("ofmjobs", {**r, "body": r["body"] + "\nTelegram username: @someone"})
        self.assertTrue(any("contact" in x for x in v), v)
        v = posts.check("ofmjobs", {**r, "body": r["body"] + "\nmessage us on WhatsApp"})
        self.assertTrue(any("contact" in x for x in v), v)

    def test_facebook_reddit_telegram_carry_their_apply_link(self):
        for c in ("facebook", "reddit", "telegram"):
            r = self.r[c]
            self.assertEqual(r["apply_url"], f"http://test/apply/{c}")
            self.assertIn(r["apply_url"], r["body"], c)
            self.assertEqual(r["body"].count("http://test/apply/"), 1, c)
        self.assertIn("https://t.me/beloura_hiring_bot", self.r["telegram"]["body"])
        self.assertLess(len(self.r["facebook"]["body"]), 700, "facebook post stays short")
        v = posts.check("reddit", {**self.r["reddit"], "body": "no link here CONVO PRO"})
        self.assertIn("apply link missing", v)
        v = posts.check("telegram", {**self.r["telegram"], "body": "http://test/apply/telegram NIGHT OWL"})
        self.assertIn("bot deep link missing", v)

    def test_public_url_from_env(self):
        r = posts.render_spec(self.sp, "reddit", "https://hire.beloura.ai/")
        self.assertIn("https://hire.beloura.ai/apply/reddit", r["body"])
        self.assertNotIn("hire.beloura.ai//", r["body"])

    def test_no_emoji_and_no_ai_mentions_in_any_render(self):
        for c, r in self.r.items():
            txt = r["title"] + "\n" + r["body"] + "\n" + "\n".join(f"{k} {v}" for k, v in r["fields"].items())
            self.assertIsNone(EMOJI.search(txt), c)
            self.assertIsNone(AI.search(r["title"] + "\n" + r["body"]), c)
        self.assertIn("mentions AI or a bot", posts.check("reddit", {**self.r["reddit"], "body": self.r["reddit"]["body"] + " our AI assistant"}))
        self.assertIn("emoji in text", posts.check("reddit", {**self.r["reddit"], "body": self.r["reddit"]["body"] + " \U0001F525"}))

    def test_spec_facts_appear(self):
        for c, r in self.r.items():
            self.assertIn("$2/hr", r["body"], c)
            self.assertIn("2% commission", r["body"], c)
        for c in ("olj", "ofmjobs", "reddit"):
            b = self.r[c]["body"]
            self.assertIn("Thursdays", b)
            self.assertIn("PH time", b)
            self.assertIn("Infloww", b)
            self.assertIn("18 or over", b)
            self.assertIn("laptop or desktop", b)
            self.assertIn("Training paid once you pass", b)

    def test_unknown_channel(self):
        with self.assertRaises(KeyError):
            posts.render_spec(self.sp, "linkedin")


class CadenceTest(unittest.TestCase):
    def test_due_state(self):
        now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        st = posts.due_state(None, 7, now)
        self.assertTrue(st["due"])
        self.assertIsNone(st["last"])
        st = posts.due_state(db.fmt_ts(now - timedelta(days=2)), 7, now)
        self.assertFalse(st["due"])
        self.assertEqual(st["next"], now + timedelta(days=5))
        self.assertEqual(st["overdue_days"], 0.0)
        self.assertEqual(st["days_since"], 2.0)
        st = posts.due_state(db.fmt_ts(now - timedelta(days=8)), 7, now)
        self.assertTrue(st["due"])
        self.assertEqual(st["overdue_days"], 1.0)
        st = posts.due_state(db.fmt_ts(now - timedelta(days=7)), 7, now)
        self.assertTrue(st["due"], "exactly on the day counts as due")

    def test_default_cadences(self):
        sp = posts.SPEC_DEFAULTS
        self.assertEqual(posts.cadence_days(sp, "olj"), 7)
        self.assertEqual(posts.cadence_days(sp, "reddit"), 7)
        self.assertEqual(posts.cadence_days(sp, "telegram"), 7)
        self.assertLess(posts.cadence_days(sp, "facebook"), 7, "FB rota is more frequent than weekly")
        self.assertEqual(posts.cadence_days({"post_cadence_olj": "junk"}, "olj"), 7)


class DbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(appmod.app)

    def setUp(self):
        with db.db() as con:
            for t in ("post_log", "applicant", "application", "message", "stage_event"):
                con.execute(f"DELETE FROM {t}")
            for k in posts.SPEC_DEFAULTS:
                con.execute("DELETE FROM settings WHERE key=?", (k,))

    def test_queue_and_mark_posted(self):
        now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        with db.db() as con:
            posts.mark_posted(con, "olj", by="mikey", note="post 1", at=now - timedelta(days=2))
            posts.mark_posted(con, "reddit", by="mikey", at=now - timedelta(days=9))
            q = {r["channel"]: r for r in posts.queue(con, now)}
            self.assertEqual([r["channel"] for r in posts.queue(con, now)], posts.CHANNELS)
            with self.assertRaises(KeyError):
                posts.mark_posted(con, "nope")
        self.assertFalse(q["olj"]["due"])
        self.assertEqual(q["olj"]["last_log"]["note"], "post 1")
        self.assertEqual(q["olj"]["cadence_days"], 7)
        self.assertTrue(q["reddit"]["due"])
        self.assertEqual(q["reddit"]["overdue_days"], 2.0)
        for c in ("ofmjobs", "facebook", "telegram"):
            self.assertTrue(q[c]["due"], c)
            self.assertIsNone(q[c]["last_at"])

    def test_spec_saved_in_settings_and_sanitised(self):
        with db.db() as con:
            posts.save_spec(con, {"post_title": "  New title ", "post_code_olj": "quick hands!", "post_cadence_facebook": "0", "post_cadence_reddit": "10"})
            sp = posts.spec(con)
            self.assertEqual(sp["post_title"], "New title")
            self.assertEqual(sp["post_code_olj"], "QUICK HANDS")
            self.assertEqual(sp["post_cadence_facebook"], "1")
            self.assertEqual(sp["post_cadence_reddit"], "10")
            self.assertEqual(sp["post_pay"], posts.SPEC_DEFAULTS["post_pay"], "untouched keys keep their default")
            r = posts.render(con, "olj")
            self.assertIn("QUICK HANDS", r["body"])
            self.assertEqual(r["violations"], [])

    def test_code_word_channel(self):
        self.assertEqual(posts.code_word_channel("Subject: FAST FINGERS - 62 wpm"), "olj")
        self.assertEqual(posts.code_word_channel("fast   fingers, available nights"), "olj")
        self.assertEqual(posts.code_word_channel("I am a people person"), "facebook")
        self.assertIsNone(posts.code_word_channel("I am one of the CLOSERS"), "whole word only")
        self.assertIsNone(posts.code_word_channel(""))
        self.assertIsNone(posts.code_word_channel(None))
        self.assertEqual(posts.code_word_channel("quick hands here", {**posts.SPEC_DEFAULTS, "post_code_olj": "QUICK HANDS"}), "olj")

    def test_attribute_moves_direct_applicants_only(self):
        with db.db() as con:
            a1, _ = engine.upsert_applicant(con, {"name": "Ana", "email": "ana@example.com", "channel_code": "direct"}, actor="t")
            engine.save_application(con, a1, {"channel_code": "direct", "why": "Saw your post. PEOPLE PERSON. I type 60 wpm."})
            a2, _ = engine.upsert_applicant(con, {"name": "Ben", "email": "ben@example.com", "channel_code": "olj"}, actor="t")
            engine.save_application(con, a2, {"channel_code": "olj", "why": "CLOSER"})
            a3, _ = engine.upsert_applicant(con, {"name": "Cy", "email": "cy@example.com", "channel_code": ""}, actor="t")
            engine.save_application(con, a3, {"channel_code": "", "why": "nothing"})
            engine.record_inbound(con, a3, "telegram", "hi, code word CONVO PRO", external_id="x1")
            found = posts.attribute(con)
            self.assertEqual(found, {a1: "facebook", a3: "reddit"})
            self.assertEqual(engine.applicant(con, a1)["channel_code"], "facebook")
            self.assertEqual(engine.applicant(con, a2)["channel_code"], "olj", "a tagged apply link wins over the word")
            self.assertEqual(engine.applicant(con, a3)["channel_code"], "reddit")
            self.assertEqual(posts.attribute(con), {}, "idempotent")

    def test_staff_page_and_actions(self):
        r = self.c.get("/staff/posts", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        for c in posts.CHANNELS:
            self.assertIn(posts.CHANNEL_LABELS[c], r.text)
            self.assertIn(posts.SPEC_DEFAULTS[f"post_code_{c}"], r.text)
        self.assertIn("data-copy=", r.text)
        self.assertIn("never", r.text)
        r = self.c.post("/staff/posts/olj/posted", data={"note": "OLJ listing 123"}, headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.c.get("/staff/posts", headers=MANAGER)
        self.assertIn("OLJ listing 123", r.text)
        self.assertIn(", ian", r.text)
        self.assertEqual(self.c.post("/staff/posts/nope/posted", headers=MANAGER).status_code, 404)
        r = self.c.post("/staff/posts", data={"post_title": "Manager title"}, headers=MANAGER)
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/staff/posts", data={"post_title": "Admin title"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.c.get("/staff/posts/olj.txt", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.text.startswith("Admin title"))
        self.assertIn("Salary per month:", r.text)
        self.assertIsNone(re.search(r"https?://", r.text))
        self.assertEqual(self.c.get("/staff/posts/nope.txt", headers=MANAGER).status_code, 404)
        self.assertEqual(self.c.get("/staff/posts").status_code, 401)
        r = self.c.post("/staff/posts/attribute", headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("attributed=0", r.headers["location"])

    def test_no_emoji_in_new_files(self):
        for fn in ("posts.py", "report.py", os.path.join("templates", "posts.html"), os.path.join("templates", "report.html")):
            with open(os.path.join(ROOT, fn), encoding="utf-8") as fh:
                self.assertIsNone(EMOJI.search(fh.read()), fn)


if __name__ == "__main__":
    unittest.main()
