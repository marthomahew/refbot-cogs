"""The text of every guide topic.

Keep this in step with the cogs (and README.md) when commands change.
`{report}` and `{chants}` are filled in live from the other cogs' settings.
"""

# (key, dropdown label, emoji, one-line description, mods only?)
TOPICS = [
    ("scoreboard", "Scoreboard", "🏈", "the live scores thing", False),
    ("links", "Link fixer", "🔗", "why your tweet got reposted", False),
    ("report", "Reporting spam", "🛎️", "flag scam bots, get a mod", False),
    ("reminders", "Reminders", "⏰", "yes, !remindme actually works now", False),
    ("chants", "Chants", "📣", "FTP etc", False),
    ("awards", "Weekly awards", "👑", "kekkest and friends", False),
    ("emotes", "Emotes & pfps", "😀", "steal emotes, grab full-size pfps", False),
    ("fun", "8-ball", "🎱", "ask it things", False),
    ("faq", "FAQ", "❓", "\"why did the bot do that\"", False),
    ("mods", "Mod stuff", "🛠️", "for mods", True),
]

TEXT = {
    "scoreboard": """\
## 🏈 Scoreboard
The pinned scoreboard updates itself. Vikings game up top, everyone else below: live scores, who has the ball, kickoff times, and highlight links once games are over.

Every minute during games, less often otherwise. Scores come from ESPN so they can trail the TV a bit.

New week shows up Wednesday at 3pm Central, so you get a couple extra days to enjoy (or suffer through) the last one.""",

    "links": """\
## 🔗 Link fixer
Post a twitter/x, insta, tiktok or reddit link and the bot deletes it and reposts it as you with a link that actually plays in Discord. It also strips the tracking junk from share links so nobody can see who shared it.

Want yours gone? React 🗑️ on the repost.
Don't want it fixed in the first place? Post the fxtwitter-style link yourself, or put the link in `<` `>`.

`!links` shows everything you've shared (or `!links @someone`).""",

    "report": """\
## 🛎️ Reporting spam
See a scam bot ("add me", "dm me", free nitro, etc)? React to the message with {report}. Your reaction vanishes right away so nobody knows it was you, and the mods get a heads up.

If a few people report the same brand new account, the bot just deletes the message. Regulars can't get deleted this way, so don't bother trying it on your friends.

Assistant Coaches: if something needs a mod *now*, use `/alert` (or right-click the message → Apps → Alert staff). That actually pings them.""",

    "reminders": """\
## ⏰ Reminders
`!remindme 2h check the injury report` and the bot replies to you in 2 hours.

Reply to someone's message with `!remindme 1d` and it'll remind you about *that* message instead. Just `!remindme` on its own gives you a button with a form. `/remindme` and right-click → Apps → Remind me about this work too.

Times look like `30m`, `2h`, `3d`, `1w`. `!reminders` shows yours, `!reminders cancel 1` kills one.""",

    "chants": """\
## 📣 Chants
Say it anywhere in a message and the bot says it louder:
{chants}
It'll only go a few times a minute, so don't bother spamming it after a win.""",

    "awards": """\
## 👑 Weekly awards
Every Monday at noon the bot crowns whoever got the most of each reaction that week (kek, thistbh, babydino, doubt) and they get the role for the week.

Reacting to your own stuff doesn't count. You can only win one per week, so if you top two, you get the better one and the next person gets the other. The card explains who got skipped. Mods can't win.

Check the race any time with `!reactking :kek:`.""",

    "emotes": """\
## 😀 Emotes & pfps
Reply to a message with `!download` to get its emotes/stickers as images.

`!avatar @someone` (or `!pfp`) gets their pfp at full size, plus their server pfp and banner if they have them. `/avatar` and right-click → Apps → Get avatar do the same thing but only you see it. Meme responsibly.""",

    "fun": """\
## 🎱 8-ball
`!8ball are the Packers frauds?` or `/8ball`. Has to end with a question mark or it won't play along.""",

    "faq": """\
## ❓ FAQ
**My message got deleted and reposted with an APP tag??**
That's the link fixer. Your link plays now. React 🗑️ if you want it gone.

**Why's my name white on the repost?**
Discord won't show role colors on bot posts. Clicking "shared by @you" gets to your real profile.

**My 🛎️ reaction disappeared**
Good, that means the report went through.

**I had more keks, why did someone else get Kekkest?**
You probably already won a higher award that week. One per person.

**My reminder never showed up**
Check `!reminders`. If the original message got deleted it posts in the channel instead.""",

    "mods": """\
## 🛠️ Mod stuff
Slash versions of the mod commands (replies are just for you, still logged): `/kick` `/ban` `/unban` `/timeout` `/mute` `/unmute` `/mutechannel` `/unmutechannel` `/slowmode`

Reports: `!reportset show`, `!reportset block @user` for serial false reporters, `!reportset autohide <n>`
Awards: `!awards list`, `!awards preview`, `!awards priority`
Scoreboard: `!scoreboard refresh`
Link fixer: `!embedfix list`, `!embedfix map <site> <proxy>` when a proxy dies
Chants: `!chant add FTG Fuck the Giants`, `!chant remove`, `!chant limit <n>`
Emotes: reply with `!steal` to add them to the server

Everything else is in the README on GitHub.""",
}
