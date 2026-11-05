"""The Five Essential Questions as a text interview, run right after screening on the first linked channel.

One question at a time; answers land on the application; the set is graded against a short rubric (the GFE answer is the gate).
Result: stage interview_passed or interview_failed (polite decline). Staff can override on the applicant page.
Without OPENROUTER_API_KEY the grader is a keyword heuristic so the flow works offline.
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

log = logging.getLogger("hire.interview")

PROMPT_VERSION = "interview-2026-11-04.1"
N = len(texts.INTERVIEW_QUESTIONS)
GFE_WORDS = re.compile(r"girlfriend|fantasy|fantasies|dream|relationship|intimacy|intimate|emotional|connection|companion|boyfriend|"
                       r"real life|real-life|irl|can'?t get|cannot get|doesn'?t get|lonely|feel (special|wanted|loved|desired)|make (him|them) feel|"
                       r"escape|attention|personal", re.I)
GFE_NEGATIVE = re.compile(r"^\s*(no|nope|not really|never heard|i don'?t know|idk|not familiar)\b", re.I)

RUBRIC = """You grade a short text interview for an OnlyFans chatter role. Return JSON only, no prose, with keys:
gfe_ok (boolean): the GFE answer shows they understand GFE means selling a relationship or fantasy the subscriber cannot get in real life
  (emotional connection, attention, feeling wanted, a girlfriend-like dynamic), not just "being nice" or "sending content".
