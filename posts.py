"""Sourcing: one canonical job spec (settings, editable on /staff/posts) rendered per channel with that channel's constraints,
a paste queue with hand-recorded "Posted" dates and refresh cadences, and code-word attribution for applicants who arrive
through boards that allow no links (OLJ, ofmjobs).

posts.spec(con)                 -> the spec dict (settings with defaults)
posts.render(con, channel)      -> {"channel", "title", "body", "fields", "apply_url", "code_word", "notes", "violations"}
posts.render_spec(spec, channel, public_url, bot_username) -> same, pure (tests)
posts.check(channel, rendered)  -> list of constraint violations (empty = fine)
posts.queue(con, now)           -> one row per channel with last posted, next due, due flag
posts.mark_posted(con, channel, by, note)
posts.code_word_channel(text)   -> channel key whose code word appears in text, or None
posts.attribute(con)            -> applicants whose channel_code is empty/direct but who repeated a code word get that channel
posts.ensure_schema(con)        -> post_log table (called from db.init)
Router: GET/POST /staff/posts, POST /staff/posts/{channel}/posted, GET /staff/posts/{channel}.txt
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db
import engine
import texts

log = logging.getLogger("hire.posts")
HERE = os.path.dirname(os.path.abspath(__file__))
router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

# ---------------------------------------------------------------- the spec

SPEC_DEFAULTS = {
    "post_title": "Remote Chat Specialist - Weekly Pay - Paid Training",
    "post_about": "We are Beloura, a UK-based agency running social and fan-page accounts for online creators. Our chat team talks "
                  "with fans over text all day: building rapport, answering questions and selling content.",
    "post_role": "Hold several text conversations at once, reply quickly and warmly, follow our playbooks and spot the moments a "
                 "friendly reply can turn into a sale. Adult-themed creator content; you must be comfortable with that.",
    "post_pay": "$2/hr base + 2% commission, paid weekly Thursdays via PayPal/Wise",
    "post_shifts": "UK 00-08 / 08-16 / 16-00, shown in PH time",
    "post_shifts_per_week": "3-6 shifts a week, chosen from your availability",
    "post_requirements": "18 or over; a laptop or desktop (no phone-only); stable internet; strong written English; 45+ WPM typing",
    "post_training": "Training paid once you pass it; first payout one or two Thursdays after you start",
    "post_tools": "Infloww and Discord",
    "post_contractor": "Independent contractor, 7 days notice either way",
    "post_olj_hours_per_week": "24-48",
    "post_olj_employment": "Part Time or Full Time",
    "post_olj_skills": "Chat Support, Customer Support, English Proficiency, Typing",
    "post_code_olj": "FAST FINGERS",
    "post_code_ofmjobs": "CLOSER",
    "post_code_facebook": "PEOPLE PERSON",
    "post_code_reddit": "CONVO PRO",
    "post_code_telegram": "NIGHT OWL",
    "post_cadence_olj": "7",
    "post_cadence_ofmjobs": "14",
    "post_cadence_facebook": "3",
    "post_cadence_reddit": "7",
    "post_cadence_telegram": "7",
}
HOURLY_BASE_USD = 2.0

CHANNELS = ["olj", "ofmjobs", "facebook", "reddit", "telegram"]
CHANNEL_LABELS = {"olj": "OnlineJobs.ph", "ofmjobs": "ofmjobs.com", "facebook": "Facebook groups", "reddit": "Reddit", "telegram": "Telegram channel"}
CHANNEL_RULES = {
    "olj": "No links in the body. Title 80 characters or fewer. Wage per month and hours per week go in the form fields. Applicants reply with their Telegram username and the code word.",
    "ofmjobs": "Apply in-platform only: no external links, no contact details, no handles. Applicants start their application with the code word.",
    "facebook": "Short and casual, one apply link. Rotate groups; repost every few days.",
    "reddit": "Title plus body, no emoji, one apply link. Follow each subreddit's posting rules and flair.",
    "telegram": "Channel post with the apply link and the bot deep link.",
}
CADENCE_HINT = {"olj": "OLJ posts drop off the first page after about a week; refresh every 7 days.",
                "ofmjobs": "Refresh fortnightly or when the listing expires.",
                "facebook": "Rota: a different group every few days, same post.",
                "reddit": "Once a week per subreddit; more reads as spam.",
                "telegram": "Weekly pin in the channel."}
URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(?:ai|com|ph|me|io|net|org|co|uk)\b|t\.me/)", re.I)
CONTACT_RE = re.compile(r"(@[a-z0-9_]{3,}|[\w.+-]+@[\w-]+\.\w+|\+?\d[\d\s-]{8,}\d|t\.me|\b(telegram|whatsapp|discord|skype|viber|signal)\s*(username|handle|id|number|:)"
                        r"|\b(add|message|dm|text|contact|reach)\s+(us|me)\s+(on|at|via)\b)", re.I)
EMOJI_RE = re.compile("[\\U0001F300-\\U0001FAFF\\u2600-\\u27BF]")
AI_RE = re.compile(r"\b(AI|A\.I\.|artificial intelligence|chatbot|bot|automated|assistant|GPT|LLM)\b")
OLJ_TITLE_MAX = 80


def public_url(path: str = "") -> str:
    return os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai").rstrip("/") + path


def bot_username() -> str:
    return (os.environ.get("TELEGRAM_BOT_USERNAME", "") or "beloura_hiring_bot").lstrip("@")


def spec(con) -> dict:
    out = {}
    for k, v in SPEC_DEFAULTS.items():
        out[k] = db.setting(con, k, v) or v
    return out


def save_spec(con, form: dict) -> None:
    for k in SPEC_DEFAULTS:
        if k in form:
            v = str(form[k]).strip()
            if k.startswith("post_cadence_"):
                v = str(max(1, engine._int(v))) if v.strip().isdigit() else SPEC_DEFAULTS[k]
            if k.startswith("post_code_"):
                v = re.sub(r"[^A-Z0-9 ]", "", v.upper()).strip() or SPEC_DEFAULTS[k]
            db.set_setting(con, k, v)


def code_word(sp: dict, channel: str) -> str:
    return (sp.get(f"post_code_{channel}") or SPEC_DEFAULTS.get(f"post_code_{channel}") or channel.upper()).strip()


def cadence_days(sp: dict, channel: str) -> int:
    try:
        return max(1, int(sp.get(f"post_cadence_{channel}") or SPEC_DEFAULTS[f"post_cadence_{channel}"]))
    except (ValueError, KeyError):
        return 7


def olj_wage_per_month(sp: dict) -> str:
    """OLJ asks for a monthly salary: base hours x $2 x 52/12, as a range when the hours are a range."""
    hrs = re.findall(r"\d+", sp.get("post_olj_hours_per_week", "") or "")
    if not hrs:
        return "USD 208-416/month base + 2% commission"
    vals = sorted({round(int(h) * HOURLY_BASE_USD * 52 / 12) for h in hrs[:2]})
    rng = f"{vals[0]}" if len(vals) == 1 else f"{vals[0]}-{vals[1]}"
    return f"USD {rng}/month base + 2% commission"


# ---------------------------------------------------------------- renderers

def _common_lines(sp: dict) -> list[str]:
    return [
        "What you get:",
        f"- Pay: {sp['post_pay']}",
        f"- Shifts: {sp['post_shifts']}; {sp['post_shifts_per_week']}",
        f"- {sp['post_training']}",
        f"- Tools we use: {sp['post_tools']} (we show you both)",
        f"- {sp['post_contractor']}",
        "",
        "What you need:",
        f"- {sp['post_requirements']}",
    ]


def _render_olj(sp: dict, url: str, bot: str) -> dict:
    cw = code_word(sp, "olj")
    body = "\n".join([
        sp["post_about"],
        "",
        "The role: " + sp["post_role"],
        "",
        *_common_lines(sp),
        "",
        f"TO APPLY: put \"{cw}\" in the subject line and reply with your Telegram username, your typing speed (WPM) and the shifts you can work. "
        "We send the short application form by Telegram within a day. Applications without the code word or a Telegram username are not read.",
    ])
    return {"title": sp["post_title"][:OLJ_TITLE_MAX], "body": body, "code_word": cw, "apply_url": url + "/apply/olj",
            "fields": {"Employment type": sp["post_olj_employment"], "Hours per week": sp["post_olj_hours_per_week"],
                       "Salary per month": olj_wage_per_month(sp), "Skills": sp["post_olj_skills"]},
            "notes": ["Paste the title and body; the fields go in OLJ's own form boxes.",
                      f"When someone replies with {cw} and a Telegram username, send them {url}/apply/olj."]}


def _render_ofmjobs(sp: dict, url: str, bot: str) -> dict:
    cw = code_word(sp, "ofmjobs")
    body = "\n".join([
        sp["post_about"],
        "",
        "The role: " + sp["post_role"],
        "",
        *_common_lines(sp),
        "",
        f"TO APPLY: apply here on the platform and start your message with the word {cw}, then tell us your typing speed (WPM) and the shifts you can work. "
        "We reply to every application that includes the code word.",
    ])
    return {"title": sp["post_title"], "body": body, "code_word": cw, "apply_url": url + "/apply/ofmjobs", "fields": {},
            "notes": ["In-platform apply only, per ofmjobs terms: the body carries no links or contact details.",
                      f"Reply to applicants inside ofmjobs with {url}/apply/ofmjobs."]}


def _render_facebook(sp: dict, url: str, bot: str) -> dict:
    cw = code_word(sp, "facebook")
    link = url + "/apply/facebook"
    body = "\n".join([
        f"Hiring: remote chat specialists (work from home, PH-friendly shifts).",
        "",
        f"Pay is {sp['post_pay']}. {sp['post_shifts_per_week']}, shifts {sp['post_shifts']}. {sp['post_training']}.",
        f"You need: {sp['post_requirements']}. Adult-themed creator content, so be comfortable with that.",
        "",
        f"Apply here (2 minutes): {link}",
        f"Mention the word {cw} in the form so we know you came from this group.",
    ])
    return {"title": sp["post_title"], "body": body, "code_word": cw, "apply_url": link, "fields": {},
            "notes": ["Short on purpose; groups bury long posts. Rotate groups rather than reposting in one."]}


def _render_reddit(sp: dict, url: str, bot: str) -> dict:
    cw = code_word(sp, "reddit")
    link = url + "/apply/reddit"
    title = f"[Hiring] {sp['post_title']} - remote, {sp['post_shifts_per_week'].split(',')[0]}"
    body = "\n".join([
        sp["post_about"],
        "",
        sp["post_role"],
        "",
        *_common_lines(sp),
        "",
        f"How it works: a two-minute form, a short text interview with our recruiting team, then a self-paced online assessment (typing, English, "
        "six short practice chats) and a final call with a chat manager. Everything is in writing except the final call.",
        "",
        f"Apply: {link} (put {cw} in the form so we can see which post you found).",
    ])
    return {"title": title, "body": body, "code_word": cw, "apply_url": link, "fields": {},
            "notes": ["Check the subreddit's hiring flair and rules first; some want [Hiring] in the title, some ban external links."]}


def _render_telegram(sp: dict, url: str, bot: str) -> dict:
    cw = code_word(sp, "telegram")
    link = url + "/apply/telegram"
    deep = f"https://t.me/{bot}"
    body = "\n".join([
        f"Hiring: {sp['post_title']}",
        "",
        f"{sp['post_pay']}. {sp['post_shifts_per_week']}; shifts {sp['post_shifts']}. {sp['post_training']}.",
        f"Need: {sp['post_requirements']}.",
        "",
        f"Apply: {link}",
        f"Or message our recruiting team on Telegram: {deep} (send the word {cw} to start)",
    ])
    return {"title": sp["post_title"], "body": body, "code_word": cw, "apply_url": link, "fields": {"Bot": deep},
            "notes": ["Pin in the channel; replace the pin on each refresh."]}


RENDERERS = {"olj": _render_olj, "ofmjobs": _render_ofmjobs, "facebook": _render_facebook, "reddit": _render_reddit, "telegram": _render_telegram}


def render_spec(sp: dict, channel: str, url: str | None = None, bot: str | None = None) -> dict:
    if channel not in RENDERERS:
        raise KeyError(channel)
    out = RENDERERS[channel](sp, (url or public_url()).rstrip("/"), bot or bot_username())
    out["channel"] = channel
    out["label"] = CHANNEL_LABELS[channel]
    out["rules"] = CHANNEL_RULES[channel]
    out["violations"] = check(channel, out)
    return out


def render(con, channel: str) -> dict:
    return render_spec(spec(con), channel)


def render_all(con) -> list[dict]:
    sp = spec(con)
    return [render_spec(sp, c) for c in CHANNELS]


def check(channel: str, r: dict) -> list[str]:
    """Constraint self-check per channel; the page shows any violation in red, tests assert the list is empty."""
    v = []
    text = f"{r.get('title', '')}\n{r.get('body', '')}"
    if EMOJI_RE.search(text):
        v.append("emoji in text")
    if AI_RE.search(text):
        v.append("mentions AI or a bot")
    if r.get("code_word") and r["code_word"] not in r.get("body", ""):
        v.append("code word missing from the body")
    if channel == "olj":
        if URL_RE.search(r.get("body", "")):
            v.append("link in the body (OLJ strips posts with links)")
        if len(r.get("title", "")) > OLJ_TITLE_MAX:
            v.append(f"title over {OLJ_TITLE_MAX} characters")
        for f in ("Salary per month", "Hours per week"):
            if not (r.get("fields") or {}).get(f):
                v.append(f"missing OLJ field {f}")
        if "telegram username" not in r.get("body", "").lower():
            v.append("does not ask for the Telegram username")
    elif channel == "ofmjobs":
        if URL_RE.search(r.get("body", "")):
            v.append("link in the body (in-platform apply only)")
        if CONTACT_RE.search(r.get("body", "")):
            v.append("contact details in the body (ofmjobs terms)")
    else:
        if r.get("apply_url") and r["apply_url"] not in r.get("body", ""):
            v.append("apply link missing")
        if channel == "telegram" and "t.me/" not in r.get("body", ""):
            v.append("bot deep link missing")
    return v


# ---------------------------------------------------------------- paste queue

def ensure_schema(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS post_log (
        id INTEGER PRIMARY KEY, channel TEXT NOT NULL, posted_at TEXT NOT NULL, posted_by TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')""")
    con.execute("CREATE INDEX IF NOT EXISTS post_log_channel ON post_log (channel, posted_at)")


