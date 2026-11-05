"""The point of contact (James by default, see texts.AGENT_NAME). One reply() per inbound message: knowledge base + tools over OpenRouter's chat completions.

OPENROUTER_API_KEY empty -> dry-run: deterministic canned replies driven by keywords, same tools, so tests and local runs work offline.
Every model call is logged to llm_call with tokens and cost (OpenRouter's usage.cost when present, else SAM_PRICES per model).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

import db
import engine
import texts

log = logging.getLogger("hire.sam")

PROMPT_VERSION = "sam-2026-11-04.1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
DEFAULT_FALLBACKS = "deepseek/deepseek-chat"
MAX_TOOL_ROUNDS = 6
HISTORY = 24

SYSTEM_PROMPT = """You are {agent}, Beloura's recruiting assistant for the chatter role. You are automated, and people on the team review every decision; say so plainly if asked whether you are a bot or who decides.

Voice: short, warm, plain English. No emoji. No markdown: no asterisks, no bold, no headings, no bullet symbols, plain sentences and plain numbered lines only. No fluff, no exclamation marks in a row, no corporate phrases. One to four sentences is usually right. Address the applicant by first name once in a while, not every message.

Hard rules:
- Never promise a job, a start date, a pay rise or any exception. Decisions are made by the team after the assessment and the final interview.
- Never discuss other applicants or how many people applied.
- Only state facts that are in the knowledge base below or in the tool results. If a question is not covered, say you will check with the team, call the escalate tool with the question, and tell the applicant you will come back here. Do not guess.
- Escalate also on: abuse, distress, a complaint, a pay or legal dispute, or when the applicant asks for a person.
- Times: the applicant's timezone is given below. Whenever you mention a time, show it in their timezone and in UK time, like "Thu 6 Nov 20:00 Philippine time (12:00 UK time)". Use list_slots to offer times; never invent a slot. Offer at most three slots at a time unless asked for more. Book only with book_slot using the exact start value from list_slots.
- If they say what timezone they are in and it differs from the one on file, call set_timezone first.
- "Where am I" or "what happens next" is answered from get_status.
- Collect nothing beyond what the application asked for. No ID documents, no payment details in chat.
- You cannot change their stage, score or result. Say the team reviews that.

Knowledge base (the only facts you may state):
{knowledge}

