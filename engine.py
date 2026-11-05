"""Every state change for beloura-hire goes through here so the form, the bots, the staff pages and the worker agree.

  upsert_applicant(con, data)          -> (id, created); dedupe on email / telegram / discord / phone
  save_application(con, aid, data)     -> application id
  screen(con, aid)                     -> (passed, reasons, score); stage screened_in / screened_out, decline sent on fail
  set_stage(con, aid, stage, ...)      -> forward-only unless force=True; writes stage_event
  record_inbound / deliver             -> message rows; outbound goes through SENDER (installed by app.py, dry-run by default)
  slots / book / reschedule / cancel   -> interviews against the manager's weekly grid, shown in the applicant's timezone
  tick(con, now)                       -> reminders (24 h, 1 h), nudges, ghost timeout, held messages
  create_escalation / answer_escalation
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
import texts

log = logging.getLogger("hire.engine")

PIPELINE_ORDER = {s: i for i, s in enumerate(db.PIPELINE)}
BOOKABLE = ("passed", "final_scheduled")
AWAIT_KINDS = ("first", "sam", "answer", "slots", "interview")
OPEN_INTERVIEW = ("booked", "rescheduled")
SENDER = None     # app.py installs channels.send_to(applicant, channel, text) -> provider id; tests install a recorder
ALERT = None      # staff alert hook (text, applicant_id) -> None
ON_BOOKED = None  # hook (applicant_id, interview row) after a final is booked or moved; channels opens the Discord room and the finals thread


class SlotTaken(Exception):
    pass


class NotAvailable(Exception):
    pass


# ---------------------------------------------------------------- identity and dedupe

def norm_email(v) -> str | None:
    s = str(v or "").strip().lower()
    return s if "@" in s else None


def norm_handle(v) -> str | None:
    s = str(v or "").strip().lstrip("@").strip()
    if "/" in s:
        parts = [p for p in s.split("/") if p]
        s = parts[-1] if parts else s
    s = s.split("#")[0].strip()
    return s.lower() or None


def norm_phone(v) -> str | None:
    s = re.sub(r"[^\d+]", "", str(v or ""))
    if not s:
        return None
    if not s.startswith("+"):
        s = "+" + s.lstrip("0") if len(s) > 9 else s
    digits = re.sub(r"\D", "", s)
    return ("+" + digits) if len(digits) >= 7 else None


def valid_tz(tz: str | None) -> str | None:
    if not tz:
        return None
    try:
        ZoneInfo(tz)
        return tz
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _clean(data: dict) -> dict:
    out = {}
    if "name" in data:
        out["name"] = str(data.get("name") or "").strip()[:120]
    if "email" in data:
        out["email"] = norm_email(data.get("email"))
    for k in ("telegram", "discord"):
        if k in data:
            out[k] = norm_handle(data.get(k))
    if "phone" in data:
        out["phone"] = norm_phone(data.get("phone"))
    if "timezone" in data:
        out["timezone"] = valid_tz(data.get("timezone")) or db.DEFAULT_APPLICANT_TZ
    if "channel_code" in data:
        out["channel_code"] = re.sub(r"[^a-z0-9_-]", "", str(data.get("channel_code") or "").lower())[:40]
    return out


def find_applicant(con, data: dict) -> dict | None:
    d = _clean(data)
    if data.get("applicant_id"):
        a = db.one(con, "SELECT * FROM applicant WHERE id=?", (int(data["applicant_id"]),))
        if a:
            return a
    for col, key in (("lower(email)", "email"), ("lower(telegram)", "telegram"), ("lower(discord)", "discord"), ("phone", "phone")):
        if d.get(key):
            a = db.one(con, f"SELECT * FROM applicant WHERE {col}=?", (d[key],))
            if a:
                return a
    return None


def upsert_applicant(con, data: dict, actor: str = "system") -> tuple[int, bool]:
    """Create or update. Existing non-empty values are kept; gaps are filled. Returns (id, created)."""
    ts = db.now()
    d = _clean(data)
    existing = find_applicant(con, data)
    if existing:
        sets, args = [], []
        for k, v in d.items():
            if v in (None, "") or k == "channel_code":
                continue
            if existing.get(k) in (None, "") or (k == "timezone" and existing.get(k) == db.DEFAULT_APPLICANT_TZ and v != existing.get(k)):
                sets.append(f"{k}=?")
                args.append(v)
        if sets:
            try:
                con.execute(f"UPDATE applicant SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, ts, existing["id"]))
            except Exception as e:  # noqa: BLE001
                log.warning("applicant %s update skipped: %s", existing["id"], e)
        return existing["id"], False
    cur = con.execute("""INSERT INTO applicant (name, email, telegram, discord, phone, timezone, channel_code, stage, stage_changed_at, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,'applied',?,?,?)""",
                      (d.get("name", ""), d.get("email"), d.get("telegram"), d.get("discord"), d.get("phone"),
                       d.get("timezone", db.DEFAULT_APPLICANT_TZ), d.get("channel_code", ""), ts, ts, ts))
    aid = cur.lastrowid
    con.execute("INSERT INTO stage_event (applicant_id, from_stage, to_stage, reason, actor, at) VALUES (?,?,?,?,?,?)",
                (aid, "", "applied", "application received", actor, ts))
    return aid, True


def applicant(con, aid: int) -> dict | None:
    return db.one(con, "SELECT * FROM applicant WHERE id=?", (aid,))


def meta_get(a: dict, key: str, default=None):
    return db.jl(a.get("meta"), {}).get(key, default)


def meta_set(con, aid: int, key: str, value) -> None:
    a = applicant(con, aid)
    m = db.jl(a.get("meta"), {}) if a else {}
    m[key] = value
    con.execute("UPDATE applicant SET meta=?, updated_at=? WHERE id=?", (json.dumps(m), db.now(), aid))


# ---------------------------------------------------------------- application, screening, score

def save_application(con, aid: int, data: dict) -> int:
    shifts = [s for s in (data.get("available_shifts") or []) if s in db.SHIFT_LABELS]
    cur = con.execute("""INSERT INTO application (applicant_id, channel_code, age_18, device, connection_type, willing_to_train, timezone,
                         available_shifts, hours_per_week, english_self_rating, experience, experience_text, why, heard_from, consent, raw, created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (aid, _clean({"channel_code": data.get("channel_code")}).get("channel_code", ""),
                       1 if _truthy(data.get("age_18")) else 0, str(data.get("device") or "").lower()[:20],
                       str(data.get("connection_type") or "").lower()[:20], 1 if _truthy(data.get("willing_to_train")) else 0,
                       valid_tz(data.get("timezone")) or db.DEFAULT_APPLICANT_TZ, json.dumps(shifts),
                       _int(data.get("hours_per_week")), max(0, min(5, _int(data.get("english_self_rating")))),
                       str(data.get("experience") or "").lower()[:20], str(data.get("experience_text") or "").strip()[:2000],
                       str(data.get("why") or "").strip()[:2000], str(data.get("heard_from") or "").lower()[:30],
                       1 if _truthy(data.get("consent")) else 0, json.dumps({k: v for k, v in data.items() if k != "raw"}, default=str), db.now()))
    return cur.lastrowid


