"""The two models behind the live scenarios and the free-text English block, via OpenRouter over httpx (same pattern as sam.py).

  fan_turn(con, aid, scenario, turns)        -> the fan's next message (SIM_MODEL, default x-ai/grok-4.3, fallback deepseek/deepseek-v4.1-flash)
  judge_scenario(con, aid, scenario, turns)  -> {ok, scores{criterion: {score, evidence}}, total, chat_fluency, chat_fluency_evidence, summary, model, raw}
  parse_judge(text)                          -> tolerant JSON extraction, safe on garbage

The judge now also returns a chat-fluency sub-score (0-5): this is OnlyFans chat register, not formal English. Lowercase, chat slang used
correctly (u, ur, rn, wyd, lol, omg, hbu, ngl), relaxed punctuation and emoji are all correct and must not be penalised; incoherent or broken
meaning, wrong/try-hard slang, and stiff formal essay-like writing are penalised. Over-polished formal text is the wrong register here and also
nudges the AI-use risk up (see ai_written_flag). Chat fluency is aggregated across the live chats in assess.py.

OPENROUTER_API_KEY empty -> dry-run: canned fan lines from the bank and deterministic heuristic scores, so tests and local runs work offline.
Every call is logged to llm_call with the applicant id, tokens and cost (OpenRouter's usage.cost when present, else ASSESS_PRICES / SAM_PRICES).
The prompt_version column carries which prompt it was: assess-sim-*, assess-judge-*, assess-grade-*.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx

import db
import sam
from tools import assess_bank as bank

log = logging.getLogger("hire.assess_llm")

SIM_VERSION = "assess-sim-2026-11-05.9"
JUDGE_VERSION = "assess-judge-2026-11-05.3"
GRADE_VERSION = "assess-grade-2026-11-04.1"
DEFAULT_SIM = "anthropic/claude-sonnet-5.5"
DEFAULT_SIM_FALLBACK = "deepseek/deepseek-v4.1-flash,x-ai/grok-4.3"
DEFAULT_JUDGE = "anthropic/claude-opus-5.5"
DEFAULT_JUDGE_FALLBACK = "anthropic/claude-sonnet-5.5"
# Fallback prices, USD per million tokens in,out, used only when OpenRouter omits usage.cost. Override with ASSESS_PRICES.
DEFAULT_PRICES = "x-ai/grok-4.3=1.25,2.5;deepseek/deepseek-v4.1-flash=0.045,0.5;anthropic/claude-opus-5.5=4,20;anthropic/claude-sonnet-5.5=2,10"

SIM_SYSTEM = """You are role-playing one specific FAN on an OnlyFans-style platform, chatting with a creator. The person you are talking to is playing the creator (she is being assessed on how she handles you; never mention that, never break character).

WHO SHE IS (the creator you subscribed to):
{creator}
WHO YOU ARE (play this man exactly; he is a real individual, not a stock fan):
{persona}

The situation right now: {brief}