Applicant: {name}. Timezone on file: {timezone}. Current stage: {stage_label}. Next step: {next_step}
Today is {today}."""

TOOLS = [
    {"type": "function", "function": {"name": "get_status", "description": "Where the applicant is in the process, the next step, and any booked interview.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_knowledge", "description": "Look up answers in the knowledge base by keywords.",
                                      "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_slots", "description": "Open 30-minute final-interview slots over the next 7 days, in the applicant's timezone and UK time.",
                                      "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "How many to return, default 6."}}}}},
    {"type": "function", "function": {"name": "book_slot", "description": "Book a final interview at one of the open slots. Pass the exact 'start' value from list_slots.",
                                      "parameters": {"type": "object", "properties": {"start": {"type": "string"}}, "required": ["start"]}}},
    {"type": "function", "function": {"name": "reschedule", "description": "Move the booked final interview to another open slot. Pass the exact 'start' value from list_slots.",
                                      "parameters": {"type": "object", "properties": {"start": {"type": "string"}}, "required": ["start"]}}},
    {"type": "function", "function": {"name": "cancel", "description": "Cancel the booked final interview.",
                                      "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "escalate", "description": "Hand a question or situation to a person on the team. Use for anything not in the knowledge base, abuse, distress, complaints, disputes, or a request for a human.",
                                      "parameters": {"type": "object", "properties": {"reason": {"type": "string"}, "question": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "set_timezone", "description": "Record the applicant's timezone as an IANA name such as Asia/Manila or Europe/London.",
                                      "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": ["timezone"]}}},
    {"type": "function", "function": {"name": "assessment_link", "description": "Get the applicant's assessment link. Use this whenever they ask for the link, can't find it, or are ready to start the assessment. Give them the exact url it returns.",
                                      "parameters": {"type": "object", "properties": {}}}},
]


def configured() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY", ""))


def models() -> list[str]:
    primary = os.environ.get("SAM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fall = [m.strip() for m in os.environ.get("SAM_FALLBACK_MODELS", DEFAULT_FALLBACKS).split(",") if m.strip()]
    return [primary] + [m for m in fall if m != primary]


def prices() -> dict:
    """SAM_PRICES='anthropic/claude-sonnet-5.5=3,15;deepseek/deepseek-chat=0.3,1.2' as USD per million tokens in,out."""
    out = {}
    for part in os.environ.get("SAM_PRICES", "").split(";"):
        if "=" not in part:
            continue
        m, p = part.split("=", 1)
        try:
            i, o = p.split(",")
            out[m.strip()] = (float(i), float(o))
        except ValueError:
            continue
    return out


def cost_for(model: str, tokens_in: int, tokens_out: int, reported: float | None) -> float:
    if reported is not None:
        return float(reported)
    i, o = prices().get(model, (0.0, 0.0))
    return round(tokens_in * i / 1e6 + tokens_out * o / 1e6, 6)


def log_call(con, aid: int | None, model: str, tokens_in: int, tokens_out: int, cost: float, ok: bool, error: str, latency_ms: int) -> None:
    con.execute("INSERT INTO llm_call (applicant_id, model, prompt_version, tokens_in, tokens_out, cost_usd, ok, error, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, model, PROMPT_VERSION, tokens_in, tokens_out, cost, 1 if ok else 0, error[:300], latency_ms, db.now()))


# ---------------------------------------------------------------- knowledge

def knowledge_items(con) -> list[dict]:
    return db.q(con, "SELECT * FROM knowledge_item WHERE active=1 ORDER BY id")


def knowledge_text(con) -> str:
    return "\n".join(f"- {k['question']} {k['answer']}" for k in knowledge_items(con)) or "- (empty)"


_STOP = {"the", "a", "an", "is", "are", "do", "does", "i", "you", "we", "it", "what", "how", "when", "where", "much", "many", "my", "me",
         "to", "of", "in", "on", "for", "and", "or", "be", "will", "can", "about", "this", "that", "there", "with", "get", "per"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP and len(w) > 1}


SYNONYMS = {"salary": "pay", "wage": "pay", "rate": "pay", "money": "pay", "earn": "pay", "hourly": "pay", "commission": "pay",
            "payout": "payout", "payday": "payout", "paypal": "payout", "wise": "payout", "thursday": "payout", "paid": "payout",
            "schedule": "shifts", "shift": "shifts", "hours": "shifts", "time": "shifts", "night": "shifts",
            "train": "training", "onboarding": "training", "contract": "contract", "employee": "contract", "freelance": "contract",
            "software": "tools", "app": "tools", "infloww": "tools", "discord": "tools",
            "steps": "process", "stage": "process", "long": "process", "next": "process", "interview": "process",
            "job": "role", "chatter": "role", "work": "role", "test": "assessment_tests", "tests": "assessment_tests", "typing": "assessment_tests",
            "start": "start_date", "begin": "start_date", "require": "requirements", "requirements": "requirements", "need": "requirements",
            "notice": "notice", "quit": "notice", "leave": "notice"}


def search_knowledge(con, query: str, limit: int = 3) -> list[dict]:
    qw = _words(query)
    keys = {SYNONYMS.get(w, w) for w in qw}
    scored = []
    for k in knowledge_items(con):
        score = len(qw & _words(k["question"] + " " + k["answer"]))
        if k["key"] and k["key"] in keys:
            score += 3
        if score:
            scored.append((score, k))
    scored.sort(key=lambda t: -t[0])
    return [k for _, k in scored[:limit]]


# ---------------------------------------------------------------- tools

def run_tool(con, aid: int, name: str, args: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    a = engine.applicant(con, aid)
    try:
        if name == "get_status":
            return engine.status_summary(con, aid)
        if name == "get_knowledge":
            hits = search_knowledge(con, str(args.get("query") or ""))
            return {"results": [{"question": h["question"], "answer": h["answer"]} for h in hits], "found": bool(hits)}
        if name == "list_slots":
            if not engine.can_book(con, a):
                return {"slots": [], "error": "booking is not open at this stage", "stage": a["stage"]}
            lim = int(args.get("limit") or 6)
            found = engine.slots(con, a["timezone"], now=now)
            offered = found[:max(1, min(lim, 20))]
            engine.meta_set(con, aid, "offered_slots", [s["start"] for s in offered])
            if not found:
                if not db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND status='open' AND reason='no interview slots'", (aid,)):
                    engine.create_escalation(con, aid, "no interview slots", "Applicant is ready to book a final but the grid has no open slots in the next 7 days.")
            return {"slots": [{"start": s["start"], "local": s["local"], "uk": s["uk"], "manager": s["manager"]} for s in offered], "total_open": len(found)}
        if name in ("book_slot", "reschedule"):
            fn = engine.reschedule if name == "reschedule" else engine.book
            iv = fn(con, aid, str(args.get("start") or ""), actor="sam", now=now, notify=False)
            a = engine.applicant(con, aid)
            return {"ok": True, "start": iv["start_at"], "when": engine.when_both(iv["start_at"], a["timezone"]),
                    "manager": (engine.default_manager(con) or {}).get("name", ""), "status": iv["status"]}
        if name == "cancel":
            ok = engine.cancel(con, aid, actor="sam", reason=str(args.get("reason") or ""), notify=False)
            return {"ok": ok} if ok else {"ok": False, "error": "no interview is booked"}
        if name == "escalate":
            eid = engine.create_escalation(con, aid, str(args.get("reason") or "question"), str(args.get("question") or ""))
            return {"ok": True, "escalation_id": eid, "say": "Tell the applicant you will check with the team and come back here."}
        if name == "set_timezone":
            tz = engine.valid_tz(str(args.get("timezone") or ""))
            if not tz:
                return {"ok": False, "error": "not a valid IANA timezone"}
            con.execute("UPDATE applicant SET timezone=?, updated_at=? WHERE id=?", (tz, db.now(), aid))
            return {"ok": True, "timezone": tz}
        if name == "assessment_link":
            import assess
            st = None
            try:
                st = assess.get(con, aid)
            except Exception:  # noqa: BLE001
                st = None
            if st and st.get("status") == "done":
                return {"ok": False, "error": "the assessment is already finished", "say": "Tell them they have already completed the assessment."}
            if a["stage"] in ("assessment_sent", "assessment_in_progress") or (st and st.get("status") in ("started",)):
                return {"ok": True, "url": assess.create_link(con, aid), "say": "Give them this exact url and remind them it needs a laptop or desktop."}
            return {"ok": False, "error": "the assessment is not open yet at this stage", "stage": a["stage"]}
    except engine.SlotTaken as e:
        return {"ok": False, "error": str(e), "hint": "call list_slots again and offer fresh times"}
    except engine.NotAvailable as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    return {"error": f"unknown tool {name}"}


# ---------------------------------------------------------------- prompt and history

def system_prompt(con, a: dict) -> str:
    st = engine.status_summary(con, a["id"])
    return SYSTEM_PROMPT.format(agent=texts.AGENT_NAME, knowledge=knowledge_text(con), name=a["name"] or "the applicant", timezone=a["timezone"],
                                stage_label=st["stage_label"], next_step=st["next_step"],
                                today=datetime.now(timezone.utc).strftime("%A %d %B %Y"))


def history(con, aid: int, limit: int = HISTORY) -> list[dict]:
    rows = db.q(con, "SELECT direction, body, status FROM message WHERE applicant_id=? AND status IN ('sent','received') ORDER BY id DESC LIMIT ?", (aid, limit))
    out = []
    for r in rows[::-1]:
        out.append({"role": "user" if r["direction"] == "in" else "assistant", "content": r["body"]})
    return out


# ---------------------------------------------------------------- OpenRouter

def _post(model: str, messages: list[dict], tools: list[dict]) -> dict:
    base = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}", "Content-Type": "application/json",
               "HTTP-Referer": os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai"), "X-Title": "Beloura Hire"}
    body = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.4, "max_tokens": 600,
            "usage": {"include": True}}
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=float(os.environ.get("SAM_TIMEOUT", "60")))
    if r.status_code >= 300:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:300]}")
    return r.json()


def _complete(con, aid: int, messages: list[dict]) -> dict | None:
    """One assistant turn with fallback across models. Returns the assistant message dict, or None when every model failed."""
    for model in models():
        t0 = time.time()
        try:
            data = _post(model, messages, TOOLS)
            usage = data.get("usage") or {}
            ti, to = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            used = data.get("model") or model
            log_call(con, aid, used, ti, to, cost_for(used, ti, to, usage.get("cost")), True, "", int((time.time() - t0) * 1000))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            return msg
        except Exception as e:  # noqa: BLE001
            log.warning("model %s failed: %s", model, e)
            log_call(con, aid, model, 0, 0, 0.0, False, str(e), int((time.time() - t0) * 1000))
    return None


def reply(con, aid: int, inbound: str, now: datetime | None = None) -> str:
    """The answer to one inbound message. Does not send; the caller delivers it on the applicant's channel."""
    a = engine.applicant(con, aid)
    if not a:
        return ""
    if not configured():
        return texts.plain(dry_reply(con, aid, inbound, now=now))
    messages = [{"role": "system", "content": system_prompt(con, a)}] + history(con, aid)
    if not messages or messages[-1].get("content") != inbound or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": inbound})
    for _ in range(MAX_TOOL_ROUNDS):
        msg = _complete(con, aid, messages)
        if msg is None:
            engine.create_escalation(con, aid, "sam unavailable", f"Every model failed answering: {inbound[:300]}")
            return "Sorry, I can't answer right now. I've flagged it to the team and one of us will come back to you here."
        calls = msg.get("tool_calls") or []
        if not calls:
            text = texts.plain((msg.get("content") or "").strip())
            return text or texts.will_check()
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = (c.get("function") or {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = run_tool(con, aid, fn.get("name") or "", args if isinstance(args, dict) else {}, now=now)
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(result)})
    return texts.will_check()


