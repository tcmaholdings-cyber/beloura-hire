"""SQLite store for beloura-hire: applicants, applications, messages, stages, availability, interviews, knowledge, LLM calls, escalations."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("HIRE_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hire.db"))

STAGES = ["applied", "screened_in", "screened_out", "interview_passed", "interview_failed", "assessment_sent", "assessment_in_progress",
          "assessment_done", "passed", "failed", "final_scheduled", "final_done", "offer_sent", "hired", "declined", "ghosted", "rejected", "talent_pool"]
PIPELINE = ["applied", "screened_in", "interview_passed", "assessment_sent", "assessment_in_progress", "assessment_done", "passed",
            "final_scheduled", "final_done", "offer_sent", "hired"]
SIDE = ["screened_out", "interview_failed", "failed", "declined", "ghosted", "rejected", "talent_pool"]
CLOSED = {"screened_out", "interview_failed", "failed", "declined", "rejected", "hired", "talent_pool"}
STAGE_LABELS = {"applied": "Applied", "screened_in": "Screened in", "screened_out": "Screened out", "interview_passed": "Interview passed",
                "interview_failed": "Interview failed", "assessment_sent": "Assessment sent",
                "assessment_in_progress": "Assessment in progress", "assessment_done": "Assessment done", "passed": "Passed",
                "failed": "Failed", "final_scheduled": "Final scheduled", "final_done": "Final done", "offer_sent": "Offer sent",
                "hired": "Hired", "declined": "Declined", "ghosted": "Ghosted", "rejected": "Rejected", "talent_pool": "Talent pool"}
CHANNELS = ["telegram", "discord", "email"]
SHIFTS = [("uk_00_08", 0, 8), ("uk_08_16", 8, 16), ("uk_16_00", 16, 24)]
SHIFT_LABELS = {"uk_00_08": "UK 00:00-08:00", "uk_08_16": "UK 08:00-16:00", "uk_16_00": "UK 16:00-00:00"}
DEVICES = ["laptop", "desktop", "tablet", "phone"]
CONNECTIONS = ["fibre", "cable_dsl", "mobile_4g_5g", "satellite", "other"]
HEARD_FROM = ["onlinejobs", "ofmjobs", "facebook", "reddit", "friend", "other"]
HOUSE_TZ = "Europe/London"
DEFAULT_APPLICANT_TZ = "Asia/Manila"

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicant (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  email TEXT, telegram TEXT, telegram_chat_id TEXT, discord TEXT, discord_user_id TEXT, phone TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Manila',
  channel_code TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'applied', stage_changed_at TEXT NOT NULL,
  score INTEGER, screen_reasons TEXT NOT NULL DEFAULT '[]',
  last_channel TEXT NOT NULL DEFAULT '',
  awaiting_since TEXT, nudges_sent INTEGER NOT NULL DEFAULT 0, last_nudge_at TEXT,
  last_inbound_at TEXT, last_outbound_at TEXT,
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}', notes TEXT NOT NULL DEFAULT '',
  discord_channel_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_applicant_email ON applicant (lower(email)) WHERE email IS NOT NULL AND email <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_applicant_telegram ON applicant (lower(telegram)) WHERE telegram IS NOT NULL AND telegram <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_applicant_discord ON applicant (lower(discord)) WHERE discord IS NOT NULL AND discord <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_applicant_phone ON applicant (phone) WHERE phone IS NOT NULL AND phone <> '';
CREATE INDEX IF NOT EXISTS ix_applicant_telegram_chat ON applicant (telegram_chat_id);
CREATE INDEX IF NOT EXISTS ix_applicant_discord_user ON applicant (discord_user_id);
CREATE INDEX IF NOT EXISTS ix_applicant_stage ON applicant (stage, stage_changed_at);

CREATE TABLE IF NOT EXISTS application (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, channel_code TEXT NOT NULL DEFAULT '',
  age_18 INTEGER NOT NULL DEFAULT 0, device TEXT NOT NULL DEFAULT '', connection_type TEXT NOT NULL DEFAULT '',
  willing_to_train INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT '',
  available_shifts TEXT NOT NULL DEFAULT '[]', hours_per_week INTEGER NOT NULL DEFAULT 0,
  english_self_rating INTEGER NOT NULL DEFAULT 0, experience TEXT NOT NULL DEFAULT '', experience_text TEXT NOT NULL DEFAULT '',
  why TEXT NOT NULL DEFAULT '', heard_from TEXT NOT NULL DEFAULT '', consent INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  interview_state TEXT NOT NULL DEFAULT '', interview_answers TEXT NOT NULL DEFAULT '{}', interview_grade TEXT NOT NULL DEFAULT '{}',
  interview_channel TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_application_applicant ON application (applicant_id, created_at);

CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, channel TEXT NOT NULL, direction TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'sam', body TEXT NOT NULL DEFAULT '', external_id TEXT, address TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'sent', provider_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'sam', created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_external ON message (channel, external_id) WHERE external_id IS NOT NULL AND external_id <> '';
CREATE INDEX IF NOT EXISTS ix_message_applicant ON message (applicant_id, created_at);

CREATE TABLE IF NOT EXISTS stage_event (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, from_stage TEXT NOT NULL, to_stage TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT 'system', at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stage_event_applicant ON stage_event (applicant_id, at);

CREATE TABLE IF NOT EXISTS manager (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, discord TEXT NOT NULL DEFAULT '', discord_user_id TEXT NOT NULL DEFAULT '',
  telegram TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', timezone TEXT NOT NULL DEFAULT 'Asia/Manila',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability (
  id INTEGER PRIMARY KEY, manager_id INTEGER NOT NULL, weekday INTEGER NOT NULL,
  start TEXT NOT NULL, end TEXT NOT NULL, tz TEXT NOT NULL DEFAULT 'Asia/Manila'
);
CREATE INDEX IF NOT EXISTS ix_availability_manager ON availability (manager_id, weekday);

CREATE TABLE IF NOT EXISTS interview (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, manager_id INTEGER NOT NULL,
  start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked',
  reminded_24h_at TEXT, reminded_1h_at TEXT, room TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL DEFAULT '', verdict_by TEXT NOT NULL DEFAULT '',
  discord_thread_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_interview_applicant ON interview (applicant_id, start_at);
CREATE INDEX IF NOT EXISTS ix_interview_manager ON interview (manager_id, status, start_at);

CREATE TABLE IF NOT EXISTS knowledge_item (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL DEFAULT '', question TEXT NOT NULL, answer TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, updated_by TEXT NOT NULL DEFAULT 'seed', updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_key ON knowledge_item (key) WHERE key <> '';

CREATE TABLE IF NOT EXISTS llm_call (
  id INTEGER PRIMARY KEY, applicant_id INTEGER, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '', latency_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_call_applicant ON llm_call (applicant_id, created_at);

CREATE TABLE IF NOT EXISTS escalation (
  id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, reason TEXT NOT NULL, question TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', answer TEXT NOT NULL DEFAULT '', answered_by TEXT NOT NULL DEFAULT '',
  discord_thread_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, answered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_escalation_status ON escalation (status, created_at);

CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'manager', last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS worker_runs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, ok INTEGER, notes TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "vacancies_uk_00_08": "2", "vacancies_uk_08_16": "2", "vacancies_uk_16_00": "2",
    "ghost_nudges": "3", "ghost_days": "5",
    "nudge_stages": "screened_in,assessment_sent,assessment_in_progress,passed",
    "book_from_screened_in": "0",
    "slot_minutes": "30", "slot_days": "7", "slot_lead_hours": "2",
    "score_fast_track": "80", "score_low": "40",
    "staff_discord_channel_id": "",
    "interview_enabled": "1",
    "discord_invite_url": "",
}
DISCORD_SETTING_KEYS = ["discord_role_manager", "discord_role_applicant", "discord_role_trainee", "discord_cat_staff", "discord_cat_applicants",
                        "discord_cat_finals", "discord_ch_hiring_queue", "discord_ch_escalations", "discord_ch_results", "discord_ch_weekly_report",
                        "discord_ch_staff_chat", "discord_ch_welcome", "discord_ch_lounge", "discord_forum_finals", "discord_voice_mock",
                        "discord_welcome_msg_id", "discord_invite_url"]

SEED_KNOWLEDGE = [
    ("pay", "What is the pay?",
     "The pay is $2 per hour base plus 2% commission on the sales you make during your shifts."),
    ("payout", "When and how am I paid?",
     "Weekly, every Thursday, by PayPal or Wise. Your first payout lands one to two Thursdays after you start."),
    ("training", "Is training paid, and how long is it?",
     "Training is paid retroactively if you pass it. There are two tracks. With OnlyFans chatting experience: 1 to 2 days of self-study, 7 written "
     "questions and 2 to 3 days of shadow shifts. Without experience: 2 to 4 days of self-study, a scenario test and 1 to 2 days of shadow shifts, "
     "with a completion bonus. Your track and the exact dates are confirmed at offer."),
    ("shifts", "What are the shifts?",
     "Shifts run in UK time: 00:00-08:00, 08:00-16:00 and 16:00-00:00. In Philippine time that is UK plus 8 hours in winter and UK plus 7 "
     "hours during British summer time (late March to late October). For example UK 08:00-16:00 is 16:00-00:00 Philippine time in winter "
     "and 15:00-23:00 in summer."),
    ("shifts_per_week", "How many shifts a week?",
     "3 to 6 shifts a week, depending on your availability and the open slots."),
    ("contract", "What kind of contract is it?",
     "You work as an independent contractor. Either side can end it with 7 days notice."),
    ("tools", "What tools do you use?",
     "Infloww for the chatting and Discord for the team."),
    ("notice", "What is the notice period?",
     "7 days notice either way."),
    ("process", "What is the hiring process and how long does each stage take?",
     "Five steps. 1) Application and screening: automatic, done within a minute. 2) A short text interview with me, five questions, about ten "
     "minutes, right after screening. 3) Assessment: about 45 minutes in one sitting, covering connection speed, typing, English and six short "
     "simulated fan chats. 4) Final interview: a 30-minute mock chat with one of our chat managers, usually within a week of passing the "
     "assessment. 5) Decision and offer: you hear back within two working days of the final. People review every decision."),
    ("role", "What does an OnlyFans chatter do?",
     "You chat with fans on a creator's OnlyFans account, writing as the creator: you build rapport, answer messages and sell paid content and "
     "tips, following the creator's persona and our guidelines. It is shift work on a laptop or desktop with a stable connection."),
    ("start_date", "When would I start?",
     "Start dates are confirmed at offer."),
    ("requirements", "What do I need to apply?",
     "You must be 18 or over, have a laptop or desktop with a stable connection, good written English, and be willing to complete the "
     "training. At least one of your available shifts has to match an open slot."),
    ("assessment_tests", "What are the tests in the assessment?",
     "A connection test (three runs, about a minute), a 3-minute typing test, an English test of 25 short items, and six simulated fan chats of "
     "3 to 4 messages each. About 45 minutes in total, in one sitting, resumable once."),
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def fmt_ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=8000")
    return con


@contextmanager
def db(path: str | None = None):
    con = connect(path)
    try:
        yield con
    finally:
        con.close()


def init(path: str | None = None) -> None:
    with db(path) as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(application)")}
        for col, typ in (("interview_state", "TEXT NOT NULL DEFAULT ''"), ("interview_answers", "TEXT NOT NULL DEFAULT '{}'"),
                         ("interview_grade", "TEXT NOT NULL DEFAULT '{}'"), ("interview_channel", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                con.execute(f"ALTER TABLE application ADD COLUMN {col} {typ}")
        if "discord_channel_id" not in {r["name"] for r in con.execute("PRAGMA table_info(applicant)")}:
            con.execute("ALTER TABLE applicant ADD COLUMN discord_channel_id TEXT")
        icols = {r["name"] for r in con.execute("PRAGMA table_info(interview)")}
        for col in ("verdict", "verdict_by", "discord_thread_id"):
            if col not in icols:
                con.execute(f"ALTER TABLE interview ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "discord_thread_id" not in {r["name"] for r in con.execute("PRAGMA table_info(escalation)")}:
            con.execute("ALTER TABLE escalation ADD COLUMN discord_thread_id TEXT NOT NULL DEFAULT ''")
        seed(con)
        __import__("assess").ensure_schema(con)
        __import__("olj").ensure_schema(con)
        __import__("posts").ensure_schema(con)


def q(con, sql, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args)]


def one(con, sql, args=()) -> dict | None:
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def jl(s: str | None, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (TypeError, ValueError):
        return default if default is not None else []


def setting(con, key: str, default: str = "") -> str:
    r = one(con, "SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(con, key: str, value: str) -> None:
    con.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def setting_int(con, key: str, default: int) -> int:
    try:
        return int(setting(con, key, str(default)) or default)
    except ValueError:
        return default


def vacancies(con) -> dict:
    return {s: setting_int(con, f"vacancies_{s}", 0) for s, _, _ in SHIFTS}


def seed(con) -> None:
    ts = now()
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))
    for key, question, answer in SEED_KNOWLEDGE:
        con.execute("INSERT OR IGNORE INTO knowledge_item (key, question, answer, active, updated_by, updated_at) VALUES (?,?,?,1,'seed',?)",
                    (key, question, answer, ts))
        con.execute("UPDATE knowledge_item SET answer=?, updated_at=? WHERE key=? AND updated_by='seed' AND answer<>?", (answer, ts, key, answer))
    for k in DISCORD_SETTING_KEYS:
        con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, '')", (k,))
    if not one(con, "SELECT id FROM manager LIMIT 1"):
        con.execute("INSERT INTO manager (name, discord, timezone, active, created_at) VALUES (?,?,?,1,?)", ("Ian", "manager1", "Asia/Manila", ts))