How to write:
- ALWAYS respond to her LAST message first: react to what she actually just said or did, in the moment, before anything else. Never ignore it or jump to an unrelated line. If she flirts or sexts, engage with THAT; if she asks a question, answer it.
- Your brief is your character and your tactics, NOT a script of lines to recite. Play the disposition; never copy phrases from it word for word. A tactic like stalling or changing the subject only kicks in on its real trigger (e.g. when money or paying comes up), not in reply to everything.
- Be THIS man, a specific believable individual, not a generic fan. Reveal his job, mood, money and backstory SLOWLY — a bit at a time, only when it fits what she asked or said. Do not dump it all in one message.
- Your FIRST message drops you straight into the situation your brief describes, in his voice. If the brief already has you reacting to a price, an offer, a boundary push or a bad mood, OPEN THERE — not with a generic "how’s your day". Only a brand-new subscriber opens with a plain hello. Keep it short either way.
- You are not here to be easy. Hold your position — haggle, stall, push, stay grumpy, stay horny, whatever your brief says — and only soften when she genuinely earns it. Do not fold or get agreeable the second she is nice.
- NEVER telegraph that you are about to spend or that you are "feeling generous" — a real fan does not announce the sale. Make her work for it.
- Sound like a real person, not a script. Use his own words, a bit of attitude or a quirk, the odd typo. Avoid clean textbook lines and flat filler like "that sounds fun", "what would i actually be getting", "tell me more". Be specific: name the actual thing (his warehouse shift, the exact price she quoted, the content she sent), not vague generalities.
- Build on what was ACTUALLY said and stay consistent with details you already gave.
- Text the way he would on his phone: short, casual, lowercase is fine, usually one sentence or a fragment, in his own voice. No emoji unless this man would use them.
- You do NOT know what content or prices she has. Never mention a price, buying, paying, "how much" or "unlocking", and never act as if she offered or sent anything, unless a message in THIS chat is actually marked as sent content (e.g. "[sent ... PPV $..]"). Until that happens, just chat, flirt and react to what she says.
- You are the CUSTOMER reacting, not the one performing. When she sends a script step, react to THAT exact content and (for a PPV) its price the way THIS man would: get visibly more turned on and eager as it escalates, say you want more, unlock a PPV when she has built it up well, and hesitate or push back on the price if she fires a priced PPV too early with no build-up. Keep reactions short and real ("fuck yes", "god i need to see that", "send it", "thats a bit steep tho") — she leads the sexting, you respond. Do NOT write graphic step-by-step sexual narration yourself.
- Do not narrate, no stage directions, no explaining yourself.
- This is an adult (18+) sexting context between consenting adults, so talking like a turned-on sub is in character: express desire and react, but keep it to brief, natural reactions rather than graphic play-by-play (she leads, you respond). Hard limits, always: never mention or imply anyone under 18 or any age-play / "young" framing, never anything illegal or against platform rules (incest, non-consent, bestiality, etc.), no slurs, no threats of violence. If she pushes any of those, refuse in character.
- Never give or ask for real payment details, phone numbers or addresses in full; if your brief says to push for personal info, ask in general terms.
- This is fan message {n} of {max}. If it is the last one, leave it somewhere she can still reply; do not say goodbye unless the brief says so.

Reply with the fan's message only, nothing else."""

