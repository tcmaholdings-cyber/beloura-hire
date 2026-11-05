"""Channels: Telegram (raw Bot API), Discord (discord.py, DMs + the staff #hiring post with buttons), email (SMTP out, IMAP in).
Every inbound lands in one place, sam.handle_inbound, and the answer goes back on the same channel.
Missing credentials = dry-run: sends are logged and recorded with provider id 'dry-run'."""
from __future__ import annotations

import asyncio
import email
import email.utils
import hashlib
import hmac
import imaplib
import json
import logging
import os
import re
import smtplib
import threading
from email.header import decode_header, make_header
from email.message import EmailMessage

import httpx

import db
import engine
import sam
import texts

log = logging.getLogger("hire.channels")


def public_url(path: str = "") -> str:
    return os.environ.get("HIRE_PUBLIC_URL", "https://hire.beloura.ai").rstrip("/") + path


def start_token(aid: int) -> str:
    secret = os.environ.get("HIRE_SECRET", "dev-secret").encode()
    return f"{aid}-{hmac.new(secret, f'tg:{aid}'.encode(), hashlib.sha256).hexdigest()[:12]}"


def applicant_from_token(con, token: str) -> dict | None:
    m = re.match(r"^(\d+)-([0-9a-f]{12})$", (token or "").strip())
    if not m:
        return None
    aid = int(m.group(1))
    if not hmac.compare_digest(start_token(aid), token.strip()):
        return None
    return engine.applicant(con, aid)


# ---------------------------------------------------------------- Telegram

class TelegramChannel:
    name = "telegram"

    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
        self.bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
        self.secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        self.staff_chat_id = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")
        self.offset = 0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def deep_link(self, aid: int) -> str:
        return f"https://t.me/{self.bot_username}?start={start_token(aid)}" if self.bot_username else ""

    def _call(self, method: str, **payload) -> dict:
        r = httpx.post(f"{self.base}/bot{self.token}/{method}", json=payload, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"telegram {r.status_code}: {r.text[:200]}")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram: {str(data)[:200]}")
        return data.get("result") or {}

    def send(self, chat_id: str, text: str) -> str:
        if not self.configured:
            log.info("telegram dry-run to %s: %s", chat_id, text[:80])
            return "dry-run"
        res = self._call("sendMessage", chat_id=chat_id, text=text[:4096], disable_web_page_preview=True)
        return str(res.get("message_id") or "sent")

    def send_staff(self, text: str) -> str:
        if not (self.configured and self.staff_chat_id):
            log.info("telegram staff dry-run: %s", text[:80])
            return "dry-run"
        return self.send(self.staff_chat_id, text)

    def set_webhook(self) -> None:
        if not (self.configured and self.secret):
            return
        self._call("setWebhook", url=public_url(f"/hooks/telegram/{self.secret}"), secret_token=self.secret, allowed_updates=["message"])

    def delete_webhook(self) -> None:
        if self.configured:
            self._call("deleteWebhook")

    def poll(self, con) -> int:
        """Long-polling for local dev (HIRE_TELEGRAM_POLL=1). Returns updates handled."""
        if not self.configured:
            return 0
        res = self._call("getUpdates", offset=self.offset, timeout=0, allowed_updates=["message"])
        n = 0
        for upd in res if isinstance(res, list) else []:
            self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
            self.handle_update(con, upd)
            n += 1
        return n

    def handle_update(self, con, update: dict) -> str | None:
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat_id = str(chat.get("id") or "")
        if not chat_id or chat.get("type") not in (None, "private") or sender.get("is_bot"):
            return None
        username = sender.get("username") or ""
        ext = f"{chat_id}:{msg.get('message_id')}" if msg.get("message_id") else None
        a = db.one(con, "SELECT * FROM applicant WHERE telegram_chat_id=?", (chat_id,))
        if text.startswith("/start"):
            token = text[6:].strip()
            linked = applicant_from_token(con, token) if token else None
            if linked:
                a = linked
            elif not a and username:
                a = db.one(con, "SELECT * FROM applicant WHERE lower(telegram)=?", (username.lower(),))
            if not a:
                self.send(chat_id, f"Hi, I'm {texts.AGENT_NAME}, Beloura's recruiting assistant. I don't have an application from this account yet. Apply at {public_url('/apply/telegram')} and I'll pick it up here.")
                return None
            fresh = not a.get("telegram_chat_id")
            engine.link_telegram(con, a["id"], chat_id, username)
            engine.record_inbound(con, a["id"], "telegram", text, external_id=ext, address=chat_id)
            if engine.flush_held(con, a["id"], channel="telegram") == 0 and fresh:
                first = (a["name"] or "").split(" ")[0]
                engine.deliver(con, a["id"], texts.telegram_linked(first), kind="sam", channel="telegram")
            return "linked"
        if not a and username:
            a = db.one(con, "SELECT * FROM applicant WHERE lower(telegram)=?", (username.lower(),))
            if a:
                engine.link_telegram(con, a["id"], chat_id, username)
                engine.flush_held(con, a["id"], channel="telegram")
        if not a:
            self.send(chat_id, f"Hi, I'm {texts.AGENT_NAME}, Beloura's recruiting assistant. I can't match this account to an application. If you applied, open the Telegram link on your confirmation page, or apply at {public_url('/apply/telegram')}.")
            return None
        if not text:
            return None
        return sam.handle_inbound(con, a["id"], "telegram", text, external_id=ext, address=chat_id)


# ---------------------------------------------------------------- Discord

