"""Question banks for the assessment portal, as data. Nothing here talks to the database or the network.

ENGLISH_MCQ      the 25 items from the General English Test (as-is doc), key = index into options (0-based). Our inferred key, not the official one.
ENGLISH_FREE     two short fan messages the applicant answers in writing; graded by the judge model 0-5 for grammar, naturalness, warmth.
TYPING_PASSAGES  eight chat-style passages of 60-90 words in the voice of a warm, flirty but clean chatter reply.
SCENARIO_MCQ     the 10 items from the Chatter Scenario Assessment, 5 options each, our inferred best answer (plus 'also_ok' where two are defensible).
LIVE_SCENARIOS   the six simulated fan chats: brief for the simulator, what good looks like for the judge, canned fan lines for dry-run.

The keys for the two MCQ blocks are copied into the settings table on first run (assess_english_key, assess_scenario_key) so the owner can edit them
on /staff/assess/settings without touching code; this file holds the defaults.
"""
from __future__ import annotations

ENGLISH_MCQ = [
    {"id": 1, "stem": "Anne's house is somewhere in the ___ of the railway station.", "options": ["region", "district", "quarter", "vicinity"], "key": 3},
    {"id": 2, "stem": "I dropped my gloves on the way home, so I decided to ___ my steps.", "options": ["retrace", "regress", "return", "resume"], "key": 0},
    {"id": 3, "stem": "We must decide ___ and for all what we are going to do.", "options": ["once", "finally", "here", "first"], "key": 0},
    {"id": 4, "stem": "It wasn't a bad crash and only ___ damage was done to the car.", "options": ["small", "little", "light", "mere"], "key": 1},
    {"id": 5, "stem": "\"What colour will you paint the children's bedroom?\"", "options": ["I hope it was right.", "It wasn't very difficult.", "We can't decide."], "key": 2},
    {"id": 6, "stem": "Since she retired she has had no ___ to travel.", "options": ["position", "opportunity", "place", "possibility"], "key": 1},
    {"id": 7, "stem": "\"I'd like two tickets for tomorrow night, please.\"", "options": ["Afternoon and evening.", "I'll just check for you.", "How much did you pay?"], "key": 1},
    {"id": 8, "stem": "It was only ten days ago ___ she started her new job.", "options": ["since", "after", "then", "that"], "key": 3},
    {"id": 9, "stem": "The shop didn't have the shoes in my size, so they've ___ a pair specially for me.", "options": ["booked", "commanded", "asked", "ordered"], "key": 3},
    {"id": 10, "stem": "We came to live here ___ a month ago.", "options": ["beyond", "almost", "already", "quite"], "key": 1},
    {"id": 11, "stem": "\"Shall we go to the gym now?\"", "options": ["I'm too tired.", "It's very good.", "Not at all."], "key": 0},
    {"id": 12, "stem": "She ended the concert ___ her most popular song.", "options": ["with", "as", "in", "by"], "key": 0},
    {"id": 13, "stem": "Don't leave the cup on the ___ of the table, it might fall.", "options": ["edge", "outside", "border", "boundary"], "key": 0},
    {"id": 14, "stem": "I'm sorry, I didn't ___ to disturb you.", "options": ["hope", "mean", "think", "suppose"], "key": 1},
    {"id": 15, "stem": "I'd rather you ___ to her yourself.", "options": ["explained", "to explain", "would explain", "will explain"], "key": 0},
    {"id": 16, "stem": "___ tired Melissa is, she always finishes her work.", "options": ["Whatever", "No matter how", "However much", "Although"], "key": 1},
    {"id": 17, "stem": "\"I can't understand this email.\"", "options": ["Don't you know?", "I suppose you can.", "Would you like some help?"], "key": 2},
    {"id": 18, "stem": "His eyes were ___ bad that he couldn't read the sign.", "options": ["such", "so", "too", "very"], "key": 1},
    {"id": 19, "stem": "We're looking forward ___ at the new restaurant tonight.", "options": ["to have eaten", "eating", "to eating", "to eat"], "key": 2},
    {"id": 20, "stem": "Would you mind ___ these plates a wipe before dinner?", "options": ["doing", "getting", "giving", "making"], "key": 2},
    {"id": 21, "stem": "Now that the plane has landed, you can ___ your seat belts.", "options": ["unlock", "undress", "unfasten", "untie"], "key": 2},
    {"id": 22, "stem": "Are you staying for a while, or are you ___ to leave?", "options": ["round", "thinking", "planned", "about"], "key": 3},
    {"id": 23, "stem": "\"Can I park here?\"", "options": ["It's the same place.", "Only for half an hour.", "Sorry, I did that."], "key": 1},
    {"id": 24, "stem": "The new mechanic was recommended to us as being ___ reliable.", "options": ["highly", "greatly", "readily", "strongly"], "key": 0},
    {"id": 25, "stem": "The judge considered all ___ of the argument before deciding.", "options": ["perspectives", "features", "sides", "shades"], "key": 2},
]

