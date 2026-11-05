"""Weekly funnel report over a seeded DB: per-channel counts for the last 7 days, the previous 7 for comparison, time to hire, no-show rate,
LLM cost per applicant, the plain-text version, the Monday 09:00 Asia/Manila once-per-week posting logic (dry-run and fake Discord), the staff page."""
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.TemporaryDirectory()
os.environ.update({"HIRE_NO_WORKER": "1", "HIRE_SECRET": "test-report", "HIRE_DB_PATH": os.path.join(TMP.name, "report.db"), "HIRE_PUBLIC_URL": "http://test",
                   "BELOURA_GATEWAY_TOKEN": "gw", "PANEL_PASSWORD": "pw", "OPENROUTER_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "DISCORD_BOT_TOKEN": "", "SMTP_HOST": ""})

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import db  # noqa: E402
import report  # noqa: E402

MANAGER = {"X-Gateway-Token": "gw", "Remote-User": "ian", "Remote-Groups": "hiring-manager", "Remote-Name": "Ian"}
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
MANILA = ZoneInfo("Asia/Manila")
NOW = datetime(2026, 11, 9, 1, 5, tzinfo=timezone.utc)   # Monday 09:05 Asia/Manila


def ago(days: float) -> str:
    return db.fmt_ts(NOW - timedelta(days=days))


class FakeDiscord:
    configured = True

    def __init__(self):
        self.posted = []

    def post_staff(self, text, aid=0, url="", channel_id=None):
        self.posted.append((channel_id, text))
        return str(100 + len(self.posted))


def seed(con):
    def applicant(name, code, created_days):
        ts = ago(created_days)
        cur = con.execute("INSERT INTO applicant (name, email, channel_code, stage, stage_changed_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                          (name, f"{name.lower()}@example.com", code, "applied", ts, ts, ts))
        return cur.lastrowid

    def event(aid, to_stage, days, frm="applied"):
        con.execute("INSERT INTO stage_event (applicant_id, from_stage, to_stage, actor, at) VALUES (?,?,?,?,?)", (aid, frm, to_stage, "t", ago(days)))
        con.execute("UPDATE applicant SET stage=? WHERE id=?", (to_stage, aid))

    def interview(aid, status, created_days, updated_days):
        con.execute("INSERT INTO interview (applicant_id, manager_id, start_at, end_at, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (aid, 1, ago(created_days), ago(created_days), status, ago(created_days), ago(updated_days)))

    def llm(aid, cost, days):
        con.execute("INSERT INTO llm_call (applicant_id, model, prompt_version, cost_usd, created_at) VALUES (?,?,?,?,?)", (aid, "m", "v", cost, ago(days)))

    # this week: OLJ 3 applied, FB 1 applied
    a = applicant("Ana", "olj", 6)          # hired after 4 days
    event(a, "screened_in", 5.9)
    event(a, "interview_passed", 5.5)
    event(a, "assessment_sent", 5.5)
    event(a, "assessment_done", 5)
    event(a, "passed", 5)
    interview(a, "done", 4.5, 2.5)
    event(a, "final_done", 2.5)
    event(a, "hired", 2)
    llm(a, 0.06, 5.5)
    llm(a, 0.04, 5)
    b = applicant("Ben", "olj", 3)          # screened in, booked, no-show
    event(b, "screened_in", 2.9)
    interview(b, "moved", 2.8, 2.7)         # rescheduled: the moved row must not double count
    interview(b, "no_show", 2.7, 1)
    llm(b, 0.05, 2.5)
    c = applicant("Cy", "olj", 1)           # just applied, screened out
    event(c, "screened_out", 0.9)
    d = applicant("Dee", "facebook", 2)
    event(d, "screened_in", 1.9)
    event(d, "ghosted", 0.5)
    # last week: OLJ 2 applied, one screened in and declined; no hires
    e = applicant("Eve", "olj", 9)
    event(e, "screened_in", 8.9)
    event(e, "declined", 8)
    llm(e, 0.20, 8.5)
    f = applicant("Fay", "olj", 10)
    # too old for either window
    g = applicant("Gus", "reddit", 20)
    event(g, "screened_in", 19)
    event(g, "hired", 15)


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(appmod.app)

    def setUp(self):
        with db.db() as con:
            for t in ("applicant", "application", "message", "stage_event", "interview", "llm_call", "post_log"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM settings WHERE key IN (?,?)", (report.SENT_KEY, report.LAST_KEY))
            db.set_setting(con, "discord_ch_weekly_report", "555")
            con.execute("DELETE FROM worker_runs")
            seed(con)

    def test_counts_this_week(self):
        with db.db() as con:
            w = report.weekly(con, NOW)
        t = w["totals"]
        self.assertEqual(t["applied"], 4)
        self.assertEqual(t["screened_in"], 3)
        self.assertEqual(t["interview_passed"], 1)
        self.assertEqual(t["assessment_sent"], 1)
        self.assertEqual(t["assessment_done"], 1)
        self.assertEqual(t["passed"], 1)
        self.assertEqual(t["final_booked"], 2, "Ana once, Ben once despite the reschedule")
        self.assertEqual(t["hired"], 1)
        self.assertEqual(t["declined"], 0)
        self.assertEqual(t["ghosted"], 1)
        self.assertEqual(t["no_show"], 1)
        by = {c["code"]: c for c in w["channels"]}
        self.assertEqual(by["olj"]["cur"]["applied"], 3)
        self.assertEqual(by["olj"]["cur"]["hired"], 1)
        self.assertEqual(by["facebook"]["cur"]["applied"], 1)
        self.assertEqual(by["facebook"]["cur"]["ghosted"], 1)
        self.assertNotIn("reddit", by, "20-day-old applicant is outside both windows")
        self.assertEqual([c["code"] for c in w["channels"]][0], "olj", "sorted by applied")

    def test_previous_week_and_deltas(self):
        with db.db() as con:
            w = report.weekly(con, NOW)
        p = w["prev_totals"]
        self.assertEqual(p["applied"], 2)
        self.assertEqual(p["screened_in"], 1)
        self.assertEqual(p["declined"], 1)
        self.assertEqual(p["hired"], 0)
        self.assertEqual(w["deltas"]["applied"], 2)
        self.assertEqual(w["deltas"]["declined"], -1)
        self.assertEqual(w["deltas"]["hired"], 1)
        by = {c["code"]: c for c in w["channels"]}
        self.assertEqual(by["olj"]["prev"]["applied"], 2)
        self.assertEqual(by["facebook"]["prev"]["applied"], 0)

    def test_time_to_hire_no_show_and_llm(self):
        with db.db() as con:
            w = report.weekly(con, NOW)
        self.assertEqual(w["tth"]["n"], 1)
        self.assertEqual(w["tth"]["median_days"], 4.0)
        self.assertEqual(w["tth_prev"]["n"], 0)
        self.assertEqual(w["no_show"], {"no_show": 1, "held": 1, "rate": 50})
        self.assertIsNone(w["no_show_prev"]["rate"])
        self.assertEqual(w["llm"]["calls"], 3)
        self.assertAlmostEqual(w["llm"]["usd"], 0.15)
        self.assertEqual(w["llm"]["applicants"], 2)
        self.assertAlmostEqual(w["llm"]["per_applicant"], 0.075)
        self.assertAlmostEqual(w["llm"]["by_channel"]["olj"]["per_applicant"], 0.075)
        self.assertAlmostEqual(w["llm_prev"]["usd"], 0.20)

    def test_empty_db(self):
        with db.db() as con:
            for t in ("applicant", "stage_event", "interview", "llm_call"):
                con.execute(f"DELETE FROM {t}")
            w = report.weekly(con, NOW)
            txt = report.weekly_text(con, NOW)
        self.assertEqual(w["channels"], [])
        self.assertEqual(sum(w["totals"].values()), 0)
        self.assertIsNone(w["llm"]["per_applicant"])
        self.assertIn("no applicants in either week", txt)
        self.assertIn("no hires this week", txt)
        self.assertIn("no finals held", txt)

    def test_weekly_text(self):
        with db.db() as con:
            txt = report.weekly_text(con, NOW)
        self.assertIn("Weekly hiring report, Mon 2 Nov to Mon 9 Nov (Asia/Manila)", txt)
        self.assertIn("Applied 4 (+2)", txt)
        self.assertIn("Hired 1 (+1)", txt)
        self.assertIn("Declined 0 (-1)", txt)
        self.assertIn("No-shows 1 (+1)", txt)
        self.assertIn("OnlineJobs.ph: 3 / 2 / 1 / 1 / 2 / 1  (2 / 1 / 0 / 0 / 0 / 0)", txt)
        self.assertIn("Facebook groups: 1 / 1 / 0 / 0 / 0 / 0  (0 / 0 / 0 / 0 / 0 / 0)", txt)
        self.assertIn("Time to hire: median 4.0 d, mean 4.0 d over 1 hire(s).", txt)
        self.assertIn("No-show rate at finals: 50% (1 of 2).", txt)
        self.assertIn("LLM cost: $0.15 over 3 call(s) and 2 applicant(s), $0.075 per applicant (last week $0.20).", txt)
        self.assertIn("http://test/staff/report", txt)
        self.assertIsNone(EMOJI.search(txt))
        self.assertNotIn("**", txt)
        self.assertNotIn("#", txt.split("Full report")[0])
        self.assertLess(len(txt), report.DISCORD_LIMIT)

    def test_chunks(self):
        txt = "\n".join(f"line {i} " + "x" * 50 for i in range(80))
        parts = report.chunks(txt, 500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual("\n".join(parts), txt)
        self.assertEqual(report.chunks("short"), ["short"])

    def test_report_time(self):
        mon_0859 = datetime(2026, 11, 9, 8, 59, tzinfo=MANILA)
        mon_0900 = datetime(2026, 11, 9, 9, 0, tzinfo=MANILA)
        mon_2300 = datetime(2026, 11, 9, 23, 0, tzinfo=MANILA)
        sun = datetime(2026, 11, 8, 12, 0, tzinfo=MANILA)
        tue = datetime(2026, 11, 10, 9, 0, tzinfo=MANILA)
        self.assertFalse(report.is_report_time(mon_0859))
        self.assertTrue(report.is_report_time(mon_0900))
        self.assertTrue(report.is_report_time(mon_2300), "a restart later on Monday still posts")
        self.assertFalse(report.is_report_time(sun))
        self.assertFalse(report.is_report_time(tue))
        self.assertEqual(report.week_key(mon_0900), "2026-W46")
        self.assertEqual(report.week_key(sun), "2026-W45")

    def test_maybe_post_weekly_once_per_week_to_the_weekly_channel(self):
        fake = FakeDiscord()
        with db.db() as con:
            out = report.maybe_post_weekly(con, fake, NOW - timedelta(days=1))
            self.assertEqual(out, {"skipped": "not report time"})
            self.assertEqual(fake.posted, [])
            out = report.maybe_post_weekly(con, fake, NOW)
            self.assertEqual(out["week"], "2026-W46")
            self.assertEqual(out["message_ids"], ["101"])
            self.assertFalse(out["dry_run"])
            self.assertEqual(len(fake.posted), 1)
            self.assertEqual(fake.posted[0][0], 555)
            self.assertIn("Weekly hiring report", fake.posted[0][1])
            out = report.maybe_post_weekly(con, fake, NOW + timedelta(hours=5))
            self.assertEqual(out, {"skipped": "already sent for 2026-W46"})
            self.assertEqual(len(fake.posted), 1)
            out = report.maybe_post_weekly(con, fake, NOW + timedelta(days=7))
            self.assertEqual(out["week"], "2026-W47")
            self.assertEqual(len(fake.posted), 2)
            self.assertEqual(db.setting(con, report.SENT_KEY), "2026-W47")
            last = db.jl(db.setting(con, report.LAST_KEY), {})
            self.assertEqual(last["channel_id"], 555)

    def test_dry_run_and_missing_channel(self):
        with db.db() as con:
            out = report.maybe_post_weekly(con, None, NOW)
            self.assertTrue(out["dry_run"])
            self.assertEqual(db.setting(con, report.SENT_KEY), "2026-W46", "dry-run still counts as sent")
            db.set_setting(con, "discord_ch_weekly_report", "")
            db.set_setting(con, "discord_weekly_report_channel_id", "777")
            self.assertEqual(report.weekly_report_channel_id(con), 777, "legacy key name also honoured")
            db.set_setting(con, "discord_weekly_report_channel_id", "")
            out = report.post_weekly(con, FakeDiscord(), NOW)
            self.assertIn("skipped", out)
            self.assertEqual(out["channel_id"], 0)

    def test_worker_tick_logs_a_run_only_when_it_posts(self):
        fake = FakeDiscord()
        self.assertIsNone(report.worker_tick(fake, NOW - timedelta(days=1)))
        out = report.worker_tick(fake, NOW)
        self.assertEqual(out["week"], "2026-W46")
        self.assertIsNone(report.worker_tick(fake, NOW + timedelta(minutes=1)))
        with db.db() as con:
            runs = db.q(con, "SELECT * FROM worker_runs WHERE kind='weekly_report'")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["ok"], 1)
        self.assertEqual(len(fake.posted), 1)

    def test_staff_page_and_send(self):
        r = self.c.get("/staff/report", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Weekly funnel", r.text)
        self.assertIn("OnlineJobs.ph", r.text)
        self.assertIn("Weekly hiring report", r.text)
        self.assertIn("555", r.text)
        self.assertIsNone(EMOJI.search(r.text))
        r = self.c.get("/staff/report.txt", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Funnel:", r.text)
        self.assertEqual(self.c.get("/staff/report").status_code, 401)
        report.DISCORD = None
        r = self.c.post("/staff/report/send", headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("sent=dry"))
        fake = FakeDiscord()
        report.DISCORD = fake
        r = self.c.post("/staff/report/send", headers=MANAGER, follow_redirects=False)
        self.assertTrue(r.headers["location"].endswith("sent=ok"))
        self.assertEqual(fake.posted[0][0], 555)
        with db.db() as con:
            self.assertEqual(db.jl(db.setting(con, report.LAST_KEY), {})["by"], "ian")
        r = self.c.get("/staff/report?sent=ok", headers=MANAGER)
        self.assertIn("Posted to #weekly-report", r.text)


if __name__ == "__main__":
    unittest.main()