STAFF_TEXT_CHANNELS = [
    ("hiring-queue", "discord_ch_hiring_queue", "Every new applicant lands here with Open, Fast-track and Reject. Replies in a summary's thread are notes for the team."),
    ("escalations", "discord_ch_escalations", "Questions the assistant could not answer. Reply inside the thread under one and the answer goes to the applicant and into the knowledge base."),
    ("results", "discord_ch_results", "Scorecards and interview results."),
    ("weekly-report", "discord_ch_weekly_report", "Weekly channel and funnel report."),
    ("staff-chat", "discord_ch_staff_chat", "Team chat."),
]

WELCOME_TEXT = f"""Welcome to Beloura hiring.

Your contact here is {texts.AGENT_NAME}, Beloura's recruiting assistant. {texts.AGENT_NAME} is automated, and people on the team review every decision. When you join, {texts.AGENT_NAME} opens a private channel that only you and the hiring team can see; that is where the conversation happens.

How the process works:
1. Application and screening: automatic, done within a minute of the form.
2. A short text interview with {texts.AGENT_NAME}: five questions, about ten minutes.
3. Assessment: about 45 minutes in one sitting (connection, typing, English, six short simulated fan chats).
4. Final interview: a 30-minute mock chat with one of our chat managers, held in the Mock Chat voice room here.
5. Decision and offer: you hear back within two working days of the final.

House tone: short, warm, plain English. No emoji. Ask {texts.AGENT_NAME} anything about the role, the pay or the shifts, and ask for a person at any time.
If you landed here without a private channel, say hello in #applicant-lounge with the email you applied with."""


def match_member(con, user_id: str, username: str | None, display_name: str | None = None) -> dict | None:
    """The applicant behind a Discord member: linked id first, then the username or display name given on the form (case-insensitive, @ stripped)."""
    a = db.one(con, "SELECT * FROM applicant WHERE discord_user_id=?", (str(user_id),))
    if a:
        return a
    for cand in (username, display_name):
        h = engine.norm_handle(cand)
        if h:
            a = db.one(con, "SELECT * FROM applicant WHERE lower(discord)=? AND (discord_user_id IS NULL OR discord_user_id='' OR discord_user_id=?)", (h, str(user_id)))
            if a:
                return a
    return None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:20] or "applicant"


def link_member(con, a: dict, user_id: str, username: str | None, actions) -> dict:
    """The join flow once an applicant is identified: role, private channel, first message, held messages, staff note, interview."""
    aid = a["id"]
    first = (a["name"] or "").split(" ")[0] or "there"
    actions.assign_role(user_id)
    chan = a.get("discord_channel_id") or actions.create_private_channel(aid, f"app-{aid}-{_slug(first)}", user_id)
    engine.link_discord(con, aid, user_id, username, channel_id=chan)
    had_first = db.one(con, "SELECT id FROM message WHERE applicant_id=? AND kind='first' AND status='sent'", (aid,))
    a = engine.applicant(con, aid)
    released = engine.flush_held(con, aid, channel="discord")
    if had_first or released:
        engine.deliver(con, aid, texts.discord_welcome(first), kind="sam", channel="discord")
    else:
        engine.deliver(con, aid, texts.first_message(first, engine.next_step(con, a)), kind="first", channel="discord")
    actions.dm(user_id, f"Hi {first}, I've opened a private channel for us in the Beloura hiring server; I'll continue with you there.")
    actions.staff_note(aid, f"joined Discord as {username or user_id}")
    started = sam.start_interview_if_due(con, aid, "discord")
    return {"matched": True, "applicant_id": aid, "channel_id": chan, "released": released, "interview_started": started}


def member_joined(con, user_id: str, username: str | None, display_name: str | None, actions) -> dict:
    a = match_member(con, user_id, username, display_name)
    if not a:
        actions.lounge_prompt(user_id, texts.lounge_prompt(f"<@{user_id}>"))
        return {"matched": False, "user_id": str(user_id)}
    return link_member(con, a, user_id, username, actions)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def lounge_message(con, user_id: str, username: str | None, display_name: str | None, text: str, actions) -> dict:
    """An unlinked member writing in #applicant-lounge: link them by the email they applied with."""
    if db.one(con, "SELECT id FROM applicant WHERE discord_user_id=?", (str(user_id),)):
        return {"matched": True, "already": True}
    m = EMAIL_RE.search(text or "")
    a = db.one(con, "SELECT * FROM applicant WHERE lower(email)=?", (m.group(0).lower(),)) if m else None
    if not a:
        a = match_member(con, user_id, username, display_name)
    if not a or (a.get("discord_user_id") and a["discord_user_id"] != str(user_id)):
        return {"matched": False}
    out = link_member(con, a, user_id, username, actions)
    actions.lounge_prompt(user_id, texts.lounge_linked(f"<@{user_id}>"))
    return out


class DryActions:
    """Join-flow actions without Discord: everything is logged, ids are synthetic."""

    def assign_role(self, user_id):
        log.info("discord dry-run: role Applicant to %s", user_id)

    def create_private_channel(self, aid, name, user_id):
        log.info("discord dry-run: private channel %s", name)
        return f"dry-{aid}"

    def dm(self, user_id, text):
        log.info("discord dry-run dm %s: %s", user_id, text[:60])

    def staff_note(self, aid, text):
        log.info("discord dry-run staff note #%s: %s", aid, text)

    def lounge_prompt(self, user_id, text):
        log.info("discord dry-run lounge: %s", text[:80])


