"""The five-question text interview in dry-run: one question at a time, answers stored, GFE gate, decline on fail, questions mid-interview, staff override."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["HIRE_NO_WORKER"] = "1"
os.environ["OPENROUTER_API_KEY"] = ""

import db  # noqa: E402
import engine  # noqa: E402
import interview  # noqa: E402
import sam  # noqa: E402
import texts  # noqa: E402

GOOD = ["I worked two years in live chat sales for a telco and a year as a chatter for a small OF agency.",
        "The agency closed its chatting team when the owner moved abroad, so the contracts ended.",
        "Yes. GFE is the girlfriend experience: you sell a relationship and a fantasy the sub cannot get in real life, remembering his day, making him feel wanted, so he stays and spends.",
        "Three or four models at once, with notes per model on persona, prices and what each fan has bought.",
        "Keeping energy up on long night shifts and handling time-wasters without losing the sale."]
BAD_GFE = GOOD[:2] + ["No, not really familiar with it."] + GOOD[3:]


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, applicant, channel, text):
        self.sent.append((channel, text))
        return "ok"


class InterviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init(self.path)
        self.con = db.connect(self.path)
        self.sender = Sender()
        engine.SENDER = self.sender
        self.alerts = []
        engine.ALERT = lambda text, aid: self.alerts.append(text)
        self.aid, _ = engine.upsert_applicant(self.con, {"name": "Maria Santos", "email": "maria@example.com", "telegram": "marias"})
        engine.save_application(self.con, self.aid, {"age_18": True, "device": "laptop", "connection_type": "fibre", "willing_to_train": True,
                                                       "available_shifts": ["uk_08_16"], "hours_per_week": 40, "english_self_rating": 4, "experience": "chat_sales",
                                                       "why": "I want remote shift work and I enjoy chatting and selling.", "consent": True})
        engine.screen(self.con, self.aid)
        engine.link_telegram(self.con, self.aid, "777", "marias")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()
        engine.SENDER = None
        engine.ALERT = None

    def test_interview_waits_for_discord_when_both_given(self):
        # applicant gave a Discord handle: the interview must not start on Telegram
        self.con.execute("UPDATE applicant SET discord=? WHERE id=?", ("marias_d", self.aid))
        n0 = len(self.sender.sent)
        started = interview.maybe_start(self.con, self.aid, "telegram")
        self.assertFalse(started, "no interview on Telegram when a Discord handle was given")
        app = engine.latest_application(self.con, self.aid)
        self.assertFalse(app.get("interview_state"))
        self.assertTrue(any(c == "telegram" and "Discord" in t for (c, t) in self.sender.sent[n0:]), "pointed to Discord on Telegram")
        # once the Discord channel is linked, it starts there
        self.con.execute("UPDATE applicant SET discord_user_id=?, discord_channel_id=? WHERE id=?", ("777", "999", self.aid))
        started = interview.maybe_start(self.con, self.aid, "discord")
        self.assertTrue(started)
        self.assertEqual(engine.latest_application(self.con, self.aid)["interview_channel"], "discord")
        self.assertEqual(self.sender.sent[-1][0], "discord")

    def run_answers(self, answers):
        outs = []
        for i, ans in enumerate(answers):
            outs.append(sam.handle_inbound(self.con, self.aid, "telegram", ans, external_id=f"m{i}"))
        return outs

    def test_starts_once_on_link_and_asks_one_at_a_time(self):
        self.assertTrue(sam.start_interview_if_due(self.con, self.aid, "telegram"))
        self.assertFalse(sam.start_interview_if_due(self.con, self.aid, "telegram"))
        self.assertIn("1 of 5", self.sender.sent[-1][1])
        self.assertIn("five short questions", self.sender.sent[-1][1])
        self.assertTrue(interview.active(self.con, self.aid))
        out = sam.handle_inbound(self.con, self.aid, "telegram", GOOD[0], external_id="a1")
        self.assertIn("2 of 5", out)
        self.assertNotIn("3 of 5", out)
        app = engine.latest_application(self.con, self.aid)
        self.assertEqual(db.jl(app["interview_answers"], {})["q1"], GOOD[0])
        self.assertEqual(app["interview_state"], "q1")
        self.assertEqual(engine.applicant(self.con, self.aid)["stage"], "screened_in")

    def test_full_pass_sets_stage_and_alerts(self):
        sam.start_interview_if_due(self.con, self.aid, "telegram")
        outs = self.run_answers(GOOD)
        self.assertIn("That's all five", outs[-1])
        self.assertIn("assessment", outs[-1])
        a = engine.applicant(self.con, self.aid)
        self.assertEqual(a["stage"], "assessment_sent")
        app = engine.latest_application(self.con, self.aid)
        g = db.jl(app["interview_grade"], {})
        self.assertTrue(g["passed"])
        self.assertTrue(g["gfe_ok"])
        self.assertEqual(app["interview_state"], "done")
        self.assertEqual(len(db.jl(app["interview_answers"], {})), 5)
        self.assertTrue(any("Interview passed" in t for t in self.alerts))
        hist = engine.stage_history(self.con, self.aid)
        self.assertTrue(any("GFE ok" in h["reason"] for h in hist))
        self.assertFalse(interview.active(self.con, self.aid))
        out = sam.handle_inbound(self.con, self.aid, "telegram", "what is the pay?", external_id="after")
        self.assertIn("$2 per hour", out)

    def test_gfe_gate_fails_with_polite_decline(self):
        sam.start_interview_if_due(self.con, self.aid, "telegram")
        outs = self.run_answers(BAD_GFE)
        self.assertIn("won't be moving forward", outs[-1])
        self.assertNotRegex(outs[-1], r"[\U0001F300-\U0001FAFF]")
        a = engine.applicant(self.con, self.aid)
        self.assertEqual(a["stage"], "interview_failed")
        self.assertEqual(self.sender.sent[-1][0], "telegram")
        self.assertEqual(db.one(self.con, "SELECT kind FROM message WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (self.aid,))["kind"], "decline")
        g = db.jl(engine.latest_application(self.con, self.aid)["interview_grade"], {})
        self.assertFalse(g["gfe_ok"])

    def test_question_mid_interview_is_answered_then_resumed(self):
        sam.start_interview_if_due(self.con, self.aid, "telegram")
        out = sam.handle_inbound(self.con, self.aid, "telegram", "wait, how much is the pay?", external_id="q")
        self.assertIn("$2 per hour", out)
        self.assertIn("1 of 5", out)
        self.assertEqual(engine.latest_application(self.con, self.aid)["interview_state"], "q0")
        self.assertEqual(db.jl(engine.latest_application(self.con, self.aid)["interview_answers"], {}), {})

    def test_override_and_restart(self):
        sam.start_interview_if_due(self.con, self.aid, "telegram")
        self.run_answers(BAD_GFE)
        self.assertEqual(engine.applicant(self.con, self.aid)["stage"], "interview_failed")
        self.assertTrue(interview.override(self.con, self.aid, "pass", "mikey"))
        a = engine.applicant(self.con, self.aid)
        self.assertEqual(a["stage"], "assessment_sent")
        g = db.jl(engine.latest_application(self.con, self.aid)["interview_grade"], {})
        self.assertEqual((g["passed"], g["override_by"]), (True, "mikey"))
        self.assertTrue(interview.restart(self.con, self.aid, "telegram"))
        self.assertEqual(engine.applicant(self.con, self.aid)["stage"], "screened_in")
        self.assertTrue(interview.active(self.con, self.aid))
        self.assertIn("1 of 5", self.sender.sent[-1][1])

    def test_disabled_setting_skips_interview(self):
        db.set_setting(self.con, "interview_enabled", "0")
        self.assertFalse(sam.start_interview_if_due(self.con, self.aid, "telegram"))
        out = sam.handle_inbound(self.con, self.aid, "telegram", "status", external_id="s")
        self.assertIn("Screened in", out)

    def test_heuristic_flags(self):
        g = interview.heuristic_grade({"q1": GOOD[0], "q2": GOOD[1], "q3": GOOD[2] + " and I would love to meet them in person later", "q4": GOOD[3], "q5": GOOD[4]})
        self.assertFalse(g["passed"])
        self.assertIn("talks about meeting subscribers", g["red_flags"])

    def test_nudges_apply_mid_interview(self):
        from datetime import datetime, timedelta, timezone
        sam.start_interview_if_due(self.con, self.aid, "telegram")
        a = engine.applicant(self.con, self.aid)
        self.assertIsNotNone(a["awaiting_since"])
        t0 = db.parse_ts(a["awaiting_since"])
        self.assertEqual(engine.tick(self.con, t0 + timedelta(hours=40))["nudged"], 1)


class PlainTextTest(unittest.TestCase):
    def test_strips_markdown(self):
        s = "**Shifts** run in *UK time*.\n\n## Pay\n- $2 per hour\n* 2% commission\n`Infloww` and [Discord](https://discord.com)\n> quoted"
        out = texts.plain(s)
        self.assertEqual(out, "Shifts run in UK time.\n\nPay\n- $2 per hour\n- 2% commission\nInfloww and Discord (https://discord.com)\nquoted")
        self.assertEqual(texts.plain("2*3 = 6 and a_b_c stays"), "2*3 = 6 and a_b_c stays")
        self.assertEqual(texts.plain("__bold__ and ___x___"), "bold and x")
        self.assertEqual(texts.plain(""), "")

    def test_reply_output_is_plain(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "t.db")
        db.init(path)
        con = db.connect(path)
        engine.SENDER = Sender()
        aid, _ = engine.upsert_applicant(con, {"name": "P", "email": "p@example.com"})
        con.execute("UPDATE knowledge_item SET answer='**Bold** answer with `code`' WHERE key='pay'")
        out = sam.reply(con, aid, "what is the pay?")
        self.assertNotIn("**", out)
        con.close()
        tmp.cleanup()
        engine.SENDER = None


if __name__ == "__main__":
    unittest.main()
