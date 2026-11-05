"""Phase 2 (redesigned 2026-11-04): the self-serve assessment is one thing only, a short set of live fan chats. Everything else is inferred from
that one session.

One link per applicant: /assess/<id>/<token> (token = HMAC of the id with HIRE_SECRET, like the thank-you page). The applicant does one visible test:
a handful of short chats with a simulated fan (fan played by SIM_MODEL, graded by JUDGE_MODEL). From that single session we derive every other signal:

  - chat quality: persona, rapport, sales, pacing, boundaries, red flags (the judge scores these per chat).
  - chat fluency: the judge's casual-chat-English sub-score (0-5), aggregated across the chats.
  - typing: WPM and an accuracy proxy, computed from the per-reply keystroke telemetry (pasted replies ignored).
  - AI-use risk: a 0-100 flag built from behavioural telemetry (paste, instant inserts, tab-blur, implausible speed, too-smooth timing), never a text classifier.
  - connection: measured by a SILENT probe (our own ping / blob / upload endpoints) that runs in the background on the chat page; stored, never a visible step.

There is no separate connection, typing, English-MCQ or scenario-MCQ step any more. PC-only is still enforced (is_laptop + the client gate).

Hooks for the rest of the app (unchanged signatures):
  create_link(con, aid) -> url            make sure the assessment row exists and return the personal link
  on_stage(con, aid, resend=False) -> url  send the link through James (engine.deliver; held automatically when no channel is linked yet)
  summary(con, aid) -> dict, summary_text  for the briefing pack and the staff post
  ensure_schema(con)                       tables, migrations, default settings; called from db.init()

Outcome rules (thresholds live in the settings table, assess_* keys, editable on /staff/assess/settings): pass needs the live overall >= scenario_live
(default 8.0), chat fluency >= its bar (default 3.5/5), typing median WPM >= typing_wpm (default 45) and connection down >= speed_down_mbps (default 10,
skipped when the probe did not run). A live score in [review_floor, scenario_live) is a review; an AI-use risk above ai_risk_review (default 70) is a
review (a human checks, never an auto-fail on its own). 'review' is not one of engine's stages, so this module registers it at import and raises an
escalation. Resumable once: re-entering the chat page a second time mid-session locks the assessment until staff reopen it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import statistics
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import assess_llm as llm
import db
import engine
import sam
import texts
from tools import assess_bank as bank

log = logging.getLogger("hire.assess")
router = APIRouter()
HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

SECTIONS = ["live"]
SECTION_LABELS = {"live": "Live chats"}
SECTION_MINUTES = {"live": 12}
REVIEW_STAGE = "review"
MAX_BLOB = 4_000_000
MAX_UPLOAD = 2_500_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS assessment (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'created', section TEXT NOT NULL DEFAULT 'live', resumes INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT, link_sent_at TEXT,
  outcome TEXT NOT NULL DEFAULT '', outcome_reason TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '', decided_at TEXT,
  scores TEXT NOT NULL DEFAULT '{}', flags TEXT NOT NULL DEFAULT '[]', user_agent TEXT NOT NULL DEFAULT '',
  probe TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessment_section (
  id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL, key TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, entries INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL DEFAULT '{}', telemetry TEXT NOT NULL DEFAULT '{}', score REAL, passed INTEGER,
  UNIQUE (assessment_id, key)
);
CREATE TABLE IF NOT EXISTS assessment_scenario (
  id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL, key TEXT NOT NULL, idx INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', judge TEXT NOT NULL DEFAULT '{}', score REAL, started_at TEXT, finished_at TEXT,
  UNIQUE (assessment_id, key)
);
CREATE TABLE IF NOT EXISTS assessment_turn (
  id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL, scenario_key TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER, client_latency_ms INTEGER, timed_out INTEGER NOT NULL DEFAULT 0, telemetry TEXT NOT NULL DEFAULT '{}',
  model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_assessment_turn ON assessment_turn (assessment_id, scenario_key, id);
CREATE TABLE IF NOT EXISTS assess_human_grade (
  id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL, scenario_key TEXT NOT NULL, grader TEXT NOT NULL,
  overall REAL, scores TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(assessment_id, scenario_key, grader)
);
"""

THRESHOLD_KEYS = {"assess_scenario_live": "8.0", "assess_live_review_floor": "7.0", "assess_chat_fluency": "3.5",
                  "assess_typing_wpm": "45", "assess_speed_down_mbps": "10", "assess_ai_risk_review": "70"}
OTHER_KEYS = {"assess_reply_seconds": "90", "assess_scenarios": str(len(bank.LIVE_SCENARIOS)), "assess_chunk_idle_seconds": "5"}
THRESHOLD_LABELS = {"assess_scenario_live": "Live chats, out of 10 (at least)",
                    "assess_live_review_floor": "Live chats review floor (between floor and pass = review; below = fail)",
                    "assess_chat_fluency": "Chat fluency, out of 5 (at least)",
                    "assess_typing_wpm": "Typing speed, WPM (at least; from the chat replies)",
                    "assess_speed_down_mbps": "Download speed, Mbps (at least; from the silent probe)",
                    "assess_ai_risk_review": "AI-use risk, out of 100 (above this = review, never an auto-fail)",
                    "assess_reply_seconds": "Seconds per fan message (the whole reply burst)", "assess_scenarios": "Number of fan chats (max 6)",
                    "assess_chunk_idle_seconds": "Idle seconds before the fan replies (end of a message burst)"}
# The staff's current belief, kept for comparison on the staff pages (plan section 5).
OTHER_SOURCES = [("HIRING UPDATES 13 Oct", {"typing": 50}), ("SOP 22 Oct", {"typing": 50})]

# AI-use risk weights (per reply, capped at 100). Behavioural signals only; the formal-register text tell is a small add, not a classifier.
AI_STRONG_PASTE = 80      # paste attempt or programmatic instant insert present
AI_STRONG_NOTYPE = 60     # a long polished reply that appeared with almost no typing / far fewer keystrokes than characters
AI_BLUR_COMBO = 20        # left the tab during the reply AND the reply was pasted/inserted
AI_MED_FASTLONG = 35      # a long reply sent with implausibly low latency
AI_MED_SMOOTH = 30        # very smooth, correction-free keystroke timing (many sub-50ms gaps, tiny spread)
AI_MED_TTFK = 35          # a long pause to first key, then a long reply appeared at once
AI_MILD_FORMAL = 15       # over-polished formal register (wrong for chat; a mild AI tell)


class Locked(Exception):
    pass


# ---------------------------------------------------------------- schema, settings, stage registration

def register_stage() -> None:
    if REVIEW_STAGE not in db.STAGES:
        db.STAGES.append(REVIEW_STAGE)
    if REVIEW_STAGE not in db.SIDE:
        db.SIDE.insert(db.SIDE.index("failed") if "failed" in db.SIDE else len(db.SIDE), REVIEW_STAGE)
    db.STAGE_LABELS.setdefault(REVIEW_STAGE, "Review")


register_stage()


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)
    # migrations for databases created before the single-session redesign
    acols = {r["name"] for r in con.execute("PRAGMA table_info(assessment)")}
    if "probe" not in acols:
        con.execute("ALTER TABLE assessment ADD COLUMN probe TEXT NOT NULL DEFAULT '{}'")
    tcols = {r["name"] for r in con.execute("PRAGMA table_info(assessment_turn)")}
    if "telemetry" not in tcols:
        con.execute("ALTER TABLE assessment_turn ADD COLUMN telemetry TEXT NOT NULL DEFAULT '{}'")
    for k, v in {**THRESHOLD_KEYS, **OTHER_KEYS}.items():
        con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))


def thresholds(con) -> dict:
    out = {}
    for k, d in {**THRESHOLD_KEYS, **OTHER_KEYS}.items():
        try:
            out[k[len("assess_"):]] = float(db.setting(con, k, d) or d)
        except ValueError:
            out[k[len("assess_"):]] = float(d)
    return out


def scenario_count(con, th: dict | None = None) -> int:
    th = th or thresholds(con)
    try:
        n = int(th.get("scenarios", len(bank.LIVE_SCENARIOS)))
    except (TypeError, ValueError):
        n = len(bank.LIVE_SCENARIOS)
    return max(1, min(n, len(bank.LIVE_SCENARIOS)))


# ---------------------------------------------------------------- tokens and links

def token(aid: int) -> str:
    return hmac.new(os.environ.get("HIRE_SECRET", "dev-secret").encode(), f"assess:{aid}".encode(), hashlib.sha256).hexdigest()[:32]


def check_token(aid: int, tok: str) -> bool:
    return bool(tok) and hmac.compare_digest(token(aid), (tok or "").strip())


def public_url(path: str = "") -> str:
    return os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai").rstrip("/") + path


def link_path(aid: int) -> str:
    return f"/assess/{aid}/{token(aid)}"


def staff_url(aid: int) -> str:
    return public_url(f"/staff/assess/{aid}")


def get(con, aid: int) -> dict | None:
    a = db.one(con, "SELECT * FROM assessment WHERE applicant_id=?", (aid,))
    if a:
        a["scores"] = db.jl(a.get("scores"), {})
        a["flags"] = db.jl(a.get("flags"), [])
        a["probe"] = db.jl(a.get("probe"), {})
    return a