async def setup_guild(guild, con, say=print) -> dict:
    """Create roles, categories, channels, the welcome post and a permanent invite. Idempotent by name; ids go to settings."""
    import discord
    from discord import PermissionOverwrite as PO

    me = guild.me
    perms = getattr(getattr(me, "guild_permissions", None), "administrator", False)
    if not perms:
        raise PermissionError("the bot is not Administrator on this server; grant it and run again")
    everyone = guild.default_role
    out = {}

    def find_role(name):
        return next((r for r in guild.roles if r.name == name), None)

    def find_channel(name, ctype):
        return next((c for c in guild.channels if c.name == name and c.type == ctype), None)

    roles = {}
    for name in ("Manager", "Applicant", "Trainee"):
        r = find_role(name)
        if not r:
            r = await guild.create_role(name=name, reason="hire setup")
            say(f"created role {name}")
        roles[name] = r
        out[f"discord_role_{name.lower()}"] = str(r.id)
    manager, applicant_role = roles["Manager"], roles["Applicant"]

    staff_ow = {everyone: PO(view_channel=False), manager: PO(view_channel=True, send_messages=True, read_message_history=True),
                me: PO(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, create_public_threads=True, create_private_threads=True, send_messages_in_threads=True)}
    appl_ow = {everyone: PO(view_channel=True, send_messages=True, read_message_history=True),
               me: PO(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, manage_permissions=True)}
    finals_ow = {everyone: PO(view_channel=False, connect=False), manager: PO(view_channel=True, send_messages=True, connect=True, speak=True, stream=True, read_message_history=True),
                 me: PO(view_channel=True, send_messages=True, manage_channels=True, manage_permissions=True, connect=True, create_public_threads=True, send_messages_in_threads=True)}

    cats = {}
    for name, ow, key in (("STAFF", staff_ow, "discord_cat_staff"), ("APPLICANTS", appl_ow, "discord_cat_applicants"), ("FINALS", finals_ow, "discord_cat_finals")):
        c = find_channel(name, discord.ChannelType.category)
        if not c:
            c = await guild.create_category(name, overwrites=ow, reason="hire setup")
            say(f"created category {name}")
        cats[name] = c
        out[key] = str(c.id)

    general = find_channel("general", discord.ChannelType.text)
    for name, key, topic in STAFF_TEXT_CHANNELS:
        ch = find_channel(name, discord.ChannelType.text)
        if not ch and name == "staff-chat" and general is not None:
            await general.edit(name="staff-chat", category=cats["STAFF"], topic=topic, sync_permissions=True)
            ch = general
            general = None
            say("renamed #general to #staff-chat and moved it under STAFF")
        if not ch:
            ch = await guild.create_text_channel(name, category=cats["STAFF"], topic=topic, reason="hire setup")
            say(f"created #{name}")
        out[key] = str(ch.id)
    out["staff_discord_channel_id"] = out["discord_ch_hiring_queue"]

    welcome = find_channel("welcome", discord.ChannelType.text)
    if not welcome:
        welcome = await guild.create_text_channel("welcome", category=cats["APPLICANTS"], topic="How the process works. Read-only.", reason="hire setup",
                                                  overwrites={everyone: PO(view_channel=True, send_messages=False, add_reactions=False, read_message_history=True),
                                                              applicant_role: PO(send_messages=False), me: PO(view_channel=True, send_messages=True, manage_messages=True)})
        say("created #welcome")
    out["discord_ch_welcome"] = str(welcome.id)
    lounge = find_channel("applicant-lounge", discord.ChannelType.text)
    if not lounge:
        lounge = await guild.create_text_channel("applicant-lounge", category=cats["APPLICANTS"], reason="hire setup",
                                                 topic="Say hello. If you have no private channel yet, post the email you applied with and the assistant links you.")
        say("created #applicant-lounge")
    out["discord_ch_lounge"] = str(lounge.id)

    forum = find_channel("finals", discord.ChannelType.forum)
    if not forum:
        forum = await guild.create_forum("finals", category=cats["FINALS"], topic="One thread per applicant who reaches the mock chat: briefing pack and verdict.", reason="hire setup")
        say("created forum #finals")
    out["discord_forum_finals"] = str(forum.id)
    voice = find_channel("Mock Chat | Ian", discord.ChannelType.voice)
    if not voice:
        old = find_channel("General", discord.ChannelType.voice)
        if old is not None:
            await old.edit(name="Mock Chat | Ian", category=cats["FINALS"], sync_permissions=True)
            voice = old
            say("renamed voice General to Mock Chat | Ian and moved it under FINALS")
        else:
            voice = await guild.create_voice_channel("Mock Chat | Ian", category=cats["FINALS"], reason="hire setup")
            say("created voice Mock Chat | Ian")
    out["discord_voice_mock"] = str(voice.id)

    for leftover in ("Text channels", "Voice channels", "Text Channels", "Voice Channels"):
        c = find_channel(leftover, discord.ChannelType.category)
        if c is not None and not [x for x in guild.channels if getattr(x, "category_id", None) == c.id]:
            await c.delete(reason="hire setup: empty default category")
            say(f"deleted empty category {leftover}")

    if not db.setting(con, "discord_welcome_msg_id", ""):
        msg = await welcome.send(WELCOME_TEXT[:2000])
        out["discord_welcome_msg_id"] = str(msg.id)
        say("posted the welcome message")
    invite_url = db.setting(con, "discord_invite_url", "")
    if not invite_url:
        inv = await welcome.create_invite(max_age=0, max_uses=0, unique=False, reason="hire setup: permanent applicant invite")
        invite_url = inv.url
        say(f"created permanent invite {invite_url}")
    out["discord_invite_url"] = invite_url
    for k, v in out.items():
        db.set_setting(con, k, v)
    return out


