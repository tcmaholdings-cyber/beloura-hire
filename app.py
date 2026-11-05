"""beloura-hire: the chatter-recruitment assistant (James, see texts.AGENT_NAME) plus the staff pipeline.

Public: GET/POST /apply/{channel_code}, GET /applied/{id}/{token}, POST /hooks/telegram/{secret}, GET /healthz, /static.
Staff (gateway groups hiring-admin / hiring-manager / id-admins; Basic fallback on the direct port):
  /staff board, /staff/applicant/{id}, /staff/escalations, /staff/knowledge, /staff/availability, /staff/settings.
Worker thread: reminders, nudges, ghost timeout, held messages, IMAP poll, Telegram polling in dev (HIRE_TELEGRAM_POLL=1).
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dotenv_lite
dotenv_lite.load()

from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import channels as channels_mod  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import interview  # noqa: E402
import sam  # noqa: E402
import texts  # noqa: E402
from assess import router as assess_router  # noqa: E402
from olj import router as olj_router  # noqa: E402
from posts import router as posts_router  # noqa: E402
import report  # noqa: E402
from beloura_sso import gateway_identity, role_for  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hire")
logging.getLogger("httpx").setLevel(logging.WARNING)

APP_NAME = "Beloura Hire"
GATEWAY_TOKEN = os.environ.get("BELOURA_GATEWAY_TOKEN", "")
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PUBLIC_PREFIXES = ("/static/", "/apply/", "/applied/", "/assess/", "/grade/", "/hooks/")
ADMIN_ONLY = ("/staff/settings",)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EXPERIENCE = [("none", "No chat or sales experience yet"), ("customer_service", "Customer service"), ("sales", "Sales"),
              ("chat_sales", "Chat-based sales or support"), ("of_chatter", "OnlyFans chatting")]
TIMEZONES = ["Asia/Manila", "Asia/Jakarta", "Asia/Kuala_Lumpur", "Asia/Singapore", "Asia/Bangkok", "Asia/Ho_Chi_Minh", "Asia/Kolkata",
             "Asia/Karachi", "Asia/Dhaka", "Asia/Dubai", "Africa/Lagos", "Africa/Nairobi", "Africa/Johannesburg", "Africa/Cairo",
             "Europe/London", "Europe/Lisbon", "Europe/Madrid", "Europe/Paris", "Europe/Berlin", "Europe/Rome", "Europe/Warsaw",
             "Europe/Bucharest", "Europe/Istanbul", "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/Bogota", "America/Lima",
             "America/Mexico_City", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Australia/Sydney", "Pacific/Auckland"]

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(APP_NAME=APP_NAME, AGENT=texts.AGENT_NAME, STAGES=db.STAGES, PIPELINE=db.PIPELINE, SIDE=db.SIDE, STAGE_LABELS=db.STAGE_LABELS,
                             SHIFT_LABELS=db.SHIFT_LABELS, WEEKDAYS=WEEKDAYS)
CH = channels_mod.Registry()
db.init()
app.include_router(assess_router)
app.include_router(olj_router)
app.include_router(posts_router)
app.include_router(report.router)


# ---------------------------------------------------------------- helpers

def ago(ts: str | None) -> str:
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


def nice_ts(ts: str | None, tz: str = db.HOUSE_TZ) -> str:
    d = db.parse_ts(ts)
    return engine.fmt_local(d, tz) if d else ""


templates.env.filters["ago"] = ago
templates.env.filters["nice"] = nice_ts
templates.env.filters["jl"] = lambda s: db.jl(s, [])
templates.env.filters["both"] = lambda ts, tz: engine.when_both(ts, tz)


def ctx(request: Request, **extra) -> dict:
    with db.db() as con:
        esc_n = (db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'") or {}).get("n", 0)
        worker = db.one(con, "SELECT * FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1")
    return {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path,
            "query": dict(request.query_params), "esc_n": esc_n, "worker": worker, **extra}


def page(name: str, request: Request, status_code: int = 200, **extra):
    return templates.TemplateResponse(request, name, ctx(request, **extra), status_code=status_code)


def actor(request: Request) -> str:
    u = getattr(request.state, "user", None)
    return (u or {}).get("user") or "staff"


def applicant_or_404(con, aid: int) -> dict:
    a = engine.applicant(con, aid)
    if not a:
        raise HTTPException(404, "no such applicant")
    return a


def staff_alert(text: str, aid: int | None = None) -> None:
    def _send():
        try:
            CH.staff_alert(text, aid)
        except Exception as e:  # noqa: BLE001
            log.warning("staff alert failed: %s", e)
    threading.Thread(target=_send, name="hire-alert", daemon=True).start()


engine.SENDER = CH.send_to
engine.ALERT = staff_alert
engine.ON_BOOKED = CH.discord.on_booked
report.DISCORD = CH.discord


def applied_token(aid: int) -> str:
    return hmac.new(os.environ.get("HIRE_SECRET", "dev-secret").encode(), f"applied:{aid}".encode(), "sha256").hexdigest()[:16]


# ---------------------------------------------------------------- auth

@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PREFIXES) or path in ("/healthz", "/"):
        return await call_next(request)
    who = gateway_identity(request.headers, GATEWAY_TOKEN) if GATEWAY_TOKEN else None
    if who:
        role = role_for(who["groups"], "hiring") or ("admin" if "id-admins" in who["groups"] else None)
        if not role:
            return templates.TemplateResponse(request, "forbidden.html", {"request": request, "who": who}, status_code=403)
        request.state.user = {"user": who["user"], "name": who["name"], "role": role, "via": "gateway"}
        with db.db() as con:
            con.execute("INSERT INTO users (username, name, role, last_seen_at) VALUES (?,?,?,?) ON CONFLICT(username) DO UPDATE SET name=excluded.name, role=excluded.role, last_seen_at=excluded.last_seen_at",
                        (who["user"], who["name"], role, db.now()))
    else:
        hdr = request.headers.get("authorization", "")
        ok = False
        if PANEL_PASSWORD and hdr.startswith("Basic "):
            try:
                u, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
            except Exception:  # noqa: BLE001
                u = pw = ""
            ok = hmac.compare_digest(u, PANEL_USER) and hmac.compare_digest(pw, PANEL_PASSWORD)
        if not ok:
            return Response("Sign in at https://workspace.beloura.ai", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Beloura Hire"'})
        request.state.user = {"user": PANEL_USER, "name": PANEL_USER, "role": "admin", "via": "basic"}
    if path.startswith(ADMIN_ONLY) and request.state.user["role"] != "admin":
        return templates.TemplateResponse(request, "forbidden.html", {"request": request, "who": {"name": request.state.user["name"]}}, status_code=403)
    return await call_next(request)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    with db.db() as con:
        w = db.one(con, "SELECT started_at, ok FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1") or {}
        n = db.one(con, "SELECT COUNT(*) n FROM applicant")["n"]
        esc = db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'")["n"]
        invite = db.setting(con, "discord_invite_url", "")
    return (f"ok applicants={n} escalations_open={esc} tick={w.get('started_at', 'never')} ok={w.get('ok')} "
            f"sam={'live' if sam.configured() else 'dry-run'} telegram={'on' if CH.telegram.configured else 'off'} "
            f"discord={'on' if CH.discord.configured else 'off'} discord_invite={'yes' if invite else 'no'} email={'on' if CH.email.configured else 'off'}")


@app.get("/")
def root():
    return RedirectResponse("/staff", status_code=302)


@app.get("/logout")
def logout():
    return RedirectResponse("https://id.beloura.ai/logout?rd=https://workspace.beloura.ai", status_code=302)


# ---------------------------------------------------------------- public: the application form

def shift_rows() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [{"key": k, "uk": db.SHIFT_LABELS[k], "ph": engine.shift_in_tz(s, e, "Asia/Manila", now)} for k, s, e in db.SHIFTS]


def form_ctx(code: str, form: dict, errors: dict) -> dict:
    return {"code": code, "form": form, "errors": errors, "tz_list": TIMEZONES, "shifts": shift_rows(), "DEVICES": db.DEVICES,
            "CONNECTIONS": db.CONNECTIONS, "HEARD_FROM": db.HEARD_FROM, "EXPERIENCE": EXPERIENCE}


def validate_application(form: dict) -> tuple[dict, dict]:
    """Server-side validation. Returns (clean, errors)."""
    errors, clean = {}, {}
    if form.get("website"):
        errors["form"] = "Something went wrong. Please try again."
    name = (form.get("name") or "").strip()
    if len(name) < 2:
        errors["name"] = "Please give your name."
    clean["name"] = name[:120]
    email_v = engine.norm_email(form.get("email"))
    if not email_v or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_v):
        errors["email"] = "A valid email is needed."
    clean["email"] = email_v
    clean["telegram"] = engine.norm_handle(form.get("telegram"))
    clean["discord"] = engine.norm_handle(form.get("discord"))
    clean["phone"] = engine.norm_phone(form.get("phone"))
    tz = engine.valid_tz(form.get("timezone"))
    if not tz:
        errors["timezone"] = "Pick your timezone."
    clean["timezone"] = tz or db.DEFAULT_APPLICANT_TZ
    clean["age_18"] = form.get("age_18") in ("1", "on", "yes")
    if not clean["age_18"]:
        errors["age_18"] = "You must confirm you are 18 or over."
    dev = (form.get("device") or "").lower()
    if dev not in db.DEVICES:
        errors["device"] = "Pick the device you would work on."
    clean["device"] = dev
    conn = (form.get("connection_type") or "").lower()
    if conn not in db.CONNECTIONS:
        errors["connection_type"] = "Pick your connection type."
    clean["connection_type"] = conn
    clean["willing_to_train"] = form.get("willing_to_train") in ("1", "on", "yes")
    shifts = form.get("available_shifts") or []
    if isinstance(shifts, str):
        shifts = [shifts]
    shifts = [s for s in shifts if s in db.SHIFT_LABELS]
    if not shifts:
        errors["available_shifts"] = "Tick at least one shift you can work."
    clean["available_shifts"] = shifts
    hrs = engine._int(form.get("hours_per_week"))
    if not 1 <= hrs <= 80:
        errors["hours_per_week"] = "Hours per week between 1 and 80."
    clean["hours_per_week"] = hrs
    eng = engine._int(form.get("english_self_rating"))
    if not 1 <= eng <= 5:
        errors["english_self_rating"] = "Rate your written English from 1 to 5."
    clean["english_self_rating"] = eng
    exp = (form.get("experience") or "none").lower()
    clean["experience"] = exp if exp in dict(EXPERIENCE) else "none"
    clean["experience_text"] = (form.get("experience_text") or "").strip()[:2000]
    why = (form.get("why") or "").strip()
    if len(why) < 20:
        errors["why"] = "Tell us in a sentence or two why this role (at least 20 characters)."
    clean["why"] = why[:2000]
    hf = (form.get("heard_from") or "").lower()
    clean["heard_from"] = hf if hf in db.HEARD_FROM else "other"
    clean["consent"] = form.get("consent") in ("1", "on", "yes")
    if not clean["consent"]:
        errors["consent"] = "We need your consent to contact you and run the assessment."
    return clean, errors


@app.get("/apply/{code}", response_class=HTMLResponse)
def apply_form(code: str, request: Request):
    code = re.sub(r"[^a-z0-9_-]", "", code.lower())[:40] or "direct"
    return templates.TemplateResponse(request, "apply.html", {"request": request, **form_ctx(code, {"timezone": db.DEFAULT_APPLICANT_TZ, "available_shifts": []}, {})})


def submit_application(con, code: str, clean: dict, telegram_link_fn=None, discord_invite: str = "") -> int:
    """Create or link the applicant, store the application, screen, announce, and send the first message."""
    aid, created = engine.upsert_applicant(con, {**clean, "channel_code": code}, actor="form")
    a = engine.applicant(con, aid)
    reopened = False
    if not created and (a["stage"] in db.CLOSED or a["stage"] == "ghosted"):
        engine.set_stage(con, aid, "applied", actor="form", reason="applied again", force=True)
        a = engine.applicant(con, aid)
        reopened = True
    # Already in the pipeline and engaged (a channel linked, an interview begun, or past screening):
    # a repeat submit does not restart them or re-send onboarding. Their details are already upserted.
    if not created and not reopened:
        app = db.one(con, "SELECT interview_state FROM application WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (aid,))
        engaged = (a.get("telegram_chat_id") or a.get("discord_channel_id") or a.get("discord_user_id")
                   or (app and app["interview_state"]) or a["stage"] not in ("applied", "screened_in"))
        if app and engaged:
            return aid
    engine.save_application(con, aid, {**clean, "channel_code": code})
    if a["stage"] == "applied":
        engine.screen(con, aid)
    a = engine.applicant(con, aid)
    if a["stage"] != "screened_out":
        link = telegram_link_fn(aid) if telegram_link_fn else ""
        sam.first_contact(con, aid, telegram_link=link if a.get("telegram") and not a.get("telegram_chat_id") else "",
                          discord_invite=discord_invite if not a.get("discord_user_id") else "")
    return aid


@app.post("/apply/{code}", response_class=HTMLResponse)
async def apply_submit(code: str, request: Request):
    code = re.sub(r"[^a-z0-9_-]", "", code.lower())[:40] or "direct"
    raw = await request.form()
    form = {k: raw.getlist(k) if k == "available_shifts" else raw.get(k) for k in raw.keys()}
    clean, errors = validate_application(form)
    if errors:
        return templates.TemplateResponse(request, "apply.html", {"request": request, **form_ctx(code, form, errors)}, status_code=400)
    with db.db() as con:
        aid = submit_application(con, code, clean, telegram_link_fn=CH.telegram.deep_link, discord_invite=db.setting(con, "discord_invite_url", ""))
        try:
            CH.announce_applicant(con, aid)
        except Exception as e:  # noqa: BLE001
            log.warning("announce failed: %s", e)
    return RedirectResponse(f"/applied/{aid}/{applied_token(aid)}", status_code=303)


@app.get("/applied/{aid}/{token}", response_class=HTMLResponse)
def applied(aid: int, token: str, request: Request):
    if not hmac.compare_digest(token, applied_token(aid)):
        raise HTTPException(404)
    with db.db() as con:
        a = applicant_or_404(con, aid)
        last = db.one(con, "SELECT channel, status FROM message WHERE applicant_id=? AND direction='out' ORDER BY id DESC LIMIT 1", (aid,))
        invite = db.setting(con, "discord_invite_url", "")
    tg = CH.telegram.deep_link(aid) if (a.get("telegram") or not last or last["status"] != "sent") else ""
    return templates.TemplateResponse(request, "applied.html", {"request": request, "a": a, "tg_link": tg, "discord_invite": invite if not a.get("discord_user_id") else "",
                                                                "sent_on": (last or {}).get("channel") if last and last["status"] == "sent" else "",
                                                                "screened_out": a["stage"] == "screened_out"})


# ---------------------------------------------------------------- public: Telegram webhook

@app.post("/hooks/telegram/{secret}")
async def telegram_hook(secret: str, request: Request):
    expected = CH.telegram.secret
    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(404)
    hdr = request.headers.get("x-telegram-bot-api-secret-token", "")
    if hdr and not hmac.compare_digest(hdr, expected):
        raise HTTPException(403)
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})
    try:
        with db.db() as con:
            CH.telegram.handle_update(con, update if isinstance(update, dict) else {})
    except Exception as e:  # noqa: BLE001
        log.exception("telegram update failed: %s", e)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- staff

def _rows(con, where: str = "1=1", args: tuple = ()) -> list[dict]:
    return db.q(con, f"""SELECT a.*, (SELECT COUNT(*) FROM escalation e WHERE e.applicant_id=a.id AND e.status='open') AS esc_open,
                         (SELECT start_at FROM interview i WHERE i.applicant_id=a.id AND i.status IN ('booked','rescheduled') ORDER BY start_at LIMIT 1) AS interview_at,
                         (SELECT COUNT(*) FROM message m WHERE m.applicant_id=a.id AND m.direction='in' AND m.created_at > COALESCE(a.last_outbound_at, '')) AS unanswered
                         FROM applicant a WHERE {where} ORDER BY a.stage_changed_at DESC LIMIT 2000""", args)


@app.get("/staff", response_class=HTMLResponse)
def board(request: Request):
    with db.db() as con:
        rows = _rows(con)
        by_code = db.q(con, "SELECT channel_code, COUNT(*) n FROM applicant GROUP BY channel_code ORDER BY n DESC")
    cols = {s: [] for s in db.STAGES}
    for r in rows:
        cols.setdefault(r["stage"], []).append(r)
    return page("board.html", request, cols=cols, n=len(rows), by_code=by_code)


@app.get("/staff/applicant/{aid}", response_class=HTMLResponse)
def applicant_page(aid: int, request: Request):
    with db.db() as con:
        a = applicant_or_404(con, aid)
        app_row = engine.latest_application(con, aid)
        msgs = engine.messages(con, aid)
        hist = engine.stage_history(con, aid)
        escs = db.q(con, "SELECT * FROM escalation WHERE applicant_id=? ORDER BY id DESC", (aid,))
        iv = engine.open_interview(con, aid)
        past = db.q(con, "SELECT i.*, m.name AS manager_name FROM interview i JOIN manager m ON m.id=i.manager_id WHERE i.applicant_id=? AND i.status NOT IN ('booked','rescheduled') ORDER BY i.start_at DESC", (aid,))
        cost = db.one(con, "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) usd, COALESCE(SUM(tokens_in),0) ti, COALESCE(SUM(tokens_out),0) tout FROM llm_call WHERE applicant_id=?", (aid,))
        open_slots = engine.slots(con, a["timezone"])[:12]
        apps = db.q(con, "SELECT id, channel_code, created_at FROM application WHERE applicant_id=? ORDER BY id DESC", (aid,))
        tg_link = CH.telegram.deep_link(aid)
        invite = db.setting(con, "discord_invite_url", "")
    answers = db.jl((app_row or {}).get("interview_answers"), {})
    qa = [(q, answers.get(f"q{i + 1}", "")) for i, q in enumerate(texts.INTERVIEW_QUESTIONS)]
    return page("applicant.html", request, a=a, app=app_row, msgs=msgs, hist=hist, escs=escs, iv=iv, past=past, cost=cost, open_slots=open_slots,
                apps=apps, reasons=db.jl(a.get("screen_reasons"), []), tg_link=tg_link, discord_invite=invite, TIMEZONES=TIMEZONES,
                qa=qa, grade=db.jl((app_row or {}).get("interview_grade"), {}), iv_state=(app_row or {}).get("interview_state") or "")


@app.post("/staff/applicant/{aid}/stage")
def applicant_stage(aid: int, request: Request, stage: str = Form(...), reason: str = Form("")):
    with db.db() as con:
        applicant_or_404(con, aid)
        if stage not in db.STAGES:
            raise HTTPException(400, "unknown stage")
        a = engine.applicant(con, aid)
        engine.set_stage(con, aid, stage, actor=actor(request), reason=reason.strip() or "set by staff", force=True)
        first = (a["name"] or "").split(" ")[0]
        if stage == "rejected" and a["stage"] != "rejected":
            engine.deliver(con, aid, texts.rejected(first), kind="decline", actor=actor(request))
        elif stage == "passed" and a["stage"] != "passed":
            res = sam.run_tool(con, aid, "list_slots", {})
            if res.get("slots"):
                lines = [f"{i + 1}. {s['local']} ({s['uk']})" for i, s in enumerate(res["slots"])]
                engine.deliver(con, aid, f"Good news, {first}: you're through to the final interview, a 30-minute mock chat with a chat manager. Open times:\n" + "\n".join(lines) + "\nReply with the number you want and I'll book it.", kind="slots")
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.post("/staff/applicant/{aid}/edit")
async def applicant_edit(aid: int, request: Request):
    form = dict(await request.form())
    with db.db() as con:
        applicant_or_404(con, aid)
        sets, args = [], []
        for k in ("name", "email", "telegram", "discord", "phone", "timezone", "notes"):
            if k in form:
                v = form[k].strip()
                if k == "email":
                    v = engine.norm_email(v)
                elif k in ("telegram", "discord"):
                    v = engine.norm_handle(v)
                elif k == "phone":
                    v = engine.norm_phone(v)
                elif k == "timezone":
                    v = engine.valid_tz(v) or db.DEFAULT_APPLICANT_TZ
                sets.append(f"{k}=?")
                args.append(v)
        sets.append("do_not_contact=?")
        args.append(1 if form.get("do_not_contact") in ("1", "on") else 0)
        try:
            con.execute(f"UPDATE applicant SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, db.now(), aid))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"could not save: {e}")
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.post("/staff/applicant/{aid}/send")
def applicant_send(aid: int, request: Request, text: str = Form(...), channel: str = Form("")):
    with db.db() as con:
        applicant_or_404(con, aid)
        if text.strip():
            engine.deliver(con, aid, text.strip(), kind="staff", channel=channel or None, actor=actor(request))
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.post("/staff/applicant/{aid}/interview")
def applicant_interview(aid: int, request: Request, action: str = Form(...), start: str = Form("")):
    with db.db() as con:
        applicant_or_404(con, aid)
        who = actor(request)
        try:
            if action == "book":
                if not engine.can_book(con, engine.applicant(con, aid)):
                    engine.set_stage(con, aid, "passed", actor=who, reason="booked by staff", force=True)
                engine.book(con, aid, start, actor=who)
            elif action == "cancel":
                engine.cancel(con, aid, actor=who, reason="cancelled by staff")
            elif action in ("done", "no_show"):
                engine.interview_outcome(con, aid, action, actor=who)
        except (engine.SlotTaken, engine.NotAvailable) as e:
            raise HTTPException(400, str(e))
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.post("/staff/applicant/{aid}/interview-override")
def applicant_interview_override(aid: int, request: Request, action: str = Form(...)):
    with db.db() as con:
        applicant_or_404(con, aid)
        if action in ("pass", "fail"):
            interview.override(con, aid, action, actor(request))
        elif action == "restart":
            interview.restart(con, aid)
        elif action == "start":
            interview.maybe_start(con, aid, engine.pick_channel(engine.applicant(con, aid)) or "")
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.post("/staff/applicant/{aid}/fasttrack")
def applicant_fasttrack(aid: int, request: Request):
    channels_mod.staff_action(aid, "fasttrack", actor(request))
    return RedirectResponse(f"/staff/applicant/{aid}", status_code=303)


@app.get("/staff/escalations", response_class=HTMLResponse)
def escalations(request: Request):
    with db.db() as con:
        rows = db.q(con, "SELECT e.*, a.name, a.stage FROM escalation e JOIN applicant a ON a.id=e.applicant_id ORDER BY CASE e.status WHEN 'open' THEN 0 ELSE 1 END, e.id DESC LIMIT 300")
    return page("escalations.html", request, rows=rows)


@app.post("/staff/escalations/{eid}/answer")
def escalation_answer(eid: int, request: Request, answer: str = Form(...), save_knowledge: str = Form(""), next: str = Form("")):
    with db.db() as con:
        engine.answer_escalation(con, eid, answer, actor(request), save_knowledge=save_knowledge in ("1", "on"))
    return RedirectResponse(next or "/staff/escalations", status_code=303)


@app.post("/staff/escalations/{eid}/close")
def escalation_close(eid: int, request: Request, next: str = Form("")):
    with db.db() as con:
        engine.close_escalation(con, eid, actor(request))
    return RedirectResponse(next or "/staff/escalations", status_code=303)


@app.get("/staff/knowledge", response_class=HTMLResponse)
def knowledge(request: Request):
    with db.db() as con:
        items = db.q(con, "SELECT * FROM knowledge_item ORDER BY active DESC, id")
    return page("knowledge.html", request, items=items)


@app.post("/staff/knowledge")
def knowledge_add(request: Request, question: str = Form(...), answer: str = Form(...), key: str = Form("")):
    with db.db() as con:
        if question.strip() and answer.strip():
            k = re.sub(r"[^a-z0-9_]", "", key.lower())[:40]
            con.execute("INSERT INTO knowledge_item (key, question, answer, active, updated_by, updated_at) VALUES (?,?,?,1,?,?)",
                        (k, question.strip()[:500], answer.strip()[:4000], actor(request), db.now()))
    return RedirectResponse("/staff/knowledge", status_code=303)


@app.post("/staff/knowledge/{kid}")
def knowledge_save(kid: int, request: Request, action: str = Form("save"), question: str = Form(""), answer: str = Form("")):
    with db.db() as con:
        if action == "delete":
            con.execute("DELETE FROM knowledge_item WHERE id=?", (kid,))
        elif action == "toggle":
            con.execute("UPDATE knowledge_item SET active=1-active, updated_by=?, updated_at=? WHERE id=?", (actor(request), db.now(), kid))
        elif question.strip() and answer.strip():
            con.execute("UPDATE knowledge_item SET question=?, answer=?, updated_by=?, updated_at=? WHERE id=?",
                        (question.strip()[:500], answer.strip()[:4000], actor(request), db.now(), kid))
    return RedirectResponse("/staff/knowledge", status_code=303)


@app.get("/staff/availability", response_class=HTMLResponse)
def availability(request: Request):
    with db.db() as con:
        mgrs = engine.managers(con, active_only=False)
        grid = db.q(con, "SELECT av.*, m.name FROM availability av JOIN manager m ON m.id=av.manager_id ORDER BY av.manager_id, av.weekday, av.start")
        upcoming = db.q(con, "SELECT i.*, a.name AS applicant_name, a.timezone AS applicant_tz, m.name AS manager_name FROM interview i JOIN applicant a ON a.id=i.applicant_id JOIN manager m ON m.id=i.manager_id WHERE i.status IN ('booked','rescheduled') AND i.end_at>=? ORDER BY i.start_at", (db.now(),))
        preview = engine.slots(con, db.HOUSE_TZ)
    return page("availability.html", request, mgrs=mgrs, grid=grid, upcoming=upcoming, preview=preview, TIMEZONES=TIMEZONES)


@app.post("/staff/availability")
def availability_add(request: Request, manager_id: int = Form(...), weekday: str = Form(...), start: str = Form(...), end: str = Form(...)):
    with db.db() as con:
        m = db.one(con, "SELECT * FROM manager WHERE id=?", (manager_id,))
        if not m:
            raise HTTPException(404)
        if not (re.match(r"^\d{1,2}:\d{2}$", start) and re.match(r"^\d{1,2}:\d{2}$", end)):
            raise HTTPException(400, "times as HH:MM")
        days = [int(d) for d in weekday.split(",") if d.strip().isdigit() and 0 <= int(d) <= 6]
        for d in days:
            con.execute("INSERT INTO availability (manager_id, weekday, start, end, tz) VALUES (?,?,?,?,?)", (manager_id, d, start.zfill(5), end.zfill(5), m["timezone"]))
    return RedirectResponse("/staff/availability", status_code=303)


@app.post("/staff/availability/{rid}/delete")
def availability_delete(rid: int, request: Request):
    with db.db() as con:
        con.execute("DELETE FROM availability WHERE id=?", (rid,))
    return RedirectResponse("/staff/availability", status_code=303)


@app.post("/staff/managers/{mid}")
def manager_save(mid: int, request: Request, name: str = Form(...), discord: str = Form(""), telegram: str = Form(""), email: str = Form(""),
                 timezone_: str = Form("", alias="timezone"), active: str = Form("")):
    with db.db() as con:
        tz = engine.valid_tz(timezone_) or db.DEFAULT_APPLICANT_TZ
        con.execute("UPDATE manager SET name=?, discord=?, telegram=?, email=?, timezone=?, active=? WHERE id=?",
                    (name.strip()[:80], engine.norm_handle(discord) or "", engine.norm_handle(telegram) or "", engine.norm_email(email) or "", tz, 1 if active in ("1", "on") else 0, mid))
        con.execute("UPDATE availability SET tz=? WHERE manager_id=?", (tz, mid))
    return RedirectResponse("/staff/availability", status_code=303)


@app.post("/staff/managers")
def manager_add(request: Request, name: str = Form(...), discord: str = Form(""), timezone_: str = Form("Asia/Manila", alias="timezone")):
    with db.db() as con:
        if name.strip():
            con.execute("INSERT INTO manager (name, discord, timezone, active, created_at) VALUES (?,?,?,1,?)",
                        (name.strip()[:80], engine.norm_handle(discord) or "", engine.valid_tz(timezone_) or db.DEFAULT_APPLICANT_TZ, db.now()))
    return RedirectResponse("/staff/availability", status_code=303)


@app.get("/staff/settings", response_class=HTMLResponse)
def settings(request: Request):
    with db.db() as con:
        s = {r["key"]: r["value"] for r in db.q(con, "SELECT * FROM settings")}
        runs = db.q(con, "SELECT * FROM worker_runs ORDER BY id DESC LIMIT 20")
        llm = db.one(con, "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) usd FROM llm_call WHERE created_at>=?", (db.fmt_ts(datetime.now(timezone.utc) - timedelta(days=30)),))
        failed = db.q(con, "SELECT m.*, a.name FROM message m JOIN applicant a ON a.id=m.applicant_id WHERE m.status IN ('failed','held') ORDER BY m.id DESC LIMIT 20")
    health = {"sam": sam.configured(), "telegram": CH.telegram.configured, "discord": CH.discord.configured, "email": CH.email.configured,
              "imap": CH.email.imap_configured, "gateway": bool(GATEWAY_TOKEN)}
    discord_ids = [(k, s.get(k, "")) for k in db.DISCORD_SETTING_KEYS]
    return page("settings.html", request, s=s, runs=runs, llm=llm, failed=failed, health=health, models=sam.models(), prompt_version=sam.PROMPT_VERSION,
                discord_ids=discord_ids)


@app.post("/staff/settings")
async def settings_save(request: Request):
    form = dict(await request.form())
    with db.db() as con:
        for k in db.DEFAULT_SETTINGS:
            if k in form:
                v = form[k].strip()
                if k.startswith("vacancies_") or k in ("ghost_nudges", "ghost_days", "slot_minutes", "slot_days", "slot_lead_hours", "score_fast_track", "score_low"):
                    v = str(max(0, engine._int(v)))
                if k in ("book_from_screened_in", "interview_enabled"):
                    v = "1" if v in ("1", "on") else "0"
                if k.startswith("discord_") and k != "discord_invite_url":
                    continue
                db.set_setting(con, k, v)
        for flag in ("book_from_screened_in", "interview_enabled"):
            if flag not in form and "vacancies_uk_00_08" in form:
                db.set_setting(con, flag, "0")
    return RedirectResponse("/staff/settings", status_code=303)


# ---------------------------------------------------------------- worker

def _log_run(kind: str, fn):
    with db.db() as con:
        cur = con.execute("INSERT INTO worker_runs (kind, started_at) VALUES (?,?)", (kind, db.now()))
        rid = cur.lastrowid
        try:
            out = fn(con)
            con.execute("UPDATE worker_runs SET finished_at=?, ok=1, notes=? WHERE id=?", (db.now(), json.dumps(out)[:500], rid))
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", kind)
            con.execute("UPDATE worker_runs SET finished_at=?, ok=0, notes=? WHERE id=?", (db.now(), str(e)[:500], rid))
        con.execute("DELETE FROM worker_runs WHERE id < (SELECT MAX(id) FROM worker_runs) - 500")


def _loop():
    tick_s = int(os.environ.get("HIRE_TICK_SECONDS", "30") or 30)
    imap_s = int(os.environ.get("HIRE_IMAP_SECONDS", "120") or 120)
    polling = os.environ.get("HIRE_TELEGRAM_POLL", "0") == "1"
    last_imap = 0.0
    if CH.telegram.configured:
        try:
            if polling:
                CH.telegram.delete_webhook()
            else:
                CH.telegram.set_webhook()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram webhook setup failed: %s", e)
    CH.discord.start()
    while True:
        _log_run("tick", lambda con: engine.tick(con))
        report.worker_tick(CH.discord)
        if CH.email.imap_configured and time.time() - last_imap >= imap_s:
            last_imap = time.time()
            _log_run("imap", CH.email.poll)
        if polling and CH.telegram.configured:
            for _ in range(max(1, tick_s // 3)):
                _log_run("telegram_poll", lambda con: {"handled": CH.telegram.poll(con)})
                time.sleep(3)
        else:
            time.sleep(tick_s)


if os.environ.get("HIRE_NO_WORKER") != "1":
    threading.Thread(target=_loop, name="hire-worker", daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8140")))