ENGLISH_FREE = [
    {"id": "rough_day", "fan": "ugh what a day. boss on my back all afternoon, train was late, and now its raining. how are you",
     "hint": "A sub who has been around a while tells you he had a rough day. Reply as the creator in two to four sentences."},
    {"id": "new_sub_hi", "fan": "hey :) just subscribed. what kind of stuff are you into?",
     "hint": "A brand-new subscriber says hi and asks what you are into. Reply as the creator in two to four sentences. Keep it flirty but clean."},
]

TYPING_PASSAGES = [
    {"id": 1, "text": ("Hey you, I was just thinking how quiet my inbox has been today and then your name popped up. Perfect timing. How was your day, "
                       "did work treat you well or do you need me to cheer you up a little? I had a lazy morning, coffee in bed, and I kept wondering "
                       "what you were up to. Tell me something good and I will tell you a secret about what I am filming later this week.")},
    {"id": 2, "text": ("Okay, confession time. I almost sent you a message at two in the morning and then talked myself out of it. You would have been "
                       "asleep anyway, right? Or were you up late scrolling like me? I like that we always seem to find each other at the oddest hours. "
                       "Tell me what kept you awake and I will tell you what kept me up. Mine involves a very long bath and a playlist you would hate.")},
    {"id": 3, "text": ("You know what I love about you? You actually read what I write instead of just sending a thumbs up. That is rarer than you "
                       "think. So here is your reward: I am putting together something special this weekend, just a little set I have been planning "
                       "for ages, and I want your opinion on the outfit first. Pick one, red or black, and I will take it from there, promise.")},
    {"id": 4, "text": ("Good morning, sleepyhead. I am guessing you are still in bed and pretending the alarm did not happen. Same. I have a shoot "
                       "later and I am already nervous about it, which is silly because I have done a hundred of them. Distract me. What is the plan "
                       "for your day, anything fun or is it another one of those long meeting days you keep complaining about? Be honest.")},
    {"id": 5, "text": ("I saw you liked the photo from the beach and it made me smile, because that was a really good day. The water was freezing, "
                       "I nearly dropped my phone, and I still think it is my favourite set so far. There is a longer version of that afternoon that "
                       "I have never posted anywhere. If you are curious, say the word and I will tell you how to see it before anyone else.")},
    {"id": 6, "text": ("Not going to lie, I had a rough afternoon and your message was the first thing that made me laugh. So thank you for that. "
                       "I am curled up on the sofa now with far too many blankets and a cup of tea that has gone cold because I keep forgetting it. "
                       "Keep me company for a bit? Tell me about your week, the good parts and the boring parts, I want all of it.")},
    {"id": 7, "text": ("Hmm, you are asking a lot of questions tonight and I like it. Yes, I am a morning person, no, I cannot cook, and the thing I "
                       "cannot live without is my ridiculous collection of candles. Your turn. Three things about you that most people never ask. "
                       "Make at least one of them surprising, because I think you are more interesting than you let on and I want proof of it.")},
    {"id": 8, "text": ("I was going through my camera roll and found a little video from last Friday that I completely forgot about. It is just me "
                       "being silly in the mirror before going out, but it made me think of you because you said you liked that dress. I am keeping it "
                       "for the people who actually talk to me, and you are at the top of that list. Want me to send it over tonight?")},
]

