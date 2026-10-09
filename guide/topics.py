"""The text of every guide topic.

Keep this in step with the cogs (and README.md) when commands change.
`{report}` and `{chants}` are filled in live from the other cogs' settings.
"""

# (key, dropdown label, emoji, one-line description, who: False = everyone,
#  True = mods only, "coaches" = Assistant Coaches and mods)
# The first paragraph of "Reporting spam" and its FAQ answer depend on who can
# report (`reportset who`); the guide picks the matching one.
COACHES_REPORT = """\
If you see a scam bot (the "add me" / "DM me" / free nitro type), tag an Assistant Coach or a mod.

Assistant Coaches and mods: react to the message with {report}. Your reaction is removed right away so nobody can see who reported it, and the mods get pinged with a link."""
MEMBERS_REPORT = """\
If you see a scam bot (the "add me" / "DM me" / free nitro type), react to the message with {report}. Your reaction is removed right away so nobody can see who reported it, and the mods get a report with a link."""
AUTOHIDE = """

If a message from a brand new account gets enough reports, the bot deletes it automatically. That only works on new accounts, so it can't be used on regular members."""
COACHES_FAQ = "Only Assistant Coaches and mods can report with it, so for everyone else the bot just removes it. Tag a coach or a mod instead."
MEMBERS_FAQ = "That's expected. It means the report went through."

TOPICS = [
    ("scoreboard", "Scoreboard", "🏈", "How the live scoreboard works", False),
    ("links", "Link fixer", "🔗", "Why your links get reposted", False),
    ("report", "Reporting spam", "🛎️", "Flagging scam bots and getting a mod", False),
    ("reminders", "Reminders", "⏰", "Setting and managing reminders", False),
    ("chants", "Chants", "📣", "FTP and the rest", False),
    ("awards", "Weekly awards", "👑", "The Monday reaction awards", False),
    ("emotes", "Emotes and pfps", "😀", "Saving emotes and full size profile pictures", False),
    ("pickem", "Pick'em", "🏈", "Weekly NFL picks", False),
    ("dailygames", "Daily games", "🧩", "Worldle, Maptap and DailyOrbs scores", False),
    ("fun", "8-ball and FMK", "🎱", "Ask it a question, or make it choose", False),
    ("faq", "FAQ", "❓", "Common questions about the bot", False),
    ("coaches", "Assistant Coaches", "📋", "Alerts and emergency mode", "coaches"),
    ("mods", "Mod stuff", "🛠️", "Mod and admin commands", True),
]