JUDGE_SYSTEM = """You are the assessor for an OnlyFans chatting agency. You grade how an applicant, writing AS THE CREATOR, handled a simulated fan in one short chat. The fan was played by a model; grade only the applicant's messages.

The creator she is playing:
{creator}
Her script ladder (pre-written steps she sends in order; teases free, PPVs priced). Her skill is TIMING — when to fire each step:
{script}

HOW OUR MANAGERS GRADE (the 3X house standard, from our own training — match it):
{standard}

Scenario: {title}
What good looks like here: {good}

Examples of answers the agency's managers consider the right move in similar situations (from the agency's own questionnaire):
{exemplars}

Score each criterion 0-10 (10 = a top experienced chatter would not do better):
- persona_consistency: stays in Mia's voice and story (who she is, what she is into, her boundaries), never breaks character, no "as an AI", no formality shifts.
- rapport: warmth, reads and uses what the fan said, asks good questions, makes him feel seen.
- sales_move: how well she RUNS THE SCRIPT and sells. The key skill is TIMING: build real rapport and sexting before firing a priced PPV, send teases to warm him up, escalate in order, and drop each PPV at the moment he is most into it. Reward good timing and sexting that builds anticipation between steps; mark down firing a priced PPV too early with no build-up, rushing through steps, spamming PPVs back to back, or quoting/justifying price bluntly. If the scenario is not about selling (rude fan, boundary push), score the judgement NOT to push and to handle the situation first.
- pacing: message length and rhythm fit a chat. Real chatters send short bursts of several quick messages, not paragraphs: reward short, punchy messages sent as a natural burst, and mark down one long essay-style block. Each line below marked APPLICANT is one message she sent; several in a row is her burst before you replied.
- boundary_handling: declines meet-ups, personal info, off-platform moves and prohibited requests kindly and clearly; handles abuse calmly.
- red_flags: 10 = none. Explicit sexual talk is EXPECTED here and is NOT a red flag. Deduct only for: insults or being passive-aggressive, begging, promising things she cannot deliver, sharing personal data, agreeing to meet up or move off-platform, any under-18 / age-play / illegal / non-consent content, empty replies, or text that reads as copied from a template or generated.

Then score chat_fluency 0-5. This is OnlyFans chat fluency, the texting / iMessage / Snapchat register, NOT formal English:
- GOOD, do not penalise: lowercase with no capitals, chat abbreviations and slang used correctly (u, ur, rn, wyd, lol, omg, hbu, ngl), relaxed or missing punctuation, emoji, a playful fast tone. This is expected and correct for the role.
- PENALISE: genuinely incoherent or broken English where the meaning is unclear; wrong or forced slang that does not make sense or reads try-hard; replies that are stiff, formal or essay-like and break the flirty casual persona.
- Over-polished, perfectly punctuated, formal writing is the WRONG register here, so mark it down even though it is "correct English"; real chatters do not write like an essay.

For each criterion and for chat_fluency quote the applicant's words that drove the score (short quotes, or "no evidence" when the issue is absence). Add a one-line summary a manager can read in two seconds.

Reply with JSON only, no prose around it:
{{"scores": {{"persona_consistency": {{"score": 0, "evidence": ""}}, "rapport": {{"score": 0, "evidence": ""}}, "sales_move": {{"score": 0, "evidence": ""}}, "pacing": {{"score": 0, "evidence": ""}}, "boundary_handling": {{"score": 0, "evidence": ""}}, "red_flags": {{"score": 0, "evidence": ""}}}}, "chat_fluency": {{"score": 0, "evidence": ""}}, "summary": ""}}"""

GRADE_SYSTEM = """You grade one short written reply from an applicant for an English-language chatting job. The applicant is writing as a creator on an OnlyFans-style platform, replying to a fan's message. Grade only the applicant's reply.

Fan's message: {fan}
Context: {hint}

Score 0-5 each:
- grammar: spelling, grammar, punctuation appropriate to casual chat (casual is fine; errors that a native reader would notice are not).
- naturalness: reads like a real person chatting, not a textbook or a template. Over-formal, stiff or obviously generated text scores low.
- warmth: responds to what the fan actually said, kind, a little flirty, makes him feel seen.
For each give one line of evidence quoting the applicant's words.

Reply with JSON only:
{{"grammar": {{"score": 0, "evidence": ""}}, "naturalness": {{"score": 0, "evidence": ""}}, "warmth": {{"score": 0, "evidence": ""}}}}"""


def configured() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY", ""))


def sim_models() -> list[str]:
    primary = os.environ.get("SIM_MODEL", DEFAULT_SIM).strip() or DEFAULT_SIM
    fall = [m.strip() for m in os.environ.get("SIM_FALLBACK_MODELS", DEFAULT_SIM_FALLBACK).split(",") if m.strip()]
    return [primary] + [m for m in fall if m != primary]


def judge_models() -> list[str]:
    primary = os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE).strip() or DEFAULT_JUDGE
    fall = [m.strip() for m in os.environ.get("JUDGE_FALLBACK_MODELS", DEFAULT_JUDGE_FALLBACK).split(",") if m.strip()]
    return [primary] + [m for m in fall if m != primary]


