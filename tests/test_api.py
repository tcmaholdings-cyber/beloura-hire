"""HTTP surface: auth, the apply form, the thank-you page, the Telegram webhook to Sam and back, staff pages."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.TemporaryDirectory()
os.environ.update({"HIRE_NO_WORKER": "1", "HIRE_SECRET": "test", "HIRE_DB_PATH": os.path.join(TMP.name, "api.db"),
                   "BELOURA_GATEWAY_TOKEN": "gw", "PANEL_PASSWORD": "pw", "TELEGRAM_WEBHOOK_SECRET": "tgsecret", "TELEGRAM_BOT_USERNAME": "beloura_sam_bot",
                   "OPENROUTER_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "DISCORD_BOT_TOKEN": "", "SMTP_HOST": ""})

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import channels  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402

ADMIN = {"X-Gateway-Token": "gw", "Remote-User": "mikey", "Remote-Groups": "id-admins,hiring-admin", "Remote-Name": "Mikey"}
MANAGER = {"X-Gateway-Token": "gw", "Remote-User": "ian", "Remote-Groups": "hiring-manager", "Remote-Name": "Ian"}


def form(**over):
    d = {"name": "Maria Santos", "email": "maria@example.com", "telegram": "@MariaS", "discord": "", "phone": "", "timezone": "Asia/Manila",
         "available_shifts": ["uk_08_16"], "hours_per_week": "40", "english_self_rating": "4", "device": "laptop", "connection_type": "fibre",
         "experience": "chat_sales", "experience_text": "Live chat sales for two years.", "why": "I like chatting and selling and want remote shifts.",
         "heard_from": "onlinejobs", "age_18": "1", "willing_to_train": "1", "consent": "1"}
    d.update(over)
    return d


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(appmod.app)
        with db.db() as con:
            ian = engine.default_manager(con)
            for d in range(7):
                con.execute("INSERT INTO availability (manager_id, weekday, start, end, tz) VALUES (?,?,?,?,?)", (ian["id"], d, "09:00", "18:00", "Asia/Manila"))

    def test_auth_gates(self):
        self.assertEqual(self.c.get("/staff").status_code, 401)
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertIn("sam=dry-run", self.c.get("/healthz").text)
        self.assertEqual(self.c.get("/staff", headers=ADMIN).status_code, 200)
        self.assertEqual(self.c.get("/staff", headers=MANAGER).status_code, 200)
        self.assertEqual(self.c.get("/staff/settings", headers=MANAGER).status_code, 403)
        self.assertEqual(self.c.get("/staff/settings", headers=ADMIN).status_code, 200)
        r = self.c.get("/staff", headers={"X-Gateway-Token": "gw", "Remote-User": "bob", "Remote-Groups": "outreach-setter"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get("/staff", auth=("admin", "pw")).status_code, 200)
        self.assertEqual(self.c.get("/staff", auth=("admin", "nope")).status_code, 401)

    def test_apply_form_renders_with_shift_conversion(self):
        r = self.c.get("/apply/olj")
        self.assertEqual(r.status_code, 200)
        self.assertIn("UK 08:00-16:00", r.text)
        self.assertIn("Philippine time", r.text)
        self.assertIn('action="/apply/olj"', r.text)

    def test_apply_validation(self):
        r = self.c.post("/apply/olj", data=form(email="not-an-email", consent="", why="short", hours_per_week="0", device=""))
        self.assertEqual(r.status_code, 400)
        for msg in ("A valid email is needed", "consent", "at least 20 characters", "Hours per week", "device"):
            self.assertIn(msg, r.text)
        self.assertIn('value="Maria Santos"', r.text)
        with db.db() as con:
            self.assertIsNone(db.one(con, "SELECT id FROM applicant WHERE lower(email)='not-an-email'"))
        r = self.c.post("/apply/olj", data=form(email="bot@example.com", website="http://spam"))
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/apply/olj", data={k: v for k, v in form(email="noshift@example.com").items() if k != "available_shifts"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least one shift", r.text)

    def test_apply_creates_screens_and_sends_first_message(self):
        r = self.c.post("/apply/olj", data=form(), follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(r.headers["location"].startswith("/applied/"))
        done = self.c.get(r.headers["location"])
        self.assertEqual(done.status_code, 200)
        self.assertIn("t.me/beloura_sam_bot?start=", done.text)
        with db.db() as con:
            a = db.one(con, "SELECT * FROM applicant WHERE email='maria@example.com'")
            self.assertEqual((a["stage"], a["channel_code"], a["telegram"]), ("screened_in", "olj", "marias"))
            self.assertIsNotNone(a["score"])
            app_row = engine.latest_application(con, a["id"])
            self.assertEqual((app_row["available_shifts"], app_row["hours_per_week"], app_row["consent"]), (["uk_08_16"], 40, 1))
            m = db.one(con, "SELECT * FROM message WHERE applicant_id=? AND kind='first'", (a["id"],))
            self.assertEqual((m["channel"], m["status"]), ("email", "sent"))
            self.assertIn("recruiting assistant", m["body"])
            self.assertIn("t.me/beloura_sam_bot", m["body"])
        self.assertEqual(self.c.get("/applied/999/badtoken").status_code, 404)

    def test_apply_declines_phone_user(self):
        r = self.c.post("/apply/fb-group-1", data=form(email="phone@example.com", telegram="", device="phone"), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            a = db.one(con, "SELECT * FROM applicant WHERE email='phone@example.com'")
            self.assertEqual(a["stage"], "screened_out")
            m = db.one(con, "SELECT * FROM message WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (a["id"],))
            self.assertEqual(m["kind"], "decline")
            self.assertIn("laptop or desktop", m["body"])
            self.assertEqual(db.one(con, "SELECT COUNT(*) n FROM message WHERE applicant_id=? AND kind='first'", (a["id"],))["n"], 0)

    def test_repeat_application_dedupes(self):
        self.c.post("/apply/olj", data=form(email="repeat@example.com", telegram="rep1"), follow_redirects=False)
        self.c.post("/apply/reddit", data=form(email="REPEAT@example.com", telegram="", name="Repeat Person"), follow_redirects=False)
        with db.db() as con:
            rows = db.q(con, "SELECT id, channel_code FROM applicant WHERE lower(email)='repeat@example.com'")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["channel_code"], "olj")
            self.assertEqual(db.one(con, "SELECT COUNT(*) n FROM application WHERE applicant_id=?", (rows[0]["id"],))["n"], 2)

    def test_telegram_webhook_start_then_question(self):
        self.c.post("/apply/olj", data=form(email="tg@example.com", telegram="tguser"), follow_redirects=False)
        with db.db() as con:
            a = db.one(con, "SELECT * FROM applicant WHERE email='tg@example.com'")
        token = channels.start_token(a["id"])
        self.assertEqual(self.c.post("/hooks/telegram/wrong", json={}).status_code, 404)
        self.assertEqual(self.c.post("/hooks/telegram/tgsecret", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "bad"}).status_code, 403)
        upd = {"update_id": 1, "message": {"message_id": 10, "chat": {"id": 777, "type": "private"}, "from": {"id": 777, "username": "tguser"}, "text": f"/start {token}"}}
        r = self.c.post("/hooks/telegram/tgsecret", json=upd)
        self.assertEqual(r.status_code, 200)
        with db.db() as con:
            a = engine.applicant(con, a["id"])
            self.assertEqual(a["telegram_chat_id"], "777")
            last = db.one(con, "SELECT * FROM message WHERE applicant_id=? AND direction='out' ORDER BY id DESC LIMIT 1", (a["id"],))
            self.assertEqual(last["channel"], "telegram")
            self.assertIn("Got you here on Telegram", last["body"])
        upd = {"update_id": 2, "message": {"message_id": 11, "chat": {"id": 777, "type": "private"}, "from": {"id": 777, "username": "tguser"}, "text": "How much is the pay?"}}
        self.assertEqual(self.c.post("/hooks/telegram/tgsecret", json=upd).status_code, 200)
        self.assertEqual(self.c.post("/hooks/telegram/tgsecret", json=upd).status_code, 200)
        with db.db() as con:
            rows = db.q(con, "SELECT direction, channel, body, kind FROM message WHERE applicant_id=? ORDER BY id", (a["id"],))
            ins = [r for r in rows if r["direction"] == "in" and r["body"] == "How much is the pay?"]
            self.assertEqual(len(ins), 1)
            self.assertEqual(rows[-1]["direction"], "out")
            self.assertEqual(rows[-1]["channel"], "telegram")
            self.assertIn("$2 per hour", rows[-1]["body"])
        upd = {"update_id": 3, "message": {"message_id": 12, "chat": {"id": 999, "type": "private"}, "from": {"id": 999, "username": "stranger"}, "text": "hello"}}
        self.assertEqual(self.c.post("/hooks/telegram/tgsecret", json=upd).status_code, 200)
        with db.db() as con:
            self.assertIsNone(db.one(con, "SELECT id FROM applicant WHERE telegram_chat_id='999'"))

    def test_staff_pages_and_actions(self):
        self.c.post("/apply/olj", data=form(email="staff@example.com", telegram="staffer"), follow_redirects=False)
        with db.db() as con:
            a = db.one(con, "SELECT * FROM applicant WHERE email='staff@example.com'")
        aid = a["id"]
        r = self.c.get(f"/staff/applicant/{aid}", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Maria Santos", r.text)
        self.assertIn("Screened in", r.text)
        r = self.c.post(f"/staff/applicant/{aid}/stage", data={"stage": "passed", "reason": "manual assessment ok"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(engine.applicant(con, aid)["stage"], "passed")
            last = db.one(con, "SELECT * FROM message WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (aid,))
            self.assertEqual(last["kind"], "slots")
            self.assertIn("1. ", last["body"])
            slot = engine.slots(con, "Asia/Manila")[0]["start"]
        r = self.c.post(f"/staff/applicant/{aid}/interview", data={"action": "book", "start": slot}, headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(engine.applicant(con, aid)["stage"], "final_scheduled")
            eid = engine.create_escalation(con, aid, "question not in the knowledge base", "Is there a dress code?")
        r = self.c.get("/staff/escalations", headers=ADMIN)
        self.assertIn("dress code", r.text)
        r = self.c.post(f"/staff/escalations/{eid}/answer", data={"answer": "No dress code; it is text chat.", "save_knowledge": "1"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertIsNotNone(db.one(con, "SELECT id FROM knowledge_item WHERE question='Is there a dress code?'"))
            self.assertIn("I checked on this. No dress code", db.one(con, "SELECT body FROM message WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (aid,))["body"])
        r = self.c.get("/staff/knowledge", headers=ADMIN)
        self.assertIn("dress code", r.text)
        r = self.c.post("/staff/knowledge", data={"question": "Holidays?", "answer": "Unpaid; agree them with your manager.", "key": "holidays"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.c.get("/staff/availability", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ian", r.text)
        r = self.c.post("/staff/availability", data={"manager_id": "1", "weekday": "5,6", "start": "14:00", "end": "16:00"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(db.one(con, "SELECT COUNT(*) n FROM availability WHERE start='14:00'")["n"], 2)
        r = self.c.post("/staff/settings", data={"vacancies_uk_00_08": "0", "ghost_nudges": "2", "ghost_days": "4"}, headers=ADMIN, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(db.setting(con, "ghost_nudges"), "2")
            db.set_setting(con, "ghost_nudges", "3")
            db.set_setting(con, "ghost_days", "5")
            db.set_setting(con, "vacancies_uk_00_08", "2")
        r = self.c.get("/staff", headers=ADMIN)
        self.assertIn("Final scheduled", r.text)

    def test_no_emoji_in_templates_or_texts(self):
        import re
        import texts
        emoji = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
        for fn in os.listdir(os.path.join(os.path.dirname(HERE), "templates")):
            with open(os.path.join(os.path.dirname(HERE), "templates", fn), encoding="utf-8") as fh:
                self.assertIsNone(emoji.search(fh.read()), fn)
        with open(texts.__file__, encoding="utf-8") as fh:
            self.assertIsNone(emoji.search(fh.read()))
        with open(os.path.join(os.path.dirname(HERE), "db.py"), encoding="utf-8") as fh:
            self.assertIsNone(emoji.search(fh.read()))


if __name__ == "__main__":
    unittest.main()
