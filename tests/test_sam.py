"""Sam in dry-run: canned replies drive the same tools (status, knowledge, slots, booking, timezone, escalation) and log nothing to OpenRouter."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["HIRE_NO_WORKER"] = "1"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["SAM_PRICES"] = "anthropic/claude-sonnet-5.5=3,15;deepseek/deepseek-chat=0.3,1.2"

import db  # noqa: E402
import engine  # noqa: E402
import sam  # noqa: E402


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, applicant, channel, text):
        self.sent.append((applicant["id"], channel, text))
        return "ok"


class SamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init(self.path)
        self.con = db.connect(self.path)
        self.rec = Recorder()
        engine.SENDER = self.rec
        engine.ALERT = None
        self.aid, _ = engine.upsert_applicant(self.con, {"name": "Maria Santos", "email": "maria@example.com", "timezone": "Asia/Manila"})
        engine.set_stage(self.con, self.aid, "passed", force=True)
        ian = engine.default_manager(self.con)
        for d in range(7):
            self.con.execute("INSERT INTO availability (manager_id, weekday, start, end, tz) VALUES (?,?,?,?,?)", (ian["id"], d, "10:00", "12:00", "Asia/Manila"))
        self.now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()
        engine.SENDER = None

    def test_dry_run_is_on_without_a_key(self):
        self.assertFalse(sam.configured())
        self.assertEqual(sam.models()[0], "anthropic/claude-sonnet-5.5")

    def test_assessment_link_tool(self):
        import assess
        # not open at 'passed' stage
        r = sam.run_tool(self.con, self.aid, "assessment_link", {})
        self.assertFalse(r.get("ok"))
        # open once the link has been sent
        engine.set_stage(self.con, self.aid, "assessment_sent", force=True)
        r = sam.run_tool(self.con, self.aid, "assessment_link", {})
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["url"], assess.create_link(self.con, self.aid))
        self.assertIn(f"/assess/{self.aid}/", r["url"])

    def test_status_question(self):
        out = sam.reply(self.con, self.aid, "where am I in the process?", now=self.now)
        self.assertIn("Passed", out)
        self.assertIn("final interview", out)

    def test_knowledge_answers(self):
        self.assertIn("$2 per hour", sam.reply(self.con, self.aid, "how much is the pay?", now=self.now))
        self.assertIn("Thursday", sam.reply(self.con, self.aid, "when do I get paid", now=self.now))
        self.assertIn("Infloww", sam.reply(self.con, self.aid, "which tools do you use", now=self.now))
        self.assertIn("confirmed at offer", sam.reply(self.con, self.aid, "how long is training and is it paid", now=self.now))
        self.assertIn("UK time", sam.reply(self.con, self.aid, "what are the shift times", now=self.now))
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM llm_call")["n"], 0)

    def test_slots_then_book_by_number(self):
        out = sam.reply(self.con, self.aid, "can I book an interview slot?", now=self.now)
        self.assertIn("1. ", out)
        self.assertIn("Philippine time", out)
        self.assertIn("UK time", out)
        offered = engine.meta_get(engine.applicant(self.con, self.aid), "offered_slots")
        self.assertEqual(len(offered), 6)
        out = sam.reply(self.con, self.aid, "2 please", now=self.now)
        self.assertIn("booked for", out)
        iv = engine.open_interview(self.con, self.aid)
        self.assertEqual(iv["start_at"], offered[1])
        self.assertEqual(engine.applicant(self.con, self.aid)["stage"], "final_scheduled")
        out = sam.reply(self.con, self.aid, "can we move it? show me times", now=self.now)
        self.assertIn("1. ", out)
        out = sam.reply(self.con, self.aid, "the first one", now=self.now)
        self.assertIn("Moved", out)
        self.assertEqual(db.one(self.con, "SELECT COUNT(*) n FROM interview WHERE status='moved'")["n"], 1)
        self.assertIn("cancelled", sam.reply(self.con, self.aid, "please cancel my interview", now=self.now))
        self.assertIsNone(engine.open_interview(self.con, self.aid))

    def test_slot_taken_between_offer_and_pick(self):
        sam.reply(self.con, self.aid, "slots please", now=self.now)
        offered = engine.meta_get(engine.applicant(self.con, self.aid), "offered_slots")
        other, _ = engine.upsert_applicant(self.con, {"name": "Ben", "email": "ben@example.com"})
        engine.set_stage(self.con, other, "passed", force=True)
        engine.book(self.con, other, offered[0], now=self.now, notify=False)
        out = sam.reply(self.con, self.aid, "1", now=self.now)
        self.assertIn("just gone", out)
        self.assertIn("1. ", out)

    def test_no_slots_escalates(self):
        self.con.execute("DELETE FROM availability")
        out = sam.reply(self.con, self.aid, "book me a slot", now=self.now)
        self.assertIn("no open interview slots", out)
        self.assertEqual(db.one(self.con, "SELECT reason FROM escalation ORDER BY id DESC LIMIT 1")["reason"], "no interview slots")

    def test_timezone_change(self):
        out = sam.reply(self.con, self.aid, "I'm in Europe/London actually", now=self.now)
        self.assertIn("Europe/London", out)
        self.assertEqual(engine.applicant(self.con, self.aid)["timezone"], "Europe/London")

    def test_unknown_question_escalates_and_says_so(self):
        out = sam.reply(self.con, self.aid, "do you reimburse electricity bills?", now=self.now)
        self.assertIn("check with the team", out)
        e = db.one(self.con, "SELECT * FROM escalation ORDER BY id DESC LIMIT 1")
        self.assertEqual(e["status"], "open")
        self.assertIn("electricity", e["question"])

    def test_asking_for_a_person_escalates(self):
        out = sam.reply(self.con, self.aid, "can I speak to a real person please", now=self.now)
        self.assertIn("passed this to the team", out)
        self.assertEqual(db.one(self.con, "SELECT reason FROM escalation ORDER BY id DESC LIMIT 1")["reason"], "asked for a person")

    def test_handle_inbound_records_both_sides_on_same_channel(self):
        out = sam.handle_inbound(self.con, self.aid, "email", "what is the pay?", external_id="e1", now=self.now)
        self.assertIn("$2", out)
        rows = db.q(self.con, "SELECT direction, channel, kind, status FROM message WHERE applicant_id=? ORDER BY id", (self.aid,))
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual((rows[1]["direction"], rows[1]["channel"], rows[1]["kind"], rows[1]["status"]), ("out", "email", "sam", "sent"))
        self.assertEqual(self.rec.sent[-1][1], "email")
        self.assertIsNone(sam.handle_inbound(self.con, self.aid, "email", "what is the pay?", external_id="e1", now=self.now))

    def test_ghosted_applicant_reply_reopens(self):
        engine.set_stage(self.con, self.aid, "ghosted", force=True)
        out = sam.handle_inbound(self.con, self.aid, "email", "status?", external_id="g1", now=self.now)
        self.assertIn("reopened your file", out)
        self.assertEqual(engine.applicant(self.con, self.aid)["stage"], "passed")

    def test_first_contact_text(self):
        sam.first_contact(self.con, self.aid)
        text = self.rec.sent[-1][2]
        self.assertIn("recruiting assistant", text)
        self.assertIn("people on the team review every decision", text)
        self.assertNotIn("https://t.me", text)
        self.assertNotRegex(text, r"[\U0001F300-\U0001FAFF]")
        sam.first_contact(self.con, self.aid, telegram_link="https://t.me/bot?start=x")
        self.assertIn("open https://t.me/bot?start=x and tap Start", self.rec.sent[-1][2])

    def test_cost_and_logging(self):
        self.assertAlmostEqual(sam.cost_for("anthropic/claude-sonnet-5.5", 1000, 200, None), 0.006, places=6)
        self.assertEqual(sam.cost_for("anthropic/claude-sonnet-5.5", 1000, 200, 0.0123), 0.0123)
        self.assertEqual(sam.cost_for("unknown/model", 1000, 200, None), 0.0)
        sam.log_call(self.con, self.aid, "anthropic/claude-sonnet-5.5", 1000, 200, 0.006, True, "", 812)
        r = db.one(self.con, "SELECT * FROM llm_call")
        self.assertEqual((r["prompt_version"], r["tokens_in"], r["ok"]), (sam.PROMPT_VERSION, 1000, 1))

    def test_system_prompt_has_rules_and_knowledge(self):
        a = engine.applicant(self.con, self.aid)
        p = sam.system_prompt(self.con, a)
        for needle in ("Never promise a job", "Never discuss other applicants", "No emoji", "$2 per hour", "Asia/Manila", "UK time"):
            self.assertIn(needle, p)

    def test_search_knowledge_synonyms(self):
        self.assertEqual(sam.search_knowledge(self.con, "salary?")[0]["key"], "pay")
        self.assertEqual(sam.search_knowledge(self.con, "what notice do I give if I quit")[0]["key"], "notice")


if __name__ == "__main__":
    unittest.main()
