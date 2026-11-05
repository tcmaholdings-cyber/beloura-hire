"""The agent's templated messages: first contact, declines, nudges, reminders, booking confirmations. Plain, short, no emoji.
The name comes from AGENT_NAME (default James, per the plan amendment of 2026-11-04; set AGENT_NAME=Sam to use the earlier name)."""
from __future__ import annotations

import os

AGENT_NAME = os.environ.get("AGENT_NAME", "James").strip() or "James"
DISCLOSURE = (f"I'm {AGENT_NAME}, Beloura's recruiting assistant. I'm automated, and people on the team review every decision. "
              "You can ask me anything about the role or the process, or ask for a person at any point.")


def first_message(name: str, next_step: str, telegram_link: str = "", discord_invite: str = "") -> str:
    hi = f"Hi {name}," if name else "Hi,"
    body = (f"{hi} thanks for applying to Beloura.\n\n{DISCLOSURE}\n\n"
            "How it works: screening is automatic and already done. Next is a short text interview with me, five questions, about ten minutes. "
            "Then the assessment, about 15 minutes in one sitting. The final interview is a 30-minute mock chat with one of our chat managers, "
            "usually within a week. You hear back within two working days of the final.\n\n"
            f"Where you are now: {next_step}")
    links = []
    if telegram_link:
        links.append(f"Telegram: open {telegram_link} and tap Start.")
    if discord_invite:
        links.append(f"Join our Discord: {discord_invite} (use the same Discord username you put on the form).")
    if links:
        body += "\n\nI can continue with you on " + ("either of these" if len(links) > 1 else "this") + ":\n" + "\n".join(links)
    return body


def decline(name: str, reasons: list[str]) -> str:
    hi = f"Hi {name}," if name else "Hi,"
    why = "; ".join(reasons) if reasons else "the role's basic requirements"
    return (f"{hi} thanks for applying to Beloura. We can't take your application forward this time because of {why}. "
            f"If that changes you're welcome to apply again. All the best, {AGENT_NAME}")


def rejected(name: str) -> str:
    hi = f"Hi {name}," if name else "Hi,"
    return (f"{hi} thank you for your time with us. After review we won't be moving forward with your application. "
            f"We appreciate you applying and wish you well. {AGENT_NAME}")


NUDGES = [
    "Hi {name}, just checking in. Did you see my last message? Reply here whenever you're ready and I'll pick it up.",
    "Hi {name}, a quick follow-up from Beloura. Your file is still open. If you'd like to continue, just reply and we carry on from where we left off.",
    "Hi {name}, last check-in from me. If I don't hear back I'll pause your application. A reply at any time reopens it.",
]


def nudge(name: str, n: int) -> str:
    i = max(0, min(n, len(NUDGES) - 1))
    return NUDGES[i].format(name=name or "there")


def reminder_24h(name: str, when_both: str, manager: str) -> str:
    return (f"Hi {name}, a reminder that your final interview with {manager} is tomorrow: {when_both}. "
            "It's a 30-minute mock chat on Discord. Reply if you need to move it.")


def reminder_1h(name: str, when_both: str, manager: str) -> str:
    return f"Hi {name}, your final interview with {manager} starts in about an hour: {when_both}. Please be on Discord a few minutes early."


def booked(name: str, when_both: str, manager: str) -> str:
    return (f"Done, {name}. Your final interview with {manager} is booked for {when_both}. It's a 30-minute mock chat on Discord. "
            "I'll remind you a day before and an hour before. Reply if you need to change it.")


def rescheduled(name: str, when_both: str, manager: str) -> str:
    return f"Moved. Your final interview with {manager} is now {when_both}. I'll send reminders before it."


def cancelled(name: str) -> str:
    return f"Your interview is cancelled, {name}. When you're ready to pick a new time, tell me and I'll send the open slots."


def telegram_linked(name: str) -> str:
    return f"Got you here on Telegram, {name}. I'll continue with you here. Ask me anything about the role or the process."


def will_check() -> str:
    return "Good question. I don't have that on file, so I'll check with the team and come back to you here."


def answer(answer_text: str) -> str:
    return f"I checked on this. {answer_text}"


def ghosted() -> str:
    return "Welcome back. I've reopened your file. Where were we?"


def no_slots() -> str:
    return "There are no open interview slots in the next week yet. I've asked the team to add some and I'll message you as soon as they're in."


def plain(text: str) -> str:
    """Markdown out, plain text in: Telegram and Discord messages are sent as plain text."""
    import re
    s = text or ""
    s = re.sub(r"```[a-zA-Z]*\n?", "", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s, flags=re.S)
    s = re.sub(r"(?<![A-Za-z0-9])[*_](?=\S)(.+?)(?<=\S)[*_](?![A-Za-z0-9])", r"\1", s, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", s, flags=re.M)
    s = re.sub(r"^[ \t]*>[ \t]?", "", s, flags=re.M)
    s = re.sub(r"^[ \t]*[*\-][ \t]+", "- ", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def discord_welcome(name: str) -> str:
    return (f"Hi {name}, this is your private channel with me, {AGENT_NAME}, Beloura's recruiting assistant. Only you and the hiring team can see it. "
            "I'm automated and people on the team review every decision. Ask me anything about the role or the process here, and I'll keep you posted on each step.")


def lounge_prompt(mention: str) -> str:
    return f"Welcome {mention}. I can't match your Discord name to an application yet. Reply here with the email you applied with and I'll link you up."


def lounge_linked(mention: str) -> str:
    return f"Thanks {mention}, found you. I've opened a private channel for us; see your channel list."


def join_discord(name: str, invite: str = "") -> str:
    who = f", {name}" if name and name != "there" else ""
    inv = f"\n\n{invite}" if invite else ""
    return (f"Great{who} — we run the interview and the rest of the process on our Discord, so it's all in one place. "
            f"Join here and I'll open your private channel and we'll carry on there.{inv}\n\n"
            f"I'll still send you reminders on Telegram.")


def interview_intro(name: str) -> str:
    return (f"Before the assessment I have five short questions, {name}. Answer in your own words, a few sentences each is fine. "
            "This is the same interview everyone gets, and a person reviews the answers along with my notes.")


INTERVIEW_QUESTIONS = [
    "1 of 5. Tell me about yourself and your previous experience, especially anything in chatting, sales or customer support.",
    "2 of 5. If you worked for an agency before, why did you leave? If you haven't, tell me what you know about how OnlyFans agencies work.",
    "3 of 5. We're big on GFE, the girlfriend experience. Are you familiar with it? Explain briefly what it means and how you'd deliver it in chat.",
    "4 of 5. How many models could you handle at once on a shift, and how would you keep them straight?",
    "5 of 5. What's your biggest challenge in chatting, or what do you expect it to be?",
]


def interview_ack(n: int) -> str:
    return ["Thanks.", "Got it.", "Noted.", "Thank you."][n % 4]


def interview_passed(name: str) -> str:
    return (f"That's all five, {name}, thank you. I've passed your answers to the team with my notes. Your next step is the assessment: about 15 minutes "
            "in one sitting covering connection, typing, English and six short simulated fan chats. I'll send the link here as soon as it's ready.")


def interview_failed(name: str) -> str:
    return (f"That's all five, {name}, thank you for taking the time. Having reviewed your answers with the team, we won't be moving forward this time. "
            "You're welcome to apply again in the future. All the best, " + AGENT_NAME)


def interview_resume(name: str) -> str:
    return f"Back to the interview, {name}."