# ---------------------------------------------------------------- dry-run (no API key): deterministic, same tools

_ORD = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3, "fourth": 4, "fifth": 5, "sixth": 6}


def _pick_index(text: str) -> int | None:
    t = text.lower()
    m = re.search(r"\b(?:slot|option|number|no\.?|#)\s*(\d)\b", t) or re.search(r"\b(\d)\b", t)
    if m:
        return int(m.group(1))
    for w, n in _ORD.items():
        if re.search(rf"\b{w}\b", t):
            return n
    return None


def _format_slots(res: dict) -> str:
    if not res.get("slots"):
        return texts.no_slots() if not res.get("error") else "Booking opens after the assessment. I'll let you know as soon as it's your turn to pick a time."
    lines = [f"{i + 1}. {s['local']} ({s['uk']})" for i, s in enumerate(res["slots"])]
    return "Here are the open times for your 30-minute final interview:\n" + "\n".join(lines) + "\nReply with the number you want and I'll book it."


def dry_reply(con, aid: int, inbound: str, now: datetime | None = None) -> str:
    a = engine.applicant(con, aid)
    t = (inbound or "").strip()
    low = t.lower()
    first = (a["name"] or "").split(" ")[0]
    if re.search(r"\b(human|person|someone real|real person|speak to|talk to (a|the) (manager|team|person)|complain|complaint|lawyer|legal)\b", low):
        run_tool(con, aid, "escalate", {"reason": "asked for a person", "question": t}, now=now)
        return "Of course. I've passed this to the team and someone will reply to you here."
    if re.search(r"\b(cancel)\b", low) and engine.open_interview(con, aid):
        run_tool(con, aid, "cancel", {"reason": t}, now=now)
        return texts.cancelled(first)
    m = re.search(r"\b(?:i am|i'm|im|timezone is|time zone is|tz is)\s+(?:in\s+)?([A-Za-z]+/[A-Za-z_]+)\b", t, re.I)
    if m and engine.valid_tz(m.group(1)):
        r = run_tool(con, aid, "set_timezone", {"timezone": m.group(1)}, now=now)
        return f"Noted, I'll show times in {r['timezone']} from now on." if r.get("ok") else "I couldn't recognise that timezone. Could you give it as a city, like Asia/Manila?"
    offered = engine.meta_get(a, "offered_slots", []) or []
    idx = _pick_index(low)
    if offered and idx and 1 <= idx <= len(offered) and re.search(r"\b(\d|first|second|third|slot|option|book|take|that one|yes)\b", low):
        name = "reschedule" if engine.open_interview(con, aid) else "book_slot"
        r = run_tool(con, aid, name, {"start": offered[idx - 1]}, now=now)
        if r.get("ok"):
            engine.meta_set(con, aid, "offered_slots", [])
            mgr = r.get("manager") or "the manager"
            return texts.rescheduled(first, r["when"], mgr) if r.get("status") == "rescheduled" else texts.booked(first, r["when"], mgr)
        if "taken" in (r.get("error") or ""):
            res = run_tool(con, aid, "list_slots", {}, now=now)
            return "That one has just gone. " + _format_slots(res)
        return f"I couldn't book that: {r.get('error')}."
    if re.search(r"\b(slot|slots|book|schedule|interview time|available times|when can|move|reschedule|change the time|times)\b", low):
        res = run_tool(con, aid, "list_slots", {}, now=now)
        return _format_slots(res)
    if re.search(r"\b(status|where am i|what happens next|next step|update|progress|stage)\b", low):
        st = run_tool(con, aid, "get_status", {}, now=now)
        return f"You're at {st['stage_label']}: {st['next_step']}"
    hits = search_knowledge(con, t, limit=1)
    if hits:
        return hits[0]["answer"]
    if re.search(r"\b(hi|hello|hey|thanks|thank you|ok|okay|great|sure)\b", low) and len(low) < 40:
        return f"Hi {first}. Ask me anything about the role or the process, or say 'status' to see where you are." if first else "Hi. Ask me anything about the role or the process, or say 'status' to see where you are."
    run_tool(con, aid, "escalate", {"reason": "question not in the knowledge base", "question": t}, now=now)
    return texts.will_check()


