"""The text of every guide topic.

Keep this in step with the cogs (and README.md) when commands change.
`{report}` and `{chants}` are filled in live from the other cogs' settings.
"""

# (key, dropdown label, emoji, one-line description, mods only?)
TOPICS = [
    ("scoreboard", "Scoreboard", "🏈", "The live NFL scoreboard", False),
    ("links", "Link fixer", "🔗", "Why links get reposted, and how to delete yours", False),
    ("report", "Reporting & alerts", "🛎️", "Flag spam or get a mod's attention", False),
    ("reminders", "Reminders", "⏰", "!remindme and friends", False),
    ("chants", "Chants", "📣", "FTP, FTB and the rest", False),
    ("awards", "Weekly awards", "👑", "The Monday reaction kings", False),
    ("emotes", "Emotes & avatars", "😀", "Save emotes, grab full-size pfps", False),
    ("fun", "Fun", "🎱", "The 8-ball", False),
    ("faq", "FAQ", "❓", "Common questions", False),
    ("mods", "For mods", "🛠️", "Mod and admin commands", True),
]

TEXT = {
    "scoreboard": """\
## 🏈 Scoreboard
One message that keeps itself up to date with every game of the week.

**What's on it**
- **Top card:** the Vikings game: kickoff, TV, line and weather before the game; score, clock, last play, scoring by quarter and stat leaders during and after.
- **Around the NFL:** every other game as a card. 🔴 Live (score, clock, who has the ball), 🗓️ Upcoming (kickoff time), ✅ Final (with a **Highlights** link, usually the NFL's YouTube video).

**How often it updates**
- Every minute while games are on, every 15 minutes on game days, hourly otherwise.
- The new week appears **Wednesday at 3 PM Central** (last week stays up until then).
-# Scores come from ESPN and can lag the TV by up to a minute.""",

    "links": """\
## 🔗 Link fixer
Post an **X/Twitter, Instagram, TikTok or Reddit** link and Refbot reposts your message under your name with a link that actually plays in Discord.

**Good to know**
- It strips tracking codes from share links, so nobody can tell who shared it.
- Your repost says *shared by @you*; search `mentions: @you` to find your links, or use `!links` (or `!links @someone`).
- **Delete your own repost:** react 🗑️ on it.
- **Don't want it fixed?** Post the fixed link yourself (fxtwitter, etc.) or wrap the link in `<` `>`.
-# Messages with stickers, voice messages or forwards get a reply instead of a repost.""",

    "report": """\
## 🛎️ Reporting & alerts
**See spam or a scam bot?** React to the message with {report}.
- Your reaction disappears straight away; nobody can see who reported.
- The mods get a report with a link, and the message gets a public note.
- If 3 different members report a message from a **brand-new account**, it's removed automatically. Established members' messages are never auto-removed.

**Need a mod right now?** (Assistant Coaches and mods)
- `/alert` or `!alert`, or right-click the message → **Apps → Alert staff**.
- This pings the mods. If none respond, Defender's emergency mode can kick in so helpers can act.
-# False reports can get your reporting turned off.""",

    "reminders": """\
## ⏰ Reminders
Reminders come back as a **reply in the same channel**, pinging you.

**Ways to set one**
- `!remindme 2h check the injury report`
- Send `!remindme 1d` **as a reply** to a message: the reminder replies to that message.
- Just `!remindme` gives you a button that opens a form.
- `/remindme` (posts a little "set a reminder" note the reminder replies to).
- Right-click a message → **Apps → Remind me about this**.

**Times:** `30m`, `2h`, `3d`, `1w`, `2h30m` (a plain number means minutes), up to a year.
**Your reminders:** `!reminders` lists them, `!reminders cancel 2` cancels one.
-# Up to 25 at a time.""",

    "chants": """\
## 📣 Chants
Type one of these anywhere in a message (any capitalisation) and Refbot finishes the thought:
{chants}
-# Only a few replies per minute, so post-game floods don't drown the channel. `!chant list` shows them all.""",

    "awards": """\
## 👑 Weekly awards
Every **Monday at noon Central**, Refbot crowns whoever got the most of each reaction emote in the past week, and the winners get a role for the week.

**The rules**
- Reacting to your own message doesn't count, and neither do bots.
- Link reposts count for the person who shared them.
- **One award per person:** if you top several, you get the highest one, and the next award goes to the next person. Statbot's top chatter always comes first.
- Mods and admins can't win.

**See the race any time:** `!reactking :kek:` (add `30d` or a `#channel` if you like).""",

    "emotes": """\
## 😀 Emotes & avatars
**Save someone's emote or sticker:** reply to their message with `!download`; you get the image files.

**Full-size profile pictures:** `!avatar @someone` (or `!pfp`), `/avatar`, or right-click them → **Apps → Get avatar**. You get their avatar, server avatar and banner, as big as they exist.
-# Mods can add emotes to the server with `!steal` (as a reply).""",

    "fun": """\
## 🎱 Fun
Ask the Magic 8-Ball: `!8ball will we make the playoffs?` or `/8ball`.
-# It has to end with a question mark.""",

    "faq": """\
## ❓ FAQ
**Why did my message disappear and come back with an APP tag?**
The link fixer reposted it with a working link. React 🗑️ on it to delete it.

**Why isn't my name coloured on a repost?**
Reposts are posted by the bot, so Discord can't show role colours. Click *shared by @you* for your real profile.

**How do I find links I shared?** `!links` (or search `mentions: @you`).

**My reaction disappeared!**
If it was the report emoji, that's normal: it means the report went through.

**Why did someone else get the award when I had more?**
One award per person; if you'd already won a higher one, it goes to the next in line. The card says who was skipped and why.

**My reminder didn't fire.**
Check `!reminders`. If the message it replied to was deleted, it posts in the channel instead.""",

    "mods": """\
## 🛠️ For mods
**Slash mod commands** (replies only you can see; logged like `!` commands)
`/kick` `/ban` `/unban` `/timeout` `/mute` `/unmute` `/mutechannel` `/unmutechannel` `/slowmode`

**Reports:** `!reportset show` · `!reportset block @user` / `unblock` · `!reportset autohide <n>`
**Awards:** `!awards list` · `!awards preview` (no roles) · `!awards testrun` · `!awards priority`
**Scoreboard:** `!scoreboard refresh` · `!scoreboard settings`
**Link fixer:** `!embedfix list` · `!embedfix map <site> <proxy>` (if a proxy breaks)
**Chants:** `!chant add FTG Fuck the Giants` · `!chant remove` · `!chant limit <n>`
**Emotes:** `!steal` as a reply adds its emotes to the server.
-# Full details are in the refbot-cogs README on GitHub.""",
}