def prices() -> dict:
    out = {}
    for src in (DEFAULT_PRICES, os.environ.get("SAM_PRICES", ""), os.environ.get("ASSESS_PRICES", "")):
        for part in src.split(";"):
            if "=" not in part:
                continue
            m, p = part.split("=", 1)
            try:
                i, o = p.split(",")
                out[m.strip()] = (float(i), float(o))
            except ValueError:
                continue
    return out


def cost_for(model: str, tokens_in: int, tokens_out: int, reported) -> float:
    if reported is not None:
        try:
            return float(reported)
        except (TypeError, ValueError):
            pass
    i, o = prices().get(model, (0.0, 0.0))
    return round(tokens_in * i / 1e6 + tokens_out * o / 1e6, 6)


def log_call(con, aid: int | None, model: str, version: str, tokens_in: int, tokens_out: int, cost: float, ok: bool, error: str, latency_ms: int) -> None:
    con.execute("INSERT INTO llm_call (applicant_id, model, prompt_version, tokens_in, tokens_out, cost_usd, ok, error, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, model, version, tokens_in, tokens_out, cost, 1 if ok else 0, (error or "")[:300], latency_ms, db.now()))


def _post(model: str, messages: list[dict], max_tokens: int, temperature: float, json_mode: bool = False) -> dict:
    base = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}", "Content-Type": "application/json",
               "HTTP-Referer": os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai"), "X-Title": "Beloura Hire assessment"}
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "usage": {"include": True}}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=float(os.environ.get("ASSESS_TIMEOUT", os.environ.get("SAM_TIMEOUT", "60"))))
    if r.status_code >= 300:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:300]}")
    return r.json()


def complete(con, aid: int | None, models: list[str], version: str, messages: list[dict], max_tokens: int = 400, temperature: float = 0.7,
             json_mode: bool = False) -> tuple[str, str] | None:
    """One completion with fallback across models. Returns (text, model) or None when every model failed. Every attempt is logged."""
    for model in models:
        t0 = time.time()
        try:
            data = _post(model, messages, max_tokens, temperature, json_mode=json_mode)
            usage = data.get("usage") or {}
            ti, to = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            used = data.get("model") or model
            log_call(con, aid, used, version, ti, to, cost_for(used, ti, to, usage.get("cost")), True, "", int((time.time() - t0) * 1000))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            return (msg.get("content") or "").strip(), used
        except Exception as e:  # noqa: BLE001
            log.warning("%s on %s failed: %s", version, model, e)
            log_call(con, aid, model, version, 0, 0, 0.0, False, str(e), int((time.time() - t0) * 1000))
    return None


# ---------------------------------------------------------------- text hygiene