def _truthy(v) -> bool:
    return v in (1, True, "1", "true", "yes", "on", "y")


def _int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def latest_application(con, aid: int) -> dict | None:
    a = db.one(con, "SELECT * FROM application WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (aid,))
    if a:
        a["available_shifts"] = db.jl(a.get("available_shifts"), [])
    return a


def hard_filters(app: dict, vac: dict) -> list[str]:
    reasons = []
    if not app.get("age_18"):
        reasons.append("applicants must be 18 or over")
    if app.get("device") not in ("laptop", "desktop"):
        reasons.append("the role needs a laptop or desktop")
    if not app.get("willing_to_train"):
        reasons.append("the role requires completing our training")
    shifts = app.get("available_shifts") or []
    if not any(vac.get(s, 0) > 0 for s in shifts):
        reasons.append("none of your available shifts match an open slot right now")
    return reasons


def soft_score(app: dict, vac: dict) -> int:
    exp = {"of_chatter": 25, "chat_sales": 18, "customer_service": 12, "sales": 10, "none": 4}.get(app.get("experience") or "none", 6)
    matching = sum(1 for s in (app.get("available_shifts") or []) if vac.get(s, 0) > 0)
    avail = {0: 0, 1: 10, 2: 20}.get(matching, 25)
    hrs = app.get("hours_per_week") or 0
    hours = 15 if hrs >= 30 else 10 if hrs >= 20 else 5 if hrs > 0 else 0
    english = max(0, min(5, app.get("english_self_rating") or 0)) * 4
    text = " ".join([app.get("why") or "", app.get("experience_text") or ""]).strip()
    written = 15 if len(text) >= 200 else 10 if len(text) >= 80 else 3 if text else 0
    return max(0, min(100, exp + avail + hours + english + written))


def screen(con, aid: int, actor: str = "sam", send_decline: bool = True) -> tuple[bool, list[str], int]:
    app = latest_application(con, aid)
    if not app:
        return False, ["no application on file"], 0
    vac = db.vacancies(con)
    reasons = hard_filters(app, vac)
    score = soft_score(app, vac)
    con.execute("UPDATE applicant SET score=?, screen_reasons=?, updated_at=? WHERE id=?", (score, json.dumps(reasons), db.now(), aid))
    if reasons:
        set_stage(con, aid, "screened_out", actor=actor, reason="; ".join(reasons))
        if send_decline:
            a = applicant(con, aid)
            deliver(con, aid, texts.decline(a["name"].split(" ")[0] if a["name"] else "", reasons), kind="decline")
        return False, reasons, score
    set_stage(con, aid, "screened_in", actor=actor, reason=f"hard filters passed, score {score}")
    return True, [], score


# ---------------------------------------------------------------- stages

def set_stage(con, aid: int, stage: str, actor: str = "system", reason: str = "", force: bool = False) -> bool:
    if stage not in db.STAGES:
        raise ValueError(f"unknown stage {stage}")
    a = db.one(con, "SELECT stage FROM applicant WHERE id=?", (aid,))
    if not a or a["stage"] == stage:
        return False
    cur = a["stage"]
    if not force:
        if cur in db.CLOSED:
            return False
        if stage in PIPELINE_ORDER and cur in PIPELINE_ORDER and PIPELINE_ORDER[stage] < PIPELINE_ORDER[cur]:
            return False
    ts = db.now()
    con.execute("UPDATE applicant SET stage=?, stage_changed_at=?, updated_at=? WHERE id=?", (stage, ts, ts, aid))
    con.execute("INSERT INTO stage_event (applicant_id, from_stage, to_stage, reason, actor, at) VALUES (?,?,?,?,?,?)",
                (aid, cur, stage, reason, actor, ts))
    if stage in db.CLOSED or stage == "ghosted":
        con.execute("UPDATE applicant SET awaiting_since=NULL WHERE id=?", (aid,))
    return True


def stage_history(con, aid: int) -> list[dict]:
    return db.q(con, "SELECT * FROM stage_event WHERE applicant_id=? ORDER BY id", (aid,))


def unghost(con, aid: int, actor: str = "sam") -> bool:
    ev = db.one(con, "SELECT from_stage FROM stage_event WHERE applicant_id=? AND to_stage='ghosted' ORDER BY id DESC LIMIT 1", (aid,))
    back = (ev or {}).get("from_stage") or "screened_in"
    return set_stage(con, aid, back, actor=actor, reason="replied", force=True)


# ---------------------------------------------------------------- messages

def reachable_channels(a: dict) -> list[str]:
    out = []
    if a.get("telegram_chat_id"):
        out.append("telegram")
    if a.get("discord_user_id") or a.get("discord_channel_id"):
        out.append("discord")
    if a.get("email"):
        out.append("email")
    return out


REMINDER_KINDS = ("reminder", "nudge")
OFF_DISCORD_KINDS = ("reminder", "nudge", "first")  # onboarding + reminders go to Telegram/email, never into a Discord channel


def pick_channel(a: dict, prefer: str | None = None, kind: str = "sam") -> str | None:
    """Discord is the conversation channel once the applicant has joined; onboarding, reminders and nudges prefer Telegram
    (and never land in a Discord channel). An explicit channel (a same-channel reply) still wins, except onboarding which
    must not go to Discord."""
    ok = reachable_channels(a)
    if kind in OFF_DISCORD_KINDS:
        if prefer in ("telegram", "email") and prefer in ok:
            return prefer
        for c in ("telegram", "email"):
            if c in ok:
                return c
        return None
    if prefer in ok:
        return prefer
    order = ["discord", "telegram", "email"] if a.get("discord_channel_id") else ["telegram", "discord", "email"]
    for c in order:
        if c in ok:
            return c
    return None


def _dry_sender(a: dict, channel: str, text: str) -> str:
    log.info("dry-run %s to applicant %s: %s", channel, a.get("id"), text[:80])
    return "dry-run"


def deliver(con, aid: int, text: str, kind: str = "sam", channel: str | None = None, actor: str = "sam") -> int:
    """Send to the applicant on a reachable channel and record it. No channel = held until one appears (see flush_held)."""
    a = applicant(con, aid)
    if not a:
        raise ValueError("no such applicant")
    ts = db.now()
    if a.get("do_not_contact") and kind not in ("decline",):
        cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, error, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (aid, channel or "", "out", kind, text, "skipped", "do not contact", actor, ts))
        return cur.lastrowid
    ch = pick_channel(a, channel, kind)
    if not ch:
        cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, error, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (aid, channel or "", "out", kind, text, "held", "no reachable channel", actor, ts))
        return cur.lastrowid
    sender = SENDER or _dry_sender
    status, pid, err = "sent", "", ""
    try:
        pid = sender(a, ch, text) or ""
    except Exception as e:  # noqa: BLE001
        status, err = "failed", f"{type(e).__name__}: {e}"[:300]
        log.warning("send %s to %s failed: %s", ch, aid, err)
    cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, status, provider_id, error, actor, address, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, ch, "out", kind, text, status, pid, err, actor, address_for(a, ch), ts))
    if status == "sent":
        con.execute("UPDATE applicant SET last_outbound_at=?, last_channel=?, updated_at=? WHERE id=?", (ts, ch, ts, aid))
        if kind in AWAIT_KINDS and not a.get("awaiting_since"):
            con.execute("UPDATE applicant SET awaiting_since=? WHERE id=?", (ts, aid))
    return cur.lastrowid