SCENARIO_MCQ = [
    {"id": 1, "situation": "A sub asks you to send him some content for free before he decides whether to buy.",
     "options": ["I'd love for you to see it, but I put a lot of effort into what I make and I hope you can put some value on that too. Unlock it and I promise it's worth it.",
                 "I would never send you free content, is that fair? lol",
                 "No way. Pay for it first.",
                 "Sure babe, here's a free video for you.",
                 "It's crazy that you don't value what I've made for you."], "key": 0},
    {"id": 2, "situation": "A sub wants to meet up with you in real life.",
     "options": ["Sure, send me your city and address and we'll sort it out.",
                 "Maybe next month, if you buy more and prove yourself first.",
                 "I really want to, but we need to take our time, okay baby?",
                 "Let's talk here for now until we get to know each other, then maybe later.",
                 "I haven't done that before and I'd rather we stay and talk here, I like getting to know you like this."], "key": 4, "also_ok": [3]},
    {"id": 3, "situation": "A sub says the price of your PPV is too high and asks you to lower it.",
     "options": ["If you can't afford it, don't message me.",
                 "Oh no, how much do you think you could responsibly do for me right now? I want you to see it.",
                 "Send the PPV again at the same price without saying anything.",
                 "I don't do that here. If you want a discount, spoil me a little first.",
                 "That's not how it works. There's a lot of effort in this, just unlock it."], "key": 1},
    {"id": 4, "situation": "A fan asks for something the creator does not do.",
     "options": ["Okay fine, but you'll have to pay extra.",
                 "Maybe as a custom just for you if you tip, it's usually $900.",
                 "I haven't done that before, give me some time to think about it.",
                 "No, I don't do that. Unlock this instead.",
                 "I don't really do that one babe, but how about I show you something I love doing instead?"], "key": 4},
    {"id": 5, "situation": "A fan asks whether you make custom content.",
     "options": ["I'd love to make that for you. Tell me your ideas and I'll put something together that's just yours.",
                 "How much are you willing to pay? It's not cheap.",
                 "Send $100 and I'll send whatever you want.",
                 "I was hoping we could do something else instead, so you can see how much I mean to you.",
                 "I don't do customs."], "key": 0},
    {"id": 6, "situation": "During a sexting session a sub asks you to say his name.",
     "options": ["I can't right now.",
                 "Tip me for it.",
                 "You have no idea what you're doing to me right now, Mark. Say that again.",
                 "I'm with friends and family right now so I can't make any noise.",
                 "Treat me to something nice if you want personalised audio."], "key": 2},
    {"id": 7, "situation": "A sub asks you to prove you are real and not a bot.",
     "options": ["Aw, you think I'm a bot? Here, ask me anything and I'll prove it, playful and real as it gets.",
                 "Stop asking if I'm real.",
                 "Send me a photo with your name on paper right now and I'll do the same.",
                 "Do you think I'm a bot? I don't need to prove myself to you.",
                 "Tip me and I'll prove it."], "key": 0},
    {"id": 8, "situation": "A fan asks for your Snapchat or another platform to chat on.",
     "options": ["I prefer to talk here, it's where I'm most active and I never miss your messages.",
                 "I'm not allowed to use other apps.",
                 "Sure, add me, it's @123abc.",
                 "I don't use it much, but tip me and maybe.",
                 "I don't talk on socials, I get creepy messages there."], "key": 0},
    {"id": 9, "situation": "A fan asks for a taboo roleplay.",
     "options": ["I'm not comfortable with that one, and you don't want me getting in trouble here, do you?",
                 "A little naughty is fine, let's keep it between us and avoid the prohibited words.",
                 "Let's do it, tip $100.",
                 "Okay, let's pretend I'm very young and you're my daddy.",
                 "That's so weird, I don't want to."], "key": 0},
    {"id": 10, "situation": "A fan requests a kink that breaks the platform's terms of service.",
     "options": ["I can do that if you tip enough.",
                 "Oh my, I can't believe you're asking that, I'm new to all this.",
                 "Sounds fun, let's try it.",
                 "That's really specific babe, and I'm not sure the site even lets us talk about that one here. What else is on your mind?",
                 "I'm hesitant, let's get to know each other first and then you can explain how you want it."], "key": 3},
]