_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
_MD = [(re.compile(r"\*\*(.+?)\*\*"), r"\1"), (re.compile(r"__(.+?)__"), r"\1"), (re.compile(r"(?<!\w)\*(?!\s)(.+?)\*(?!\w)"), r"\1"),
       (re.compile(r"(?<!\w)_(?!\s)(.+?)_(?!\w)"), r"\1"), (re.compile(r"`+([^`]*)`+"), r"\1"), (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
       (re.compile(r"^\s{0,3}[-*]\s+", re.M), ""), (re.compile(r"^\s{0,3}>\s?", re.M), "")]


def clean_text(s: str) -> str:
    """Plain chat text: no markdown, no emoji, no surrounding quotes or 'Fan:' labels, collapsed whitespace."""
    s = (s or "").strip()
    s = re.sub(r"^(fan|creator|assistant|user)\s*:\s*", "", s, flags=re.I)
    for rx, rep in _MD:
        s = rx.sub(rep, s)
    s = _EMOJI.sub("", s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _json_block(text: str) -> dict | None:
    """The first JSON object in a reply, tolerating code fences and prose around it."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    return None


def _score(v, lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return max(lo, min(hi, f))


def parse_judge(text: str) -> dict:
    """Judge JSON -> {ok, scores{c: {score, evidence}}, total, chat_fluency, chat_fluency_evidence, summary}. Missing or malformed criteria
    leave ok=False and total=None. chat_fluency (0-5) is optional: a missing one leaves chat_fluency=None but does not fail the grade."""
    data = _json_block(text)
    out = {"ok": False, "scores": {}, "total": None, "chat_fluency": None, "chat_fluency_evidence": "", "summary": "", "error": ""}
    if not data:
        out["error"] = "no JSON object in reply"
        return out
    raw = data.get("scores") if isinstance(data.get("scores"), dict) else data
    got = []
    for c in bank.JUDGE_CRITERIA:
        item = raw.get(c)
        if isinstance(item, dict):
            sc, ev = _score(item.get("score"), 0, 10), str(item.get("evidence") or "")[:400]
        else:
            sc, ev = _score(item, 0, 10), ""
        if sc is None:
            out["error"] = f"missing score for {c}"
            out["scores"][c] = {"score": None, "evidence": ev}
            continue
        out["scores"][c] = {"score": round(sc, 1), "evidence": ev}
        got.append(sc)
    cf = data.get("chat_fluency")
    if isinstance(cf, dict):
        cfs, cfe = _score(cf.get("score"), 0, 5), str(cf.get("evidence") or "")[:400]
    else:
        cfs, cfe = _score(cf, 0, 5), ""
    out["chat_fluency"] = None if cfs is None else round(cfs, 1)
    out["chat_fluency_evidence"] = cfe
    out["summary"] = clean_text(str(data.get("summary") or ""))[:300]
    if len(got) == len(bank.JUDGE_CRITERIA):
        out["ok"] = True
        out["total"] = round(sum(got) / len(got), 2)
    return out


def parse_grade(text: str) -> dict:
    data = _json_block(text)
    out = {"ok": False, "grammar": None, "naturalness": None, "warmth": None, "evidence": {}, "average": None, "error": ""}
    if not data:
        out["error"] = "no JSON object in reply"
        return out
    got = []
    for c in ("grammar", "naturalness", "warmth"):
        item = data.get(c)
        if isinstance(item, dict):
            sc, ev = _score(item.get("score"), 0, 5), str(item.get("evidence") or "")[:300]
        else:
            sc, ev = _score(item, 0, 5), ""
        out[c] = None if sc is None else round(sc, 1)
        out["evidence"][c] = ev
        if sc is not None:
            got.append(sc)
        else:
            out["error"] = f"missing score for {c}"
    if len(got) == 3:
        out["ok"] = True
        out["average"] = round(sum(got) / 3, 2)
    return out


# ---------------------------------------------------------------- the fan

def _transcript(turns: list[dict]) -> list[dict]:
    """Stored turns -> chat messages from the fan model's point of view (fan = assistant, creator = user). Consecutive applicant chunks
    (a message burst) are merged into one user message so the fan replies to the whole burst, not to each short message."""
    out = []
    for t in turns:
        role = "assistant" if t["role"] == "fan" else "user"
        body = t.get("body") or ""
        if out and out[-1]["role"] == role:
            out[-1]["content"] = (out[-1]["content"] + "\n" + body).strip()
        else:
            out.append({"role": role, "content": body})
    return out


_DRY_TAIL = ["mmm keep going", "god i want more", "fuck youre so hot", "ok im obsessed, whats next", "dont stop"]


def dry_fan_turn(scenario: dict, n_fan_turns_so_far: int) -> str:
    lines = scenario.get("canned") or []
    if n_fan_turns_so_far < len(lines):
        return lines[n_fan_turns_so_far]
    return _DRY_TAIL[n_fan_turns_so_far % len(_DRY_TAIL)]


def fan_turn(con, aid: int, scenario: dict, turns: list[dict]) -> tuple[str, str]:
    """The fan's next message given the turns so far. Returns (text, model). Dry-run without a key."""
    n_fan = sum(1 for t in turns if t["role"] == "fan")
    if not configured():
        return dry_fan_turn(scenario, n_fan), "dry-run"
    sys_prompt = SIM_SYSTEM.format(creator=bank.creator_brief(), persona=bank.fan_persona_text(scenario),
                                   brief=scenario["brief"], n=n_fan + 1, max=scenario["fan_turns"])
    messages = [{"role": "system", "content": sys_prompt}] + _transcript(turns)
    if not turns:
        messages.append({"role": "user", "content": "(the creator's inbox opens; send your first message)"})
    res = complete(con, aid, sim_models(), SIM_VERSION, messages, max_tokens=120, temperature=0.9)
    if not res:
        return dry_fan_turn(scenario, n_fan), "dry-run-fallback"
    text, model = res
    text = clean_text(text.split("\n")[0] if "\n" in text and len(text) > 200 else text)[:400]
    return (text or dry_fan_turn(scenario, n_fan)), model


# ---------------------------------------------------------------- the judge

def _exemplars() -> str:
    return "\n".join(f"- Situation: {e['situation']}\n  Good answer: {e['answer']}" for e in bank.best_answers())


def _chat_text(turns: list[dict]) -> str:
    return "\n".join(f"{'FAN' if t['role'] == 'fan' else 'APPLICANT (as creator)'}: {t['body'] or '(no reply)'}" for t in turns)


def dry_judge(scenario: dict, turns: list[dict]) -> dict:
    """Deterministic heuristic for offline runs: length, questions, a few red-flag words. Not a real grade."""
    mine = [t["body"] or "" for t in turns if t["role"] == "applicant"]
    joined = " ".join(mine).lower()
    words = len(joined.split())
    base = 5.0
    if words >= 8 * max(1, len(mine)):
        base += 2
    if "?" in joined:
        base += 1
    if any(w in joined for w in ("babe", "hun", "love", "cutie", "you")):
        base += 1
    flags = 10.0
    if any(w in joined for w in ("snapchat", "whatsapp", "my address", "refund", "idiot", "stupid")):
        flags -= 4
    if any(not m.strip() for m in mine):
        flags -= 3
        base -= 2
    base = max(0.0, min(10.0, base))
    # Chat fluency (0-5): casual chat is fine; empty replies and over-polished formal text are marked down.
    fluency = round(min(5.0, base / 2), 1)
    if any(not m.strip() for m in mine):
        fluency = max(0.0, fluency - 2)
    elif any(ai_written_flag(m)["flag"] for m in mine):
        fluency = max(0.0, fluency - 1.5)
    scores = {c: {"score": base, "evidence": (mine[0][:80] if mine and mine[0] else "no evidence")} for c in bank.JUDGE_CRITERIA}
    scores["red_flags"] = {"score": max(0.0, flags), "evidence": "dry-run heuristic"}
    total = round(sum(v["score"] for v in scores.values()) / len(scores), 2)
    return {"ok": True, "scores": scores, "total": total, "chat_fluency": fluency, "chat_fluency_evidence": "dry-run heuristic",
            "summary": "Dry-run heuristic grade; no model was called.", "model": "dry-run", "raw": ""}


def judge_scenario(con, aid: int, scenario: dict, turns: list[dict]) -> dict:
    if not configured():
        return dry_judge(scenario, turns)
    sys_prompt = JUDGE_SYSTEM.format(creator=bank.creator_brief(), script=bank.script_text(), standard=bank.house_standard_text(), title=scenario["title"], good=scenario["good"], exemplars=_exemplars())
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "Transcript:\n" + _chat_text(turns) + "\n\nGrade the applicant."}]
    res = complete(con, aid, judge_models(), JUDGE_VERSION, messages, max_tokens=1600, temperature=0.1, json_mode=True)
    if not res:
        return {"ok": False, "scores": {}, "total": None, "chat_fluency": None, "chat_fluency_evidence": "", "summary": "", "model": "", "raw": "", "error": "every judge model failed"}
    text, model = res
    out = parse_judge(text)
    out["model"] = model
    out["raw"] = text[:4000]
    return out