def ensure(con, aid: int) -> dict:
    if not engine.applicant(con, aid):
        raise ValueError("no such applicant")
    ts = db.now()
    con.execute("INSERT OR IGNORE INTO assessment (applicant_id, created_at, updated_at) VALUES (?,?,?)", (aid, ts, ts))
    return get(con, aid)


def create_link(con, aid: int) -> str:
    ensure(con, aid)
    return public_url(link_path(aid))


# ---------------------------------------------------------------- James's words for this stage (plain text, no markdown, no emoji)

def _first(a: dict) -> str:
    return (a.get("name") or "").split(" ")[0]


def link_message(a: dict, url: str) -> str:
    hi = f"Hi {_first(a)}," if _first(a) else "Hi,"
    return (f"{hi} you're through to the assessment. It is one short chat test, about 12 minutes, on a laptop or desktop: a handful of quick "
            "conversations with a simulated fan where you reply as the creator, just like a real shift. There is nothing to revise, we learn "
            f"everything we need from how you chat.\n\nYour personal link: {url}\n\nDo it in one sitting on a computer, not a phone. You can pause "
            f"and come back once. Pasting is switched off. The team reviews every result and I'll message you here as soon as yours is in. {texts.AGENT_NAME}")


def passed_message(a: dict, slots_text: str) -> str:
    base = f"Good news, {_first(a) or 'there'}: you passed the assessment. Next is a 30-minute final interview, a mock chat with one of our chat managers."
    return base + ("\n" + slots_text if slots_text else " I'll send you the open times as soon as the team has them.")


def failed_message(a: dict, reason: str) -> str:
    hi = f"Hi {_first(a)}," if _first(a) else "Hi,"
    why = reason or "the results were below the marks we need for this role"
    return (f"{hi} thank you for completing the assessment. We won't be taking your application further this time: {why}. "
            f"You're welcome to apply again in the future. All the best, {texts.AGENT_NAME}")


def review_message(a: dict) -> str:
    return (f"Thanks, {_first(a) or 'there'}, your assessment is in. The team is looking at your results and I'll message you here within two "
            "working days with the next step.")


# ---------------------------------------------------------------- hooks used by the rest of the app

def on_stage(con, aid: int, resend: bool = False) -> str | None:
    """After interview_passed (or whenever staff want): send the link through James. Returns the URL, or None when nothing was sent.
    engine.deliver is the outbound helper; with no linked channel the message is held and goes out when one links."""
    a = engine.applicant(con, aid)
    if not a or a.get("do_not_contact"):
        return None
    if a["stage"] in db.CLOSED and not resend:
        return None
    url = create_link(con, aid)
    row = get(con, aid)
    if row.get("link_sent_at") and not resend:
        return url
    engine.set_stage(con, aid, "assessment_sent", actor="assess", reason="assessment link sent")
    engine.deliver(con, aid, link_message(a, url), kind="assessment", actor="assess")
    ts = db.now()
    con.execute("UPDATE applicant SET awaiting_since=COALESCE(awaiting_since, ?), updated_at=? WHERE id=?", (ts, ts, aid))
    con.execute("UPDATE assessment SET link_sent_at=?, updated_at=? WHERE applicant_id=?", (ts, ts, aid))
    return url


def cost(con, aid: int) -> dict:
    return db.one(con, "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) usd, COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tout "
                       "FROM llm_call WHERE applicant_id=? AND prompt_version LIKE 'assess-%'", (aid,))


def sections(con, assessment_id: int) -> dict:
    out = {}
    for r in db.q(con, "SELECT * FROM assessment_section WHERE assessment_id=?", (assessment_id,)):
        r["data"] = db.jl(r.get("data"), {})
        r["telemetry"] = db.jl(r.get("telemetry"), {})
        out[r["key"]] = r
    return out