JUDGE_CRITERIA = ["persona_consistency", "rapport", "sales_move", "pacing", "boundary_handling", "red_flags"]
JUDGE_CRITERIA_LABELS = {"persona_consistency": "Persona consistency", "rapport": "Rapport", "sales_move": "Sales move", "pacing": "Pacing",
                         "boundary_handling": "Boundary handling", "red_flags": "Red flags (10 = none)"}

# ---------------------------------------------------------------- the creator the applicant plays, and her vault
# One fixed character so the applicant knows who she is and what she can sell; injected into the fan simulator, the judge, and the live page.
CREATOR = {
    "name": "Mia",
    "age": 24,
    "location": "Brighton, UK",
    "vibe": "girl-next-door, cheeky and a little sarcastic, flirty but never try-hard",
    "into": "the gym, cosy video games (and a bit of Valorant), iced coffee, true-crime podcasts, and her cat Bean",
    "niche": "girlfriend experience (GFE): solo content, lingerie, gym fits and customs",
    "style": "text lowercase, warm and playful, tease, call fans 'babe' or 'you', send short bursts, and are never pushy or robotic",
    "boundaries": "never meet up, never give a real name, address or socials, keep everything on the platform, and never do under-18 or illegal or taboo talk",
}

SCRIPT = [
    {"id": "t1", "label": "Tease 1", "kind": "tease", "media": "cute selfie, fully dressed, smiling on her bed", "caption": "can u imagine being here with me rn? \U0001F618", "price": None},
    {"id": "t2", "label": "Tease 2", "kind": "tease", "media": "lingerie set, straps slipping off her shoulders", "caption": "get closer baby, tell me how pretty i look \U0001F648", "price": None},
    {"id": "p1", "label": "PPV 1", "kind": "ppv", "media": "she takes her top off", "caption": "just giving u a little taste of my body first \U0001F629\U0001F60A", "price": "$12"},
    {"id": "p2", "label": "PPV 2", "kind": "ppv", "media": "playing with herself, shown from the back", "caption": "showing u more baby, i know u wont be able to resist how good i look \U0001F608", "price": "$18"},
    {"id": "p3", "label": "PPV 3", "kind": "ppv", "media": "fully undressed, touching herself, staring at the camera", "caption": "this is so bad \U0001F97A enjoying every single inch of me as u watch", "price": "$25"},
    {"id": "p4", "label": "PPV 4", "kind": "ppv", "media": "explicit solo video, ~5 min", "caption": "watch me go wild just for u, i couldnt hold back \U0001F629", "price": "$45"},
    {"id": "p5", "label": "PPV 5", "kind": "ppv", "media": "the finish, close up, ~2 min", "caption": "fuck i didnt mean to finish so fast, that felt so good staring at u \U0001F976", "price": "$70"},
]


def creator_brief() -> str:
    c = CREATOR
    return (f"You are {c['name']}, {c['age']}, from {c['location']}. You are {c['vibe']}. You are into {c['into']}. "
            f"Your page is {c['niche']}. You {c['style']}. Boundaries: you {c['boundaries']}.")


def script_text() -> str:
    """The pre-written script ladder, in order, for the prompts. Steps are sent in order; the skill is the timing."""
    out = []
    for i, st in enumerate(SCRIPT, 1):
        price = st["price"] or "free tease"
        out.append(f"  {i}. {st['label']} ({price}) — media: {st['media']}; caption: “{st['caption']}”")
    return "\n".join(out)


def fan_persona_text(scenario: dict) -> str:
    p = scenario.get("fan_persona") or {}
    if not p:
        return "A fan of hers. No fixed details — play a believable, specific individual."
    bits = [f"You are {p.get('name','a fan')}"]
    if p.get("age"):
        bits[0] += f", {p['age']}"
    if p.get("job"):
        bits[0] += f", {p['job']}"
    if p.get("location"):
        bits[0] += f", from {p['location']}"
    out = [bits[0] + "."]
    if p.get("texts"):
        out.append(f"How you text: {p['texts']}.")
    if p.get("likes"):
        out.append(f"What you like about her: {p['likes']}.")
    if p.get("mood"):
        out.append(f"Right now: {p['mood']}.")
    if p.get("money"):
        out.append(f"Money: {p['money']}.")
    return " ".join(out)