# ---------------------------------------------------------------- the free-text grader

def dry_grade(item: dict, reply: str) -> dict:
    words = reply.split()
    n = len(words)
    grammar = 4.0 if n >= 8 else 2.0
    if re.search(r"\b(i|im|dont|cant)\b", reply):
        grammar -= 1
    natural = 4.0 if 8 <= n <= 90 else 2.0
    warmth = 2.0 + (1 if "?" in reply else 0) + (1 if re.search(r"\b(you|your)\b", reply, re.I) else 0) + (1 if re.search(r"\b(sorry|aw|hey|hi|glad|love)\b", reply, re.I) else 0)
    g, na, w = max(0.0, min(5.0, grammar)), max(0.0, min(5.0, natural)), max(0.0, min(5.0, float(warmth)))
    return {"ok": True, "grammar": g, "naturalness": na, "warmth": w, "evidence": {k: "dry-run heuristic" for k in ("grammar", "naturalness", "warmth")},
            "average": round((g + na + w) / 3, 2), "model": "dry-run", "raw": ""}


def grade_free_text(con, aid: int, item: dict, reply: str) -> dict:
    if not reply.strip():
        return {"ok": True, "grammar": 0.0, "naturalness": 0.0, "warmth": 0.0, "evidence": {k: "no reply" for k in ("grammar", "naturalness", "warmth")},
                "average": 0.0, "model": "none", "raw": ""}
    if not configured():
        return dry_grade(item, reply)
    sys_prompt = GRADE_SYSTEM.format(fan=item["fan"], hint=item["hint"])
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "Applicant's reply:\n" + reply.strip()[:2000]}]
    res = complete(con, aid, judge_models(), GRADE_VERSION, messages, max_tokens=400, temperature=0.1, json_mode=True)
    if not res:
        return {"ok": False, "grammar": None, "naturalness": None, "warmth": None, "evidence": {}, "average": None, "model": "", "raw": "", "error": "every grader model failed"}
    text, model = res
    out = parse_grade(text)
    out["model"] = model
    out["raw"] = text[:2000]
    return out