def scenarios(con, assessment_id: int) -> list[dict]:
    rows = db.q(con, "SELECT * FROM assessment_scenario WHERE assessment_id=? ORDER BY idx", (assessment_id,))
    for r in rows:
        r["judge"] = db.jl(r.get("judge"), {})
        r["title"] = bank.LIVE_BY_KEY.get(r["key"], {}).get("title", r["key"])
        r["turns"] = db.q(con, "SELECT * FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (assessment_id, r["key"]))
    return rows


def live_data(con, aid: int) -> dict:
    """The derived bundle, read from the one live section row (empty dict before the section is scored)."""
    a = get(con, aid)
    if not a:
        return {}
    r = db.one(con, "SELECT data FROM assessment_section WHERE assessment_id=? AND key='live'", (a["id"],))
    return db.jl((r or {}).get("data"), {})


def summary(con, aid: int) -> dict:
    """Everything the briefing pack needs, as plain data."""
    a = get(con, aid)
    if not a:
        return {"applicant_id": aid, "status": "none", "link": public_url(link_path(aid)), "staff_url": staff_url(aid)}
    th = thresholds(con)
    d = live_data(con, aid)
    out = {"applicant_id": aid, "status": a["status"], "outcome": a["outcome"], "outcome_reason": a["outcome_reason"], "started_at": a["started_at"],
           "finished_at": a["finished_at"], "resumes": a["resumes"], "flags": a["flags"], "live": [], "derived": _derived_view(d),
           "cost_usd": round(cost(con, aid)["usd"], 4), "link": public_url(link_path(aid)), "staff_url": staff_url(aid), "thresholds": th}
    s, f = db.parse_ts(a["started_at"]), db.parse_ts(a["finished_at"])
    out["minutes"] = round((f - s).total_seconds() / 60, 1) if s and f else None
    for sc in scenarios(con, a["id"]):
        out["live"].append({"key": sc["key"], "title": sc["title"], "score": sc["score"], "chat_fluency": (sc["judge"] or {}).get("chat_fluency"),
                            "summary": (sc["judge"] or {}).get("summary", ""), "status": sc["status"]})
    return out


def _derived_view(d: dict) -> dict:
    ty = d.get("typing") or {}
    ar = d.get("ai_risk") or {}
    cn = d.get("connection") or {}
    return {"live_score": d.get("score"), "chat_fluency": d.get("chat_fluency_avg"), "typing_wpm": ty.get("wpm"), "typing_accuracy": ty.get("accuracy"),
            "ai_risk": ar.get("score"), "ai_risk_reasons": ar.get("reasons") or [], "connection": cn, "eq": d.get("eq") or {}}


def summary_text(con, aid: int) -> str:
    s = summary(con, aid)
    a = engine.applicant(con, aid) or {}
    if s["status"] == "none":
        return f"Assessment for {a.get('name') or '#' + str(aid)}: not created."
    dv = s["derived"]
    lines = [f"Assessment scorecard: {a.get('name') or '#' + str(aid)} ({s['status']}{', ' + s['outcome'] if s['outcome'] else ''})"]
    if s["outcome_reason"]:
        lines.append(f"Reason: {s['outcome_reason']}")
    if dv["live_score"] is not None:
        lines.append(f"- Live chats: {dv['live_score']:g}/10")
    if dv["chat_fluency"] is not None:
        lines.append(f"- Chat fluency: {dv['chat_fluency']:g}/5")
    if dv["typing_wpm"] is not None:
        lines.append(f"- Typing: {dv['typing_wpm']:g} WPM" + (f" at {dv['typing_accuracy']:g}%" if dv["typing_accuracy"] is not None else ""))
    cn = dv["connection"]
    if cn:
        lines.append("- Connection: unknown (probe did not run)" if cn.get("unknown") else f"- Connection: {cn.get('down_mbps', 0):g} Mbps down, {cn.get('latency_ms', 0):g} ms")
    if dv["ai_risk"] is not None:
        lines.append(f"- AI-use risk: {dv['ai_risk']:g}/100" + (" (check the chats)" if dv["ai_risk"] > s["thresholds"]["ai_risk_review"] else ""))
    for sc in s["live"]:
        if sc["score"] is not None:
            lines.append(f"  {sc['title']}: {sc['score']}/10. {sc['summary']}".rstrip())
    if dv["ai_risk_reasons"]:
        lines.append("AI-risk evidence: " + "; ".join(dv["ai_risk_reasons"][:6]))
    if s["flags"]:
        lines.append("Flags: " + "; ".join(s["flags"]))
    lines.append(f"Cost ${s['cost_usd']:.4f}" + (f", {s['minutes']} min" if s["minutes"] is not None else "") + f". {s['staff_url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- pure maths (tested on their own)

def median(values: list[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else 0.0


def _median_or_none(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def jitter(pings: list[float]) -> float:
    vals = [float(v) for v in pings if v is not None]
    if len(vals) < 2:
        return 0.0
    return round(sum(abs(b - a) for a, b in zip(vals, vals[1:])) / (len(vals) - 1), 2)


def mbps(nbytes: float, ms: float) -> float:
    if not ms or ms <= 0:
        return 0.0
    return round(float(nbytes) * 8 / (float(ms) * 1000), 2)


def wpm(chars: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return round(chars / 5 / (seconds / 60), 1)


def accuracy(correct_chars: int, typed_chars: int) -> float:
    if typed_chars <= 0:
        return 0.0
    return round(100.0 * correct_chars / typed_chars, 1)


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default


def _num_list(v, limit: int = 12) -> list[float]:
    out = []
    for x in (v if isinstance(v, list) else [])[:limit]:
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if f == f and f >= 0:
            out.append(f)
    return out


# ---------------------------------------------------------------- per-reply telemetry -> typing and AI-use risk

def parse_turn_telemetry(raw) -> dict:
    """Normalise the browser's per-reply telemetry to a fixed shape, robust to missing or junk fields."""
    t = raw if isinstance(raw, dict) else {}
    iki = t.get("iki") if isinstance(t.get("iki"), dict) else {}
    return {"ttfk_ms": max(0, _int(t.get("ttfk_ms"))), "typing_ms": max(0, _int(t.get("typing_ms"))),
            "keystrokes": max(0, _int(t.get("keystrokes"))), "backspaces": max(0, _int(t.get("backspaces"))),
            "iki_mean": _float(iki.get("mean")), "iki_stdev": _float(iki.get("stdev")), "iki_min": _float(iki.get("min")),
            "iki_sub50": max(0, _int(iki.get("sub50"))), "iki_count": max(0, _int(iki.get("count"))),
            "paste": max(0, _int(t.get("paste"))), "instant": max(0, _int(t.get("instant_inserts", t.get("instant")))),
            "blur": max(0, _int(t.get("blur"))), "blur_ms": max(0, _int(t.get("blur_ms"))),
            "latency_ms": max(0, _int(t.get("latency_ms"))), "length": max(0, _int(t.get("length"))),
            "content": bool(t.get("content"))}


def turn_wpm(tel: dict) -> float:
    """Words per minute from the real typed characters (keystroke count) and the typing duration. 0 when it cannot be computed."""
    minutes = tel.get("typing_ms", 0) / 60000.0
    ks = tel.get("keystrokes", 0)
    if minutes <= 0 or ks <= 0:
        return 0.0
    return round(ks / 5 / minutes, 1)


def accuracy_from_backspaces(tel: dict) -> float:
    """Accuracy proxy: how little of the typing was corrections. 100 - backspaces / (keystrokes + backspaces)."""
    denom = tel.get("keystrokes", 0) + tel.get("backspaces", 0)
    if denom <= 0:
        return 0.0
    return round(100.0 * (1 - tel.get("backspaces", 0) / denom), 1)


def _is_pasted(tel: dict, length: int) -> bool:
    return tel.get("paste", 0) > 0 or tel.get("instant", 0) > 0 or (length >= 20 and tel.get("keystrokes", 0) < length * 0.5)


def applicant_bursts(turns: list[dict]) -> list[list[dict]]:
    """Group a scenario's ordered turns into applicant bursts: the runs of consecutive applicant chunks between fan messages.
    Real chatters send several short messages before the fan replies, so one fan message maps to one burst of 1+ applicant rows."""
    bursts, cur = [], []
    for t in turns:
        if t.get("role") == "applicant":
            cur.append(t)
        elif cur:
            bursts.append(cur)
            cur = []
    if cur:
        bursts.append(cur)
    return bursts


def aggregate_wpm(chunk_tels: list[dict]) -> tuple:
    """Pool the TYPED chunks of one burst: WPM and accuracy from the summed keystrokes and typing time. (None, None) when nothing was typed."""
    ks = sum(c.get("keystrokes", 0) for c in chunk_tels)
    ms = sum(c.get("typing_ms", 0) for c in chunk_tels)
    bs = sum(c.get("backspaces", 0) for c in chunk_tels)
    if ks <= 0 or ms <= 0:
        return None, None
    w = round(ks / 5 / (ms / 60000.0), 1)
    denom = ks + bs
    acc = round(100.0 * (1 - bs / denom), 1) if denom > 0 else 0.0
    return w, acc


def typing_from_bursts(bursts: list[list[dict]]) -> dict:
    """Median WPM / accuracy across the bursts (one sample per burst, pooled over its typed chunks). Pasted chunks are left out."""
    wpms, accs, pasted = [], [], 0
    for burst in bursts:
        typed = []
        for t in burst:
            tel = parse_turn_telemetry(db.jl(t.get("telemetry"), {}))
            if tel.get("content"):
                continue
            length = tel.get("length") or len(t.get("body") or "")
            if length == 0:
                continue
            if _is_pasted(tel, length):
                pasted += 1
                continue
            typed.append(tel)
        w, acc = aggregate_wpm(typed)
        if w is not None:
            wpms.append(w)
            accs.append(acc)
    return {"wpm": _median_or_none(wpms), "accuracy": _median_or_none(accs), "samples": len(wpms), "pasted": pasted, "bursts": len(bursts)}


def ai_risk_reply(tel: dict, text: str = "") -> tuple[int, list[str]]:
    """0-100 AI-use risk for one reply from behavioural telemetry, plus the reasons. Text is used only for the mild formal-register tell."""
    length = tel.get("length") or len(text or "")
    ks = tel.get("keystrokes", 0)
    typing_ms = tel.get("typing_ms", 0)
    latency = tel.get("latency_ms", 0)
    score, reasons = 0, []
    near_zero_typing = length > 0 and typing_ms < length * 25            # < 25 ms/char is faster than any human can type
    few_keys = length >= 20 and ks < length * 0.5                        # far fewer keystrokes than characters in the box
    polished_long = length >= 60
    if tel.get("paste", 0) > 0 or tel.get("instant", 0) > 0:
        score += AI_STRONG_PASTE
        reasons.append("paste attempt or instant text insert in the reply")
        if tel.get("blur", 0) > 0:
            score += AI_BLUR_COMBO
            reasons.append("left the tab, then the reply was pasted or inserted")
    if polished_long and (near_zero_typing or few_keys):
        score += AI_STRONG_NOTYPE
        reasons.append("a long reply appeared with almost no typing")
    if length >= 40 and 0 < latency < length * 25:
        score += AI_MED_FASTLONG
        reasons.append("a long reply sent unusually fast")
    if tel.get("iki_sub50", 0) >= 5 and 0 < tel.get("iki_stdev", 0) < 15 and tel.get("backspaces", 0) == 0 and ks >= 20:
        score += AI_MED_SMOOTH
        reasons.append("unusually smooth, correction-free keystroke timing")
    if tel.get("ttfk_ms", 0) > 8000 and length >= 40 and near_zero_typing:
        score += AI_MED_TTFK
        reasons.append("a long pause, then a long reply appeared at once")
    if llm.ai_written_flag(text or "").get("flag"):
        score += AI_MILD_FORMAL
        reasons.append("over-polished formal register (wrong for chat; a mild AI tell)")
    return min(100, score), reasons


def ai_risk(scenario_rows: list[dict]) -> dict:
    """Aggregate the per-chunk risk across every applicant burst. The headline is the worst single chunk, then adjusted for chunking shape:
    natural short-message bursts (a human tell) lower a low score; a single long wall-of-text burst raises it. A strong per-chunk signal
    (paste / instant insert) is never masked by the chunking bonus."""
    per, chunk_scores, worst = [], [], 0
    total_chunks = typed_chunks = pasted_chunks = wall_turns = 0
    bursts_total = 0
    lengths = []
    for s in scenario_rows:
        for bi, burst in enumerate(applicant_bursts(s.get("turns", []))):
            bursts_total += 1
            real, burst_len = 0, 0
            for ci, t in enumerate(burst):
                tel = parse_turn_telemetry(db.jl(t.get("telemetry"), {}))
                if tel.get("content"):
                    continue
                length = tel.get("length") or len(t.get("body") or "")
                if length == 0:
                    continue
                real += 1
                total_chunks += 1
                burst_len += length
                lengths.append(length)
                if _is_pasted(tel, length):
                    pasted_chunks += 1
                else:
                    typed_chunks += 1
                sc, reasons = ai_risk_reply(tel, t.get("body") or "")
                chunk_scores.append(sc)
                worst = max(worst, sc)
                if sc or reasons:
                    per.append({"scenario": s.get("title", s.get("key")), "n": bi + 1, "chunk": ci + 1, "score": sc, "reasons": reasons})
            if real == 1 and burst_len >= 220:
                wall_turns += 1
    msgs_per_turn = round(total_chunks / bursts_total, 2) if bursts_total else 0.0
    median_chunk = round(statistics.median(lengths), 1) if lengths else 0.0
    notes = []
    adj = worst
    if wall_turns:
        adj = min(100, adj + 20)
        notes.append(f"{wall_turns} turn(s) sent as one long wall-of-text, not a natural chat burst")
    natural = bursts_total and msgs_per_turn >= 2 and median_chunk <= 120 and pasted_chunks == 0 and typed_chunks >= bursts_total
    if natural and worst <= 40:
        adj = max(0, adj - 15)
        notes.append("short-message chunking, each typed live (a human signal)")
    out_reasons = list(notes)
    for p in per:
        for r in p["reasons"]:
            line = f"{p['scenario']} msg {p['n']}.{p['chunk']}: {r}"
            if line not in out_reasons:
                out_reasons.append(line)
    return {"score": adj, "worst_chunk": worst, "mean": round(sum(chunk_scores) / len(chunk_scores), 1) if chunk_scores else 0.0,
            "reasons": out_reasons[:14], "per_reply": per,
            "chunk_stats": {"messages_per_turn": msgs_per_turn, "median_chunk_chars": median_chunk, "chunks": total_chunks, "bursts": bursts_total,
                            "typed": typed_chunks, "pasted": pasted_chunks, "wall_of_text_turns": wall_turns}}


def connection_result(probe: dict, th: dict) -> dict:
    """The silent probe result as a connection verdict. unknown=True when the probe never ran; then it is skipped in the outcome."""
    if not probe or not isinstance(probe, dict) or not probe.get("ran"):
        return {"unknown": True, "down_mbps": 0.0, "up_mbps": 0.0, "latency_ms": 0.0, "jitter_ms": 0.0, "ok": True}
    down = _float(probe.get("down_mbps"))
    return {"unknown": False, "down_mbps": down, "up_mbps": _float(probe.get("up_mbps")), "latency_ms": _float(probe.get("latency_ms")),
            "jitter_ms": _float(probe.get("jitter_ms")), "ping_samples": probe.get("ping_samples") or [], "down_samples": probe.get("down_samples") or [],
            "up_samples": probe.get("up_samples") or [], "ok": down >= th["speed_down_mbps"]}


def probe_data(payload: dict) -> dict:
    """The connection numbers from the browser probe payload (ping ms list; down/up as {bytes, ms})."""
    pings = _num_list(payload.get("ping"))
    downs = [mbps(_int(d.get("bytes")), float(d.get("ms") or 0)) for d in (payload.get("down") or []) if isinstance(d, dict)][:6]
    ups = [mbps(_int(d.get("bytes")), float(d.get("ms") or 0)) for d in (payload.get("up") or []) if isinstance(d, dict)][:6]
    return {"ran": True, "latency_ms": median(pings), "jitter_ms": jitter(pings), "down_mbps": median(downs), "up_mbps": median(ups),
            "ping_samples": pings, "down_samples": downs, "up_samples": ups}


def outcome_for(results: dict, th: dict) -> tuple[str, list[str], list[str]]:
    """results: {live:{score, unjudged}, chat_fluency:{avg}, typing:{wpm|None}, connection:{down_mbps, unknown}, ai_risk:{score}}.
    Returns (outcome, hard_reasons, review_reasons)."""
    hard, review = [], []
    lv = results.get("live") or {}
    if lv:
        score = lv.get("score")
        if score is None:
            review.append("live chats could not be graded")
        elif score < th["live_review_floor"]:
            hard.append(f"live chats {score:g}/10 (we need {th['scenario_live']:g})")
        elif score < th["scenario_live"]:
            review.append(f"live chats {score:g}/10, in the review band")
        if lv.get("unjudged"):
            review.append(f"{lv['unjudged']} chat(s) without a judge score")
    cf = results.get("chat_fluency") or {}
    if cf and cf.get("avg") is not None and cf["avg"] < th["chat_fluency"]:
        hard.append(f"chat fluency {cf['avg']:g}/5 (we need {th['chat_fluency']:g})")
    ty = results.get("typing") or {}
    if ty and ty.get("wpm") is not None and ty["wpm"] < th["typing_wpm"]:
        hard.append(f"typing {ty['wpm']:g} WPM (we need {th['typing_wpm']:g})")
    cn = results.get("connection") or {}
    if cn and not cn.get("unknown") and cn.get("down_mbps", 0) < th["speed_down_mbps"]:
        hard.append(f"download speed {cn.get('down_mbps', 0):g} Mbps (we need {th['speed_down_mbps']:g})")
    ar = results.get("ai_risk") or {}
    if ar and ar.get("score", 0) > th["ai_risk_review"]:
        review.append(f"AI-use risk {ar.get('score', 0):g}/100 (please check the chats)")
    if hard:
        return "failed", hard, review
    if review:
        return "review", hard, review
    return "passed", hard, review


def results_from_live(d: dict) -> dict:
    ty = d.get("typing") or {}
    ar = d.get("ai_risk") or {}
    return {"live": {"score": d.get("score"), "unjudged": d.get("unjudged", 0)},
            "chat_fluency": {"avg": d.get("chat_fluency_avg")},
            "typing": {"wpm": ty.get("wpm")},
            "connection": d.get("connection") or {"unknown": True},
            "ai_risk": {"score": ar.get("score", 0)}}


def section_detail(key: str, d: dict) -> str:
    if key == "live":
        if d.get("score") is None:
            return "not graded"
        return f"{d.get('score'):g}/10 over {d.get('judged', 0)} chats"
    return ""


# ---------------------------------------------------------------- the flow

def start(con, aid: int, ua: str = "") -> dict:
    a = ensure(con, aid)
    if a["status"] == "locked":
        raise Locked()
    ts = db.now()
    if a["status"] == "created":
        con.execute("UPDATE assessment SET status='started', started_at=?, section='live', user_agent=?, updated_at=? WHERE id=?", (ts, ua[:300], ts, a["id"]))
        engine.set_stage(con, aid, "assessment_in_progress", actor="assess", reason="assessment started")
    elif a["status"] == "started" and a["section"] not in SECTIONS:
        con.execute("UPDATE assessment SET section='live', updated_at=? WHERE id=?", (ts, a["id"]))
    return get(con, aid)


def section_row(con, assessment_id: int, key: str) -> dict:
    con.execute("INSERT OR IGNORE INTO assessment_section (assessment_id, key) VALUES (?,?)", (assessment_id, key))
    r = db.one(con, "SELECT * FROM assessment_section WHERE assessment_id=? AND key=?", (assessment_id, key))
    r["data"] = db.jl(r.get("data"), {})
    r["telemetry"] = db.jl(r.get("telemetry"), {})
    return r


def enter_section(con, aid: int, key: str, ua: str = "") -> str:
    """'ok' to render, 'redirect' when this is not the current section, raises Locked on the second re-entry (resumable once)."""
    a = get(con, aid)
    if not a or a["status"] in ("created", "done"):
        return "redirect"
    if a["status"] == "locked":
        raise Locked()
    if key != a["section"]:
        return "redirect"
    r = section_row(con, a["id"], key)
    ts = db.now()
    is_test = (engine.applicant(con, aid) or {}).get("channel_code") in TEST_CHANNELS  # test/collect links never lock, so people can refresh/tab away freely
    if r["started_at"] and not r["finished_at"] and r["entries"] > 0:
        resumes = a["resumes"] + 1
        if resumes > 1 and not is_test:
            con.execute("UPDATE assessment SET status='locked', resumes=?, updated_at=? WHERE id=?", (resumes, ts, a["id"]))
            engine.create_escalation(con, aid, "assessment locked", f"Re-entered the {SECTION_LABELS[key]} section a second time; the assessment is locked until staff reopen it.")
            raise Locked()
        con.execute("UPDATE assessment SET resumes=?, updated_at=? WHERE id=?", (resumes, ts, a["id"]))
    con.execute("UPDATE assessment_section SET started_at=COALESCE(started_at, ?), entries=entries+1 WHERE id=?", (ts, r["id"]))
    if ua and not a.get("user_agent"):
        con.execute("UPDATE assessment SET user_agent=? WHERE id=?", (ua[:300], a["id"]))
    return "ok"


def reopen(con, aid: int, who: str = "staff") -> None:
    """Unlock after a second re-entry: the resume counter resets, finished sections are kept."""
    a = get(con, aid)
    if not a or a["status"] == "done":
        return
    con.execute("UPDATE assessment SET status='started', resumes=0, updated_at=? WHERE id=?", (db.now(), a["id"]))
    con.execute("UPDATE escalation SET status='closed', answered_by=?, answered_at=? WHERE applicant_id=? AND reason='assessment locked' AND status='open'", (who, db.now(), aid))


def store_probe(con, aid: int, payload: dict) -> dict:
    """Store the silent connection probe on the assessment, once. Called from the chat page in the background; failures never block the flow."""
    a = get(con, aid)
    if not a or a["status"] != "started":
        raise HTTPException(409, "the assessment is not in progress")
    if a.get("probe") and a["probe"].get("ran"):
        return {"ok": True, "stored": False}
    data = probe_data(payload if isinstance(payload, dict) else {})
    con.execute("UPDATE assessment SET probe=?, updated_at=? WHERE id=?", (json.dumps(data), db.now(), a["id"]))
    return {"ok": True, "stored": True}


def _telemetry(payload: dict, ua: str, r: dict) -> dict:
    t = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
    started = db.parse_ts(r.get("started_at"))
    server_seconds = int((datetime.now(timezone.utc) - started).total_seconds()) if started else None
    return {"blur": max(0, _int(t.get("blur"))), "paste": max(0, _int(t.get("paste"))), "seconds": max(0, _int(t.get("seconds"))),
            "server_seconds": server_seconds, "user_agent": (ua or "")[:300]}


def score_live(con, aid: int, payload: dict, th: dict) -> tuple[dict, float | None, bool]:
    a = get(con, aid)
    n = scenario_count(con, th)
    scs = [s for s in scenarios(con, a["id"]) if s["idx"] < n and s["key"] in bank.LIVE_BY_KEY]
    if len(scs) < n or any(s["status"] != "done" for s in scs):
        raise HTTPException(409, "the live chats are not finished")
    judged = [s["score"] for s in scs if s["score"] is not None]
    score = round(sum(judged) / len(judged), 2) if judged else None
    cfs = [(s["judge"] or {}).get("chat_fluency") for s in scs]
    cfs = [c for c in cfs if c is not None]
    cf_avg = round(sum(cfs) / len(cfs), 2) if cfs else None
    eq = {}
    for c in bank.JUDGE_CRITERIA:
        vals = [((s["judge"] or {}).get("scores") or {}).get(c, {}).get("score") for s in scs]
        vals = [v for v in vals if v is not None]
        eq[c] = round(sum(vals) / len(vals), 2) if vals else None
    all_bursts = [b for s in scs for b in applicant_bursts(s["turns"])]
    typing = typing_from_bursts(all_bursts)
    risk = ai_risk(scs)
    conn = connection_result(a.get("probe") or {}, th)
    # reply latency = time to the first message of each burst (fan shown -> first reply), not every chunk
    firsts = []
    for burst in all_bursts:
        for t in burst:
            if t["latency_ms"] is not None:
                firsts.append(t["latency_ms"])
                break
    data = {"score": score, "judged": len(judged), "unjudged": len(scs) - len(judged), "per_scenario": {s["key"]: s["score"] for s in scs},
            "chat_fluency_avg": cf_avg, "chat_fluency_per": {s["key"]: (s["judge"] or {}).get("chat_fluency") for s in scs}, "eq": eq,
            "typing": typing, "ai_risk": risk, "connection": conn, "median_reply_ms": median(firsts) if firsts else None,
            "timed_out": sum(1 for s in scs for t in s["turns"] if t["role"] == "applicant" and t["timed_out"])}
    return data, score, (score is not None and score >= th["scenario_live"])


SCORERS = {"live": score_live}


def submit_section(con, aid: int, key: str, payload: dict, ua: str = "") -> dict:
    a = get(con, aid)
    if not a or a["status"] != "started":
        raise HTTPException(409, "the assessment is not in progress")
    if key != a["section"]:
        raise HTTPException(409, f"the current section is {a['section']}")
    r = section_row(con, a["id"], key)
    if not r["started_at"] or r["finished_at"]:
        raise HTTPException(409, "this section is not open")
    th = thresholds(con)
    data, score, passed = SCORERS[key](con, aid, payload if isinstance(payload, dict) else {}, th)
    tel = _telemetry(payload if isinstance(payload, dict) else {}, ua, r)
    ts = db.now()
    con.execute("UPDATE assessment_section SET finished_at=?, data=?, telemetry=?, score=?, passed=? WHERE id=?",
                (ts, json.dumps(data), json.dumps(tel), score, 1 if passed else 0, r["id"]))
    idx = SECTIONS.index(key)
    nxt = SECTIONS[idx + 1] if idx + 1 < len(SECTIONS) else None
    if nxt:
        con.execute("UPDATE assessment SET section=?, updated_at=? WHERE id=?", (nxt, ts, a["id"]))
        return {"ok": True, "next": f"{link_path(aid)}/s/{nxt}", "section": nxt}
    finish(con, aid)
    return {"ok": True, "next": link_path(aid), "section": None}


def finish(con, aid: int) -> dict:
    a = get(con, aid)
    secs = sections(con, a["id"])
    th = thresholds(con)
    d = (secs.get("live") or {}).get("data") or {}
    results = results_from_live(d)
    outcome, hard, review = outcome_for(results, th)
    flags = []
    risk = d.get("ai_risk") or {}
    if risk.get("score", 0) > th["ai_risk_review"]:
        flags.append(f"AI-use risk {risk['score']:g}/100 - check the chats")
    if (d.get("typing") or {}).get("pasted"):
        flags.append(f"{d['typing']['pasted']} repl(ies) looked pasted (left out of the typing score)")
    live_tel = (secs.get("live") or {}).get("telemetry") or {}
    if live_tel.get("paste"):
        flags.append(f"{live_tel['paste']} blocked paste attempt(s)")
    if live_tel.get("blur", 0) >= 3:
        flags.append(f"left the tab {live_tel['blur']} times")
    if d.get("timed_out"):
        flags.append(f"{d['timed_out']} live repl(ies) timed out")
    if (d.get("connection") or {}).get("unknown"):
        flags.append("connection check did not run")
    if a["resumes"]:
        flags.append("resumed once")
    reason = "; ".join(hard if outcome == "failed" else review)
    cn = d.get("connection") or {}
    scores = {"live": d.get("score"), "chat_fluency": d.get("chat_fluency_avg"), "typing_wpm": (d.get("typing") or {}).get("wpm"),
              "ai_risk": (d.get("ai_risk") or {}).get("score"), "connection_down": None if cn.get("unknown") else cn.get("down_mbps")}
    ts = db.now()
    con.execute("UPDATE assessment SET status='done', finished_at=?, outcome=?, outcome_reason=?, scores=?, flags=?, updated_at=? WHERE id=?",
                (ts, outcome, reason[:1000], json.dumps(scores), json.dumps(flags), ts, a["id"]))
    engine.set_stage(con, aid, "assessment_done", actor="assess", reason="live chats submitted")
    if not hides_result(engine.applicant(con, aid)):
        apply_outcome(con, aid, outcome, reason, actor="assess")
    if engine.ALERT:
        try:
            engine.ALERT(summary_text(con, aid), aid)
        except Exception as e:  # noqa: BLE001
            log.warning("assessment alert failed: %s", e)
    return get(con, aid)


def _slots_text(con, aid: int) -> str:
    try:
        res = sam.run_tool(con, aid, "list_slots", {})
    except Exception as e:  # noqa: BLE001
        log.warning("list_slots failed: %s", e)
        return ""
    if not res.get("slots"):
        return ""
    lines = [f"{i + 1}. {s['local']} ({s['uk']})" for i, s in enumerate(res["slots"])]
    return "Open times:\n" + "\n".join(lines) + "\nReply with the number you want and I'll book it."


def apply_outcome(con, aid: int, outcome: str, reason: str, actor: str = "assess", force: bool = False) -> None:
    """Stage + James's message for passed / failed / review. Review = my own stage plus an escalation so the owner is alerted."""
    a = engine.applicant(con, aid)
    if not a:
        return
    if outcome == "passed":
        engine.set_stage(con, aid, "passed", actor=actor, reason=reason or "assessment passed", force=force)
        engine.deliver(con, aid, passed_message(a, _slots_text(con, aid)), kind="slots", actor=actor)
    elif outcome == "failed":
        engine.set_stage(con, aid, "failed", actor=actor, reason=reason or "assessment failed", force=force)
        engine.deliver(con, aid, failed_message(a, reason), kind="decline", actor=actor)
    elif outcome == "review":
        engine.set_stage(con, aid, REVIEW_STAGE, actor=actor, reason=reason or "assessment needs a decision", force=force)
        if not db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND reason='assessment review' AND status='open'", (aid,)):
            engine.create_escalation(con, aid, "assessment review", f"Decide on the staff scorecard: {staff_url(aid)}. {reason}")
        engine.deliver(con, aid, review_message(a), kind="assessment", actor=actor)


def decide(con, aid: int, outcome: str, who: str) -> None:
    if outcome not in ("passed", "failed"):
        raise HTTPException(400, "outcome must be passed or failed")
    a = get(con, aid)
    if not a:
        raise HTTPException(404)
    ts = db.now()
    con.execute("UPDATE assessment SET outcome=?, decided_by=?, decided_at=?, updated_at=? WHERE id=?", (outcome, who, ts, ts, a["id"]))
    con.execute("UPDATE escalation SET status='closed', answered_by=?, answered_at=? WHERE applicant_id=? AND reason='assessment review' AND status='open'", (who, ts, aid))
    apply_outcome(con, aid, outcome, f"decided by {who} after review" + (f": {a['outcome_reason']}" if a.get("outcome_reason") else ""), actor=who, force=True)


# ---------------------------------------------------------------- live scenarios

def _open_scenario(con, aid: int, assessment_id: int, sc: dict) -> None:
    spec = bank.LIVE_BY_KEY[sc["key"]]
    ts = db.now()
    con.execute("UPDATE assessment_scenario SET status='open', started_at=? WHERE id=?", (ts, sc["id"]))
    text, model = llm.fan_turn(con, aid, spec, [])
    con.execute("INSERT INTO assessment_turn (assessment_id, scenario_key, role, body, model, created_at) VALUES (?,?,?,?,?,?)",
                (assessment_id, sc["key"], "fan", text, model, db.now()))


def live_state(con, aid: int) -> dict:
    a = get(con, aid)
    if not a or a["status"] != "started" or a["section"] != "live":
        raise HTTPException(409, "the live chats are not open")
    th = thresholds(con)
    n = scenario_count(con, th)
    for i, spec in enumerate(bank.LIVE_SCENARIOS[:n]):
        con.execute("INSERT OR IGNORE INTO assessment_scenario (assessment_id, key, idx) VALUES (?,?,?)", (a["id"], spec["key"], i))
    scs = [s for s in scenarios(con, a["id"]) if s["idx"] < n and s["key"] in bank.LIVE_BY_KEY]
    current = next((s for s in scs if s["status"] != "done"), None)
    if current is None:
        return {"all_done": True, "total": n, "reply_seconds": int(th["reply_seconds"]), "scenario": None, "turns": []}
    if current["status"] == "pending":
        _open_scenario(con, aid, a["id"], current)
        current = next(s for s in scenarios(con, a["id"]) if s["key"] == current["key"])
    spec = bank.LIVE_BY_KEY[current["key"]]
    turns = [{"role": t["role"], "body": t["body"]} for t in current["turns"]]
    fan_n = sum(1 for t in turns if t["role"] == "fan")
    return {"all_done": False, "total": n, "reply_seconds": int(th["reply_seconds"]), "chunk_idle_seconds": int(th["chunk_idle_seconds"]),
            "scenario": {"key": current["key"], "title": spec["title"], "n": current["idx"] + 1, "fan_turns": spec["fan_turns"], "fan_so_far": fan_n},
            "turns": turns, "your_turn": bool(turns) and turns[-1]["role"] in ("fan", "applicant")}


def live_reply(con, aid: int, scenario_key: str, text: str, client_latency_ms: int | None = None, timed_out: bool = False,
               telemetry: dict | None = None, end_turn: bool = True, content: dict | None = None) -> dict:
    """One Send from the applicant. A reply to a fan message can be a BURST of several short messages: each call with end_turn=False stores one
    chunk and the fan stays quiet; the fan (or the judge, on the last fan turn) only responds when end_turn=True (the applicant paused for the idle
    gap or pressed the over-to-them control). The reply-time budget is per fan message, so the client runs one clock across the whole burst."""
    a = get(con, aid)
    if not a or a["status"] != "started" or a["section"] != "live":
        raise HTTPException(409, "the live chats are not open")
    if scenario_key not in bank.LIVE_BY_KEY:
        raise HTTPException(409, "that chat is no longer part of the assessment")
    sc = db.one(con, "SELECT * FROM assessment_scenario WHERE assessment_id=? AND key=?", (a["id"], scenario_key))
    if not sc or sc["status"] != "open":
        raise HTTPException(409, "that chat is not open")
    spec = bank.LIVE_BY_KEY[scenario_key]
    turns = db.q(con, "SELECT * FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], scenario_key))
    if not turns or turns[-1]["role"] not in ("fan", "applicant"):
        raise HTTPException(409, "it is not your turn")
    now = datetime.now(timezone.utc)
    last_fan = next((db.parse_ts(t["created_at"]) for t in reversed(turns) if t["role"] == "fan"), None) or now
    latency = int((now - last_fan).total_seconds() * 1000)
    is_content = bool(content and (content.get("name") or content.get("price") or content.get("caption")))
    if is_content:
        nm = str(content.get("name") or "content").strip()[:60]
        pr = str(content.get("price") or "").strip()[:20]
        media = str(content.get("media") or "").strip()[:120]
        cap = llm.clean_text(str(content.get("caption") or ""))[:400]
        kind = str(content.get("kind") or "").strip().lower()
        if kind == "tease" or not pr:
            tag = f"[sent {nm}: {media}]" if media else f"[sent {nm}]"
        else:
            tag = f"[sent {nm} — locked PPV {pr}: {media}]" if media else f"[sent {nm} — locked PPV {pr}]"
        body = (f"{cap} {tag}").strip()[:1000]
    else:
        body = llm.clean_text(text)[:1000]
    # every fan message must get at least one applicant row: store the chunk when there is text/content, on a time-out, or to close an empty burst
    new_burst = turns[-1]["role"] == "fan"
    if body or timed_out or (end_turn and new_burst):
        tel = parse_turn_telemetry(telemetry if isinstance(telemetry, dict) else {})
        if is_content:
            tel["content"] = True   # a click, not typing: keep it out of WPM and AI-risk
        if not tel.get("latency_ms"):
            tel["latency_ms"] = latency
        if not tel.get("length"):
            tel["length"] = len(body)
        con.execute("INSERT INTO assessment_turn (assessment_id, scenario_key, role, body, latency_ms, client_latency_ms, timed_out, telemetry, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (a["id"], scenario_key, "applicant", body, latency, client_latency_ms, 1 if timed_out else 0, json.dumps(tel), db.fmt_ts(now)))
    if not end_turn:
        return {"ok": True, "chunk": True, "done": False, "all_done": False}
    fan_n = sum(1 for t in turns if t["role"] == "fan")
    if fan_n < spec["fan_turns"]:
        all_turns = db.q(con, "SELECT role, body FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], scenario_key))
        fan_text, model = llm.fan_turn(con, aid, spec, all_turns)
        con.execute("INSERT INTO assessment_turn (assessment_id, scenario_key, role, body, model, created_at) VALUES (?,?,?,?,?,?)",
                    (a["id"], scenario_key, "fan", fan_text, model, db.now()))
        return {"ok": True, "fan": fan_text, "done": False, "all_done": False, "fan_so_far": fan_n + 1}
    all_turns = db.q(con, "SELECT role, body FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], scenario_key))
    verdict = llm.judge_scenario(con, aid, spec, all_turns)
    con.execute("UPDATE assessment_scenario SET status='done', finished_at=?, judge=?, score=? WHERE id=?",
                (db.now(), json.dumps(verdict), verdict.get("total"), sc["id"]))
    state = live_state(con, aid)
    return {"ok": True, "fan": None, "done": True, "all_done": state["all_done"], "next": state.get("scenario"), "turns": state.get("turns") or []}


# ---------------------------------------------------------------- page helpers

def _ago(ts: str | None) -> str:
    d = db.parse_ts(ts)
    if not d:
        return "-"
    s = int((datetime.now(timezone.utc) - d).total_seconds())
    pre = ""
    if s < 0:
        s, pre = -s, "in "
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= n:
            return f"{pre}{s // n}{unit}" + ("" if pre else " ago")
    return "now" if not pre else "in <1m"


def _nice(ts: str | None, tz: str = db.HOUSE_TZ) -> str:
    d = db.parse_ts(ts)
    return engine.fmt_local(d, tz) if d else ""


templates.env.filters["ago"] = _ago
templates.env.filters["nice"] = _nice
templates.env.filters["both"] = lambda ts, tz: engine.when_both(ts, tz)
templates.env.globals.update(APP_NAME="Beloura Hire", AGENT=texts.AGENT_NAME, STAGES=db.STAGES, PIPELINE=db.PIPELINE, SIDE=db.SIDE, STAGE_LABELS=db.STAGE_LABELS,
                             SHIFT_LABELS=db.SHIFT_LABELS, SECTIONS=SECTIONS, SECTION_LABELS=SECTION_LABELS, SECTION_MINUTES=SECTION_MINUTES,
                             CRITERIA=bank.JUDGE_CRITERIA, CRITERIA_LABELS=bank.JUDGE_CRITERIA_LABELS)


def _staff_ctx(request: Request, **extra) -> dict:
    with db.db() as con:
        esc_n = (db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'") or {}).get("n", 0)
        worker = db.one(con, "SELECT * FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1")
    return {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path, "query": dict(request.query_params),
            "esc_n": esc_n, "worker": worker, **extra}


def _staff_page(name: str, request: Request, status_code: int = 200, **extra):
    return templates.TemplateResponse(request, name, _staff_ctx(request, **extra), status_code=status_code)


def _who(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return (u or {}).get("user") or "staff"


def _is_admin(request: Request) -> bool:
    u = getattr(request.state, "user", None) or {}
    return u.get("role") == "admin"


def _gate(con, aid: int, tok: str) -> dict:
    if not check_token(aid, tok):
        raise HTTPException(404)
    a = engine.applicant(con, aid)
    if not a:
        raise HTTPException(404)
    return a


def _steps(a: dict | None, secs: dict) -> list[dict]:
    out = []
    for k in SECTIONS:
        r = secs.get(k) or {}
        state = "done" if r.get("finished_at") else ("current" if a and a.get("status") == "started" and a.get("section") == k else "todo")
        out.append({"key": k, "label": SECTION_LABELS[k], "minutes": SECTION_MINUTES[k], "state": state})
    return out


def _public(name: str, request: Request, a: dict, applicant: dict, status_code: int = 200, **extra):
    secs = extra.pop("secs", {})
    ctx = {"request": request, "a": a, "applicant": applicant, "first": _first(applicant), "steps": _steps(a, secs), "link": link_path(applicant["id"]), **extra}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]


TEST_CHANNELS = ("test", "collect")   # internal links: never lock
HIDE_RESULT_CHANNELS = ("collect",)   # data-collection runs: applicant sees no score/outcome, no pass-fail message


def hides_result(applicant) -> bool:
    return (applicant or {}).get("channel_code") in HIDE_RESULT_CHANNELS


MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPod|iPad|Tablet|Silk|Kindle|PlayBook|BB10|BlackBerry|Opera Mini|IEMobile|Windows Phone|webOS", re.I)


def is_laptop(ua: str) -> bool:
    """The chats need a real keyboard, so phones and tablets are refused. iPadOS reports a desktop UA; the client-side guard catches those."""
    return not MOBILE_RE.search(ua or "")


async def _json(request: Request) -> dict:
    try:
        v = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON body expected")
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------- public: connection probes (no third party)

@router.get("/assess/ping", response_class=PlainTextResponse)
def ping():
    return PlainTextResponse("pong", headers={"Cache-Control": "no-store"})


@router.get("/assess/blob")
def blob(bytes: int = 2_000_000):
    n = max(1, min(int(bytes), MAX_BLOB))
    return Response(content=os.urandom(n), media_type="application/octet-stream", headers={"Cache-Control": "no-store", "Content-Length": str(n)})


@router.post("/assess/upload")
async def upload(request: Request):
    t0 = datetime.now(timezone.utc)
    declared = _int(request.headers.get("content-length"), 0)
    if declared > MAX_UPLOAD:
        raise HTTPException(413, "too large")
    body = await request.body()
    if len(body) > MAX_UPLOAD:
        raise HTTPException(413, "too large")
    ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    return JSONResponse({"bytes": len(body), "server_ms": ms}, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- public: the assessment

@router.get("/assess/{aid}/{tok}", response_class=HTMLResponse)
def landing(aid: int, tok: str, request: Request):
    with db.db() as con:
        applicant = _gate(con, aid, tok)
        a = ensure(con, aid)
        secs = sections(con, a["id"])
        th = thresholds(con)
        if a["status"] == "done":
            if hides_result(applicant):
                return _public("assess_done_neutral.html", request, a, applicant, secs=secs)
            return _public("assess_result.html", request, a, applicant, secs=secs, summary=summary(con, aid), th=th)
        if a["status"] == "locked":
            return _public("assess_locked.html", request, a, applicant, secs=secs, status_code=423)
        if a["status"] == "created" and applicant["stage"] in db.CLOSED:
            return _public("assess_locked.html", request, a, applicant, secs=secs, closed=True, status_code=410)
        if not is_laptop(_ua(request)):
            return _public("assess_device.html", request, a, applicant, secs=secs)
    return _public("assess_intro.html", request, a, applicant, secs=secs, th=th, gate_device=True, hide_result=hides_result(applicant))


@router.post("/assess/{aid}/{tok}/start")
def start_route(aid: int, tok: str, request: Request):
    with db.db() as con:
        _gate(con, aid, tok)
        if not is_laptop(_ua(request)):
            return RedirectResponse(link_path(aid), status_code=303)
        try:
            a = start(con, aid, ua=_ua(request))
        except Locked:
            return RedirectResponse(link_path(aid), status_code=303)
    if a["status"] == "done":
        return RedirectResponse(link_path(aid), status_code=303)
    return RedirectResponse(f"{link_path(aid)}/s/{a['section']}", status_code=303)


@router.get("/assess/{aid}/{tok}/s/{key}", response_class=HTMLResponse)
def section_page(aid: int, tok: str, key: str, request: Request):
    if key not in SECTIONS:
        raise HTTPException(404)
    with db.db() as con:
        applicant = _gate(con, aid, tok)
        if not is_laptop(_ua(request)):
            return RedirectResponse(link_path(aid), status_code=303)
        try:
            state = enter_section(con, aid, key, ua=_ua(request))
        except Locked:
            return RedirectResponse(link_path(aid), status_code=303)
        if state != "ok":
            return RedirectResponse(link_path(aid), status_code=303)
        a = get(con, aid)
        secs = sections(con, a["id"])
        th = thresholds(con)
    base = link_path(aid)
    cfg = {"section": key, "submit_url": f"{base}/s/{key}", "home": base}
    if key == "live":
        cfg.update({"state_url": f"{base}/live/state", "reply_url": f"{base}/live/reply", "probe_url": f"{base}/probe",
                    "reply_seconds": int(th["reply_seconds"]), "chunk_idle_seconds": int(th["chunk_idle_seconds"]), "total": scenario_count(con, th),
                    "ping_url": "/assess/ping", "blob_url": "/assess/blob", "upload_url": "/assess/upload",
                    "ping_n": 5, "down_n": 3, "down_bytes": 2_000_000, "up_n": 3, "up_bytes": 1_000_000,
                    "creator": bank.CREATOR, "script": bank.SCRIPT})
    return _public(f"assess_{key}.html", request, a, applicant, secs=secs, cfg=cfg, th=th, gate_device=True)


@router.post("/assess/{aid}/{tok}/s/{key}")
async def section_submit(aid: int, tok: str, key: str, request: Request):
    if key not in SECTIONS:
        raise HTTPException(404)
    payload = await _json(request)
    with db.db() as con:
        _gate(con, aid, tok)
        out = submit_section(con, aid, key, payload, ua=_ua(request))
    return JSONResponse(out)


@router.post("/assess/{aid}/{tok}/probe")
async def probe_route(aid: int, tok: str, request: Request):
    payload = await _json(request)
    with db.db() as con:
        _gate(con, aid, tok)
        out = store_probe(con, aid, payload)
    return JSONResponse(out)


@router.get("/assess/{aid}/{tok}/live/state")
def live_state_route(aid: int, tok: str, request: Request):
    with db.db() as con:
        _gate(con, aid, tok)
        return JSONResponse(live_state(con, aid))


@router.post("/assess/{aid}/{tok}/live/reply")
async def live_reply_route(aid: int, tok: str, request: Request):
    payload = await _json(request)
    with db.db() as con:
        _gate(con, aid, tok)
        out = live_reply(con, aid, str(payload.get("scenario") or ""), str(payload.get("text") or ""),
                         client_latency_ms=_int(payload.get("latency_ms"), 0) or None, timed_out=bool(payload.get("timed_out")),
                         telemetry=payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else None,
                         end_turn=bool(payload.get("end_turn", True)),
                         content=payload.get("content") if isinstance(payload.get("content"), dict) else None)
    return JSONResponse(out)


# ---------------------------------------------------------------- staff

@router.get("/staff/assess", response_class=HTMLResponse)
def staff_list(request: Request):
    with db.db() as con:
        rows = db.q(con, """SELECT s.*, a.name, a.stage, a.timezone,
                            (SELECT COALESCE(SUM(cost_usd),0) FROM llm_call l WHERE l.applicant_id=s.applicant_id AND l.prompt_version LIKE 'assess-%') AS usd
                            FROM assessment s JOIN applicant a ON a.id=s.applicant_id ORDER BY s.updated_at DESC LIMIT 500""")
        for r in rows:
            r["scores"] = db.jl(r.get("scores"), {})
        th = thresholds(con)
    return _staff_page("assess_staff_list.html", request, rows=rows, th=th)


@router.get("/staff/assess/settings", response_class=HTMLResponse)
def staff_settings(request: Request):
    if not _is_admin(request):
        return _staff_page("forbidden.html", request, status_code=403, who={"name": (getattr(request.state, "user", None) or {}).get("name", "")})
    with db.db() as con:
        s = {k: db.setting(con, k, d) for k, d in {**THRESHOLD_KEYS, **OTHER_KEYS}.items()}
    return _staff_page("assess_settings.html", request, s=s, labels=THRESHOLD_LABELS, other_sources=OTHER_SOURCES)


@router.post("/staff/assess/settings")
async def staff_settings_save(request: Request):
    if not _is_admin(request):
        raise HTTPException(403)
    form = dict(await request.form())
    with db.db() as con:
        for k in {**THRESHOLD_KEYS, **OTHER_KEYS}:
            if k in form:
                try:
                    v = float(str(form[k]).strip())
                except ValueError:
                    continue
                db.set_setting(con, k, f"{v:g}")
    return RedirectResponse("/staff/assess/settings", status_code=303)


@router.get("/staff/assess/{aid}", response_class=HTMLResponse)
def staff_card(aid: int, request: Request):
    with db.db() as con:
        applicant = engine.applicant(con, aid)
        if not applicant:
            raise HTTPException(404, "no such applicant")
        a = get(con, aid)
        secs = sections(con, a["id"]) if a else {}
        scs = scenarios(con, a["id"]) if a else []
        th = thresholds(con)
        c = cost(con, aid)
        link = public_url(link_path(aid))
        d = (secs.get("live") or {}).get("data") or {}
        msgs = db.q(con, "SELECT * FROM message WHERE applicant_id=? AND kind IN ('assessment','slots','decline') ORDER BY id DESC LIMIT 10", (aid,))
        calls = db.q(con, "SELECT * FROM llm_call WHERE applicant_id=? AND prompt_version LIKE 'assess-%' ORDER BY id DESC LIMIT 100", (aid,))
    return _staff_page("assess_staff.html", request, applicant=applicant, a=a, secs=secs, scs=scs, th=th, cost=c, link=link, msgs=msgs, calls=calls,
                       d=d, steps=_steps(a, secs), detail=section_detail, other_sources=OTHER_SOURCES)


@router.post("/staff/assess/{aid}/send")
def staff_send(aid: int, request: Request):
    with db.db() as con:
        if not engine.applicant(con, aid):
            raise HTTPException(404)
        on_stage(con, aid, resend=True)
    return RedirectResponse(f"/staff/assess/{aid}", status_code=303)


@router.post("/staff/assess/{aid}/reopen")
def staff_reopen(aid: int, request: Request):
    with db.db() as con:
        reopen(con, aid, who=_who(request))
    return RedirectResponse(f"/staff/assess/{aid}", status_code=303)


@router.post("/staff/assess/{aid}/decide")
async def staff_decide(aid: int, request: Request):
    form = dict(await request.form())
    with db.db() as con:
        decide(con, aid, str(form.get("outcome") or ""), _who(request))
    return RedirectResponse(f"/staff/assess/{aid}", status_code=303)


# ---------------------------------------------------------------- team grading & judge calibration
# A few named people on the team grade the saved test chats so we can calibrate the AI judge to their scores.
# Everyone grades the same chats; no one grades their own run. Weighted influence: Connor > Mae > Jam > Gabby.
GRADERS = [("connor", "Connor", 4), ("mae", "Mae", 3), ("jam", "Jam", 2), ("gabby", "Gabby", 1)]
GRADER_BY_KEY = {k: (label, w) for k, label, w in GRADERS}


def grade_token(grader: str) -> str:
    return hmac.new(os.environ.get("HIRE_SECRET", "dev-secret").encode(), f"grade:{grader}".encode(), hashlib.sha256).hexdigest()[:32]


def check_grade_token(grader: str, tok: str) -> bool:
    return grader in GRADER_BY_KEY and bool(tok) and hmac.compare_digest(grade_token(grader), (tok or "").strip())


def grade_link(grader: str) -> str:
    return public_url(f"/grade/{grader}/{grade_token(grader)}")


def _own_applicant_id(con, grader: str) -> int | None:
    """A grader never grades their own run: match the grader's name to an applicant's first name."""
    label = GRADER_BY_KEY.get(grader, (grader, 0))[0]
    row = db.one(con, "SELECT id FROM applicant WHERE lower(name)=? OR lower(name) LIKE ?", (label.lower(), label.lower() + " %"))
    return row["id"] if row else None


def gradable_chats(con, grader: str) -> list[dict]:
    """Every finished chat (one per scenario of an assessment that has reached the live section), minus the grader's own run."""
    own = _own_applicant_id(con, grader)
    rows = db.q(con, """SELECT s.applicant_id AS aid, a.name, sc.key, sc.idx, sc.status, sc.score
                        FROM assessment_scenario sc JOIN assessment s ON s.id=sc.assessment_id JOIN applicant a ON a.id=s.applicant_id
                        WHERE sc.status='done' ORDER BY s.applicant_id DESC, sc.idx""")
    out = []
    for r in rows:
        if own and r["aid"] == own:
            continue
        g = db.one(con, "SELECT overall FROM assess_human_grade WHERE assessment_id=(SELECT id FROM assessment WHERE applicant_id=?) AND scenario_key=? AND grader=?",
                   (r["aid"], r["key"], grader))
        n = db.one(con, "SELECT COUNT(*) c FROM assess_human_grade WHERE assessment_id=(SELECT id FROM assessment WHERE applicant_id=?) AND scenario_key=?", (r["aid"], r["key"]))
        r["title"] = bank.LIVE_BY_KEY.get(r["key"], {}).get("title", r["key"])
        r["mine"] = g["overall"] if g else None
        r["graders_done"] = (n or {}).get("c", 0)
        out.append(r)
    return out


def chat_turns(con, aid: int, key: str) -> list[dict]:
    a = get(con, aid)
    if not a:
        return []
    return db.q(con, "SELECT role, body FROM assessment_turn WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], key))


def save_grade(con, aid: int, key: str, grader: str, overall, scores: dict, note: str) -> None:
    a = get(con, aid)
    if not a:
        raise HTTPException(404)
    ts = db.now()
    con.execute("""INSERT INTO assess_human_grade (assessment_id, scenario_key, grader, overall, scores, note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(assessment_id, scenario_key, grader) DO UPDATE SET overall=excluded.overall, scores=excluded.scores, note=excluded.note, updated_at=excluded.updated_at""",
                (a["id"], key, grader, overall, json.dumps(scores or {}), (note or "")[:1000], ts, ts))


def grade_of(con, aid: int, key: str, grader: str) -> dict | None:
    a = get(con, aid)
    row = db.one(con, "SELECT * FROM assess_human_grade WHERE assessment_id=? AND scenario_key=? AND grader=?", (a["id"], key, grader)) if a else None
    if row:
        row["scores"] = db.jl(row.get("scores"), {})
    return row


def chat_grades(con, aid: int, key: str) -> list[dict]:
    a = get(con, aid)
    rows = db.q(con, "SELECT * FROM assess_human_grade WHERE assessment_id=? AND scenario_key=? ORDER BY id", (a["id"], key)) if a else []
    for r in rows:
        r["scores"] = db.jl(r.get("scores"), {})
        r["weight"] = GRADER_BY_KEY.get(r["grader"], (r["grader"], 1))[1]
        r["label"] = GRADER_BY_KEY.get(r["grader"], (r["grader"], 1))[0]
    return rows


def weighted_human(grades: list[dict]):
    pairs = [(g["overall"], g["weight"]) for g in grades if g.get("overall") is not None]
    if not pairs:
        return None
    return round(sum(o * w for o, w in pairs) / sum(w for _, w in pairs), 2)


# ---- grading routes: a tokened link per grader (no SSO onboarding needed), compare view under /staff

@router.get("/grade/{grader}/{tok}", response_class=HTMLResponse)
def grade_hub(grader: str, tok: str, request: Request):
    if not check_grade_token(grader, tok):
        raise HTTPException(404)
    with db.db() as con:
        chats = gradable_chats(con, grader)
    label = GRADER_BY_KEY[grader][0]
    return templates.TemplateResponse(request, "assess_grade_hub.html",
                                      {"request": request, "grader": grader, "label": label, "tok": tok, "chats": chats, "total_graders": len(GRADERS),
                                       "first": label, "applicant": {}, "link": f"/grade/{grader}/{tok}"})


@router.get("/grade/{grader}/{tok}/{aid}/{key}", response_class=HTMLResponse)
def grade_one(grader: str, tok: str, aid: int, key: str, request: Request):
    if not check_grade_token(grader, tok):
        raise HTTPException(404)
    with db.db() as con:
        own = _own_applicant_id(con, grader)
        if own and aid == own:
            raise HTTPException(403, "you cannot grade your own chat")
        turns = chat_turns(con, aid, key)
        if not turns:
            raise HTTPException(404)
        existing = grade_of(con, aid, key, grader)
        applicant = engine.applicant(con, aid)
    spec = bank.LIVE_BY_KEY.get(key, {})
    return templates.TemplateResponse(request, "assess_grade_one.html",
                                      {"request": request, "grader": grader, "label": GRADER_BY_KEY[grader][0], "tok": tok,
                                       "aid": aid, "key": key, "name": (applicant or {}).get("name", ""), "title": spec.get("title", key),
                                       "good": spec.get("good", ""), "turns": turns, "criteria": bank.JUDGE_CRITERIA,
                                       "criteria_labels": bank.JUDGE_CRITERIA_LABELS, "existing": existing,
                                       "first": GRADER_BY_KEY[grader][0], "applicant": applicant or {}, "link": f"/grade/{grader}/{tok}"})


@router.post("/grade/{grader}/{tok}/{aid}/{key}")
async def grade_save(grader: str, tok: str, aid: int, key: str, request: Request):
    if not check_grade_token(grader, tok):
        raise HTTPException(404)
    form = dict(await request.form())
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    scores = {c: num(form.get(c)) for c in bank.JUDGE_CRITERIA if form.get(c) not in (None, "")}
    overall = num(form.get("overall"))
    with db.db() as con:
        own = _own_applicant_id(con, grader)
        if own and aid == own:
            raise HTTPException(403)
        save_grade(con, aid, key, grader, overall, scores, str(form.get("note") or ""))
    return RedirectResponse(f"/grade/{grader}/{tok}", status_code=303)


def _compare_rows(con) -> list[dict]:
    rows = db.q(con, """SELECT DISTINCT s.applicant_id AS aid, a.name, sc.key, sc.idx, sc.score AS ai
                        FROM assessment_scenario sc JOIN assessment s ON s.id=sc.assessment_id JOIN applicant a ON a.id=s.applicant_id
                        WHERE sc.status='done' ORDER BY s.applicant_id DESC, sc.idx""")
    out = []
    for r in rows:
        grades = chat_grades(con, r["aid"], r["key"])
        r["title"] = bank.LIVE_BY_KEY.get(r["key"], {}).get("title", r["key"])
        r["grades"] = grades
        r["human"] = weighted_human(grades)
        r["ai10"] = r["ai"]
        r["human10"] = round(r["human"] * 2, 1) if r["human"] is not None else None   # 1-5 human -> 0-10 to compare with the judge
        r["gap"] = round(r["ai10"] - r["human10"], 1) if (r["ai10"] is not None and r["human10"] is not None) else None
        out.append(r)
    return out


@router.get("/staff/grade", response_class=HTMLResponse)
def grade_compare(request: Request):
    with db.db() as con:
        out = _compare_rows(con)
        links = [(label, grade_link(k)) for k, label, w in GRADERS]
    return _staff_page("assess_grade_compare.html", request, rows=out, graders=GRADERS, links=links)


@router.get("/grade/{grader}/{tok}/compare", response_class=HTMLResponse)
def grade_compare_grader(grader: str, tok: str, request: Request):
    if not check_grade_token(grader, tok):
        raise HTTPException(404)
    with db.db() as con:
        out = _compare_rows(con)
    label = GRADER_BY_KEY[grader][0]
    return templates.TemplateResponse(request, "assess_grade_compare_pub.html",
                                      {"request": request, "rows": out, "graders": GRADERS, "grader": grader, "tok": tok,
                                       "label": label, "first": label, "applicant": {}, "link": f"/grade/{grader}/{tok}"})
