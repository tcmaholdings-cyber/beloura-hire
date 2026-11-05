"""Discord layer without Discord: the guild setup against a fake guild (ids persisted, idempotent), member matching, the join flow, the lounge link, the briefing pack."""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["HIRE_NO_WORKER"] = "1"
os.environ["OPENROUTER_API_KEY"] = ""

import discord  # noqa: E402

import channels  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402


class _Ids:
    n = 1000

    @classmethod
    def next(cls):
        cls.n += 1
        return cls.n


class FakeRole:
    def __init__(self, name, rid=None):
        self.name, self.id = name, rid or _Ids.next()


class FakePerms:
    def __init__(self, admin=True):
        self.administrator = admin


class FakeMe(FakeRole):
    def __init__(self, admin=True):
        super().__init__("hiring")
        self.guild_permissions = FakePerms(admin)


class FakeMsg:
    def __init__(self):
        self.id = _Ids.next()


class FakeInvite:
    url = "https://discord.gg/testinvite"


class FakeChannel:
    def __init__(self, guild, name, ctype, category=None, overwrites=None, topic=""):
        self.guild, self.name, self.type, self.category, self.overwrites, self.topic = guild, name, ctype, category, overwrites or {}, topic
        self.id = _Ids.next()
        self.category_id = category.id if category else None
        self.sent, self.deleted, self.invites = [], False, []

    async def edit(self, **kw):
        if "name" in kw:
            self.name = kw["name"]
        if "category" in kw:
            self.category = kw["category"]
            self.category_id = kw["category"].id
        if "topic" in kw:
            self.topic = kw["topic"]
        self.guild.edits.append((self.name, kw))

    async def send(self, text, **kw):
        self.sent.append(text)
        return FakeMsg()

    async def create_invite(self, **kw):
        self.invites.append(kw)
        return FakeInvite()

    async def delete(self, **kw):
        self.deleted = True
        self.guild.channels.remove(self)


class FakeGuild:
    def __init__(self, admin=True):
        self.id = 100000000000000001
        self.name = "beloura hiring v2"
        self.me = FakeMe(admin)
        self.default_role = FakeRole("@everyone", 1)
        self.roles = [self.default_role, self.me]
        self.channels = []
        self.edits, self.created = [], []
        tc = FakeChannel(self, "Text channels", discord.ChannelType.category)
        vc = FakeChannel(self, "Voice channels", discord.ChannelType.category)
        self.channels += [tc, vc, FakeChannel(self, "general", discord.ChannelType.text, tc), FakeChannel(self, "General", discord.ChannelType.voice, vc)]

    async def create_role(self, *, name, **kw):
        r = FakeRole(name)
        self.roles.append(r)
        self.created.append(("role", name))
        return r

    async def create_category(self, name, *, overwrites=None, **kw):
        c = FakeChannel(self, name, discord.ChannelType.category, overwrites=overwrites)
        self.channels.append(c)
        self.created.append(("category", name))
        return c

    async def _mk(self, name, ctype, category=None, overwrites=None, topic="", **kw):
        c = FakeChannel(self, name, ctype, category, overwrites, topic)
        self.channels.append(c)
        self.created.append((ctype.name, name))
        return c

    async def create_text_channel(self, name, *, category=None, overwrites=None, topic="", **kw):
        return await self._mk(name, discord.ChannelType.text, category, overwrites, topic)

    async def create_forum(self, name, *, category=None, overwrites=None, topic="", **kw):
        return await self._mk(name, discord.ChannelType.forum, category, overwrites, topic)

    async def create_voice_channel(self, name, *, category=None, overwrites=None, **kw):
        return await self._mk(name, discord.ChannelType.voice, category, overwrites)


class RecorderActions:
    def __init__(self):
        self.calls = []

    def assign_role(self, user_id):
        self.calls.append(("role", str(user_id)))

    def create_private_channel(self, aid, name, user_id):
        self.calls.append(("channel", aid, name, str(user_id)))
        return f"chan-{aid}"

    def dm(self, user_id, text):
        self.calls.append(("dm", str(user_id), text))

    def staff_note(self, aid, text):
        self.calls.append(("note", aid, text))

    def lounge_prompt(self, user_id, text):
        self.calls.append(("lounge", str(user_id), text))


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, applicant, channel, text):
        self.sent.append((applicant["id"], channel, text))
        return "ok"


class DiscordSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init(self.path)
        self.con = db.connect(self.path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_setup_creates_layout_persists_ids_and_is_idempotent(self):
        g = FakeGuild()
        said = []
        out = asyncio.run(channels.setup_guild(g, self.con, say=said.append))
        names = {c.name for c in g.channels}
        for n in ("STAFF", "APPLICANTS", "FINALS", "hiring-queue", "escalations", "results", "weekly-report", "staff-chat", "welcome", "applicant-lounge", "finals", "Mock Chat | Ian"):
            self.assertIn(n, names)
        self.assertNotIn("general", names)
        self.assertNotIn("General", names)
        self.assertNotIn("Text channels", names)
        self.assertNotIn("Voice channels", names)
        self.assertEqual({r.name for r in g.roles} & {"Manager", "Applicant", "Trainee"}, {"Manager", "Applicant", "Trainee"})
        welcome = next(c for c in g.channels if c.name == "welcome")
        self.assertEqual(len(welcome.sent), 1)
        self.assertIn("automated", welcome.sent[0])
        self.assertIn("review every decision", welcome.sent[0])
        self.assertEqual(welcome.invites[0]["max_age"], 0)
        self.assertEqual(welcome.invites[0]["max_uses"], 0)
        for k in db.DISCORD_SETTING_KEYS:
            self.assertTrue(db.setting(self.con, k, ""), k)
        self.assertEqual(db.setting(self.con, "discord_invite_url"), "https://discord.gg/testinvite")
        self.assertEqual(db.setting(self.con, "staff_discord_channel_id"), str(next(c for c in g.channels if c.name == "hiring-queue").id))
        staff = next(c for c in g.channels if c.name == "STAFF")
        self.assertFalse(staff.overwrites[g.default_role].view_channel)
        voice = next(c for c in g.channels if c.name == "Mock Chat | Ian")
        self.assertEqual(voice.category.name, "FINALS")
        created_before = len(g.created)
        out2 = asyncio.run(channels.setup_guild(g, self.con, say=said.append))
        self.assertEqual(len(g.created), created_before)
        self.assertEqual(len(welcome.sent), 1)
        self.assertEqual(len(welcome.invites), 1)
        self.assertEqual(out["discord_ch_hiring_queue"], out2["discord_ch_hiring_queue"])

    def test_setup_refuses_without_administrator(self):
        g = FakeGuild(admin=False)
        with self.assertRaises(PermissionError):
            asyncio.run(channels.setup_guild(g, self.con, say=lambda s: None))
        self.assertEqual(g.created, [])


class DiscordJoinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init(self.path)
        self.con = db.connect(self.path)
        self.sender = Sender()
        engine.SENDER = self.sender
        engine.ALERT = None
        engine.ON_BOOKED = None
        self.aid, _ = engine.upsert_applicant(self.con, {"name": "Maria Santos", "email": "maria@example.com", "discord": "@Maria_S", "telegram": "marias"})
        engine.save_application(self.con, self.aid, {"age_18": True, "device": "laptop", "connection_type": "fibre", "willing_to_train": True,
                                                       "available_shifts": ["uk_08_16"], "hours_per_week": 40, "english_self_rating": 4, "experience": "none",
                                                       "why": "I want remote shift work and I enjoy chatting.", "consent": True})
        engine.screen(self.con, self.aid)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()
        engine.SENDER = None

    def test_match_member_exact_at_prefixed_and_case(self):
        self.assertEqual(engine.applicant(self.con, self.aid)["discord"], "maria_s")
        self.assertEqual(channels.match_member(self.con, "42", "maria_s")["id"], self.aid)
        self.assertEqual(channels.match_member(self.con, "42", "MARIA_S")["id"], self.aid)
        self.assertEqual(channels.match_member(self.con, "42", "@Maria_S")["id"], self.aid)
        self.assertEqual(channels.match_member(self.con, "42", "somebody", "Maria_S")["id"], self.aid)
        self.assertIsNone(channels.match_member(self.con, "42", "stranger", "Nobody"))
        engine.link_discord(self.con, self.aid, "42", "maria_s")
        self.assertEqual(channels.match_member(self.con, "42", "renamed")["id"], self.aid)
        self.assertIsNone(channels.match_member(self.con, "43", "maria_s"))

    def test_join_links_creates_channel_welcomes_releases_held_and_starts_interview(self):
        self.con.execute("UPDATE applicant SET email=NULL WHERE id=?", (self.aid,))
        held = engine.deliver(self.con, self.aid, "held hello", kind="first")
        self.assertEqual(db.one(self.con, "SELECT status FROM message WHERE id=?", (held,))["status"], "held")
        acts = RecorderActions()
        out = channels.member_joined(self.con, "42", "Maria_S", "Maria", acts)
        self.assertTrue(out["matched"])
        self.assertEqual(out["channel_id"], f"chan-{self.aid}")
        self.assertEqual(out["released"], 1)
        self.assertTrue(out["interview_started"])
        a = engine.applicant(self.con, self.aid)
        self.assertEqual((a["discord_user_id"], a["discord_channel_id"]), ("42", f"chan-{self.aid}"))
        kinds = [c[0] for c in acts.calls]
        self.assertEqual(kinds[:2], ["role", "channel"])
        self.assertIn("dm", kinds)
        self.assertIn(("note", self.aid, "joined Discord as Maria_S"), acts.calls)
        self.assertEqual(acts.calls[1][2], f"app-{self.aid}-maria")
        chans = [s[1] for s in self.sender.sent]
        self.assertTrue(all(c == "discord" for c in chans), chans)
        texts_sent = [s[2] for s in self.sender.sent]
        self.assertEqual(texts_sent[0], "held hello")
        self.assertIn("private channel with me", texts_sent[1])
        self.assertIn("1 of 5", texts_sent[-1])
        self.assertEqual(db.one(self.con, "SELECT interview_state, interview_channel FROM application WHERE applicant_id=?", (self.aid,))["interview_channel"], "discord")

    def test_join_without_prior_message_sends_full_first_message(self):
        acts = RecorderActions()
        channels.member_joined(self.con, "42", "maria_s", None, acts)
        first = self.sender.sent[0][2]
        self.assertIn("recruiting assistant", first)
        self.assertIn("thanks for applying", first)

    def test_unmatched_join_prompts_in_lounge_then_links_by_email(self):
        acts = RecorderActions()
        out = channels.member_joined(self.con, "99", "stranger", "Stranger", acts)
        self.assertFalse(out["matched"])
        self.assertEqual(acts.calls[0][0], "lounge")
        self.assertIn("<@99>", acts.calls[0][2])
        self.assertIn("email you applied with", acts.calls[0][2])
        self.assertIsNone(engine.applicant(self.con, self.aid)["discord_user_id"])
        out = channels.lounge_message(self.con, "99", "stranger", "Stranger", "hi it's me, MARIA@example.com", acts)
        self.assertTrue(out["matched"])
        a = engine.applicant(self.con, self.aid)
        self.assertEqual((a["discord_user_id"], a["discord_channel_id"]), ("99", f"chan-{self.aid}"))
        self.assertIn("found you", acts.calls[-1][2])
        self.assertFalse(channels.lounge_message(self.con, "100", "other", None, "hello no email", acts)["matched"])
        self.assertFalse(channels.lounge_message(self.con, "100", "other", None, "maria@example.com", acts)["matched"])

    def test_briefing_pack_and_verdict(self):
        ian = engine.default_manager(self.con)
        for d in range(7):
            self.con.execute("INSERT INTO availability (manager_id, weekday, start, end, tz) VALUES (?,?,?,?,?)", (ian["id"], d, "10:00", "12:00", "Asia/Manila"))
        engine.set_stage(self.con, self.aid, "passed", force=True)
        booked = []
        engine.ON_BOOKED = lambda aid, iv: booked.append((aid, iv["status"]))
        now = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        iv = engine.book(self.con, self.aid, engine.slots(self.con, "Asia/Manila", now=now)[0]["start"], now=now)
        self.assertEqual(booked, [(self.aid, "booked")])
        pack = channels.briefing_pack(engine.applicant(self.con, self.aid), engine.latest_application(self.con, self.aid), iv, "http://x/staff/applicant/1")
        self.assertIn("Final interview: Maria Santos with Ian", pack)
        self.assertIn("UK time", pack)
        self.assertIn("Verdict: Hire / Second look / No", pack)
        db.DB_PATH, old = self.path, db.DB_PATH
        try:
            out = channels.verdict_action(self.aid, "hire", "manager1")
        finally:
            db.DB_PATH = old
        self.assertIn("owner has been asked", out)
        a = engine.applicant(self.con, self.aid)
        self.assertEqual(a["stage"], "final_done")
        self.assertEqual(db.one(self.con, "SELECT verdict, verdict_by FROM interview WHERE id=?", (iv["id"],))["verdict"], "hire")
        self.assertEqual(db.one(self.con, "SELECT reason FROM escalation ORDER BY id DESC LIMIT 1")["reason"], "verdict: hire")


if __name__ == "__main__":
    unittest.main()