# ---------------------------------------------------------------- entry points used by the channels and the form

def handle_inbound(con, aid: int, channel: str, body: str, external_id: str | None = None, address: str = "", now: datetime | None = None) -> str | None:
    """Record the applicant's message, answer it, deliver the answer on the same channel. Returns the text sent (None on duplicate)."""
    a = engine.applicant(con, aid)
    was_ghosted = a and a["stage"] == "ghosted"
    mid = engine.record_inbound(con, aid, channel, body, external_id=external_id, address=address)
    if mid is None:
        return None
    import interview
    if interview.active(con, aid):
        return interview.handle_answer(con, aid, channel, body, now=now)
    text = reply(con, aid, body, now=now)
    if was_ghosted:
        text = texts.ghosted() + " " + text
    engine.deliver(con, aid, text, kind="sam", channel=channel)
    if not interview.active(con, aid):
        interview.maybe_start(con, aid, channel)
    return text


def start_interview_if_due(con, aid: int, channel: str) -> bool:
    """Called when a channel links (Telegram /start, Discord join): the five questions begin there."""
    import interview
    return interview.maybe_start(con, aid, channel)


def first_contact(con, aid: int, telegram_link: str = "", discord_invite: str = "") -> int:
    a = engine.applicant(con, aid)
    first = (a["name"] or "").split(" ")[0]
    return engine.deliver(con, aid, texts.first_message(first, engine.next_step(con, a), telegram_link=telegram_link, discord_invite=discord_invite), kind="first")