def mark_posted(con, channel: str, by: str = "staff", note: str = "", at: datetime | None = None) -> int:
    if channel not in CHANNELS:
        raise KeyError(channel)
    ts = db.fmt_ts(at) if at else db.now()
    cur = con.execute("INSERT INTO post_log (channel, posted_at, posted_by, note) VALUES (?,?,?,?)", (channel, ts, by[:60], note[:200]))
    return cur.lastrowid


def last_posted(con, channel: str) -> dict | None:
    return db.one(con, "SELECT * FROM post_log WHERE channel=? ORDER BY posted_at DESC, id DESC LIMIT 1", (channel,))


def due_state(last_at: str | None, days: int, now: datetime | None = None) -> dict:
    """Pure cadence logic: never posted = due; otherwise due once the cadence has elapsed since the last post."""
    now = now or datetime.now(timezone.utc)
    last = db.parse_ts(last_at)
    if not last:
        return {"last": None, "next": None, "due": True, "overdue_days": None, "days_since": None}
    nxt = last + timedelta(days=days)
    over = (now - nxt).total_seconds() / 86400
    return {"last": last, "next": nxt, "due": now >= nxt, "overdue_days": round(over, 1) if over > 0 else 0.0,
            "days_since": round((now - last).total_seconds() / 86400, 1)}