class DiscordChannel:
    """Applicant channels (private channel per applicant, DMs as a fallback), the staff posts, and the guild events.
    discord.py runs on its own thread and event loop; every call from the app is scheduled onto it and waited for."""
    name = "discord"

    def __init__(self):
        self.token = os.environ.get("DISCORD_BOT_TOKEN", "")
        self.guild_id = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
        self.staff_channel_id = int(os.environ.get("DISCORD_STAFF_CHANNEL_ID", "0") or 0)
        self.client = None
        self.loop = None
        self.ready = threading.Event()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def setting_id(self, key: str) -> int:
        with db.db() as con:
            v = db.setting(con, key, "")
        return int(v) if v.isdigit() else 0

    def start(self) -> None:
        if not self.configured or self.client is not None:
            return
        threading.Thread(target=self._run, name="hire-discord", daemon=True).start()

    def _run(self) -> None:
        import discord
        from discord import ui

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.dm_messages = True
        client = discord.Client(intents=intents)
        channel = self

        def _aid_from(message) -> int:
            m = re.search(r"/staff/applicant/(\d+)", (message.content if message else "") or "")
            return int(m.group(1)) if m else 0

        class StaffView(ui.View):
            def __init__(self, aid: int = 0, url: str = ""):
                super().__init__(timeout=None)
                if url:
                    self.add_item(ui.Button(label="Open", url=url, style=discord.ButtonStyle.link))

            @ui.button(label="Fast-track", style=discord.ButtonStyle.success, custom_id="hire:fasttrack")
            async def fasttrack(self, interaction, button):
                out = await asyncio.to_thread(staff_action, _aid_from(interaction.message), "fasttrack", interaction.user.name if interaction.user else "discord")
                await interaction.response.send_message(out, ephemeral=True)

            @ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="hire:reject")
            async def reject(self, interaction, button):
                out = await asyncio.to_thread(staff_action, _aid_from(interaction.message), "reject", interaction.user.name if interaction.user else "discord")
                await interaction.response.send_message(out, ephemeral=True)

        class VerdictView(ui.View):
            def __init__(self, aid: int = 0, url: str = ""):
                super().__init__(timeout=None)
                if url:
                    self.add_item(ui.Button(label="Open", url=url, style=discord.ButtonStyle.link))

            async def _do(self, interaction, verdict):
                starter = interaction.message
                aid = _aid_from(starter)
                if not aid and hasattr(interaction.channel, "starter_message"):
                    try:
                        aid = _aid_from(interaction.channel.starter_message or await interaction.channel.fetch_message(interaction.channel.id))
                    except Exception:  # noqa: BLE001
                        aid = 0
                out = await asyncio.to_thread(verdict_action, aid, verdict, interaction.user.name if interaction.user else "discord")
                await interaction.response.send_message(out)

            @ui.button(label="Hire", style=discord.ButtonStyle.success, custom_id="hire:verdict:hire")
            async def hire(self, interaction, button):
                await self._do(interaction, "hire")

            @ui.button(label="Second look", style=discord.ButtonStyle.secondary, custom_id="hire:verdict:second_look")
            async def second(self, interaction, button):
                await self._do(interaction, "second_look")

            @ui.button(label="No", style=discord.ButtonStyle.danger, custom_id="hire:verdict:no")
            async def no(self, interaction, button):
                await self._do(interaction, "no")

        channel.view_cls = StaffView
        channel.verdict_cls = VerdictView

        @client.event
        async def on_ready():
            client.add_view(StaffView())
            client.add_view(VerdictView())
            channel.ready.set()
            log.info("discord ready as %s", client.user)

        @client.event
        async def on_member_join(member):
            if member.bot or (channel.guild_id and member.guild.id != channel.guild_id):
                return
            await asyncio.to_thread(channel._joined, str(member.id), member.name, member.display_name)

        @client.event
        async def on_message(message):
            if message.author.bot:
                return
            text = (message.content or "").strip()
            if message.guild is None:
                if text:
                    await asyncio.to_thread(channel._inbound_dm, str(message.author.id), message.author.name, text, str(message.id))
                return
            if channel.guild_id and message.guild.id != channel.guild_id:
                return
            await asyncio.to_thread(channel._inbound_guild, message.channel, str(message.author.id), message.author.name,
                                    getattr(message.author, "display_name", None), text, str(message.id))

        self.client = client
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(client.start(self.token))
        except Exception as e:  # noqa: BLE001
            log.error("discord stopped: %s", e)

    # ---- inbound (worker threads)

    def _joined(self, user_id: str, username: str, display_name: str | None) -> None:
        try:
            with db.db() as con:
                out = member_joined(con, user_id, username, display_name, LiveActions(self))
            log.info("discord join %s: %s", username, out)
        except Exception as e:  # noqa: BLE001
            log.exception("discord join failed: %s", e)

    def _inbound_dm(self, user_id: str, username: str, text: str, message_id: str) -> None:
        with db.db() as con:
            a = match_member(con, user_id, username)
            if not a:
                self._dm_now(user_id, f"Hi, I'm {texts.AGENT_NAME}, Beloura's recruiting assistant. I can't match this account to an application. "
                                      f"Apply at {public_url('/apply/discord')} with this Discord username, or join the server and post your email in #applicant-lounge.")
                return
            if not a.get("discord_user_id"):
                engine.link_discord(con, a["id"], user_id, username)
            sam.handle_inbound(con, a["id"], "discord", text, external_id=message_id, address=user_id)

    def _inbound_guild(self, ch, user_id: str, username: str, display_name: str | None, text: str, message_id: str) -> None:
        import discord
        cid = str(ch.id)
        with db.db() as con:
            if isinstance(ch, discord.Thread):
                e = db.one(con, "SELECT * FROM escalation WHERE discord_thread_id=? AND status='open'", (cid,))
                if e and text:
                    engine.answer_escalation(con, e["id"], text, by=username, save_knowledge=True)
                    a = engine.applicant(con, e["applicant_id"]) or {}
                    self._say(ch, f"Sent to {a.get('name') or '#' + str(e['applicant_id'])} and saved to the knowledge base.")
                return
            a = db.one(con, "SELECT * FROM applicant WHERE discord_channel_id=?", (cid,))
            if a:
                if text:
                    if not a.get("discord_user_id"):
                        engine.link_discord(con, a["id"], user_id, username)
                    sam.handle_inbound(con, a["id"], "discord", text, external_id=message_id, address=cid)
                return
            if cid == db.setting(con, "discord_ch_lounge", ""):
                lounge_message(con, user_id, username, display_name, text, LiveActions(self))

    # ---- scheduling onto the bot loop

    def _submit(self, coro, timeout: float = 25):
        if not self.loop:
            raise RuntimeError("discord loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def _say(self, ch, text: str) -> None:
        try:
            self._submit(ch.send(text[:2000]))
        except Exception as e:  # noqa: BLE001
            log.warning("discord say failed: %s", e)

    async def _guild(self):
        g = self.client.get_guild(self.guild_id) if self.guild_id else None
        if g is None and self.client.guilds:
            g = self.client.guilds[0]
        if g is None and self.guild_id:
            g = await self.client.fetch_guild(self.guild_id)
        return g

    async def _channel(self, cid: int):
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def _resolve(self, user_id: str, handle: str):
        import discord
        if user_id:
            try:
                return await self.client.fetch_user(int(user_id))
            except Exception:  # noqa: BLE001
                pass
        if not handle:
            return None
        h = handle.lower()
        g = await self._guild()
        for gg in ([g] if g else []):
            for m in gg.members:
                if m.name.lower() == h or (m.global_name or "").lower() == h or (m.display_name or "").lower() == h:
                    return m
            try:
                for m in await gg.query_members(query=handle, limit=5):
                    if m.name.lower() == h or (m.global_name or "").lower() == h:
                        return m
            except (discord.HTTPException, discord.Forbidden):
                pass
        return None

    async def _dm(self, user_id: str, handle: str, text: str) -> str:
        user = await self._resolve(user_id, handle)
        if not user:
            raise RuntimeError(f"discord user {handle or user_id} not found in the server")
        msg = await user.send(text[:2000])
        with db.db() as con:
            a = db.one(con, "SELECT id FROM applicant WHERE lower(discord)=? OR discord_user_id=?", (handle.lower() if handle else "", str(user.id)))
            if a:
                engine.link_discord(con, a["id"], str(user.id), getattr(user, "name", None))
        return str(msg.id)

    def _dm_now(self, user_id: str, text: str) -> None:
        try:
            self._submit(self._dm(user_id, "", text))
        except Exception as e:  # noqa: BLE001
            log.warning("discord dm failed: %s", e)

    async def _send_applicant(self, applicant: dict, text: str) -> str:
        import discord
        cid = applicant.get("discord_channel_id")
        if cid and str(cid).isdigit():
            try:
                ch = await self._channel(int(cid))
                uid = applicant.get("discord_user_id")
                body = f"<@{uid}>\n{text}" if uid and str(uid).isdigit() else text
                msg = await ch.send(body[:2000], allowed_mentions=discord.AllowedMentions(users=True))
                return str(msg.id)
            except Exception as e:  # noqa: BLE001
                log.warning("discord channel %s send failed, trying DM: %s", cid, e)
        return await self._dm(applicant.get("discord_user_id") or "", applicant.get("discord") or "", text)

    def send(self, applicant: dict, text: str) -> str:
        if not self.configured:
            log.info("discord dry-run to %s: %s", applicant.get("discord") or applicant.get("discord_user_id"), text[:80])
            return "dry-run"
        if not self.ready.wait(timeout=15):
            raise RuntimeError("discord not ready")
        return self._submit(self._send_applicant(applicant, text))

    async def _post(self, channel_id: int, text: str, view=None, thread_name: str | None = None) -> tuple[str, str]:
        ch = await self._channel(channel_id)
        msg = await ch.send(text[:2000], view=view)
        tid = ""
        if thread_name:
            try:
                th = await msg.create_thread(name=thread_name[:100])
                tid = str(th.id)
            except Exception as e:  # noqa: BLE001
                log.warning("thread on %s failed: %s", msg.id, e)
        return str(msg.id), tid

    def post_staff(self, text: str, aid: int = 0, url: str = "", channel_id: int | None = None) -> str:
        cid = channel_id or self.setting_id("discord_ch_hiring_queue") or self.staff_channel_id
        if not (self.configured and cid):
            log.info("discord staff dry-run: %s", text[:80])
            return "dry-run"
        if not self.ready.wait(timeout=15):
            raise RuntimeError("discord not ready")
        view = self.view_cls(aid, url) if aid else None
        mid, _ = self._submit(self._post(cid, text, view))
        if aid:
            with db.db() as con:
                engine.meta_set(con, aid, "staff_msg", {"channel_id": str(cid), "message_id": mid})
        return mid

    def post_escalation(self, text: str, eid: int, aid: int) -> str:
        cid = self.setting_id("discord_ch_escalations") or self.setting_id("discord_ch_hiring_queue") or self.staff_channel_id
        if not (self.configured and cid):
            log.info("discord escalation dry-run: %s", text[:80])
            return "dry-run"
        if not self.ready.wait(timeout=15):
            raise RuntimeError("discord not ready")
        with db.db() as con:
            a = engine.applicant(con, aid) or {}
        mid, tid = self._submit(self._post(cid, text + "\nReply in this thread to answer the applicant.", None, thread_name=f"#{eid} {a.get('name') or 'applicant ' + str(aid)}"))
        if tid:
            with db.db() as con:
                con.execute("UPDATE escalation SET discord_thread_id=? WHERE id=?", (tid, eid))
        return mid

    async def _staff_note(self, aid: int, text: str) -> None:
        with db.db() as con:
            a = engine.applicant(con, aid) or {}
        ref = engine.meta_get(a, "staff_msg") or {}
        if ref.get("message_id"):
            try:
                ch = await self._channel(int(ref["channel_id"]))
                msg = await ch.fetch_message(int(ref["message_id"]))
                th = msg.thread or await msg.create_thread(name=f"#{aid} {a.get('name') or 'applicant'}"[:100])
                await th.send(text[:2000])
                return
            except Exception as e:  # noqa: BLE001
                log.warning("staff note thread failed: %s", e)
        cid = self.setting_id("discord_ch_hiring_queue") or self.staff_channel_id
        if cid:
            ch = await self._channel(cid)
            await ch.send(f"#{aid} {a.get('name') or ''}: {text}"[:2000])

    async def _create_private_channel(self, aid: int, name: str, user_id: str) -> str:
        from discord import PermissionOverwrite as PO
        g = await self._guild()
        cat_id = self.setting_id("discord_cat_applicants")
        cat = g.get_channel(cat_id) if cat_id else None
        member = g.get_member(int(user_id)) or await g.fetch_member(int(user_id))
        manager_id = self.setting_id("discord_role_manager")
        ow = {g.default_role: PO(view_channel=False), member: PO(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
              g.me: PO(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True)}
        if manager_id and g.get_role(manager_id):
            ow[g.get_role(manager_id)] = PO(view_channel=True, send_messages=True, read_message_history=True)
        ch = await g.create_text_channel(name[:100], category=cat, overwrites=ow, topic=f"Private channel: applicant #{aid} and the hiring team.", reason="applicant joined")
        return str(ch.id)

    async def _assign_role(self, user_id: str) -> None:
        g = await self._guild()
        rid = self.setting_id("discord_role_applicant")
        role = g.get_role(rid) if rid else None
        if role:
            member = g.get_member(int(user_id)) or await g.fetch_member(int(user_id))
            await member.add_roles(role, reason="applicant joined")

    async def _lounge(self, text: str) -> None:
        cid = self.setting_id("discord_ch_lounge")
        if cid:
            ch = await self._channel(cid)
            await ch.send(text[:2000])

    async def _open_final(self, aid: int, iv: dict) -> None:
        from discord import PermissionOverwrite as PO
        g = await self._guild()
        with db.db() as con:
            a = engine.applicant(con, aid) or {}
            app = engine.latest_application(con, aid) or {}
            url = public_url(f"/staff/applicant/{aid}")
            pack = briefing_pack(a, app, iv, url)
        vid = self.setting_id("discord_voice_mock")
        if vid and a.get("discord_user_id"):
            try:
                voice = await self._channel(vid)
                member = g.get_member(int(a["discord_user_id"])) or await g.fetch_member(int(a["discord_user_id"]))
                await voice.set_permissions(member, overwrite=PO(view_channel=True, connect=True, speak=True, stream=True), reason=f"final booked for applicant #{aid}")
            except Exception as e:  # noqa: BLE001
                log.warning("voice overwrite failed: %s", e)
        fid = self.setting_id("discord_forum_finals")
        if not fid:
            return
        forum = await self._channel(fid)
        prev = db.one(db.connect(), "SELECT discord_thread_id FROM interview WHERE applicant_id=? AND discord_thread_id<>'' ORDER BY id DESC LIMIT 1", (aid,)) or {}
        if prev.get("discord_thread_id") and iv.get("status") == "rescheduled":
            try:
                th = await self._channel(int(prev["discord_thread_id"]))
                await th.send(f"Moved: {engine.when_both(iv['start_at'], db.HOUSE_TZ)}.")
                with db.db() as con:
                    con.execute("UPDATE interview SET discord_thread_id=? WHERE id=?", (prev["discord_thread_id"], iv["id"]))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("finals thread update failed: %s", e)
        created = await forum.create_thread(name=f"{a.get('name') or 'Applicant ' + str(aid)} - {engine.fmt_local(db.parse_ts(iv['start_at']), db.HOUSE_TZ)} UK"[:100],
                                            content=pack[:2000], view=self.verdict_cls(aid, url))
        th = created.thread if hasattr(created, "thread") else created
        with db.db() as con:
            con.execute("UPDATE interview SET discord_thread_id=? WHERE id=?", (str(th.id), iv["id"]))

    def on_booked(self, aid: int, iv: dict) -> None:
        if not self.configured:
            log.info("discord dry-run: final booked for #%s", aid)
            return
        if not self.ready.wait(timeout=15):
            raise RuntimeError("discord not ready")
        self._submit(self._open_final(aid, iv), timeout=40)


class LiveActions:
    """Join-flow actions on the real bot loop (called from worker threads)."""

    def __init__(self, ch: DiscordChannel):
        self.ch = ch

    def assign_role(self, user_id):
        try:
            self.ch._submit(self.ch._assign_role(user_id))
        except Exception as e:  # noqa: BLE001
            log.warning("assign role failed: %s", e)

    def create_private_channel(self, aid, name, user_id):
        return self.ch._submit(self.ch._create_private_channel(aid, name, user_id), timeout=40)

    def dm(self, user_id, text):
        try:
            self.ch._submit(self.ch._dm(user_id, "", text))
        except Exception as e:  # noqa: BLE001
            log.info("dm to %s skipped: %s", user_id, e)

    def staff_note(self, aid, text):
        try:
            self.ch._submit(self.ch._staff_note(aid, text))
        except Exception as e:  # noqa: BLE001
            log.warning("staff note failed: %s", e)

    def lounge_prompt(self, user_id, text):
        try:
            self.ch._submit(self.ch._lounge(text))
        except Exception as e:  # noqa: BLE001
            log.warning("lounge prompt failed: %s", e)


def briefing_pack(a: dict, app: dict, iv: dict, url: str) -> str:
    answers = db.jl(app.get("interview_answers"), {})
    grade = db.jl(app.get("interview_grade"), {})
    lines = [f"Final interview: {a.get('name') or '#' + str(a.get('id'))} with Ian, {engine.when_both(iv['start_at'], db.HOUSE_TZ)}; applicant's time {engine.fmt_local(db.parse_ts(iv['start_at']), a.get('timezone') or db.DEFAULT_APPLICANT_TZ)} {a.get('timezone')}",
             f"Discord: {a.get('discord') or '-'} | Telegram: {('@' + a['telegram']) if a.get('telegram') else '-'} | Score {a.get('score')}/100",
             f"Experience: {app.get('experience') or '-'} | Shifts (UK): {', '.join(db.SHIFT_LABELS.get(s, s) for s in (app.get('available_shifts') or [])) or '-'} | Hours/week {app.get('hours_per_week') or '-'}"]
    if grade:
        lines.append(f"Text interview: {'passed' if grade.get('passed') else 'failed'}, score {grade.get('score')}/10, GFE {'ok' if grade.get('gfe_ok') else 'not shown'}. {grade.get('summary') or ''}")
    for i, q in enumerate(texts.INTERVIEW_QUESTIONS):
        ans = answers.get(f"q{i + 1}")
        if ans:
            lines.append(f"Q{i + 1}: {ans[:300]}")
    lines.append("Verdict: Hire / Second look / No. The owner approves every hire.")
    lines.append(f"Open: {url}")
    return "\n".join(lines)


def verdict_action(aid: int, verdict: str, who: str) -> str:
    with db.db() as con:
        a = engine.applicant(con, aid)
        if not a:
            return "No such applicant."
        iv = engine.open_interview(con, aid)
        if not iv:
            return "No open final to record a verdict on."
        first = (a["name"] or "").split(" ")[0]
        if verdict == "no":
            engine.interview_outcome(con, aid, "done", actor=who, verdict="no")
            engine.set_stage(con, aid, "rejected", actor=who, reason="final interview verdict: no", force=True)
            engine.deliver(con, aid, texts.rejected(first), kind="decline")
            return f"Verdict No recorded by {who}; {a['name'] or '#' + str(aid)} has been told."
        engine.interview_outcome(con, aid, "done", actor=who, verdict=verdict)
        label = "hire" if verdict == "hire" else "second look"
        engine.create_escalation(con, aid, f"verdict: {label}", f"{who} recorded '{label}' after the final interview. Owner decision needed: approve the hire or ask for another look.")
        return f"Verdict {label} recorded by {who}; the owner has been asked to approve."


def staff_action(aid: int, action: str, who: str) -> str:
    """Fast-track / Reject from the Discord buttons (and the staff page)."""
    with db.db() as con:
        a = engine.applicant(con, aid)
        if not a:
            return "No such applicant."
        first = (a["name"] or "").split(" ")[0]
        if action == "fasttrack":
            moved = engine.set_stage(con, aid, "passed", actor=who, reason="fast-tracked to the final interview", force=True)
            res = sam.run_tool(con, aid, "list_slots", {})
            if res.get("slots"):
                lines = [f"{i + 1}. {s['local']} ({s['uk']})" for i, s in enumerate(res["slots"])]
                engine.deliver(con, aid, f"Good news, {first}: the team has moved you straight to the final interview, a 30-minute mock chat with a chat manager. Open times:\n" + "\n".join(lines) + "\nReply with the number you want and I'll book it.", kind="slots")
            else:
                engine.deliver(con, aid, f"Good news, {first}: the team has moved you straight to the final interview. I'm waiting on open times from the manager and will send them here as soon as I have them.", kind="sam")
            return f"{a['name'] or '#' + str(aid)} fast-tracked to Passed; {texts.AGENT_NAME} has sent the slots." if moved else "Already past that stage."
        if action == "reject":
            moved = engine.set_stage(con, aid, "rejected", actor=who, reason="rejected from the staff summary", force=True)
            if moved:
                engine.deliver(con, aid, texts.rejected(first), kind="decline")
            return f"{a['name'] or '#' + str(aid)} rejected and told." if moved else "Already rejected."
    return "Unknown action."


# ---------------------------------------------------------------- email

AUTO_SUBJECT = re.compile(r"^(auto(matic)?[\s-]*reply|out of office|automatic response|delivery status|undeliver)", re.I)


class EmailChannel:
    name = "email"

    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "465") or 465)
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASS", "")
        self.from_addr = os.environ.get("MAIL_FROM", "")
        self.reply_to = os.environ.get("MAIL_REPLY_TO", "") or self.from_addr
        self.ehlo = os.environ.get("SMTP_EHLO_NAME", "hire.beloura.ai")
        self.subject = os.environ.get("MAIL_SUBJECT", "Your Beloura application")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_addr)

    @property
    def imap_configured(self) -> bool:
        return bool(os.environ.get("IMAP_HOST") and os.environ.get("IMAP_USER"))

    def build(self, to_addr: str, text: str, in_reply_to: str | None = None) -> EmailMessage:
        m = EmailMessage()
        m["From"] = self.from_addr
        m["To"] = to_addr
        m["Subject"] = self.subject if not in_reply_to else "Re: " + self.subject
        m["Date"] = email.utils.formatdate(localtime=False)
        m["Message-ID"] = email.utils.make_msgid(domain=self.from_addr.split("@")[-1].strip(">") if "@" in self.from_addr else "beloura.ai")
        if self.reply_to:
            m["Reply-To"] = self.reply_to
        if in_reply_to:
            m["In-Reply-To"] = in_reply_to
            m["References"] = in_reply_to
        m["Auto-Submitted"] = "auto-generated"
        m.set_content(text)
        return m

    def send(self, to_addr: str, text: str, in_reply_to: str | None = None) -> str:
        if not self.configured:
            log.info("email dry-run to %s: %s", to_addr, text[:80])
            return "dry-run"
        m = self.build(to_addr, text, in_reply_to)
        implicit_tls = os.environ.get("SMTP_SSL", "1" if self.port == 465 else "0") == "1"
        cls = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP
        with cls(self.host, self.port, local_hostname=self.ehlo, timeout=30) as s:
            s.ehlo()
            if not implicit_tls and s.has_extn("starttls"):
                s.starttls()
                s.ehlo()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(m)
        return m["Message-ID"]

    def poll(self, con) -> dict:
        if not self.imap_configured:
            return {"skipped": "not configured"}
        host = os.environ["IMAP_HOST"]
        port = int(os.environ.get("IMAP_PORT", "993") or 993)
        user, password = os.environ["IMAP_USER"], os.environ.get("IMAP_PASS", "")
        folder = os.environ.get("IMAP_FOLDER", "INBOX")
        found = matched = 0
        M = imaplib.IMAP4_SSL(host, port) if os.environ.get("IMAP_SSL", "1") != "0" else imaplib.IMAP4(host, port)
        try:
            M.login(user, password)
            M.select(folder)
            typ, data = M.search(None, "UNSEEN")
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            for num in ids[:100]:
                typ, parts = M.fetch(num, "(RFC822)")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                found += 1
                msg = email.message_from_bytes(parts[0][1])
                if self.handle_message(con, msg):
                    matched += 1
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        return {"found": found, "matched": matched}

    def handle_message(self, con, msg) -> bool:
        from_addr = (email.utils.parseaddr(_hdr(msg, "From"))[1] or "").lower()
        subject = _hdr(msg, "Subject")
        if not from_addr or msg.get("Auto-Submitted", "no").lower() not in ("no", "") or AUTO_SUBJECT.match(subject or ""):
            return False
        a = db.one(con, "SELECT * FROM applicant WHERE lower(email)=?", (from_addr,))
        if not a:
            log.info("email from unknown sender %s ignored", from_addr)
            return False
        body = _strip_quotes(_text(msg)) or subject
        sam.handle_inbound(con, a["id"], "email", body, external_id=(msg.get("Message-ID") or "").strip() or None, address=from_addr)
        return True


