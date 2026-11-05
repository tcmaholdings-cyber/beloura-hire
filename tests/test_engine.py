"""Engine rules: dedupe, screening, scoring, stages, slots across timezones and DST, booking conflicts, nudges and ghosting, reminders."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["HIRE_NO_WORKER"] = "1"

import db  # noqa: E402
import engine  # noqa: E402


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, applicant, channel, text):
        self.sent.append((applicant["id"], channel, text))
        return f"id-{len(self.sent)}"


def good_application(**over):
    d = {"age_18": True, "device": "laptop", "connection_type": "fibre", "willing_to_train": True, "timezone": "Asia/Manila",
         "available_shifts": ["uk_08_16", "uk_16_00"], "hours_per_week": 40, "english_self_rating": 4, "experience": "chat_sales",
         "experience_text": "Two years of live chat sales for a telco, handling upsells and objections.",
         "why": "I enjoy chatting and selling and I want stable remote shift work with commission.", "heard_from": "onlinejobs", "consent": True}
    d.update(over)
    return d


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init(self.path)
        self.con = db.connect(self.path)
        self.rec = Recorder()
        engine.SENDER = self.rec
        engine.ALERT = None
        self.ian = engine.default_manager(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()
        engine.SENDER = None

    def applicant(self, **over):
        data = {"name": "Maria Santos", "email": "maria@example.com", "telegram": "@MariaS", "timezone": "Asia/Manila", "channel_code": "olj"}
        data.update(over)
        aid, created = engine.upsert_applicant(self.con, data)
        return aid, created

    def test_reapply_when_engaged_is_a_noop(self):
        import app as appmod
        d = good_application(name="Re Apply", email="re@example.com", telegram="reapply")
        aid = appmod.submit_application(self.con, "test", d)
        engine.set_stage(self.con, aid, "screened_in", force=True)
        self.con.execute("UPDATE applicant SET discord_user_id='1', discord_channel_id='2' WHERE id=?", (aid,))
        n_apps = self.con.execute("SELECT count(*) FROM application WHERE applicant_id=?", (aid,)).fetchone()[0]
        n_first = sum(1 for (_i, _c, t) in self.rec.sent if "thanks for applying" in t.lower())
        aid2 = appmod.submit_application(self.con, "test", d)
        self.assertEqual(aid2, aid)
        self.assertEqual(self.con.execute("SELECT count(*) FROM application WHERE applicant_id=?", (aid,)).fetchone()[0], n_apps, "no duplicate application on re-apply")
        self.assertEqual(sum(1 for (_i, _c, t) in self.rec.sent if "thanks for applying" in t.lower()), n_first, "no second onboarding message")

    def test_channel_policy_discord_primary_telegram_reminders(self):
        import interview
        aid, _ = self.applicant(name="Jam Reyes", discord="jamr")
        # both channels linked: Telegram chat id + a Discord private channel
        self.con.execute("UPDATE applicant SET telegram_chat_id='555', discord_user_id='777', discord_channel_id='999' WHERE id=?", (aid,))
        a = engine.applicant(self.con, aid)
        self.assertEqual(engine.pick_channel(a, kind="sam"), "discord", "conversation goes to Discord once joined")
        self.assertEqual(engine.pick_channel(a, kind="interview"), "discord")
        self.assertEqual(engine.pick_channel(a, kind="reminder"), "telegram", "reminders go to Telegram")
        self.assertEqual(engine.pick_channel(a, kind="nudge"), "telegram")
        self.assertEqual(engine.pick_channel(a, kind="first"), "telegram", "onboarding goes to Telegram, never a Discord channel")
        # an explicit same-channel reply still wins
        self.assertEqual(engine.pick_channel(a, prefer="telegram", kind="sam"), "telegram")
        # telegram only -> everything on telegram
        self.con.execute("UPDATE applicant SET discord_user_id='', discord_channel_id='' WHERE id=?", (aid,))
        a = engine.applicant(self.con, aid)
        self.assertEqual(engine.pick_channel(a, kind="sam"), "telegram")
        self.assertEqual(engine.pick_channel(a, kind="reminder"), "telegram")
        # only a Discord channel: onboarding is held, not pushed into Discord; conversation still uses Discord
        self.con.execute("UPDATE applicant SET telegram_chat_id='', email='', discord_user_id='1', discord_channel_id='9' WHERE id=?", (aid,))
        a = engine.applicant(self.con, aid)
        self.assertIsNone(engine.pick_channel(a, kind="first"))
        self.assertEqual(engine.pick_channel(a, kind="sam"), "discord")


    def grid(self, start="10:00", end="12:00", days=(0, 1, 2, 3, 4)):
        for d in days:
            self.con.execute("INSERT INTO availability (manager_id, weekday, start, end, tz) VALUES (?,?,?,?,?)", (self.ian["id"], d, start, end, "Asia/Manila"))

    # ---- dedupe

    def test_dedupe_on_email_telegram_discord_phone(self):
        aid, created = self.applicant()
        self.assertTrue(created)
        same, c2 = engine.upsert_applicant(self.con, {"name": "M. Santos", "email": "MARIA@example.com"})
        self.assertEqual((same, c2), (aid, False))
        same, _ = engine.upsert_applicant(self.con, {"telegram": "marias", "email": "other@example.com"})
        self.assertEqual(same, aid)
        a = engine.applicant(self.con, aid)
        self.assertEqual(a["email"], "maria@example.com")
        other, c3 = engine.upsert_applicant(self.con, {"name": "Jo", "discord": "@JoJo#1234", "phone": "0917 123 4567"})
        self.assertTrue(c3)
        self.assertNotEqual(other, aid)
        self.assertEqual(engine.applicant(self.con, other)["discord"], "jojo")
        again, c4 = engine.upsert_applicant(self.con, {"phone": "+9171234567"})
        self.assertEqual((again, c4), (other, False))
        again, _ = engine.upsert_applicant(self.con, {"discord": "jojo"})
        self.assertEqual(again, other)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM applicant")["n"], 2)

    def test_gaps_filled_but_values_kept(self):
        aid, _ = self.applicant(discord=None)
        engine.upsert_applicant(self.con, {"email": "maria@example.com", "discord": "maria_d", "name": "Someone Else"})
        a = engine.applicant(self.con, aid)
        self.assertEqual((a["discord"], a["name"]), ("maria_d", "Maria Santos"))

    # ---- screening

    def test_screening_passes_and_scores(self):
        aid, _ = self.applicant()
        engine.save_application(self.con, aid, good_application())
        ok, reasons, score = engine.screen(self.con, aid)
        self.assertTrue(ok, reasons)
        self.assertEqual(engine.applicant(self.con, aid)["stage"], "screened_in")
        self.assertTrue(60 <= score <= 100, score)
        self.assertEqual(self.rec.sent, [])

    def test_screening_hard_filters_decline(self):
        cases = [({"age_18": False}, "18"), ({"device": "phone"}, "laptop"), ({"willing_to_train": False}, "training"),
                 ({"available_shifts": ["uk_00_08"]}, "shifts")]
        db.set_setting(self.con, "vacancies_uk_00_08", "0")
        for i, (over, word) in enumerate(cases):
            aid, _ = engine.upsert_applicant(self.con, {"name": f"A{i}", "email": f"a{i}@example.com"})
            engine.save_application(self.con, aid, good_application(**over))
            ok, reasons, _ = engine.screen(self.con, aid)
            self.assertFalse(ok)
            self.assertTrue(any(word in r for r in reasons), reasons)
            self.assertEqual(engine.applicant(self.con, aid)["stage"], "screened_out")
        self.assertEqual(len(self.rec.sent), 4)
        self.assertIn("can't take your application forward", self.rec.sent[0][2])
        self.assertEqual(self.rec.sent[0][1], "email")

    def test_soft_score_orders_applicants(self):
        vac = db.vacancies(self.con)
        strong = engine.soft_score(good_application(experience="of_chatter", hours_per_week=45, english_self_rating=5), vac)
        weak = engine.soft_score(good_application(experience="none", hours_per_week=10, english_self_rating=2, why="money", experience_text=""), vac)
        self.assertGreater(strong, weak)
        self.assertLessEqual(strong, 100)
        self.assertGreaterEqual(weak, 0)

    # ---- stages

    def test_set_stage_forward_only_unless_forced(self):
        aid, _ = self.applicant()
        self.assertTrue(engine.set_stage(self.con, aid, "screened_in"))
        self.assertTrue(engine.set_stage(self.con, aid, "passed", reason="fast"))
        self.assertFalse(engine.set_stage(self.con, aid, "screened_in"))
        self.assertTrue(engine.set_stage(self.con, aid, "screened_in", force=True, actor="mikey"))
        self.assertTrue(engine.set_stage(self.con, aid, "rejected"))
        self.assertFalse(engine.set_stage(self.con, aid, "passed"))
        hist = engine.stage_history(self.con, aid)
        self.assertEqual([h["to_stage"] for h in hist], ["applied", "screened_in", "passed", "screened_in", "rejected"])
        self.assertEqual(hist[3]["actor"], "mikey")

    # ---- slots

    def test_slots_manila_grid_seen_from_london_summer_and_winter(self):
        self.grid("10:00", "12:00")
        summer = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)   # Monday, BST
        s = engine.slots(self.con, "Europe/London", now=summer, days=7, lead_hours=0)
        monday = [x for x in s if x["start"].startswith("2026-07-06")]
        self.assertEqual(len(monday), 4)
        self.assertEqual(monday[0]["start"], "2026-07-06T02:00:00Z")
        self.assertIn("3:00 UK time", monday[0]["local"])
        self.assertIn("10:00 Philippine time", engine.slots(self.con, "Asia/Manila", now=summer, lead_hours=0)[0]["local"])
        winter = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)   # Monday, GMT
        w = engine.slots(self.con, "Europe/London", now=winter, days=7, lead_hours=0)
        self.assertEqual(w[0]["start"], "2026-01-05T02:00:00Z")
        self.assertIn("2:00 UK time", w[0]["local"])
        self.assertTrue(all(datetime.fromisoformat(x["start"].replace("Z", "+00:00")).weekday() < 5 for x in w))
        self.assertEqual(len(w), 4 * 5)

    def test_slots_respect_lead_and_horizon(self):
        self.grid("10:00", "11:00")
        now = datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc)   # 09:30 Manila Monday
        s = engine.slots(self.con, "Asia/Manila", now=now, days=7, lead_hours=2)
        self.assertFalse(any(x["start"].startswith("2026-07-06") for x in s))
        self.assertTrue(all(x["start"] <= "2026-07-13T01:30:00Z" for x in s))

    def test_when_both_shows_applicant_and_uk_time(self):
        self.assertEqual(engine.when_both("2026-07-06T02:00:00Z", "Asia/Manila"), "Mon 6 Jul 10:00 Philippine time (Mon 6 Jul 03:00 UK time)")
        self.assertEqual(engine.when_both("2026-01-05T02:00:00Z", "Europe/London"), "Mon 5 Jan 02:00 UK time")

    # ---- booking

    def test_booking_conflicts_and_reschedule(self):
        self.grid("10:00", "11:00")
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        a1, _ = self.applicant()
        a2, _ = engine.upsert_applicant(self.con, {"name": "Ben", "email": "ben@example.com", "timezone": "Europe/London"})
        for a in (a1, a2):
            engine.set_stage(self.con, a, "passed", force=True)
        first = engine.slots(self.con, "Asia/Manila", now=now)[0]["start"]
        iv = engine.book(self.con, a1, first, now=now)
        self.assertEqual(iv["status"], "booked")
        self.assertEqual(engine.applicant(self.con, a1)["stage"], "final_scheduled")
        self.assertIn("booked for", self.rec.sent[-1][2])
        self.assertIn("UK time", self.rec.sent[-1][2])
        with self.assertRaises(engine.SlotTaken):
            engine.book(self.con, a2, first, now=now)
        self.assertNotIn(first, [s["start"] for s in engine.slots(self.con, "Europe/London", now=now)])
        with self.assertRaises(engine.NotAvailable):
            engine.book(self.con, a2, "2026-07-06T09:00:00Z", now=now)
        second = engine.slots(self.con, "Europe/London", now=now)[0]["start"]
        iv2 = engine.reschedule(self.con, a1, second, now=now)
        self.assertEqual(iv2["status"], "rescheduled")
        self.assertEqual(db.one(self.con, "SELECT status FROM interview WHERE id=?", (iv["id"],))["status"], "moved")
        self.assertIn(first, [s["start"] for s in engine.slots(self.con, "Europe/London", now=now)])
        self.assertTrue(engine.cancel(self.con, a1))
        self.assertEqual(engine.applicant(self.con, a1)["stage"], "passed")
        self.assertIsNone(engine.open_interview(self.con, a1))

    def test_booking_needs_bookable_stage(self):
        self.grid()
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        aid, _ = self.applicant()
        engine.set_stage(self.con, aid, "screened_in")
        start = engine.slots(self.con, "Asia/Manila", now=now)[0]["start"]
        with self.assertRaises(engine.NotAvailable):
            engine.book(self.con, aid, start, now=now)
        db.set_setting(self.con, "book_from_screened_in", "1")
        engine.book(self.con, aid, start, now=now)
        self.assertEqual(engine.applicant(self.con, aid)["stage"], "final_scheduled")

    # ---- nudges, ghosting, replies

    def test_ghost_after_three_nudges_over_five_days_and_reversible(self):
        aid, _ = self.applicant()
        engine.set_stage(self.con, aid, "screened_in")
        t0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        self.con.execute("UPDATE applicant SET awaiting_since=? WHERE id=?", (db.fmt_ts(t0), aid))
        engine.deliver(self.con, aid, "hello", kind="first")
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=39))["nudged"], 0)
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=40))["nudged"], 1)
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=41))["nudged"], 0)
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=80))["nudged"], 1)
        out = engine.tick(self.con, t0 + timedelta(hours=120))
        self.assertEqual((out["nudged"], out["ghosted"]), (1, 0))
        self.assertEqual(engine.applicant(self.con, aid)["nudges_sent"], 3)
        self.assertIn("pause your application", self.rec.sent[-1][2])
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=159))["ghosted"], 0)
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=160))["ghosted"], 1)
        a = engine.applicant(self.con, aid)
        self.assertEqual(a["stage"], "ghosted")
        self.assertIsNone(a["awaiting_since"])
        engine.record_inbound(self.con, aid, "email", "sorry, I was away", external_id="m1")
        a = engine.applicant(self.con, aid)
        self.assertEqual((a["stage"], a["nudges_sent"], a["awaiting_since"]), ("screened_in", 0, None))
        self.assertIsNone(engine.record_inbound(self.con, aid, "email", "dup", external_id="m1"))
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=400))["nudged"], 0)

    def test_no_nudges_while_interview_booked_or_closed(self):
        self.grid()
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        aid, _ = self.applicant()
        engine.set_stage(self.con, aid, "passed", force=True)
        engine.deliver(self.con, aid, "pick a slot", kind="slots")
        engine.book(self.con, aid, engine.slots(self.con, "Asia/Manila", now=now)[0]["start"], now=now)
        self.assertEqual(engine.tick(self.con, now + timedelta(days=3))["nudged"], 0)
        bid, _ = engine.upsert_applicant(self.con, {"name": "Z", "email": "z@example.com"})
        engine.set_stage(self.con, bid, "screened_in")
        engine.deliver(self.con, bid, "hi", kind="first")
        engine.set_stage(self.con, bid, "rejected")
        self.assertEqual(engine.tick(self.con, datetime.now(timezone.utc) + timedelta(days=9))["nudged"], 0)

    def test_reminders_24h_and_1h_once_each(self):
        self.grid("10:00", "11:00")
        booked_at = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
        aid, _ = self.applicant()
        engine.set_stage(self.con, aid, "passed", force=True)
        start_iso = [s for s in engine.slots(self.con, "Asia/Manila", now=booked_at) if db.parse_ts(s["start"]) > booked_at + timedelta(hours=30)][0]["start"]
        iv = engine.book(self.con, aid, start_iso, now=booked_at)
        self.con.execute("UPDATE interview SET created_at=? WHERE id=?", (db.fmt_ts(booked_at), iv["id"]))
        start = db.parse_ts(start_iso)
        n0 = len(self.rec.sent)
        self.assertEqual(engine.tick(self.con, start - timedelta(hours=30))["reminders"], 0)
        self.assertEqual(engine.tick(self.con, start - timedelta(hours=23))["reminders"], 1)
        self.assertIn("tomorrow", self.rec.sent[-1][2])
        self.assertEqual(engine.tick(self.con, start - timedelta(hours=22))["reminders"], 0)
        self.assertEqual(engine.tick(self.con, start - timedelta(minutes=50))["reminders"], 1)
        self.assertIn("about an hour", self.rec.sent[-1][2])
        self.assertEqual(engine.tick(self.con, start - timedelta(minutes=10))["reminders"], 0)
        self.assertEqual(len(self.rec.sent) - n0, 2)

    def test_held_message_sent_when_channel_appears(self):
        aid, _ = engine.upsert_applicant(self.con, {"name": "Tele Only", "telegram": "teleonly"})
        mid = engine.deliver(self.con, aid, "welcome", kind="first")
        self.assertEqual(db.one(self.con, "SELECT status FROM message WHERE id=?", (mid,))["status"], "held")
        engine.link_telegram(self.con, aid, "555", "teleonly")
        self.assertEqual(engine.flush_held(self.con, aid, channel="telegram"), 1)
        m = db.one(self.con, "SELECT * FROM message WHERE id=?", (mid,))
        self.assertEqual((m["status"], m["channel"]), ("sent", "telegram"))
        self.assertIsNotNone(engine.applicant(self.con, aid)["awaiting_since"])

    def test_escalation_answer_saves_knowledge_and_replies(self):
        aid, _ = self.applicant()
        alerts = []
        engine.ALERT = lambda text, a: alerts.append(text)
        eid = engine.create_escalation(self.con, aid, "question not in the knowledge base", "Do you pay for holidays?")
        self.assertEqual(len(alerts), 1)
        engine.answer_escalation(self.con, eid, "No, contractors are paid per shift worked.", by="mikey")
        self.assertEqual(db.one(self.con, "SELECT status FROM escalation WHERE id=?", (eid,))["status"], "answered")
        k = db.one(self.con, "SELECT * FROM knowledge_item WHERE question='Do you pay for holidays?'")
        self.assertEqual(k["updated_by"], "mikey")
        self.assertIn("I checked on this. No, contractors", self.rec.sent[-1][2])


if __name__ == "__main__":
    unittest.main()