def address_for(a: dict, ch: str) -> str:
    return {"telegram": a.get("telegram_chat_id") or a.get("telegram") or "", "discord": a.get("discord_channel_id") or a.get("discord_user_id") or a.get("discord") or "",
            "email": a.get("email") or ""}.get(ch, "") or ""


def flush_held(con, aid: int, channel: str | None = None) -> int:
    """Send messages that were held for want of a channel. Returns how many went out."""
    n = 0
    for m in db.q(con, "SELECT * FROM message WHERE applicant_id=? AND status='held' ORDER BY id", (aid,)):
        a = applicant(con, aid)
        ch = pick_channel(a, channel)
        if not ch:
            break
        sender = SENDER or _dry_sender
        try:
            pid = sender(a, ch, m["body"]) or ""
        except Exception as e:  # noqa: BLE001
            con.execute("UPDATE message SET status='failed', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:300], m["id"]))
            continue
        ts = db.now()
        con.execute("UPDATE message SET status='sent', channel=?, provider_id=?, error='', address=?, created_at=? WHERE id=?", (ch, pid, address_for(a, ch), ts, m["id"]))
        con.execute("UPDATE applicant SET last_outbound_at=?, last_channel=?, updated_at=? WHERE id=?", (ts, ch, ts, aid))
        if m["kind"] in AWAIT_KINDS and not a.get("awaiting_since"):
            con.execute("UPDATE applicant SET awaiting_since=? WHERE id=?", (ts, aid))
        n += 1
    return n


def record_inbound(con, aid: int, channel: str, body: str, external_id: str | None = None, address: str = "") -> int | None:
    """An applicant's message. Deduped on (channel, external_id). Clears the nudge clock and reopens a ghosted file."""
    if external_id:
        if db.one(con, "SELECT id FROM message WHERE channel=? AND external_id=?", (channel, external_id)):
            return None
    ts = db.now()
    cur = con.execute("INSERT INTO message (applicant_id, channel, direction, kind, body, external_id, address, status, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (aid, channel, "in", "reply", body, external_id or None, address, "received", "applicant", ts))
    con.execute("UPDATE applicant SET last_inbound_at=?, last_channel=?, awaiting_since=NULL, nudges_sent=0, last_nudge_at=NULL, updated_at=? WHERE id=?",
                (ts, channel, ts, aid))
    a = applicant(con, aid)
    if a and a["stage"] == "ghosted":
        unghost(con, aid)
    return cur.lastrowid


def messages(con, aid: int, limit: int = 200) -> list[dict]:
    return db.q(con, "SELECT * FROM message WHERE applicant_id=? ORDER BY id DESC LIMIT ?", (aid, limit))[::-1]


def link_telegram(con, aid: int, chat_id: str, username: str | None = None) -> None:
    sets, args = ["telegram_chat_id=?"], [str(chat_id)]
    u = norm_handle(username)
    a = applicant(con, aid)
    if u and a and not a.get("telegram"):
        if not db.one(con, "SELECT id FROM applicant WHERE lower(telegram)=? AND id<>?", (u, aid)):
            sets.append("telegram=?")
            args.append(u)
    con.execute(f"UPDATE applicant SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, db.now(), aid))


def link_discord(con, aid: int, user_id: str, username: str | None = None, channel_id: str | None = None) -> None:
    sets, args = ["discord_user_id=?"], [str(user_id)]
    if channel_id:
        sets.append("discord_channel_id=?")
        args.append(str(channel_id))
    u = norm_handle(username)
    a = applicant(con, aid)
    if u and a and not a.get("discord"):
        if not db.one(con, "SELECT id FROM applicant WHERE lower(discord)=? AND id<>?", (u, aid)):
            sets.append("discord=?")
            args.append(u)
    con.execute(f"UPDATE applicant SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, db.now(), aid))


# ---------------------------------------------------------------- time helpers

def tz_of(a: dict) -> ZoneInfo:
    return ZoneInfo(valid_tz(a.get("timezone")) or db.DEFAULT_APPLICANT_TZ)


def fmt_local(d: datetime, tz: str) -> str:
    loc = d.astimezone(ZoneInfo(tz))
    return f"{loc.strftime('%a')} {loc.day} {loc.strftime('%b %H:%M')}"


def tz_short(tz: str) -> str:
    return {"Asia/Manila": "Philippine time", "Europe/London": "UK time"}.get(tz) or tz.split("/")[-1].replace("_", " ") + " time"


def when_both(start_iso: str, applicant_tz: str) -> str:
    d = db.parse_ts(start_iso)
    if not d:
        return start_iso
    a_tz = valid_tz(applicant_tz) or db.DEFAULT_APPLICANT_TZ
    local = f"{fmt_local(d, a_tz)} {tz_short(a_tz)}"
    if a_tz == db.HOUSE_TZ:
        return local
    return f"{local} ({fmt_local(d, db.HOUSE_TZ)} {tz_short(db.HOUSE_TZ)})"


def shift_in_tz(start_h: int, end_h: int, tz: str, on: datetime | None = None) -> str:
    """A UK shift block rendered in another timezone for the date given (DST-aware)."""
    on = on or datetime.now(timezone.utc)
    uk = ZoneInfo(db.HOUSE_TZ)
    day = on.astimezone(uk).date()
    s = datetime(day.year, day.month, day.day, start_h % 24, tzinfo=uk)
    e = datetime(day.year, day.month, day.day, 0, tzinfo=uk) + timedelta(hours=end_h)
    z = ZoneInfo(tz)
    return f"{s.astimezone(z).strftime('%H:%M')}-{e.astimezone(z).strftime('%H:%M')}"


# ---------------------------------------------------------------- slots and interviews

def _hhmm(s: str) -> tuple[int, int]:
    h, m = (s or "0:0").split(":")[:2]
    return int(h), int(m)


def managers(con, active_only: bool = True) -> list[dict]:
    return db.q(con, "SELECT * FROM manager" + (" WHERE active=1" if active_only else "") + " ORDER BY id")


def default_manager(con) -> dict | None:
    return db.one(con, "SELECT * FROM manager WHERE active=1 ORDER BY id LIMIT 1")


def booked_ranges(con, manager_id: int, since: datetime) -> list[tuple[datetime, datetime]]:
    rows = db.q(con, "SELECT start_at, end_at FROM interview WHERE manager_id=? AND status IN ('booked','rescheduled') AND end_at>=?",
                (manager_id, db.fmt_ts(since)))
    return [(db.parse_ts(r["start_at"]), db.parse_ts(r["end_at"])) for r in rows]


def slots(con, applicant_tz: str, now: datetime | None = None, manager_id: int | None = None, days: int | None = None,
          minutes: int | None = None, lead_hours: int | None = None) -> list[dict]:
    """30-minute finals over the next 7 days from the manager's weekly grid, minus booked, in the applicant's timezone."""
    now = now or datetime.now(timezone.utc)
    m = db.one(con, "SELECT * FROM manager WHERE id=?", (manager_id,)) if manager_id else default_manager(con)
    if not m:
        return []
    days = days if days is not None else db.setting_int(con, "slot_days", 7)
    minutes = minutes if minutes is not None else db.setting_int(con, "slot_minutes", 30)
    lead = timedelta(hours=lead_hours if lead_hours is not None else db.setting_int(con, "slot_lead_hours", 2))
    a_tz = valid_tz(applicant_tz) or db.DEFAULT_APPLICANT_TZ
    grid = db.q(con, "SELECT * FROM availability WHERE manager_id=? ORDER BY weekday, start", (m["id"],))
    if not grid:
        return []
    taken = booked_ranges(con, m["id"], now)
    horizon = now + timedelta(days=days)
    out, seen = [], set()
    for d in range(days + 2):
        for row in grid:
            tz = ZoneInfo(valid_tz(row["tz"]) or valid_tz(m["timezone"]) or db.DEFAULT_APPLICANT_TZ)
            day = (now.astimezone(tz) + timedelta(days=d)).date()
            if day.weekday() != int(row["weekday"]):
                continue
            sh, sm = _hhmm(row["start"])
            eh, em = _hhmm(row["end"])
            start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
            end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)
            t = start
            while t + timedelta(minutes=minutes) <= end:
                s_utc, e_utc = t.astimezone(timezone.utc), (t + timedelta(minutes=minutes)).astimezone(timezone.utc)
                t += timedelta(minutes=minutes)
                if s_utc < now + lead or s_utc > horizon or s_utc in seen:
                    continue
                if any(bs < e_utc and be > s_utc for bs, be in taken):
                    continue
                seen.add(s_utc)
                out.append({"start": db.fmt_ts(s_utc), "end": db.fmt_ts(e_utc), "manager_id": m["id"], "manager": m["name"],
                            "local": f"{fmt_local(s_utc, a_tz)} {tz_short(a_tz)}", "uk": f"{fmt_local(s_utc, db.HOUSE_TZ)} {tz_short(db.HOUSE_TZ)}",
                            "manager_local": fmt_local(s_utc, valid_tz(m["timezone"]) or db.DEFAULT_APPLICANT_TZ)})
    out.sort(key=lambda s: s["start"])
    return out


def open_interview(con, aid: int) -> dict | None:
    return db.one(con, "SELECT i.*, m.name AS manager_name, m.timezone AS manager_tz FROM interview i JOIN manager m ON m.id=i.manager_id "
                       "WHERE i.applicant_id=? AND i.status IN ('booked','rescheduled') ORDER BY i.start_at LIMIT 1", (aid,))


def can_book(con, a: dict) -> bool:
    if a["stage"] in BOOKABLE:
        return True
    return a["stage"] in ("screened_in", "interview_passed") and db.setting(con, "book_from_screened_in", "0") == "1"


def book(con, aid: int, start_iso: str, actor: str = "sam", manager_id: int | None = None, now: datetime | None = None, notify: bool = True) -> dict:
    """Book a final at one of the open slots. Raises NotAvailable (not on the grid / stage) or SlotTaken (someone got it first)."""
    now = now or datetime.now(timezone.utc)
    a = applicant(con, aid)
    if not a:
        raise ValueError("no such applicant")
    if not can_book(con, a):
        raise NotAvailable("booking is not open at this stage")
    start = db.parse_ts(start_iso)
    if not start:
        raise NotAvailable("bad time")
    wanted = db.fmt_ts(start)
    offered = [s for s in slots(con, a["timezone"], now=now, manager_id=manager_id) if s["start"] == wanted]
    if not offered:
        if any(bs <= start < be for bs, be in booked_ranges(con, manager_id or (default_manager(con) or {}).get("id", 0), now)):
            raise SlotTaken("that slot has just been taken")
        raise NotAvailable("that time is not one of the open slots")
    s = offered[0]
    con.execute("BEGIN IMMEDIATE")
    try:
        clash = db.one(con, "SELECT id FROM interview WHERE manager_id=? AND status IN ('booked','rescheduled') AND start_at<? AND end_at>?",
                       (s["manager_id"], s["end"], s["start"]))
        if clash:
            raise SlotTaken("that slot has just been taken")
        old = open_interview(con, aid)
        status = "booked"
        if old:
            con.execute("UPDATE interview SET status='moved', updated_at=? WHERE id=?", (db.now(), old["id"]))
            status = "rescheduled"
        ts = db.now()
        cur = con.execute("INSERT INTO interview (applicant_id, manager_id, start_at, end_at, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                          (aid, s["manager_id"], s["start"], s["end"], status, ts, ts))
        iid = cur.lastrowid
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    set_stage(con, aid, "final_scheduled", actor=actor, reason=f"final booked {s['uk']} with {s['manager']}")
    con.execute("UPDATE applicant SET awaiting_since=NULL, nudges_sent=0, last_nudge_at=NULL WHERE id=?", (aid,))
    if notify:
        first = (a["name"] or "").split(" ")[0]
        text = texts.rescheduled(first, when_both(s["start"], a["timezone"]), s["manager"]) if status == "rescheduled" else texts.booked(first, when_both(s["start"], a["timezone"]), s["manager"])
        deliver(con, aid, text, kind="booking")
    row = db.one(con, "SELECT * FROM interview WHERE id=?", (iid,))
    if ON_BOOKED:
        try:
            ON_BOOKED(aid, row)
        except Exception as e:  # noqa: BLE001
            log.warning("on_booked hook failed: %s", e)
    return row


def reschedule(con, aid: int, start_iso: str, actor: str = "sam", now: datetime | None = None, notify: bool = True) -> dict:
    if not open_interview(con, aid):
        raise NotAvailable("there is no interview to move")
    return book(con, aid, start_iso, actor=actor, now=now, notify=notify)


def cancel(con, aid: int, actor: str = "sam", reason: str = "", notify: bool = True) -> bool:
    iv = open_interview(con, aid)
    if not iv:
        return False
    con.execute("UPDATE interview SET status='cancelled', updated_at=? WHERE id=?", (db.now(), iv["id"]))
    a = applicant(con, aid)
    if a["stage"] == "final_scheduled":
        set_stage(con, aid, "passed", actor=actor, reason=reason or "interview cancelled", force=True)
    if notify:
        deliver(con, aid, texts.cancelled((a["name"] or "").split(" ")[0]), kind="booking")
    return True


def interview_outcome(con, aid: int, outcome: str, actor: str = "staff", verdict: str = "") -> bool:
    iv = open_interview(con, aid)
    if not iv or outcome not in ("done", "no_show"):
        return False
    con.execute("UPDATE interview SET status=?, verdict=?, verdict_by=?, updated_at=? WHERE id=?", (outcome, verdict, actor if verdict else "", db.now(), iv["id"]))
    if outcome == "done":
        set_stage(con, aid, "final_done", actor=actor, reason="final interview held")
    else:
        set_stage(con, aid, "passed", actor=actor, reason="no-show at the final; rebook once", force=True)
    return True


# ---------------------------------------------------------------- escalations

def create_escalation(con, aid: int, reason: str, question: str = "") -> int:
    cur = con.execute("INSERT INTO escalation (applicant_id, reason, question, status, created_at) VALUES (?,?,?,'open',?)",
                      (aid, reason[:200], question[:2000], db.now()))
    a = applicant(con, aid) or {}
    if ALERT:
        try:
            ALERT(f"Escalation for applicant #{aid} {a.get('name') or ''}: {reason}" + (f"\nQ: {question[:300]}" if question else ""), aid)
        except Exception as e:  # noqa: BLE001
            log.warning("alert failed: %s", e)
    return cur.lastrowid


def answer_escalation(con, eid: int, answer: str, by: str, save_knowledge: bool = True, notify: bool = True) -> int | None:
    e = db.one(con, "SELECT * FROM escalation WHERE id=?", (eid,))
    if not e or not answer.strip():
        return None
    ts = db.now()
    con.execute("UPDATE escalation SET status='answered', answer=?, answered_by=?, answered_at=? WHERE id=?", (answer.strip(), by, ts, eid))
    if save_knowledge and e.get("question"):
        con.execute("INSERT INTO knowledge_item (key, question, answer, active, updated_by, updated_at) VALUES ('',?,?,1,?,?)",
                    (e["question"][:500], answer.strip(), by, ts))
    if notify:
        return deliver(con, e["applicant_id"], texts.answer(answer.strip()), kind="answer", actor=by)
    return None


def close_escalation(con, eid: int, by: str) -> None:
    con.execute("UPDATE escalation SET status='closed', answered_by=?, answered_at=? WHERE id=? AND status<>'closed'", (by, db.now(), eid))


# ---------------------------------------------------------------- status

def next_step(con, a: dict) -> str:
    iv = open_interview(con, a["id"])
    s = a["stage"]
    if s == "final_scheduled" and iv:
        return f"your final interview with {iv['manager_name']} is booked for {when_both(iv['start_at'], a['timezone'])}."
    if s in ("screened_in", "interview_passed") and can_book(con, a):
        return "you're through to the final interview, a 30-minute mock chat with a chat manager; I can book that for you now."
    if s == "screened_in" and db.setting(con, "interview_enabled", "1") == "1":
        app = latest_application(con, a["id"])
        st = (app or {}).get("interview_state") or ""
        if st.startswith("q"):
            return f"you're in the middle of my five-question interview (question {int(st[1:]) + 1} of 5). Answer here and we carry on."
        return "you passed screening. Next is a short five-question text interview with me, about ten minutes, then the assessment."
    return {
        "applied": "your application is in and being screened.",
        "screened_in": "you passed screening. Next is the assessment; the team sends the link once they open your file, usually within a day.",
        "interview_passed": "you passed the interview. Next is the assessment, about 15 minutes in one sitting; I'll send the link here as soon as it's ready.",
        "interview_failed": "your application was not taken forward after the interview.",
        "assessment_sent": "your assessment link is out. It takes about 15 minutes in one sitting.",
        "assessment_in_progress": "your assessment is started but not finished. Finish it when you can.",
        "assessment_done": "your assessment is in and being reviewed.",
        "passed": "you passed the assessment. Next is a 30-minute final interview with a chat manager; I can book that for you.",
        "final_scheduled": "your final interview is booked.",
        "final_done": "your final interview is done. You hear back within two working days.",
        "offer_sent": "an offer has been sent to you.",
        "hired": "you're hired. Welcome aboard.",
        "ghosted": "your file is paused because we didn't hear back. Replying reopens it.",
        "talent_pool": "you're in our talent pool; we get in touch when a matching vacancy opens.",
    }.get(s, "your application was not taken forward this time.")


def status_summary(con, aid: int) -> dict:
    a = applicant(con, aid)
    iv = open_interview(con, aid)
    return {"stage": a["stage"], "stage_label": db.STAGE_LABELS.get(a["stage"], a["stage"]), "next_step": next_step(con, a),
            "timezone": a["timezone"], "score": a["score"], "can_book": can_book(con, a),
            "interview": {"start": iv["start_at"], "when": when_both(iv["start_at"], a["timezone"]), "manager": iv["manager_name"]} if iv else None}


# ---------------------------------------------------------------- worker: reminders, nudges, ghost

def send_reminders(con, now: datetime) -> int:
    n = 0
    rows = db.q(con, "SELECT i.*, m.name AS manager_name FROM interview i JOIN manager m ON m.id=i.manager_id "
                     "WHERE i.status IN ('booked','rescheduled') AND i.start_at>?", (db.fmt_ts(now),))
    for iv in rows:
        start = db.parse_ts(iv["start_at"])
        created = db.parse_ts(iv["created_at"]) or now
        a = applicant(con, iv["applicant_id"])
        if not a or a.get("do_not_contact"):
            continue
        first = (a["name"] or "").split(" ")[0]
        left = start - now
        if not iv["reminded_24h_at"] and timedelta(hours=1) < left <= timedelta(hours=24) and created <= start - timedelta(hours=24):
            deliver(con, a["id"], texts.reminder_24h(first, when_both(iv["start_at"], a["timezone"]), iv["manager_name"]), kind="reminder")
            con.execute("UPDATE interview SET reminded_24h_at=? WHERE id=?", (db.fmt_ts(now), iv["id"]))
            n += 1
        if not iv["reminded_1h_at"] and timedelta(0) < left <= timedelta(hours=1):
            deliver(con, a["id"], texts.reminder_1h(first, when_both(iv["start_at"], a["timezone"]), iv["manager_name"]), kind="reminder")
            con.execute("UPDATE interview SET reminded_1h_at=? WHERE id=?", (db.fmt_ts(now), iv["id"]))
            n += 1
    return n


def nudge_interval(con) -> timedelta:
    nudges = max(1, db.setting_int(con, "ghost_nudges", 3))
    days = max(1, db.setting_int(con, "ghost_days", 5))
    return timedelta(hours=days * 24 / nudges)


def run_nudges(con, now: datetime) -> dict:
    stages = [s.strip() for s in db.setting(con, "nudge_stages", "").split(",") if s.strip()]
    max_n = max(1, db.setting_int(con, "ghost_nudges", 3))
    gap = nudge_interval(con)
    nudged = ghosted = 0
    for a in db.q(con, "SELECT * FROM applicant WHERE awaiting_since IS NOT NULL AND do_not_contact=0"):
        if a["stage"] not in stages or open_interview(con, a["id"]):
            continue
        anchor = db.parse_ts(a["last_nudge_at"]) or db.parse_ts(a["awaiting_since"])
        if not anchor or now < anchor + gap:
            continue
        first = (a["name"] or "").split(" ")[0]
        if a["nudges_sent"] < max_n:
            deliver(con, a["id"], texts.nudge(first, a["nudges_sent"]), kind="nudge")
            con.execute("UPDATE applicant SET nudges_sent=nudges_sent+1, last_nudge_at=? WHERE id=?", (db.fmt_ts(now), a["id"]))
            nudged += 1
        else:
            set_stage(con, a["id"], "ghosted", actor="sam", reason=f"no reply after {max_n} nudges over {db.setting_int(con, 'ghost_days', 5)} days")
            ghosted += 1
    return {"nudged": nudged, "ghosted": ghosted}


def flush_all_held(con) -> int:
    n = 0
    for r in db.q(con, "SELECT DISTINCT applicant_id FROM message WHERE status='held'"):
        n += flush_held(con, r["applicant_id"])
    return n


def tick(con, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"reminders": send_reminders(con, now)}
    out.update(run_nudges(con, now))
    out["held_sent"] = flush_all_held(con)
    return out