def _hdr(msg, name: str) -> str:
    try:
        return str(make_header(decode_header(msg.get(name, "") or "")))
    except Exception:  # noqa: BLE001
        return msg.get(name, "") or ""


def _text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _strip_quotes(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------- registry, outbound dispatch, staff announcements

class Registry:
    def __init__(self):
        self.telegram = TelegramChannel()
        self.discord = DiscordChannel()
        self.email = EmailChannel()

    def send_to(self, applicant: dict, channel: str, text: str) -> str:
        if channel == "telegram":
            if not applicant.get("telegram_chat_id"):
                raise RuntimeError("telegram chat not linked yet")
            return self.telegram.send(applicant["telegram_chat_id"], text)
        if channel == "discord":
            return self.discord.send(applicant, text)
        if channel == "email":
            if not applicant.get("email"):
                raise RuntimeError("no email on file")
            return self.email.send(applicant["email"], text)
        raise RuntimeError(f"unknown channel {channel}")

    def staff_alert(self, text: str, aid: int | None = None) -> None:
        url = public_url(f"/staff/applicant/{aid}") if aid else ""
        full = text + (f"\n{url}" if url else "")
        try:
            if self.discord.configured:
                if aid and text.startswith("Escalation"):
                    with db.db() as con:
                        e = db.one(con, "SELECT id FROM escalation WHERE applicant_id=? AND status='open' AND discord_thread_id='' ORDER BY id DESC LIMIT 1", (aid,))
                    if e:
                        self.discord.post_escalation(full, e["id"], aid)
                        return
                self.discord.post_staff(full, 0, url)
                return
        except Exception as e:  # noqa: BLE001
            log.warning("discord staff alert failed: %s", e)
        try:
            self.telegram.send_staff(full)
        except Exception as e:  # noqa: BLE001
            log.warning("telegram staff alert failed: %s", e)

    def announce_applicant(self, con, aid: int) -> str:
        """The new-applicant summary for #hiring with Open / Fast-track / Reject."""
        a = engine.applicant(con, aid)
        app = engine.latest_application(con, aid) or {}
        url = public_url(f"/staff/applicant/{aid}")
        text = applicant_summary(a, app, url)
        try:
            if self.discord.configured:
                return self.discord.post_staff(text, aid, url)
        except Exception as e:  # noqa: BLE001
            log.warning("discord announce failed: %s", e)
        return self.telegram.send_staff(text)


def applicant_summary(a: dict, app: dict, url: str) -> str:
    reasons = db.jl(a.get("screen_reasons"), [])
    screening = "passed" if a["stage"] != "screened_out" and not reasons else "declined: " + "; ".join(reasons)
    shifts = ", ".join(db.SHIFT_LABELS.get(s, s) for s in (app.get("available_shifts") or [])) or "none"
    lines = [
        f"New applicant: {a['name'] or 'no name'}",
        f"Channel: {a.get('channel_code') or '-'} | Score: {a.get('score') if a.get('score') is not None else '-'}/100 | Screening: {screening}",
        f"Email: {a.get('email') or '-'} | Telegram: {('@' + a['telegram']) if a.get('telegram') else '-'} | Discord: {a.get('discord') or '-'}" + (f" | Phone: {a['phone']}" if a.get("phone") else ""),
        f"Timezone: {a.get('timezone')}",
        f"Shifts (UK): {shifts} | Hours/week: {app.get('hours_per_week') or '-'}",
        f"English: {app.get('english_self_rating') or '-'}/5 | Device: {app.get('device') or '-'} | Connection: {app.get('connection_type') or '-'}",
        f"Experience: {app.get('experience') or '-'}" + (f" - {app['experience_text'][:300]}" if app.get("experience_text") else ""),
        f"Why: {(app.get('why') or '-')[:400]}",
        f"Heard from: {app.get('heard_from') or '-'}",
        f"Open: {url}",
    ]
    return "\n".join(lines)