# ---------------------------------------------------------------- the AI-written heuristic (a flag, never a fail)

def ai_written_flag(text: str, typing_seconds: float | None = None) -> dict:
    """Cheap signals that a free-text reply was pasted or generated: too-perfect punctuation and casing, uniform sentence length,
    typographic characters chat users rarely type, implausible characters per second. Two or more signals -> flag."""
    t = (text or "").strip()
    signals = []
    if not t:
        return {"flag": False, "signals": [], "score": 0}
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    if len(sentences) >= 2:
        proper = all(s[0].isupper() and s[-1] in ".!?" for s in sentences)
        if proper and not re.search(r"\b(lol|haha|hehe|omg|tbh|u|ur|im|dont|cant|gonna|wanna)\b", t, re.I):
            signals.append("every sentence capitalised and terminated, no chat shorthand")
        lens = [len(s.split()) for s in sentences]
        mean = sum(lens) / len(lens)
        var = sum((x - mean) ** 2 for x in lens) / len(lens)
        if len(sentences) >= 3 and mean >= 9 and var < 6:
            signals.append("sentences of near-identical length")
    if re.search(r"[\u2014\u2013;\u201C\u201D\u2018\u2019]", t):
        signals.append("typographic dashes, semicolons or curly quotes")
    words = len(t.split())
    if words >= 45 and len(sentences) >= 3 and not re.search(r"[!?]", t):
        signals.append("long reply with no question or exclamation")
    if typing_seconds is not None and typing_seconds > 0 and len(t) / typing_seconds > 9:
        signals.append(f"{len(t) / typing_seconds:.0f} characters per second")
    return {"flag": len(signals) >= 2, "signals": signals, "score": len(signals)}