def queue(con, now: datetime | None = None) -> list[dict]:
    sp = spec(con)
    out = []
    for c in CHANNELS:
        lp = last_posted(con, c)
        days = cadence_days(sp, c)
        st = due_state(lp["posted_at"] if lp else None, days, now)
        out.append({"channel": c, "label": CHANNEL_LABELS[c], "cadence_days": days, "cadence_hint": CADENCE_HINT[c], "last_log": lp,
                    "last_at": db.fmt_ts(st["last"]) if st["last"] else None, "next_at": db.fmt_ts(st["next"]) if st["next"] else None,
                    "due": st["due"], "overdue_days": st["overdue_days"], "days_since": st["days_since"],
                    "history": db.q(con, "SELECT * FROM post_log WHERE channel=? ORDER BY posted_at DESC, id DESC LIMIT 6", (c,))})
    return out


# ---------------------------------------------------------------- attribution by code word

def code_words(sp: dict) -> dict:
    return {c: code_word(sp, c) for c in CHANNELS}


def code_word_channel(text: str | None, sp: dict | None = None) -> str | None:
    if not text:
        return None
    words = code_words(sp or SPEC_DEFAULTS)
    t = re.sub(r"\s+", " ", text.upper())
    for c, w in words.items():
        if w and re.search(r"(?<![A-Z])" + re.escape(w) + r"(?![A-Z])", t):
            return c
    return None