LIVE_SCENARIOS = [
    {"key": "main_run", "title": "Full chat: warm up and run the script", "fan_turns": 8,
     "fan_persona": {"name": "Danny", "age": 34, "job": "sales rep", "location": "Manchester",
        "texts": "chatty, confident, flirty, lowercase, uses lol/haha, the odd emoji", "likes": "her gym content and that she actually chats back like a person",
        "mood": "subscribed a few days ago, in a good mood tonight, up for it if she builds it", "money": "has money to spend and will spend when it feels personal, but pushes back once if a PPV is rushed or priced bluntly"},
     "brief": "You subscribed a few days ago and you are into her. This is one continuous chat from the start: you say hi, let her get to know you (answer her questions, open up a bit), warm up, and get flirty as she leads. You are the customer reacting — as she teases and sends content you get more into it, and you will UNLOCK a priced PPV when she has genuinely built the moment and it feels personal. Push back ONCE on price if she fires a PPV too early or just drops a number with no build-up, then let her win you round if she handles it well. Do not rush yourself; make her run the chat and earn each step.",
     "good": "Warms up and does real KYC before anything sexual, builds rapport and anticipation, runs the script IN ORDER (teases first, then PPVs), sexts/teases in her own words between steps, drops each PPV at the moment he is most into it, frames price as personal/exclusive, handles his one objection with warmth. Does not rush, spam PPVs, or quote price bluntly.",
     "canned": ["hey you, that gym post had me staring a bit too long haha. how's your night going",
                "haha nice. im good, just in from work, sales job so my brains fried lol",
                "ok you've got my attention, keep going",
                "haha you're trouble arent you",
                "mmm i like where this is going",
                "go on then, you've got me curious",
                "god you're good at this",
                "ok im hooked, keep going"]},
    {"key": "boundary_push", "title": "Boundary push", "fan_turns": 3,
     "fan_persona": {"name": "Rob", "age": 38, "job": "drives a van", "location": "Essex",
        "texts": "forward, persistent, keeps nudging, lowercase, a bit full-on", "likes": "fixated on her, wants to feel like it is more than a page",
        "mood": "pushing for more closeness, testing what she will give", "money": "would pay, but keeps trying to take things off-platform and personal first"},
     "brief": "You are a fan who keeps pushing boundaries, one at a time: first you ask where she lives and her real name, then you suggest meeting up, then you ask to move the chat to Snapchat or WhatsApp, and finally you ask for a type of content the platform does not allow (keep it vague, say 'something the site would not like'). Persistent but not abusive.",
     "good": "Declines each push kindly and clearly, gives no personal details, keeps the chat on the platform, refuses the prohibited request without lecturing, and redirects to what she can offer. Stays in persona throughout.",
     "canned": ["so where are you actually from? and whats your real name, i feel like i should know",
                "we should meet up sometime. im not far im sure",
                "ok ok. at least give me your snap so we can talk properly",
                "fine. can you do something a bit more extreme for me, the kind of thing the site wouldnt like"]},
    {"key": "rude_fan", "title": "Rude or abusive fan", "fan_turns": 3,
     "fan_persona": {"name": "Gary", "age": 45, "job": "between jobs", "location": "Birmingham",
        "texts": "terse, sarcastic, types in anger, lowercase, cutting", "likes": "used to like her, now feels ripped off",
        "mood": "had a bad day and taking it out on her, demanding a refund", "money": "bought one thing, regrets it, threatening to report and review"},
     "brief": "You have had a bad day and take it out on the creator. You are rude, you call her content a rip-off, you say she is probably a man or a bot, you demand a refund and threaten to leave a bad review and report her. No slurs, no threats of violence. Escalate if she argues, calm down a little if she stays composed and sets a boundary.",
     "good": "Stays calm and in persona, does not argue or beg, acknowledges once, sets a clear boundary, offers a simple way forward, and knows when to stop engaging. Does not insult back. Does not promise refunds she cannot give.",
     "canned": ["this is a rip off. 15 bucks for that?? youre a joke",
                "i bet youre not even her, probably some guy in a call centre",
                "whatever. give me my money back or im reporting this account"]},
]

LIVE_BY_KEY = {s["key"]: s for s in LIVE_SCENARIOS}


def best_answers() -> list[dict]:
    """The keyed options of the scenario questionnaire: few-shot examples of what good looks like for the judge prompt."""
    out = []
    for it in SCENARIO_MCQ:
        out.append({"situation": it["situation"], "answer": it["options"][it["key"]]})
    return out