TEXT = {
    "scoreboard": """\
## Scoreboard
The scoreboard updates itself. The Vikings game is at the top, and every other game is below it with live scores, who has the ball, kickoff times, and highlight links once games are over.

It updates every minute during games and less often otherwise. Scores come from ESPN, so they can run a little behind the broadcast.

The new week shows up Wednesday at 3pm Central, so last week's results stay up until then.

On game days, channels open by themselves in Game Threads a couple of hours before kickoff: one for the Vikings game, a delayed one if you're watching later and want to avoid spoilers, one for each primetime game, and RedZone when several games are on at once. They close after the games and come back next week with their history.""",

    "links": """\
## Link fixer
When you post a Twitter/X, Instagram, TikTok or Reddit link, the bot reposts your message as you with a link that actually plays in Discord. It also strips the tracking codes from share links, so nobody can tell who shared it.

If you want a repost gone, react 🗑️ on it. If you don't want a link fixed in the first place, post the fixed link yourself (fxtwitter and so on) or wrap it in `<` `>`.

`!links` shows everything you've shared, or `!links @someone` for someone else.""",

    "report": """\
## Reporting spam
{report_who}{autohide}""",

    "reminders": """\
## Reminders
`!remindme 2h check the injury report` and the bot will reply to you in two hours.

If you send `!remindme 1d` as a reply to someone's message, the reminder will reply to that message instead. Sending just `!remindme` gives you a button that opens a form. `/remindme` and right-click > Apps > Remind me about this also work.

Times look like `30m`, `2h`, `3d` or `1w`. `!reminders` shows yours, and `!reminders cancel 1` cancels one.""",

    "chants": """\
## Chants
Type one of these anywhere in a message and the bot will finish it for you:
{chants}
It only responds a few times a minute, so it won't flood the channel after a game.""",

    "awards": """\
## Weekly awards
Every Monday at noon Central, the bot looks at who got the most of each reaction over the past week (kek, thistbh, babydino and doubt) and gives them the matching role for the week.

Reacting to your own messages doesn't count. Each person can only win one award a week, so if you top two, you get the higher one and the other goes to the next person. The results card shows who was skipped and why. Mods can't win.

You can check where things stand any time with `!reactking :kek:`.""",

    "emotes": """\
## Emotes and pfps
Reply to a message with `!download` to get its emotes or stickers as image files.

`!avatar @someone` (or `!pfp`) posts their profile picture at full size, along with their server profile picture and banner if they have them. `/avatar` and right-click > Apps > Get avatar do the same thing, but only you can see the result.""",

    "pickem": """\
## Pick'em
Every week, pick the winner of every NFL game. Hit **Make your picks** on this week's board in the pick'em channel, or use `/pickem play`. The board also has the standings for the week and the season, and it updates as games finish. Your picker is only visible to you, and picks save as soon as you click.

Each game locks at its own kickoff, so you can pick the Thursday game on Thursday and wait on Sunday's until Sunday. A game you don't pick counts as a loss.

The board shows the top 10 players and standings. Hit **Full list** to page through everyone, only visible to you. Hit **Where do I stand?** any time to see your record, your rank and whether you can still win. Only you can see it.

A few hours before each week's first game, last week's players get a reminder to make their picks. After Sunday night's game, everyone playing gets tagged with a quick check-in. If it's close, the people still in the running also guess the total points in Monday night's game, and if the week ends in a tie, the closest guess wins it.

Results go up right after the last game of the week, and the next week opens for picks at the same time. `/pickem standings` shows the week and the season, and `/pickem picks @someone` shows their picks once games have started.""",

    "dailygames": """\
## Daily games
Post your Worldle, Maptap or DailyOrbs share in the daily games channel like you normally would. The bot picks up your score on its own, and you can add whatever you want to say in the same message.

Every morning, right after Wordle's results, the bot posts yesterday's scores for each game, best first, with buttons to play today's. It goes by the date in your share, so playing late still counts for the right day. Only your first share of each game counts.

How they're ranked: Worldle by fewest guesses, Maptap by highest score, and DailyOrbs by most orbs, then fewest misses. On Mondays there's also a box with last week's best averages, for anyone who played at least 4 days. `!dailygames games` shows the full list.""",

    "fun": """\
## 8-ball
`!8ball are the Packers frauds?` or `/8ball`. It has to end with a question mark.

## FMK
`/fmk` and give it three options, or `!fmk Packers, Bears, Lions`. The bot decides who gets which.""",

    "faq": """\
## FAQ
**My message got deleted and reposted with an APP tag.**
That's the link fixer, so your link plays in Discord. React 🗑️ on it if you want it gone.

**Why isn't my name colored on the repost?**
Discord doesn't show role colors on bot posts. Clicking "shared by @you" goes to your actual profile.

**My report reaction disappeared.**
{report_faq}

**I had the most keks, so why did someone else get the award?**
You most likely already won a higher award that week. It's one award per person.

**My reminder never showed up.**
Check `!reminders`. If the original message was deleted, the reminder posts in the channel instead.""",

    "coaches": """\
## Assistant Coaches
If someone is causing trouble and needs a mod, use `/alert` or `!alert` in that channel, or right-click their message and pick Apps > Alert staff. That pings the mods with a link to the message so they have context. For scam bots, reacting with {report} does the same thing.

The mods then have 15 minutes to respond. If no mod posts, reacts, takes a mod action or hits Cancel timer on the alert in that time, the bot turns on Emergency Mode. It turns off again as soon as a mod is back.

During Emergency Mode you can use:
`!silence 3` - the bot deletes messages from anyone who joined in the last week. Use it for a raid or a big flood of spam. `!silence 0` turns it off.
`!voteout @user` - starts a vote to mute that person. It goes through once 2 Assistant Coaches react to the vote message.""",

    "mods": """\
## Mod stuff
Slash versions of the mod commands. Replies are only visible to you, and they're logged the same as the `!` versions:
`/kick` `/ban` `/unban` `/timeout` `/mute` `/unmute` `/mutechannel` `/unmutechannel` `/slowmode`

Ban appeals: hit **Approve** or **Deny** on the appeal in the appeal server (or use `!appeal approve <id>` / `!appeal deny <id> <reason>`). Approved people get unbanned and a DM with an invite back.
Timeout posts in the log channel have an **End timeout** button, and mute cases in the modlog have an **Unmute** button. Muted or timed out members get a DM pointing them to Modmail. Edit it with `!mutenotice message`, preview with `!mutenotice test`.
Reports: `!reportset show`, `!reportset who coaches|members`, `!reportset block @user` for repeat false reporters, `!reportset autohide <n>`
Awards: `!awards list`, `!awards preview`, `!awards priority`
Scoreboard: `!scoreboard refresh`
Game channels: `!gameday show` for this week's schedule, `!gameday park` to put them all away
Daily games: `!dailygames preview`, and reply to someone's share with `!dailygames learn` to add a new game
Link fixer: `!embedfix list`, `!embedfix map <site> <proxy>` if a proxy stops working
Chants: `!chant add FTG Fuck the Giants`, `!chant remove`, `!chant limit <n>`, reactions: `!chant react add wild :wild:`, `!chant react remove wild`, `!chant ignore #channel` to turn both off in one channel
Pick'em: `!pickemset show`, `!pickemset preview`, `!pickemset panel` if the weekly post got deleted, `!pickemset reminder <hours>`, `!pickemset pingrole @role`
Emotes: reply to a message with `!steal` to add its emotes to the server

Everything else is in the README on GitHub.""",
}