def attribute(con, apply: bool = True) -> dict:
    """Applicants who arrived through /apply/direct (or with no channel) but repeated a channel's code word in the form or a message
    get that channel as channel_code, so the funnel report counts them where they came from. Returns {aid: channel}."""
    sp = spec(con)
    found = {}
    rows = db.q(con, "SELECT id, channel_code FROM applicant WHERE channel_code IN ('', 'direct')")
    for a in rows:
        aid = a["id"]
        app = db.one(con, "SELECT why, experience_text, raw FROM application WHERE applicant_id=? ORDER BY id DESC LIMIT 1", (aid,)) or {}
        texts_ = [app.get("why", ""), app.get("experience_text", ""), app.get("raw", "")]
        texts_ += [m["body"] for m in db.q(con, "SELECT body FROM message WHERE applicant_id=? AND direction='in' ORDER BY id LIMIT 10", (aid,))]
        ch = code_word_channel("\n".join(t for t in texts_ if t), sp)
        if ch:
            found[aid] = ch
            if apply:
                con.execute("UPDATE applicant SET channel_code=?, updated_at=? WHERE id=?", (ch, db.now(), aid))
                engine.meta_set(con, aid, "code_word", {"word": code_word(sp, ch), "channel": ch})
    return found


# ---------------------------------------------------------------- staff pages

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
templates.env.globals.update(APP_NAME="Beloura Hire", AGENT=texts.AGENT_NAME, STAGES=db.STAGES, PIPELINE=db.PIPELINE, SIDE=db.SIDE,
                             STAGE_LABELS=db.STAGE_LABELS, SHIFT_LABELS=db.SHIFT_LABELS, CHANNELS=CHANNELS, CHANNEL_LABELS=CHANNEL_LABELS,
                             SPEC_DEFAULTS=SPEC_DEFAULTS)