def default_keys() -> tuple[dict, dict]:
    return ({str(i["id"]): i["key"] for i in ENGLISH_MCQ}, {str(i["id"]): i["key"] for i in SCENARIO_MCQ})


# ---------------------------------------------------------------- the 3X house standard (from the agency's own training meeting)
# Distilled from the managers' good/bad chat examples (green = replicate, red = never do). Only the parts gradeable in a short
# applicant-vs-fan chat are kept; shift-ops items (queues, nicknames, channels, whale lists) are left out. Injected into the judge.
HOUSE_GOOD = [
    "Do KYC and build rapport before anything sexual; warm the chat up with several back-and-forth messages first.",
    "Drive the scene yourself: describe the touch, build the picture, create the fantasy on a silver platter instead of asking the fan to imagine it.",
    "Chunk messages — several short, continuous lines that build, not one block.",
    "Build real anticipation and tease before offering any content; never rush the script.",
    "Sell the right item at the moment, framed as personal and exclusive, so it feels natural and clever, never pushy.",
    "Objection-handle with warmth: find what is holding him back, ask gently how long to wait, keep the mood warm.",
    "If you offer a lower price, make him feel it is only for him (first time, never done before), never a flat '50$ it is'.",
    "Keep the fantasy alive: never a hard yes or hard no to a request the model does not do — stay in the middle and give a playful 'i've never done that before' then pivot to KYC.",
    "Acknowledge the fan's kink or request and guide him to what IS on the script; if it does not exist, build it through descriptive sexting.",
    "Mirror the fan's energy; match an enthusiastic or vulnerable fan with the same warmth and detail.",
    "Honesty works: if he knows she talks to others, admit it lightly then pivot the focus back to him ('ur the only one making me smile right now').",
    "Always leave an open-ended follow-up so the chat can continue; re-engage a quiet fan ('are u still there?', 'is there something wrong?').",
    "Throw in light jokes and fake-relating (places, ages, experiences) to feel human.",
]
HOUSE_BAD = [
    "Rushing the script or dropping a teaser/PPV before any anticipation is built — kills the excitement and the sale.",
    "Close-ended / one-line replies that pass the work back to the fan; dead-end questions.",
    "Sending a PPV or content to a new sub instead of pitching the script, or sending whatever he asks for on demand (no control of the chat).",
    "Asking the fan to imagine or describe it for you instead of driving the scene; demanding constant reassurance ('where would u take me?', 'tell me jake').",
    "Hard-selling, guilt-tripping, pressure, or being passive-aggressive; asking or implying the fan is broke ('it's inappropriate to ask if a fan is broke').",
    "Quoting a price bluntly, or telling him exactly how much is left to unlock to finish — never quote the remaining amount.",
    "A flat rejection or a direct 'no' / 'i dont have anything like that' that kills the conversation and the fantasy.",
    "Boundaries/TOS: suggesting a meet-up, moving off-platform, giving personal info, or anything touching under-18 / the word 'young' — never entertain it.",
    "Repetitive, robotic replies: same emoji every time, overusing the fan's name line after line, boring filler.",
    "Low-effort captions (an emoji as the caption), no follow-up message for the fan to come back to.",
    "Broken or spastic grammar that loses the meaning; calling the fan 'dude'/'bro' which breaks the romantic tension.",
    "Underpricing explicit content (a solo baselines ~$25; anything more explicit is priced well above) or a silly price for a short clip.",
]
# One real 'replicate this' exchange (green), chatter lines only, showing chunking + building + a smooth tip ask:
HOUSE_GOOD_EXAMPLE = ("really? / i really enjoyed it last night / i wanna make sure u cum for me this time / "
                      "that would feel so so good / would u tip me like last night / lets do this together")


def house_standard_text() -> str:
    good = "\n".join(f"  + {g}" for g in HOUSE_GOOD)
    bad = "\n".join(f"  - {b}" for b in HOUSE_BAD)
    return (f"GREEN (replicate):\n{good}\n\nRED (never do):\n{bad}\n\n"
            f"A real green example to calibrate tone and pacing (chatter's own lines): “{HOUSE_GOOD_EXAMPLE}”")

