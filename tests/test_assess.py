"""The assessment portal in dry-run, redesigned so the one visible test is the live fan chats and every other signal is inferred from that session:
tokens, the single-section flow, the silent connection probe, per-reply telemetry parsing, WPM/accuracy from telemetry, behavioural AI-use risk,
the judge's chat-fluency sub-score, threshold outcomes incl. the review band and the AI-risk review, laptop-only, message chunking, markdown/emoji
hygiene and full dry runs (pass, typing fail, review band + staff decision, live time-outs)."""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.TemporaryDirectory()
os.environ.update({"HIRE_NO_WORKER": "1", "HIRE_SECRET": "test-assess", "HIRE_DB_PATH": os.path.join(TMP.name, "assess.db"), "HIRE_PUBLIC_URL": "http://test",
                   "BELOURA_GATEWAY_TOKEN": "gw", "PANEL_PASSWORD": "pw", "OPENROUTER_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "DISCORD_BOT_TOKEN": "", "SMTP_HOST": ""})

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import assess  # noqa: E402
import assess_llm as llm  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
from tools import assess_bank as bank  # noqa: E402

ADMIN = {"X-Gateway-Token": "gw", "Remote-User": "mikey", "Remote-Groups": "id-admins,hiring-admin", "Remote-Name": "Mikey"}
MANAGER = {"X-Gateway-Token": "gw", "Remote-User": "ian", "Remote-Groups": "hiring-manager", "Remote-Name": "Ian"}
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s|```", re.M)

GOOD_CONNECTION = {"ping": [20, 22, 21, 25, 19], "down": [{"bytes": 2_000_000, "ms": 500}] * 3, "up": [{"bytes": 1_000_000, "ms": 400}] * 3}
SLOW_CONNECTION = {"ping": [80, 90, 85], "down": [{"bytes": 2_000_000, "ms": 4000}] * 3, "up": [{"bytes": 1_000_000, "ms": 4000}] * 3}
WARM = "Hey you, I love that you came to find me today babe. Tell me about your day, what are you up to right now?"


def human_tel(text, wpm=60, backspaces=2, blur=0):
    """Plausible human typing telemetry for a reply: every character typed, a steady rhythm, no paste or instant insert."""
    L = len(text)
    ks = L
    typing_ms = int(round((ks / 5) / wpm * 60000)) if ks else 0
    return {"ttfk_ms": 1500, "typing_ms": typing_ms, "keystrokes": ks, "backspaces": backspaces,
            "iki": {"mean": 180, "stdev": 70, "min": 90, "sub50": 0, "count": max(0, ks - 1)},
            "paste": 0, "instant_inserts": 0, "blur": blur, "blur_ms": 0, "latency_ms": typing_ms + 1500, "length": L}


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, applicant, channel, text):
        self.sent.append((applicant["id"], channel, text))
        return "ok"


class AssessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(appmod.app)

    def setUp(self):
        self.rec = Recorder()
        self.alerts = []
        engine.SENDER = self.rec
        engine.ALERT = lambda text, aid=None: self.alerts.append((aid, text))
        self.n = getattr(AssessTest, "_n", 0) + 1
        AssessTest._n = self.n
        with db.db() as con:
            self.aid, _ = engine.upsert_applicant(con, {"name": f"Test Person{self.n}", "email": f"tp{self.n}@example.com", "timezone": "Asia/Manila"})
        self.tok = assess.token(self.aid)
        self.base = f"/assess/{self.aid}/{self.tok}"

    def tearDown(self):
        engine.SENDER = appmod.CH.send_to
        engine.ALERT = appmod.staff_alert

    # ------------------------------------------------------------ helpers

    def begin(self):
        with db.db() as con:
            assess.on_stage(con, self.aid)
        r = self.c.post(f"{self.base}/start", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("/s/live"))

    def open_live(self, probe=GOOD_CONNECTION):
        r = self.c.get(f"{self.base}/s/live", follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        if probe is not None:
            self.assertEqual(self.c.post(f"{self.base}/probe", json=probe).status_code, 200)

    def run_live(self, reply_fn, wpm=60, finish=True):
        for _ in range(60):
            st = self.c.get(f"{self.base}/live/state").json()
            if st["all_done"]:
                break
            self.assertTrue(st["your_turn"])
            fan = st["turns"][-1]["body"]
            text = reply_fn(fan)
            tel = human_tel(text or "x", wpm=wpm)
            if not text:
                tel = None
            j = self.c.post(f"{self.base}/live/reply", json={"scenario": st["scenario"]["key"], "text": text, "telemetry": tel,
                                                             "end_turn": True, "timed_out": not text}).json()
            self.assertTrue(j["ok"])
        else:
            self.fail("live chats never finished")
        if not finish:
            return None
        r = self.c.post(f"{self.base}/s/live", json={"telemetry": {"blur": 1, "paste": 0, "seconds": 600}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def full_run(self, reply_fn=None, wpm=60, probe=GOOD_CONNECTION):
        self.begin()
        self.open_live(probe=probe)
        return self.run_live(reply_fn or (lambda fan: WARM), wpm=wpm)

    # ------------------------------------------------------------ 1. tokens

    def test_token_validation(self):
        self.assertEqual(len(self.tok), 32)
        self.assertEqual(assess.token(self.aid), self.tok)
        self.assertNotEqual(assess.token(self.aid + 1), self.tok)
        self.assertTrue(assess.check_token(self.aid, self.tok))
        self.assertFalse(assess.check_token(self.aid, ""))
        self.assertFalse(assess.check_token(self.aid, self.tok[:-1] + "0"))
        self.assertEqual(self.c.get(f"/assess/{self.aid}/{'0' * 32}").status_code, 404)
        self.assertEqual(self.c.get("/assess/99999/" + assess.token(99999)).status_code, 404)
        r = self.c.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Start the assessment", r.text)
        self.assertIn("15 minutes", r.text)
        self.assertNotIn("WWW-Authenticate", r.headers)
        self.assertEqual(self.c.get(f"/staff/assess/{self.aid}").status_code, 401)

    def test_create_link_and_summary_before_start(self):
        with db.db() as con:
            url = assess.create_link(con, self.aid)
            self.assertEqual(url, f"http://test{self.base}")
            self.assertEqual(assess.create_link(con, self.aid), url)
            s = assess.summary(con, self.aid)
            self.assertEqual((s["status"], s["outcome"], s["derived"]["live_score"]), ("created", "", None))
            self.assertIn("/staff/assess/", s["staff_url"])
            with self.assertRaises(ValueError):
                assess.create_link(con, 424242)

    # ------------------------------------------------------------ 2. single-section flow

    def test_single_section_flow_is_only_live(self):
        self.assertEqual(assess.SECTIONS, ["live"])
        self.begin()
        # the old steps are gone: no connection / typing / english / scenario pages
        for key in ("connection", "typing", "english", "scenario_mcq"):
            self.assertEqual(self.c.get(f"{self.base}/s/{key}", follow_redirects=False).status_code, 404)
        self.assertEqual(self.c.get(f"{self.base}/s/nope").status_code, 404)
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 200)
        with db.db() as con:
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "assessment_in_progress")
            self.assertEqual(assess.get(con, self.aid)["section"], "live")
        st = self.c.get(f"{self.base}/live/state").json()
        self.assertEqual((st["scenario"]["n"], st["total"], st["reply_seconds"], st["chunk_idle_seconds"]), (1, 3, 90, 5))

    def test_assessment_is_laptop_only(self):
        iphone = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"}
        android = {"user-agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"}
        ipad = {"user-agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"}
        self.assertFalse(assess.is_laptop(iphone["user-agent"]))
        self.assertFalse(assess.is_laptop(android["user-agent"]))
        self.assertFalse(assess.is_laptop(ipad["user-agent"]))
        self.assertTrue(assess.is_laptop("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"))
        self.assertTrue(assess.is_laptop("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"))
        r = self.c.get(self.base, headers=iphone)
        self.assertEqual(r.status_code, 200)
        self.assertIn("laptop or desktop", r.text)
        self.assertNotIn("/start", r.text)
        r = self.c.post(f"{self.base}/start", headers=android, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], self.base)
        self.begin()
        r = self.c.get(f"{self.base}/s/live", headers=iphone, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], self.base)
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 200)

    def test_resumable_once(self):
        self.begin()
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 200)
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 200, "first re-entry is the one resume")
        with db.db() as con:
            self.assertEqual(assess.get(con, self.aid)["resumes"], 1)
        r = self.c.get(f"{self.base}/s/live", follow_redirects=False)
        self.assertEqual(r.status_code, 303, "second re-entry locks")
        r = self.c.get(self.base)
        self.assertEqual(r.status_code, 423)
        self.assertIn("paused", r.text)
        self.assertEqual(self.c.get(f"{self.base}/live/state").status_code, 409)
        with db.db() as con:
            self.assertEqual(assess.get(con, self.aid)["status"], "locked")
            self.assertTrue(db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND reason='assessment locked' AND status='open'", (self.aid,)))
        r = self.c.post(f"/staff/assess/{self.aid}/reopen", headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.c.get(self.base).status_code, 200)
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 200)
        with db.db() as con:
            self.assertEqual(assess.get(con, self.aid)["resumes"], 1, "a re-entry after reopen is the resume again")
            self.assertFalse(db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND reason='assessment locked' AND status='open'", (self.aid,)))

    def test_closed_applicant_link_is_inactive_before_start(self):
        with db.db() as con:
            assess.create_link(con, self.aid)
            engine.set_stage(con, self.aid, "rejected", force=True)
        r = self.c.get(self.base)
        self.assertEqual(r.status_code, 410)
        self.assertIn("no longer active", r.text)

    # ------------------------------------------------------------ 3. silent connection probe

    def test_probe_endpoints(self):
        r = self.c.get("/assess/ping")
        self.assertEqual((r.status_code, r.text, r.headers["cache-control"]), (200, "pong", "no-store"))
        self.assertEqual(len(self.c.get("/assess/blob?bytes=3000").content), 3000)
        self.assertEqual(len(self.c.get("/assess/blob?bytes=99999999").content), assess.MAX_BLOB)
        self.assertEqual(len(self.c.get("/assess/blob?bytes=0").content), 1)
        j = self.c.post("/assess/upload", content=b"z" * 5000).json()
        self.assertEqual(j["bytes"], 5000)
        self.assertIn("server_ms", j)
        self.assertEqual(self.c.post("/assess/upload", content=b"z", headers={"content-length": str(assess.MAX_UPLOAD + 1)}).status_code, 413)

    def test_silent_probe_is_stored_on_the_assessment(self):
        self.begin()
        self.open_live(probe=None)
        # before the probe posts, connection is unknown and never fails
        with db.db() as con:
            self.assertFalse(assess.get(con, self.aid)["probe"])
            cn = assess.connection_result({}, assess.thresholds(con))
            self.assertTrue(cn["unknown"] and cn["ok"])
        j = self.c.post(f"{self.base}/probe", json=GOOD_CONNECTION).json()
        self.assertEqual((j["ok"], j["stored"]), (True, True))
        self.assertFalse(self.c.post(f"{self.base}/probe", json=SLOW_CONNECTION).json()["stored"], "the probe stores once")
        with db.db() as con:
            probe = assess.get(con, self.aid)["probe"]
            self.assertTrue(probe["ran"])
            self.assertEqual((probe["down_mbps"], probe["up_mbps"], probe["latency_ms"], probe["jitter_ms"]), (32.0, 20.0, 21.0, 3.25))
            cn = assess.connection_result(probe, assess.thresholds(con))
            self.assertEqual((cn["unknown"], cn["down_mbps"], cn["ok"]), (False, 32.0, True))
        self.assertEqual(assess.probe_data(SLOW_CONNECTION)["down_mbps"], 4.0)
        self.assertFalse(assess.connection_result(assess.probe_data(SLOW_CONNECTION), {"speed_down_mbps": 10.0})["ok"])
        bad = assess.probe_data({"ping": ["a", None, -3, 10], "down": [{"bytes": "x", "ms": 0}], "up": "nope"})
        self.assertEqual((bad["latency_ms"], bad["down_mbps"], bad["up_mbps"]), (10.0, 0.0, 0.0))

    # ------------------------------------------------------------ 4. per-reply telemetry parsing

    def test_telemetry_parsing_is_robust_to_missing_fields(self):
        t = assess.parse_turn_telemetry({})
        self.assertEqual((t["keystrokes"], t["backspaces"], t["paste"], t["instant"], t["blur"], t["length"]), (0, 0, 0, 0, 0, 0))
        self.assertEqual(t["iki_mean"], 0.0)
        t = assess.parse_turn_telemetry("not a dict")
        self.assertEqual(t["typing_ms"], 0)
        raw = {"ttfk_ms": "1500", "typing_ms": 8000, "keystrokes": "40", "backspaces": 3, "instant_inserts": 1, "blur": 2, "blur_ms": 4000, "length": 40,
               "iki": {"mean": "120", "stdev": 55, "min": 40, "sub50": "2", "count": 39}, "paste": "x", "latency_ms": None}
        t = assess.parse_turn_telemetry(raw)
        self.assertEqual((t["ttfk_ms"], t["typing_ms"], t["keystrokes"], t["backspaces"], t["instant"], t["blur"], t["blur_ms"]), (1500, 8000, 40, 3, 1, 2, 4000))
        self.assertEqual((t["iki_mean"], t["iki_stdev"], t["iki_sub50"], t["iki_count"]), (120.0, 55.0, 2, 39))
        self.assertEqual((t["paste"], t["latency_ms"]), (0, 0))

    # ------------------------------------------------------------ 5. WPM / accuracy maths from telemetry

    def test_wpm_and_accuracy_maths(self):
        self.assertEqual(assess.wpm(900, 180), 60.0)
        self.assertEqual(assess.wpm(225, 60), 45.0)
        self.assertEqual(assess.wpm(100, 0), 0.0)
        self.assertEqual(assess.turn_wpm({"keystrokes": 300, "typing_ms": 60000}), 60.0)
        self.assertEqual(assess.turn_wpm({"keystrokes": 0, "typing_ms": 60000}), 0.0)
        self.assertEqual(assess.turn_wpm({"keystrokes": 300, "typing_ms": 0}), 0.0)
        self.assertEqual(assess.accuracy_from_backspaces({"keystrokes": 100, "backspaces": 0}), 100.0)
        self.assertEqual(assess.accuracy_from_backspaces({"keystrokes": 90, "backspaces": 10}), 90.0)
        self.assertEqual(assess.accuracy_from_backspaces({"keystrokes": 0, "backspaces": 0}), 0.0)
        # aggregate over the typed chunks of a burst
        w, acc = assess.aggregate_wpm([{"keystrokes": 100, "typing_ms": 20000, "backspaces": 0}, {"keystrokes": 200, "typing_ms": 40000, "backspaces": 0}])
        self.assertEqual((w, acc), (60.0, 100.0))
        self.assertEqual(assess.aggregate_wpm([{"keystrokes": 0, "typing_ms": 0, "backspaces": 0}]), (None, None))
        # a flat human reply (len ~ WARM) at 60 wpm, one burst of one typed chunk
        burst = [{"telemetry": json.dumps(human_tel(WARM, wpm=60)), "body": WARM}]
        out = assess.typing_from_bursts([burst])
        self.assertEqual((out["wpm"], out["samples"], out["pasted"], out["bursts"]), (60.0, 1, 0, 1))
        self.assertGreaterEqual(out["accuracy"], 97.0)
        # a pasted reply is left out of the WPM, and a slow typist is below the mark
        pasted = [{"telemetry": json.dumps({"paste": 1, "length": 40, "keystrokes": 0}), "body": "x" * 40}]
        slow = [{"telemetry": json.dumps(human_tel(WARM, wpm=30)), "body": WARM}]
        out = assess.typing_from_bursts([pasted, slow])
        self.assertEqual((out["wpm"], out["samples"], out["pasted"]), (30.0, 1, 1))

    # ------------------------------------------------------------ 6. behavioural AI-use risk

    def _scenario(self, chunks, title="Chat"):
        """Build one scenario row: chunks is a list of bursts, each a list of (body, telemetry_dict); a fan turn sits before each burst."""
        turns = []
        for burst in chunks:
            turns.append({"role": "fan", "body": "fan says something", "latency_ms": None, "timed_out": 0})
            for body, tel in burst:
                turns.append({"role": "applicant", "body": body, "latency_ms": tel.get("latency_ms", 1000), "timed_out": 0, "telemetry": json.dumps(tel)})
        return {"key": "k", "title": title, "turns": turns}

    def test_ai_risk_per_reply(self):
        clean, _ = assess.ai_risk_reply(assess.parse_turn_telemetry(human_tel(WARM)), WARM)
        self.assertLessEqual(clean, 15)
        paste, rp = assess.ai_risk_reply(assess.parse_turn_telemetry({"paste": 1, "length": 40, "keystrokes": 0}), "x" * 40)
        self.assertGreater(paste, 70)
        self.assertTrue(any("paste" in r for r in rp))
        instant, _ = assess.ai_risk_reply(assess.parse_turn_telemetry({"instant_inserts": 1, "length": 80, "keystrokes": 0, "typing_ms": 200}), "y" * 80)
        self.assertGreater(instant, 70)
        blur, _ = assess.ai_risk_reply(assess.parse_turn_telemetry({"instant_inserts": 1, "blur": 1, "length": 80, "keystrokes": 0}), "y" * 80)
        self.assertGreaterEqual(blur, instant)
        self.assertEqual(blur, 100)

    def test_ai_risk_aggregate_chunking(self):
        # clean, naturally chunked human session: several short typed messages per turn -> low, with the human note
        natural = self._scenario([[("hey you", human_tel("hey you")), ("hows your day", human_tel("hows your day"))],
                                  [("aw tell me more", human_tel("aw tell me more")), ("i love that", human_tel("i love that"))]])
        out = assess.ai_risk([natural])
        self.assertLessEqual(out["score"], 15)
        self.assertEqual(out["chunk_stats"]["messages_per_turn"], 2.0)
        self.assertTrue(any("chunking" in r for r in out["reasons"]))
        # a single long wall-of-text turn raises the risk
        wall = self._scenario([[("x" * 300, human_tel("x" * 300, wpm=55))]])
        self.assertEqual(assess.ai_risk([wall])["chunk_stats"]["wall_of_text_turns"], 1)
        self.assertGreaterEqual(assess.ai_risk([wall])["score"], 20)
        # a paste is never masked by the chunking bonus
        mixed = self._scenario([[("hi", human_tel("hi")), ("x" * 40, {"paste": 1, "length": 40, "keystrokes": 0, "latency_ms": 500})],
                                [("ok", human_tel("ok")), ("sure", human_tel("sure"))]])
        self.assertGreater(assess.ai_risk([mixed])["score"], 70)

    # ------------------------------------------------------------ 7. judge JSON carries chat fluency

    def test_judge_json_parsing_with_malformed_reply(self):
        for garbage in ("", "I cannot grade this.", "{not json", "[1,2,3]", '{"scores": "nope"}', None):
            out = llm.parse_judge(garbage or "")
            self.assertFalse(out["ok"], garbage)
            self.assertIsNone(out["total"])
            self.assertTrue(out["error"])
        good = {"scores": {c: {"score": 8, "evidence": "quoted"} for c in bank.JUDGE_CRITERIA}, "chat_fluency": {"score": 4, "evidence": "lowercase, slangy"},
                "summary": "Solid, warm, one clear ask."}
        out = llm.parse_judge("Here is my grade:\n```json\n" + json.dumps(good) + "\n```\nHope that helps.")
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 8.0)
        self.assertEqual(out["chat_fluency"], 4.0)
        self.assertEqual(out["chat_fluency_evidence"], "lowercase, slangy")
        self.assertEqual(out["summary"], "Solid, warm, one clear ask.")
        # chat_fluency is optional: a judge reply without it still grades, with chat_fluency None
        no_cf = {"scores": {c: {"score": 7} for c in bank.JUDGE_CRITERIA}}
        out = llm.parse_judge(json.dumps(no_cf))
        self.assertTrue(out["ok"])
        self.assertIsNone(out["chat_fluency"])
        # chat_fluency clamps to 0-5
        over = {"scores": {c: {"score": 9} for c in bank.JUDGE_CRITERIA}, "chat_fluency": 11}
        self.assertEqual(llm.parse_judge(json.dumps(over))["chat_fluency"], 5.0)
        partial = {"scores": {c: {"score": 9} for c in bank.JUDGE_CRITERIA[:-1]}}
        out = llm.parse_judge(json.dumps(partial))
        self.assertFalse(out["ok"])
        self.assertIn("red_flags", out["error"])

    def test_dry_run_judge_and_fan(self):
        self.assertFalse(llm.configured())
        self.assertEqual(llm.sim_models()[0], "anthropic/claude-sonnet-5.5")
        self.assertEqual(llm.judge_models(), ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5"])
        spec = bank.LIVE_BY_KEY["main_run"]
        with db.db() as con:
            text, model = llm.fan_turn(con, self.aid, spec, [])
            self.assertEqual((text, model), (spec["canned"][0], "dry-run"))
            turns = [{"role": "fan", "body": text}, {"role": "applicant", "body": "Hey you, welcome. What brings you here tonight?"}]
            text2, _ = llm.fan_turn(con, self.aid, spec, turns)
            self.assertEqual(text2, spec["canned"][1])
            v = llm.judge_scenario(con, self.aid, spec, turns)
            self.assertTrue(v["ok"])
            self.assertTrue(0 <= v["total"] <= 10)
            self.assertIsNotNone(v["chat_fluency"])
            self.assertTrue(0 <= v["chat_fluency"] <= 5)
            self.assertEqual(db.one(con, "SELECT COUNT(*) n FROM llm_call WHERE applicant_id=?", (self.aid,))["n"], 0, "dry-run makes no model calls")

    def test_every_model_call_is_logged_with_applicant_and_cost(self):
        os.environ.update({"OPENROUTER_API_KEY": "x", "OPENROUTER_BASE": "http://127.0.0.1:9", "ASSESS_TIMEOUT": "2"})
        try:
            with db.db() as con:
                text, model = llm.fan_turn(con, self.aid, bank.LIVE_BY_KEY["rude_fan"], [])
                self.assertEqual(model, "dry-run-fallback")
                self.assertEqual(text, bank.LIVE_BY_KEY["rude_fan"]["canned"][0])
                rows = db.q(con, "SELECT * FROM llm_call WHERE applicant_id=? ORDER BY id", (self.aid,))
                self.assertEqual([r["model"] for r in rows], llm.sim_models())
                self.assertTrue(all(r["ok"] == 0 and r["prompt_version"] == llm.SIM_VERSION and r["error"] for r in rows))
                self.assertEqual(assess.cost(con, self.aid)["n"], len(rows))
        finally:
            os.environ.update({"OPENROUTER_API_KEY": "", "OPENROUTER_BASE": "", "ASSESS_TIMEOUT": ""})
        self.assertEqual(llm.cost_for("x-ai/grok-4.3", 1_000_000, 0, None), 1.25)
        self.assertEqual(llm.cost_for("whatever", 10, 10, 0.0123), 0.0123)
        self.assertEqual(llm.cost_for("unknown/model", 100, 100, None), 0.0)

    def test_bank_is_well_formed(self):
        self.assertEqual(len(bank.LIVE_SCENARIOS), 3)
        for s in bank.LIVE_SCENARIOS:
            self.assertTrue(3 <= s["fan_turns"] <= 12, s["key"])
            self.assertGreaterEqual(len(s["canned"]), s["fan_turns"])
        self.assertEqual(len(bank.best_answers()), 10)

    # ------------------------------------------------------------ 8. thresholds and outcomes

    def test_threshold_outcomes_incl_review_and_ai_risk(self):
        with db.db() as con:
            th = assess.thresholds(con)
        self.assertEqual((th["scenario_live"], th["live_review_floor"], th["chat_fluency"], th["typing_wpm"], th["speed_down_mbps"], th["ai_risk_review"]),
                         (8.0, 7.0, 3.5, 45.0, 10.0, 70.0))
        good = {"live": {"score": 8.5, "unjudged": 0}, "chat_fluency": {"avg": 4.2}, "typing": {"wpm": 60},
                "connection": {"down_mbps": 30, "unknown": False}, "ai_risk": {"score": 10}}
        self.assertEqual(assess.outcome_for(good, th)[0], "passed")
        # hard gates: chat fluency, typing, connection, live below the floor
        for k, v, needle in (("chat_fluency", {"avg": 3.4}, "chat fluency"), ("typing", {"wpm": 44.9}, "typing"),
                             ("connection", {"down_mbps": 9.9, "unknown": False}, "download"), ("live", {"score": 6.9, "unjudged": 0}, "live chats")):
            oc, hard, _ = assess.outcome_for({**good, k: v}, th)
            self.assertEqual(oc, "failed", k)
            self.assertIn(needle, " ".join(hard))
        # review band
        oc, hard, review = assess.outcome_for({**good, "live": {"score": 7.0, "unjudged": 0}}, th)
        self.assertEqual((oc, hard), ("review", []))
        self.assertIn("review band", review[0])
        self.assertEqual(assess.outcome_for({**good, "live": {"score": 8.0, "unjudged": 0}}, th)[0], "passed")
        # ungraded chat -> review
        self.assertEqual(assess.outcome_for({**good, "live": {"score": None, "unjudged": 6}}, th)[0], "review")
        # AI-use risk above the mark -> review, never an auto-fail
        oc, hard, review = assess.outcome_for({**good, "ai_risk": {"score": 85}}, th)
        self.assertEqual((oc, hard), ("review", []))
        self.assertIn("AI-use risk", review[0])
        # a hard fail still wins over a review reason
        oc, hard, review = assess.outcome_for({**good, "typing": {"wpm": 10}, "ai_risk": {"score": 90}}, th)
        self.assertEqual(oc, "failed")
        self.assertTrue(review)
        # unknown connection is skipped, not failed
        self.assertEqual(assess.outcome_for({**good, "connection": {"unknown": True}}, th)[0], "passed")
        # no measurable typing is skipped, not failed
        self.assertEqual(assess.outcome_for({**good, "typing": {"wpm": None}}, th)[0], "passed")
        self.assertEqual(assess.outcome_for({}, th)[0], "passed")
        with db.db() as con:
            db.set_setting(con, "assess_scenario_live", "7.5")
            self.assertEqual(assess.outcome_for({**good, "live": {"score": 7.6, "unjudged": 0}}, assess.thresholds(con))[0], "passed")
            db.set_setting(con, "assess_scenario_live", "8.0")

    # ------------------------------------------------------------ 9. hooks

    def test_on_stage_sends_link_via_james_and_holds_without_channel(self):
        with db.db() as con:
            url = assess.on_stage(con, self.aid)
            self.assertEqual(url, f"http://test{self.base}")
            a = engine.applicant(con, self.aid)
            self.assertEqual(a["stage"], "assessment_sent")
            self.assertIsNotNone(a["awaiting_since"])
            m = db.one(con, "SELECT * FROM message WHERE applicant_id=? AND kind='assessment' ORDER BY id DESC", (self.aid,))
            self.assertEqual((m["status"], m["channel"]), ("sent", "email"))
            self.assertIn(url, m["body"])
            self.assertIn("12 minutes", m["body"])
            self.assertEqual(len(self.rec.sent), 1)
            self.assertEqual(assess.on_stage(con, self.aid), url)
            self.assertEqual(len(self.rec.sent), 1, "no resend by default")
            assess.on_stage(con, self.aid, resend=True)
            self.assertEqual(len(self.rec.sent), 2)
            hid, _ = engine.upsert_applicant(con, {"name": "Held Person", "telegram": f"held{self.n}"})
            assess.on_stage(con, hid)
            m = db.one(con, "SELECT * FROM message WHERE applicant_id=? AND kind='assessment'", (hid,))
            self.assertEqual(m["status"], "held")
            engine.link_telegram(con, hid, "12345")
            self.assertEqual(engine.flush_held(con, hid), 1)
            self.assertEqual(self.rec.sent[-1][1], "telegram")
            engine.set_stage(con, hid, "rejected", force=True)
            self.assertIsNone(assess.on_stage(con, hid))
            self.assertIsNone(assess.on_stage(con, 999999))

    def test_staff_send_button_and_settings(self):
        r = self.c.post(f"/staff/assess/{self.aid}/send", headers=MANAGER, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.rec.sent), 1)
        r = self.c.get("/staff/assess", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"Test Person{self.n}", r.text)
        self.assertEqual(self.c.get("/staff/assess/settings", headers=MANAGER).status_code, 403)
        r = self.c.get("/staff/assess/settings", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertIn("assess_typing_wpm", r.text)
        self.assertIn("assess_ai_risk_review", r.text)
        r = self.c.post("/staff/assess/settings", headers=ADMIN, data={"assess_typing_wpm": "50", "assess_scenarios": "3"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            self.assertEqual(assess.thresholds(con)["typing_wpm"], 50.0)
            self.assertEqual(assess.scenario_count(con), 3)
            db.set_setting(con, "assess_typing_wpm", "45")
            db.set_setting(con, "assess_scenarios", "6")
        self.assertEqual(self.c.get("/staff/assess/999999", headers=MANAGER).status_code, 404)

    # ------------------------------------------------------------ 10. text hygiene

    def test_markdown_free_and_emoji_free_applicant_text(self):
        self.assertEqual(llm.clean_text("**hey** there \U0001F600 _you_ `ok`"), "hey there you ok")
        self.assertEqual(llm.clean_text("Fan: \"sup\""), "sup")
        for fn in sorted(os.listdir(os.path.join(ROOT, "templates"))):
            if fn.startswith("assess_"):
                with open(os.path.join(ROOT, "templates", fn), encoding="utf-8") as fh:
                    body = fh.read()
                self.assertIsNone(EMOJI.search(body), fn)
                self.assertNotIn("**", body, fn)
        for fn in ("assess.py", "assess_llm.py", os.path.join("tools", "assess_bank.py"), os.path.join("static", "assess.js")):
            with open(os.path.join(ROOT, fn), encoding="utf-8") as fh:
                self.assertIsNone(EMOJI.search(fh.read()), fn)
        a = {"name": "Maria Santos"}
        for text in (assess.link_message(a, "http://test/assess/1/x"), assess.passed_message(a, ""), assess.passed_message(a, "Open times:\n1. x"),
                     assess.failed_message(a, "typing 30 WPM"), assess.review_message(a)):
            self.assertIsNone(EMOJI.search(text))
            self.assertIsNone(MARKDOWN.search(text), text)
            self.assertIn("Maria", text)
        for s in bank.LIVE_SCENARIOS:
            for line in s["canned"]:
                self.assertIsNone(EMOJI.search(line))
                self.assertIsNone(MARKDOWN.search(line))

    def test_ai_written_heuristic_feeds_the_formal_register_signal(self):
        perfect = ("I completely understand how exhausting that must have been for you. Please take a moment to rest and recharge this evening. "
                   "I am here whenever you would like to talk about it further; your wellbeing matters to me.")
        self.assertTrue(llm.ai_written_flag(perfect)["flag"])
        self.assertFalse(llm.ai_written_flag("aw no, that sounds rough babe. come tell me about it, what did he do this time?")["flag"])
        # a perfectly polished short reply nudges the per-reply AI risk up (mild), typed telemetry aside
        score, reasons = assess.ai_risk_reply(assess.parse_turn_telemetry(human_tel(perfect)), perfect)
        self.assertGreaterEqual(score, assess.AI_MILD_FORMAL)
        self.assertTrue(any("formal" in r for r in reasons))

    # ------------------------------------------------------------ 11. message chunking

    def test_message_chunking_within_a_turn(self):
        self.begin()
        self.open_live()
        st = self.c.get(f"{self.base}/live/state").json()
        key = st["scenario"]["key"]
        # three short messages before the fan replies
        j = self.c.post(f"{self.base}/live/reply", json={"scenario": key, "text": "hey you", "telemetry": human_tel("hey you"), "end_turn": False}).json()
        self.assertTrue(j["ok"] and j.get("chunk") and not j["done"])
        self.assertIsNone(self.c.get(f"{self.base}/live/state").json()["turns"][-1]["body"] == "fan" or None)
        self.c.post(f"{self.base}/live/reply", json={"scenario": key, "text": "just subbed", "telemetry": human_tel("just subbed"), "end_turn": False})
        # the fan has still not replied: last turn is the applicant's second chunk
        turns = self.c.get(f"{self.base}/live/state").json()["turns"]
        self.assertEqual([t["role"] for t in turns], ["fan", "applicant", "applicant"])
        # end the burst -> the fan replies once, to the whole burst
        j = self.c.post(f"{self.base}/live/reply", json={"scenario": key, "text": "whats up", "telemetry": human_tel("whats up"), "end_turn": True}).json()
        self.assertFalse(j["done"])
        self.assertEqual(j["fan_so_far"], 2)
        with db.db() as con:
            a = assess.get(con, self.aid)
            rows = db.q(con, "SELECT role, body FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], key))
            self.assertEqual([r["role"] for r in rows], ["fan", "applicant", "applicant", "applicant", "fan"], "three chunks stored, then one fan reply")
            scs = assess.scenarios(con, a["id"])
            burst = assess.applicant_bursts([s for s in scs if s["key"] == key][0]["turns"])[0]
            self.assertEqual(len(burst), 3, "the three chunks group into one logical turn")

    def test_vault_content_send(self):
        self.begin()
        self.open_live()
        st = self.c.get(f"{self.base}/live/state").json()
        key = st["scenario"]["key"]
        # tease, then click a vault item to send it as a locked PPV, then end the burst
        self.c.post(f"{self.base}/live/reply", json={"scenario": key, "text": "ok dont judge me", "telemetry": human_tel("ok dont judge me"), "end_turn": False})
        j = self.c.post(f"{self.base}/live/reply", json={"scenario": key, "content": {"name": "PPV 1", "kind": "ppv", "price": "$12", "media": "she takes her top off", "caption": "a little taste first"}, "telemetry": {"content": True}, "end_turn": False}).json()
        self.assertTrue(j["ok"] and j.get("chunk"))
        self.c.post(f"{self.base}/live/reply", json={"scenario": key, "text": "just for you x", "telemetry": human_tel("just for you x"), "end_turn": True})
        with db.db() as con:
            a = assess.get(con, self.aid)
            rows = db.q(con, "SELECT role, body, telemetry FROM assessment_turn WHERE assessment_id=? AND scenario_key=? AND role='applicant' ORDER BY id", (a["id"], key))
            ppv = [r for r in rows if "locked PPV" in r["body"]]
            self.assertEqual(len(ppv), 1, "the script step stored one PPV turn")
            self.assertIn("PPV 1", ppv[0]["body"])
            self.assertIn("$12", ppv[0]["body"])
            self.assertIn("a little taste first", ppv[0]["body"])
            self.assertTrue(assess.parse_turn_telemetry(db.jl(ppv[0]["telemetry"], {}))["content"], "PPV turn is flagged as content")
            # the click must not count as pasted / AI in the typing + AI aggregates
            scs = assess.scenarios(con, a["id"])
            risk = assess.ai_risk(scs)
            self.assertEqual(risk["chunk_stats"]["pasted"], 0, "a vault click is never counted as a paste")

    def test_team_grading_and_calibration(self):
        self.full_run()  # self.aid now has 3 done chats
        tok = assess.grade_token("connor")
        base = f"/grade/connor/{tok}"
        self.assertEqual(self.c.get(f"/grade/connor/{'0'*32}").status_code, 404, "bad token is rejected")
        self.assertEqual(self.c.get("/grade/nope/" + assess.grade_token("nope")).status_code, 404, "unknown grader is rejected")
        hub = self.c.get(base)
        self.assertEqual(hub.status_code, 200)
        self.assertIn("Grading as Connor", hub.text)
        # grade one chat
        r = self.c.get(f"{base}/{self.aid}/main_run")
        self.assertEqual(r.status_code, 200)
        r = self.c.post(f"{base}/{self.aid}/main_run", data={"overall": "4", "sales_move": "5", "note": "good build"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            g = assess.grade_of(con, self.aid, "main_run", "connor")
            self.assertEqual(g["overall"], 4.0)
            self.assertEqual(g["scores"]["sales_move"], 5.0)
            # a second grader with a different weight
            assess.save_grade(con, self.aid, "main_run", "gabby", 2.0, {}, "")
            grades = assess.chat_grades(con, self.aid, "main_run")
            self.assertEqual(len(grades), 2)
            # weighted mean: connor(4, w4) + gabby(2, w1) = 18/5 = 3.6
            self.assertEqual(assess.weighted_human(grades), 3.6)
        # compare view (staff)
        r = self.c.get("/staff/grade", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Grade calibration", r.text)
        self.assertIn("/grade/connor/", r.text)

    def test_grader_cannot_grade_own_run(self):
        self.full_run()
        # name this applicant "Connor" so the connor grader is excluded from his own chat
        with db.db() as con:
            con.execute("UPDATE applicant SET name=? WHERE id=?", ("Connor", self.aid))
        tok = assess.grade_token("connor")
        self.assertEqual(self.c.post(f"/grade/connor/{tok}/{self.aid}/main_run", data={"overall": "5"}).status_code, 403)
        hub = self.c.get(f"/grade/connor/{tok}")
        self.assertNotIn(f"/{self.aid}/main_run", hub.text, "his own chat link is hidden from the list")

    def test_collect_run_hides_result(self):
        with db.db() as con:
            con.execute("UPDATE applicant SET channel_code=? WHERE id=?", ("collect", self.aid))
        self.full_run()  # completes all chats
        r = self.c.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertIn("All done", r.text)
        self.assertNotIn("You passed", r.text)
        self.assertNotIn("/10", r.text, "no scores shown to the applicant")
        msgs = [m for m in self.rec.sent if m[0] == self.aid]
        self.assertFalse(any("passed the assessment" in (m[2] or "") for m in msgs), "no pass/fail message to a collect run")
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual(a["status"], "done")
            self.assertTrue(a["scores"], "scores are still stored for staff/graders")
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "assessment_done", "stage stops at done, not passed")

    # ------------------------------------------------------------ 12. full runs
    # ------------------------------------------------------------ 12. full runs
    # ------------------------------------------------------------ 12. full runs

    def test_full_run_passes_and_sends_slots(self):
        out = self.full_run()
        self.assertTrue(out["ok"])
        self.assertEqual(out["next"], self.base)
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual((a["status"], a["outcome"]), ("done", "passed"))
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "passed")
            hist = [h["to_stage"] for h in engine.stage_history(con, self.aid)]
            self.assertEqual(hist[-3:], ["assessment_in_progress", "assessment_done", "passed"])
            d = assess.live_data(con, self.aid)
            self.assertGreaterEqual(d["score"], 8.0)
            self.assertGreaterEqual(d["chat_fluency_avg"], 3.5)
            self.assertGreaterEqual(d["typing"]["wpm"], 45)
            self.assertFalse(d["connection"]["unknown"])
            self.assertEqual(d["connection"]["down_mbps"], 32.0)
            self.assertLessEqual(d["ai_risk"]["score"], 20)
            scs = assess.scenarios(con, a["id"])
            self.assertEqual(len(scs), 3)
            for sc in scs:
                self.assertEqual(sc["status"], "done")
                self.assertEqual(sum(1 for t in sc["turns"] if t["role"] == "fan"), bank.LIVE_BY_KEY[sc["key"]]["fan_turns"])
                self.assertIsNotNone(sc["score"])
                self.assertIsNotNone((sc["judge"] or {}).get("chat_fluency"))
            s = assess.summary(con, self.aid)
            self.assertEqual(s["outcome"], "passed")
            self.assertEqual(len(s["live"]), 3)
            txt = assess.summary_text(con, self.aid)
            self.assertIn("passed", txt)
            self.assertIn("Chat fluency", txt)
            self.assertIn("Typing", txt)
            self.assertIsNone(MARKDOWN.search(txt))
        kinds = [m for m in self.rec.sent if m[0] == self.aid]
        self.assertEqual(len(kinds), 2, "the link, then the good news")
        self.assertIn("passed the assessment", kinds[-1][2])
        self.assertTrue(self.alerts and self.alerts[-1][0] == self.aid and "scorecard" in self.alerts[-1][1].lower())
        r = self.c.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertIn("You passed", r.text)
        self.assertNotIn("evidence", r.text.lower(), "the applicant never sees judge notes")
        self.assertEqual(self.c.get(f"{self.base}/s/live", follow_redirects=False).status_code, 303)
        r = self.c.get(f"/staff/assess/{self.aid}", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        for needle in ("Full chat: warm up and run the script", "Rude or abusive fan", "Persona consistency", "Chat fluency", "Typing", "AI-use risk", "Cost",
                       "that gym post had me staring a bit too long"):
            self.assertIn(needle, r.text)
        self.assertNotIn("Pass</button>", r.text, "no decision buttons on a decided scorecard")

    def test_full_run_typing_fail_declines_with_reason(self):
        self.full_run(wpm=30)
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual(a["outcome"], "failed", a["outcome_reason"])
            self.assertIn("typing", a["outcome_reason"])
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "failed")
        last = [m for m in self.rec.sent if m[0] == self.aid][-1][2]
        self.assertIn("won't be taking your application further", last)
        self.assertIn("typing", last)
        r = self.c.get(self.base)
        self.assertIn("below the mark", r.text)
        self.assertIn("typing", r.text)

    def test_full_run_review_band_then_staff_decides(self):
        # dry-run judge: replies of 8+ words with no question and no warmth words land at 7.5/10 -> review band; fluency 3.75 passes its bar
        out = self.full_run(reply_fn=lambda fan: "That is a fair point and I hear what is being said here tonight.")
        self.assertTrue(out["ok"])
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual(a["outcome"], "review", a["outcome_reason"])
            self.assertIn("review band", a["outcome_reason"])
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "review")
            self.assertIn("review", db.STAGES)
            self.assertTrue(db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND reason='assessment review' AND status='open'", (self.aid,)))
        self.assertIn("within two working days", [m for m in self.rec.sent if m[0] == self.aid][-1][2])
        r = self.c.get(self.base)
        self.assertIn("Your results are in", r.text)
        r = self.c.get(f"/staff/assess/{self.aid}", headers=MANAGER)
        self.assertIn('value="passed"', r.text)
        r = self.c.post(f"/staff/assess/{self.aid}/decide", headers=MANAGER, data={"outcome": "passed"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual((a["outcome"], a["decided_by"]), ("passed", "ian"))
            self.assertEqual(engine.applicant(con, self.aid)["stage"], "passed")
            self.assertFalse(db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND reason='assessment review' AND status='open'", (self.aid,)))
        self.assertIn("passed the assessment", self.rec.sent[-1][2])
        self.assertEqual(self.c.post(f"/staff/assess/{self.aid}/decide", headers=MANAGER, data={"outcome": "maybe"}).status_code, 400)

    def test_live_timeouts_and_empty_replies_fail(self):
        self.begin()
        self.open_live()
        self.assertEqual(self.c.post(f"{self.base}/s/live", json={}).status_code, 409, "live cannot be submitted before the chats")
        st = self.c.get(f"{self.base}/live/state").json()
        self.assertEqual((st["scenario"]["n"], st["total"], st["reply_seconds"]), (1, 3, 90))
        self.assertEqual(self.c.post(f"{self.base}/live/reply", json={"scenario": "rude_fan", "text": "hi"}).status_code, 409, "only the open chat accepts replies")
        # a timed-out empty reply still advances the turn
        j = self.c.post(f"{self.base}/live/reply", json={"scenario": st["scenario"]["key"], "text": "", "timed_out": True, "end_turn": True}).json()
        self.assertFalse(j["done"])
        out = self.run_live(lambda fan: "ok")
        self.assertTrue(out["ok"])
        with db.db() as con:
            a = assess.get(con, self.aid)
            self.assertEqual(a["outcome"], "failed")
            self.assertIn("live chats", a["outcome_reason"])
            d = assess.live_data(con, self.aid)
            self.assertEqual(d["timed_out"], 1)
            self.assertLess(d["score"], 7.0)
            self.assertTrue(any("timed out" in f for f in a["flags"]))

    def test_schema_and_settings_are_idempotent(self):
        with db.db() as con:
            assess.ensure_schema(con)
            assess.ensure_schema(con)
            self.assertEqual(db.setting(con, "assess_typing_wpm"), "45")
            self.assertEqual(db.setting(con, "assess_scenario_live"), "8.0")
            self.assertEqual(db.setting(con, "assess_chat_fluency"), "3.5")
            self.assertEqual(db.setting(con, "assess_ai_risk_review"), "70")
            self.assertIn("review", db.SIDE)
            self.assertEqual(db.STAGE_LABELS["review"], "Review")
            cols = {r["name"] for r in con.execute("PRAGMA table_info(assessment)")}
            self.assertIn("probe", cols)
            tcols = {r["name"] for r in con.execute("PRAGMA table_info(assessment_turn)")}
            self.assertIn("telemetry", tcols)
        self.assertEqual(self.c.get("/staff", headers=MANAGER).status_code, 200, "the board renders with the review column")


if __name__ == "__main__":
    unittest.main()