def _ctx(request: Request, **extra) -> dict:
    with db.db() as con:
        esc_n = (db.one(con, "SELECT COUNT(*) n FROM escalation WHERE status='open'") or {}).get("n", 0)
        worker = db.one(con, "SELECT * FROM worker_runs WHERE kind='tick' ORDER BY id DESC LIMIT 1")
    return {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path, "query": dict(request.query_params),
            "esc_n": esc_n, "worker": worker, **extra}


def _page(name: str, request: Request, status_code: int = 200, **extra):
    return templates.TemplateResponse(request, name, _ctx(request, **extra), status_code=status_code)


def _who(request: Request) -> str:
    return ((getattr(request.state, "user", None) or {}).get("user")) or "staff"


def _is_admin(request: Request) -> bool:
    return ((getattr(request.state, "user", None) or {}).get("role")) == "admin"


@router.get("/staff/posts")
def posts_page(request: Request):
    with db.db() as con:
        sp = spec(con)
        rendered = render_all(con)
        rows = queue(con)
        attributed = db.q(con, "SELECT channel_code, COUNT(*) n FROM applicant WHERE channel_code<>'' GROUP BY channel_code ORDER BY n DESC")
    by_ch = {r["channel"]: r for r in rendered}
    for r in rows:
        r["post"] = by_ch[r["channel"]]
    return _page("posts.html", request, sp=sp, rows=rows, attributed=attributed, public=public_url(), bot=bot_username(),
                 saved=request.query_params.get("saved"), is_admin=_is_admin(request))


@router.post("/staff/posts")
async def posts_save(request: Request):
    if not _is_admin(request):
        return _page("forbidden.html", request, status_code=403, who={"name": (getattr(request.state, "user", None) or {}).get("name", "")})
    form = dict(await request.form())
    with db.db() as con:
        save_spec(con, form)
    return RedirectResponse("/staff/posts?saved=1", status_code=303)


@router.post("/staff/posts/{channel}/posted")
async def posts_mark(channel: str, request: Request):
    if channel not in CHANNELS:
        raise HTTPException(404)
    form = dict(await request.form())
    with db.db() as con:
        mark_posted(con, channel, by=_who(request), note=str(form.get("note", ""))[:200])
    return RedirectResponse("/staff/posts#" + channel, status_code=303)


@router.post("/staff/posts/attribute")
def posts_attribute(request: Request):
    with db.db() as con:
        found = attribute(con)
    return RedirectResponse(f"/staff/posts?attributed={len(found)}", status_code=303)


@router.get("/staff/posts/{channel}.txt", response_class=PlainTextResponse)
def posts_text(channel: str):
    if channel not in CHANNELS:
        raise HTTPException(404)
    with db.db() as con:
        r = render(con, channel)
    head = [r["title"], ""]
    if r["fields"]:
        head += [f"{k}: {v}" for k, v in r["fields"].items()] + [""]
    return "\n".join(head) + r["body"] + "\n"