experience_ok (boolean): the experience answer is coherent and relevant (chatting, sales or support count; honest "none" with sensible understanding also counts).
red_flags (array of short strings): anything disqualifying: abusive language, wants to meet subs, plans to use scripts/AI only, dishonesty, cannot do shifts, confusion about the job.
score (integer 0-10): overall fit from these answers alone.
summary (string, max 60 words): plain notes for the hiring team, no emoji."""


def enabled(con) -> bool:
    return db.setting(con, "interview_enabled", "1") == "1"


def state(con, aid: int) -> dict | None:
    return engine.latest_application(con, aid)


def active(con, aid: int) -> bool:
    app = state(con, aid)
    return bool(app and app.get("interview_state", "").startswith("q"))


def _idx(app: dict) -> int:
    s = app.get("interview_state") or ""
    return int(s[1:]) if s.startswith("q") and s[1:].isdigit() else -1


def maybe_start(con, aid: int, channel: str) -> bool:
    """Start the interview if the applicant is screened in and it has not started. Returns True when the first question went out."""
    a = engine.applicant(con, aid)
    app = state(con, aid)
    if not a or not app or not enabled(con) or a["stage"] != "screened_in" or app.get("interview_state"):
        return False
    first = (a["name"] or "").split(" ")[0] or "there"
    if channel == "telegram" and a.get("discord") and not a.get("discord_channel_id"):
        invite = db.setting(con, "discord_invite_url", "")
        engine.deliver(con, aid, texts.join_discord(first, invite), kind="first", channel="telegram")
        return False
    con.execute("UPDATE application SET interview_state='q0', interview_channel=? WHERE id=?", (channel, app["id"]))
    engine.deliver(con, aid, texts.interview_intro(first) + "\n\n" + texts.INTERVIEW_QUESTIONS[0], kind="interview", channel=channel)
    return True


def restart(con, aid: int, channel: str | None = None) -> bool:
    app = state(con, aid)
    if not app:
        return False
    con.execute("UPDATE application SET interview_state='', interview_answers='{}', interview_grade='{}' WHERE id=?", (app["id"],))
    a = engine.applicant(con, aid)
    if a["stage"] in ("interview_passed", "interview_failed", "assessment_sent"):
        engine.set_stage(con, aid, "screened_in", actor="staff", reason="interview restarted", force=True)
    return maybe_start(con, aid, channel or a.get("last_channel") or engine.pick_channel(a) or "")


def _looks_like_question(text: str) -> bool:
    t = text.strip()
    return len(t) < 160 and ("?" in t) and not GFE_WORDS.search(t)


def handle_answer(con, aid: int, channel: str, text: str, now: datetime | None = None) -> str:
    """Store the answer to the current question and ask the next one, or grade and close. Returns what was sent."""
    import sam
    a = engine.applicant(con, aid)
    app = state(con, aid)
    i = _idx(app)
    first = (a["name"] or "").split(" ")[0] or "there"
    if i < 0:
        return ""
    if _looks_like_question(text):
        answer = sam.reply(con, aid, text, now=now)
        out = answer + "\n\n" + texts.interview_resume(first) + " " + texts.INTERVIEW_QUESTIONS[i]
        engine.deliver(con, aid, out, kind="interview", channel=channel)
        return out
    answers = db.jl(app.get("interview_answers"), {})
    answers[f"q{i + 1}"] = text.strip()[:3000]
    nxt = i + 1
    if nxt < N:
        con.execute("UPDATE application SET interview_answers=?, interview_state=? WHERE id=?", (json.dumps(answers), f"q{nxt}", app["id"]))
        out = texts.interview_ack(i) + " " + texts.INTERVIEW_QUESTIONS[nxt]
        engine.deliver(con, aid, out, kind="interview", channel=channel)
        return out
    grade = grade_answers(con, aid, answers)
    con.execute("UPDATE application SET interview_answers=?, interview_state='done', interview_grade=? WHERE id=?",
                (json.dumps(answers), json.dumps(grade), app["id"]))
    passed = bool(grade.get("passed"))
    reason = f"interview {'passed' if passed else 'failed'}: score {grade.get('score')}/10, GFE {'ok' if grade.get('gfe_ok') else 'not shown'}" \
             + (f", flags: {'; '.join(grade.get('red_flags') or [])}" if grade.get("red_flags") else "")
    engine.set_stage(con, aid, "interview_passed" if passed else "interview_failed", actor="james", reason=reason)
    out = texts.interview_passed(first) if passed else texts.interview_failed(first)
    engine.deliver(con, aid, out, kind="interview" if passed else "decline", channel=channel)
    if passed:
        _send_assessment(con, aid)
    if engine.ALERT:
        try:
            engine.ALERT(f"Interview {'passed' if passed else 'failed'} for {a['name'] or '#' + str(aid)}: {grade.get('summary') or reason}", aid)
        except Exception as e:  # noqa: BLE001
            log.warning("alert failed: %s", e)
    return out


def heuristic_grade(answers: dict) -> dict:
    gfe = answers.get("q3", "")
    gfe_ok = bool(GFE_WORDS.search(gfe)) and not GFE_NEGATIVE.match(gfe) and len(gfe.split()) >= 6
    exp = answers.get("q1", "")
    experience_ok = len(exp.split()) >= 8
    flags = []
    joined = " ".join(answers.values()).lower()
    if re.search(r"\bmeet (him|them|the sub|subs|fans?)\b|in person|real date", joined):
        flags.append("talks about meeting subscribers")
    if re.search(r"\b(chatgpt|ai will|let the ai|copy.?paste scripts?)\b", joined):
        flags.append("plans to lean on AI or scripts")
    score = 2 + (4 if gfe_ok else 0) + (2 if experience_ok else 0) + (1 if len(answers.get("q5", "").split()) >= 5 else 0) + (1 if len(answers.get("q4", "").split()) >= 3 else 0)
    score = max(0, min(10, score - 2 * len(flags)))
    return {"gfe_ok": gfe_ok, "experience_ok": experience_ok, "red_flags": flags, "score": score,
            "summary": ("GFE understood" if gfe_ok else "GFE not explained") + ("; relevant experience" if experience_ok else "; thin experience answer")
                       + (f"; flags: {', '.join(flags)}" if flags else "") + ". Heuristic grade (no model key).",
            "passed": gfe_ok and not flags and score >= 5, "grader": "heuristic"}


def model_grade(con, aid: int, answers: dict) -> dict | None:
    import sam
    transcript = "\n".join(f"Q{i + 1}: {texts.INTERVIEW_QUESTIONS[i]}\nA{i + 1}: {answers.get(f'q{i + 1}', '')}" for i in range(N))
    messages = [{"role": "system", "content": RUBRIC}, {"role": "user", "content": transcript}]
    base = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}", "Content-Type": "application/json",
               "HTTP-Referer": os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai"), "X-Title": "Beloura Hire"}
    for model in sam.models():
        t0 = time.time()
        try:
            r = httpx.post(f"{base}/chat/completions", json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 400,
                           "response_format": {"type": "json_object"}, "usage": {"include": True}}, headers=headers,
                           timeout=float(os.environ.get("SAM_TIMEOUT", "60")))
            if r.status_code >= 300:
                raise RuntimeError(f"openrouter {r.status_code}: {r.text[:200]}")
            data = r.json()
            usage = data.get("usage") or {}
            ti, to = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            used = data.get("model") or model
            con.execute("INSERT INTO llm_call (applicant_id, model, prompt_version, tokens_in, tokens_out, cost_usd, ok, error, latency_ms, created_at) VALUES (?,?,?,?,?,?,1,'',?,?)",
                        (aid, used, PROMPT_VERSION, ti, to, sam.cost_for(used, ti, to, usage.get("cost")), int((time.time() - t0) * 1000), db.now()))
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
            m = re.search(r"\{.*\}", content, re.S)
            g = json.loads(m.group(0) if m else content)
            g = {"gfe_ok": bool(g.get("gfe_ok")), "experience_ok": bool(g.get("experience_ok")), "red_flags": [str(x)[:80] for x in (g.get("red_flags") or [])][:5],
                 "score": max(0, min(10, int(g.get("score") or 0))), "summary": str(g.get("summary") or "")[:400], "grader": used}
            g["passed"] = g["gfe_ok"] and not g["red_flags"] and g["score"] >= 5
            return g
        except Exception as e:  # noqa: BLE001
            log.warning("interview grader %s failed: %s", model, e)
            con.execute("INSERT INTO llm_call (applicant_id, model, prompt_version, tokens_in, tokens_out, cost_usd, ok, error, latency_ms, created_at) VALUES (?,?,?,0,0,0,0,?,?,?)",
                        (aid, model, PROMPT_VERSION, str(e)[:300], int((time.time() - t0) * 1000), db.now()))
    return None


def grade_answers(con, aid: int, answers: dict) -> dict:
    if os.environ.get("OPENROUTER_API_KEY", ""):
        g = model_grade(con, aid, answers)
        if g:
            return g
    return heuristic_grade(answers)


def override(con, aid: int, result: str, by: str) -> bool:
    """Staff decision: result 'pass' or 'fail'. Sets the stage with force and marks the grade as overridden."""
    app = state(con, aid)
    if not app or result not in ("pass", "fail"):
        return False
    g = db.jl(app.get("interview_grade"), {})
    g.update({"passed": result == "pass", "override_by": by})
    con.execute("UPDATE application SET interview_grade=?, interview_state='done' WHERE id=?", (json.dumps(g), app["id"]))
    ok = engine.set_stage(con, aid, "interview_passed" if result == "pass" else "interview_failed", actor=by, reason=f"interview {result} by staff", force=True)
    if ok and result == "pass":
        _send_assessment(con, aid)
    return ok


def _send_assessment(con, aid: int) -> None:
    try:
        import assess
        assess.on_stage(con, aid)
    except Exception as e:  # noqa: BLE001
        log.warning("assessment link not sent for %s: %s", aid, e)
