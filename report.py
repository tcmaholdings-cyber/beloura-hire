"""Weekly funnel report per channel: the last 7 days against the 7 before, time to hire, no-show rate, LLM cost per applicant.

report.weekly(con, now=None)        -> dict (periods, per-channel counts, totals, deltas, tth, no_show, llm)
report.weekly_text(con, now=None)   -> plain text for Discord / email / the staff page
report.post_weekly(con, discord)    -> posts the text to the #weekly-report channel (settings discord_ch_weekly_report); dry-run when
                                       Discord is not configured, skipped with a note when no channel id is stored
report.maybe_post_weekly(con, discord, now=None) -> called every worker tick; posts once per ISO week on Monday at or after 09:00 Asia/Manila
Router: GET /staff/report, GET /staff/report.txt, POST /staff/report/send
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db
import engine
import posts
import texts

log = logging.getLogger("hire.report")
HERE = os.path.dirname(os.path.abspath(__file__))
router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

REPORT_TZ = "Asia/Manila"
REPORT_WEEKDAY = 0      # Monday
REPORT_HOUR = 9
SENT_KEY = "weekly_report_sent_for"
LAST_KEY = "weekly_report_last"
CHANNEL_KEYS = ("discord_ch_weekly_report", "discord_weekly_report_channel_id")
DISCORD_LIMIT = 1900

METRICS = ["applied", "screened_in", "interview_passed", "assessment_sent", "assessment_done", "passed", "final_booked", "hired",
           "declined", "ghosted", "no_show"]
METRIC_LABELS = {"applied": "Applied", "screened_in": "Screened in", "interview_passed": "Interview passed", "assessment_sent": "Assessment sent",
                 "assessment_done": "Assessment done", "passed": "Passed", "final_booked": "Finals booked", "hired": "Hired", "declined": "Declined",
                 "ghosted": "Ghosted", "no_show": "No-shows"}
STAGE_METRICS = ["screened_in", "interview_passed", "assessment_sent", "assessment_done", "passed", "hired", "declined", "ghosted"]


def _label(code: str) -> str:
    return posts.CHANNEL_LABELS.get(code, code or "direct")


def _code(code: str | None) -> str:
    return code or "direct"


def _zero() -> dict:
    return {m: 0 for m in METRICS}


# ---------------------------------------------------------------- counts

def funnel(con, start: datetime, end: datetime) -> dict:
    """Counts by channel for events inside [start, end): applied = applicant created, stage metrics = stage_event into that stage
    (distinct applicants), final_booked = distinct applicants with an interview created, no_show = interview outcomes 'no_show'."""
    s, e = db.fmt_ts(start), db.fmt_ts(end)
    by: dict[str, dict] = {}

    def bump(code, metric, n):
        by.setdefault(_code(code), _zero())[metric] += n

    for r in db.q(con, "SELECT channel_code c, COUNT(*) n FROM applicant WHERE created_at>=? AND created_at<? GROUP BY channel_code", (s, e)):
        bump(r["c"], "applied", r["n"])
    for r in db.q(con, """SELECT a.channel_code c, e.to_stage st, COUNT(DISTINCT e.applicant_id) n FROM stage_event e JOIN applicant a ON a.id=e.applicant_id
                          WHERE e.at>=? AND e.at<? GROUP BY a.channel_code, e.to_stage""", (s, e)):
        if r["st"] in STAGE_METRICS:
            bump(r["c"], r["st"], r["n"])
    for r in db.q(con, """SELECT a.channel_code c, COUNT(DISTINCT i.applicant_id) n FROM interview i JOIN applicant a ON a.id=i.applicant_id
                          WHERE i.created_at>=? AND i.created_at<? AND i.status<>'moved' GROUP BY a.channel_code""", (s, e)):
        bump(r["c"], "final_booked", r["n"])
    for r in db.q(con, """SELECT a.channel_code c, COUNT(*) n FROM interview i JOIN applicant a ON a.id=i.applicant_id
                          WHERE i.status='no_show' AND i.updated_at>=? AND i.updated_at<? GROUP BY a.channel_code""", (s, e)):
        bump(r["c"], "no_show", r["n"])
    totals = _zero()
    for d in by.values():
        for m in METRICS:
            totals[m] += d[m]
    return {"by_channel": by, "totals": totals}


def time_to_hire(con, start: datetime, end: datetime) -> dict:
    rows = db.q(con, """SELECT e.at hired_at, a.created_at FROM stage_event e JOIN applicant a ON a.id=e.applicant_id
                        WHERE e.to_stage='hired' AND e.at>=? AND e.at<?""", (db.fmt_ts(start), db.fmt_ts(end)))
    days = []
    for r in rows:
        h, c = db.parse_ts(r["hired_at"]), db.parse_ts(r["created_at"])
        if h and c:
            days.append(max(0.0, (h - c).total_seconds() / 86400))
    return {"n": len(days), "median_days": round(statistics.median(days), 1) if days else None,
            "mean_days": round(sum(days) / len(days), 1) if days else None}


def no_show_rate(con, start: datetime, end: datetime) -> dict:
    s, e = db.fmt_ts(start), db.fmt_ts(end)
    r = db.one(con, """SELECT SUM(status='no_show') ns, SUM(status='done') held FROM interview WHERE status IN ('no_show','done') AND updated_at>=? AND updated_at<?""", (s, e)) or {}
    ns, held = int(r.get("ns") or 0), int(r.get("held") or 0)
    return {"no_show": ns, "held": held, "rate": round(100 * ns / (ns + held)) if ns + held else None}


def llm_cost(con, start: datetime, end: datetime) -> dict:
    s, e = db.fmt_ts(start), db.fmt_ts(end)
    r = db.one(con, "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) usd, COUNT(DISTINCT applicant_id) applicants FROM llm_call WHERE created_at>=? AND created_at<?", (s, e)) or {}
    calls, usd, n = int(r.get("calls") or 0), float(r.get("usd") or 0), int(r.get("applicants") or 0)
    by = {}
    for row in db.q(con, """SELECT a.channel_code c, COALESCE(SUM(l.cost_usd),0) usd, COUNT(DISTINCT l.applicant_id) n FROM llm_call l JOIN applicant a ON a.id=l.applicant_id
                            WHERE l.created_at>=? AND l.created_at<? GROUP BY a.channel_code""", (s, e)):
        by[_code(row["c"])] = {"usd": round(float(row["usd"]), 4), "applicants": int(row["n"]),
                               "per_applicant": round(float(row["usd"]) / row["n"], 4) if row["n"] else None}
    return {"calls": calls, "usd": round(usd, 4), "applicants": n, "per_applicant": round(usd / n, 4) if n else None, "by_channel": by}


def weekly(con, now: datetime | None = None, days: int = 7) -> dict:
    now = now or datetime.now(timezone.utc)
    end = now
    start = end - timedelta(days=days)
    pstart = start - timedelta(days=days)
    cur, prev = funnel(con, start, end), funnel(con, pstart, start)
    codes = sorted(set(cur["by_channel"]) | set(prev["by_channel"]), key=lambda c: (-cur["by_channel"].get(c, _zero())["applied"], c))
    channels = []
    for c in codes:
        channels.append({"code": c, "label": _label(c), "cur": cur["by_channel"].get(c, _zero()), "prev": prev["by_channel"].get(c, _zero())})
    deltas = {m: cur["totals"][m] - prev["totals"][m] for m in METRICS}
    return {"start": start, "end": end, "prev_start": pstart, "days": days, "channels": channels, "totals": cur["totals"], "prev_totals": prev["totals"],
            "deltas": deltas, "tth": time_to_hire(con, start, end), "tth_prev": time_to_hire(con, pstart, start),
            "no_show": no_show_rate(con, start, end), "no_show_prev": no_show_rate(con, pstart, start),
            "llm": llm_cost(con, start, end), "llm_prev": llm_cost(con, pstart, start), "generated_at": now}


# ---------------------------------------------------------------- text

def _d(d: datetime, tz: str = REPORT_TZ) -> str:
    return d.astimezone(ZoneInfo(tz)).strftime("%a %-d %b")


def _delta(n: int) -> str:
    return f"+{n}" if n > 0 else (str(n) if n < 0 else "0")


def weekly_text(con, now: datetime | None = None) -> str:
    posts.attribute(con)
    w = weekly(con, now)
    t, p, d = w["totals"], w["prev_totals"], w["deltas"]
    lines = [f"Weekly hiring report, {_d(w['start'])} to {_d(w['end'])} ({REPORT_TZ}); change against the week before in brackets.", ""]
    lines.append("Funnel: " + " | ".join(f"{METRIC_LABELS[m]} {t[m]} ({_delta(d[m])})" for m in METRICS))
    lines.append("")
    if w["channels"]:
        lines.append("By channel (applied / screened in / interview passed / passed / finals booked / hired; last week in brackets):")
        for c in w["channels"]:
            cu, pr = c["cur"], c["prev"]
            lines.append(f"  {c['label']}: {cu['applied']} / {cu['screened_in']} / {cu['interview_passed']} / {cu['passed']} / {cu['final_booked']} / {cu['hired']}"
                         f"  ({pr['applied']} / {pr['screened_in']} / {pr['interview_passed']} / {pr['passed']} / {pr['final_booked']} / {pr['hired']})")
    else:
        lines.append("By channel: no applicants in either week.")
    lines.append("")
    tth = w["tth"]
    lines.append("Time to hire: " + (f"median {tth['median_days']} d, mean {tth['mean_days']} d over {tth['n']} hire(s)" if tth["n"] else "no hires this week")
                 + (f" (last week median {w['tth_prev']['median_days']} d)" if w["tth_prev"]["n"] else "") + ".")
    ns = w["no_show"]
    lines.append("No-show rate at finals: " + (f"{ns['rate']}% ({ns['no_show']} of {ns['no_show'] + ns['held']})" if ns["rate"] is not None else "no finals held")
                 + (f" (last week {w['no_show_prev']['rate']}%)" if w["no_show_prev"]["rate"] is not None else "") + ".")
    llm = w["llm"]
    lines.append(f"LLM cost: ${llm['usd']:.2f} over {llm['calls']} call(s) and {llm['applicants']} applicant(s)"
                 + (f", ${llm['per_applicant']:.3f} per applicant" if llm["per_applicant"] is not None else "")
                 + (f" (last week ${w['llm_prev']['usd']:.2f})" if w["llm_prev"]["calls"] else "") + ".")
    lines.append("")
    lines.append(f"Full report: {posts.public_url('/staff/report')}")
    return "\n".join(lines)


def chunks(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- posting

def weekly_report_channel_id(con) -> int:
    for k in CHANNEL_KEYS:
        v = db.setting(con, k, "")
        if v.isdigit():
            return int(v)
    return 0


def post_weekly(con, discord, now: datetime | None = None, by: str = "worker") -> dict:
    text = weekly_text(con, now)
    cid = weekly_report_channel_id(con)
    out = {"by": by, "at": db.now(), "chars": len(text), "channel_id": cid}
    if not cid:
        out["skipped"] = "no weekly report channel id in settings"
        log.warning("weekly report not posted: %s", out["skipped"])
    elif discord is None or not getattr(discord, "configured", False):
        out["dry_run"] = True
        log.info("weekly report dry-run (%d chars):\n%s", len(text), text)
    else:
        ids = [discord.post_staff(part, channel_id=cid) for part in chunks(text)]
        out["message_ids"] = ids
        out["dry_run"] = all(i == "dry-run" for i in ids)
    db.set_setting(con, LAST_KEY, json.dumps(out)[:900])
    return out


def week_key(local: datetime) -> str:
    y, w, _ = local.isocalendar()
    return f"{y}-W{w:02d}"


def is_report_time(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(REPORT_TZ))
    return local.weekday() == REPORT_WEEKDAY and local.hour >= REPORT_HOUR


def maybe_post_weekly(con, discord, now: datetime | None = None) -> dict:
    """One call per worker tick. Posts once per ISO week, the first tick on Monday at or after 09:00 Asia/Manila (a restart later on
    Monday still posts; Tuesday does not). Dry-run counts as sent so the log is not spammed."""
    now = now or datetime.now(timezone.utc)
    if not is_report_time(now):
        return {"skipped": "not report time"}
    key = week_key(now.astimezone(ZoneInfo(REPORT_TZ)))
    if db.setting(con, SENT_KEY, "") == key:
        return {"skipped": f"already sent for {key}"}
    out = post_weekly(con, discord, now)
    db.set_setting(con, SENT_KEY, key)
    out["week"] = key
    return out


def worker_tick(discord, now: datetime | None = None) -> dict | None:
    """The one call app.py's worker makes per tick. Cheap when it is not report time; when it posts, it records a worker_runs row
    (kind weekly_report) so the settings page shows it. Never raises into the loop."""
    now = now or datetime.now(timezone.utc)
    if not is_report_time(now):
        return None
    try:
        with db.db() as con:
            if db.setting(con, SENT_KEY, "") == week_key(now.astimezone(ZoneInfo(REPORT_TZ))):
                return None
            cur = con.execute("INSERT INTO worker_runs (kind, started_at) VALUES ('weekly_report', ?)", (db.now(),))
            rid = cur.lastrowid
            try:
                out = maybe_post_weekly(con, discord, now)
                con.execute("UPDATE worker_runs SET finished_at=?, ok=1, notes=? WHERE id=?", (db.now(), json.dumps(out)[:500], rid))
            except Exception as e:  # noqa: BLE001
                log.exception("weekly report failed")
                con.execute("UPDATE worker_runs SET finished_at=?, ok=0, notes=? WHERE id=?", (db.now(), str(e)[:500], rid))
                db.set_setting(con, SENT_KEY, week_key(now.astimezone(ZoneInfo(REPORT_TZ))))  # do not retry every 30 s all day
                return {"error": str(e)}
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("weekly report tick failed: %s", e)
        return {"error": str(e)}


# ---------------------------------------------------------------- staff page

templates.env.filters["ago"] = posts._ago
templates.env.filters["nice"] = posts._nice
templates.env.globals.update(APP_NAME="Beloura Hire", AGENT=texts.AGENT_NAME, STAGES=db.STAGES, PIPELINE=db.PIPELINE, SIDE=db.SIDE,
                             STAGE_LABELS=db.STAGE_LABELS, SHIFT_LABELS=db.SHIFT_LABELS, METRICS=METRICS, METRIC_LABELS=METRIC_LABELS)


def _ctx(request: Request, **extra) -> dict:
    with db.db() as con:
        esc_n = (db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'") or {}).get("n", 0)
        worker = db.one(con, "SELECT * FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1")
    return {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path, "query": dict(request.query_params),
            "esc_n": esc_n, "worker": worker, **extra}


DISCORD = None  # app.py sets this to CH.discord so the Send button can post


@router.get("/staff/report")
def report_page(request: Request):
    with db.db() as con:
        posts.attribute(con)
        w = weekly(con)
        text = weekly_text(con)
        last = db.jl(db.setting(con, LAST_KEY, ""), {})
        cid = weekly_report_channel_id(con)
        sent_for = db.setting(con, SENT_KEY, "")
    return templates.TemplateResponse(request, "report.html", _ctx(request, w=w, text=text, last=last, cid=cid, sent_for=sent_for, tz=REPORT_TZ,
                                                                   fmt=lambda d: engine.fmt_local(d, REPORT_TZ), sent=request.query_params.get("sent")))


@router.get("/staff/report.txt", response_class=PlainTextResponse)
def report_text():
    with db.db() as con:
        return weekly_text(con) + "\n"


@router.post("/staff/report/send")
def report_send(request: Request):
    who = ((getattr(request.state, "user", None) or {}).get("user")) or "staff"
    with db.db() as con:
        out = post_weekly(con, DISCORD, by=who)
    flag = "skipped" if out.get("skipped") else ("dry" if out.get("dry_run") else "ok")
    return RedirectResponse(f"/staff/report?sent={flag}", status_code=303)
