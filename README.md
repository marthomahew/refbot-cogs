# refbot-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for Refbot.
Built for Red 3.5.24 / Python 3.11.

## Install

In Discord (prefix `!`):

```
!repo add refbot-cogs https://github.com/marthomahew/refbot-cogs
!cog install refbot-cogs scoreboard embedfix emotesteal reactking modslash remindme chants avatar guide pickem
!load scoreboard embedfix emotesteal reactking modslash remindme chants avatar guide pickem
```

Updating after pushing changes:

```
!cog update
!reload scoreboard embedfix emotesteal reactking modslash remindme chants avatar guide pickem
```

Slash commands (optional): as bot owner, run `!slash enable scoreboard`, `!slash enable embedfix`, then `!slash sync`.

## scoreboard

One NFL scoreboard message that the bot keeps editing in place, using ESPN's public
scoreboard data. It has two cards:

- **Team card** (Vikings by default), in team colors with its logo, and team logos
  in the big score line. Before kickoff it
  shows records, TV, venue, betting line and weather. During and after the game it shows
  the score, clock, who has the ball and down & distance, the last play, scoring by
  quarter, stat leaders, and a recap headline when it's over.
- **Around the NFL card**: a grid of game cards (three per row on desktop, one per
  row on mobile) under 🔴 Live / 🗓️ Upcoming / ✅ Final headers. Live and upcoming
  cards are titled with their clock or kickoff time. Each card shows team logos,
  names and scores (leader in bold), the possession line while live, and a link:
  **Highlights** for finished games (the NFL's YouTube video when found, else
  ESPN's), **Gamecast** otherwise. Logos are the bot's own emoji, created
  automatically from ESPN (no server emoji slots used). YouTube links come from
  the NFL channel's public feed, checked every ~10 minutes while finished games
  lack one.

It updates every minute while games are live, every 15 minutes on game days, and hourly
otherwise. A new week appears at 3:00 PM Central on Wednesday (ESPN switches at 2:00 AM;
the bot keeps last week's results up until the afternoon). Week 1 and preseason weeks,
which don't start on a Wednesday, switch right away. If ESPN is down, the last good scoreboard stays up.

| Command | Who | What it does |
| --- | --- | --- |
| `!scoreboard channel #channel` | Admin | Set the channel and post the scoreboard there |
| `!scoreboard start` | Admin | Turn on automatic updates |
| `!scoreboard stop` | Admin | Turn off automatic updates (the embed stays) |
| `!scoreboard refresh` | Mod | Update right now (once per 30 s) |
| `!scoreboard settings` | Admin | Show channel, message link, poll interval, and status |
| `!scoreboard team <abbr>` | Admin | Change the highlighted team, e.g. `MIN`, `GB` |

Quick setup: `!scoreboard channel #scores` then `!scoreboard start`.

The bot needs View Channel, Send Messages and Embed Links in the scoreboard channel.

## embedfix

When someone posts a Twitter/X, Instagram, TikTok or Reddit link, the bot swaps it
for a proxy link that embeds properly. It's on as soon as it's loaded. Two modes:

- **repost** (default): deletes the message and reposts it under the author's name
  and avatar, with the same text, attachments and fixed links. A small
  "shared by @name" line links to their profile and makes it findable with
  `mentions: @name` search (sent silently, so it doesn't notify). Replies get a
  small "↪ replying to" line. Stickers, voice messages, forwards, files over the upload
  limit, and the first post of a thread use **reply** instead, since a repost would
  lose something.
- **reply**: keeps the message, hides its preview, and replies (without pinging)
  with the fixed links.

Links that already use a proxy (fxtwitter, etc.) are left alone.

**Deleting a repost:** react with 🗑️ on your own repost and the bot deletes it
(and drops it from your `!links`). Anyone else's 🗑️ is removed. Mods can delete
reposts normally.

**Privacy:** share links often contain a code that identifies whoever shared them.
The bot strips tracking (`?igsh=`, `?s=46&t=`, TikTok's `?_t=`), and follows share
links (`vm.tiktok.com/…`, `tiktok.com/t/…`, `instagram.com/share/…`, Reddit `/s/…`)
to the real post so the code isn't passed on. In repost mode the original message
is deleted. If a share link can't be followed (e.g. a site blocks the bot), it's
posted as-is. Reply mode leaves the original message up.

Links are left alone when they're wrapped in `<...>`, inside `||spoilers||` or
`code`, posted by bots, or in an ignored channel.

| Command | Who | What it does |
| --- | --- | --- |
| `!links [@member]` | Everyone | List the last 25 links someone shared through the fixer (yours if no name), with jump links |
| `!embedfix toggle` | Admin | Turn link fixing on or off |
| `!embedfix mode <repost\|reply>` | Admin | Choose how fixed links are posted |
| `!embedfix map <site> <proxy>` | Admin | Add a site, or swap a proxy that stopped working |
| `!embedfix unmap <site>` | Admin | Stop fixing links for a site |
| `!embedfix list` | Admin | Show proxies, ignored channels and status |
| `!embedfix ignore #channel` | Admin | Leave links alone in a channel (and its threads) |
| `!embedfix unignore #channel` | Admin | Fix links in that channel again |

Starting proxies (checked September 2026):

| Site | Proxy |
| --- | --- |
| twitter.com | fxtwitter.com |
| x.com | fixupx.com |
| instagram.com | hhinstagram.com |
| tiktok.com | tnktok.com |
| reddit.com | vxreddit.com |

If a proxy stops working, swap it: e.g. `!embedfix map instagram.com kkinstagram.com`.

Permissions: **Manage Messages** and **Manage Webhooks** for repost mode. Reply mode
needs Send Messages, Embed Links and Read Message History, plus Manage Messages to
hide the original preview. If repost permissions are missing, it replies instead.

## emotesteal

Copy custom emoji and stickers from other servers into this one. Handy when a
Nitro user posts something good.

| Command | Who | What it does |
| --- | --- | --- |
| `!steal` (as a reply) | Mod | Add every custom emoji and sticker in the replied-to message |
| `!steal newname` (as a reply) | Mod | Same, but give a single emoji/sticker a new name |
| `!steal :emoji:` | Mod | Add an emoji pasted straight into the command |
| `!download` (as a reply, or with emoji) | Everyone | Post the emoji/stickers as image files anyone can save (10 per message, 10 s cooldown) |

It reports how many slots are left, skips ones already in the server, and adds up
to 10 at a time. Discord's own built-in stickers can't be copied. The bot needs
the **Manage Expressions** permission (not needed for `!download`). Both are text
commands only (not slash commands), because they work by replying to a message.

## reactking

Leaderboards for who gets the most of one reaction emoji (the :kek: king).

| Command | Who | What it does |
| --- | --- | --- |
| `!reactking :emoji: [period] [#channel]` | Everyone | Top 10 members by that reaction, plus the most-reacted message |
| `!awards channel #channel` | Admin | Where the weekly awards post |
| `!awards modchannel #channel` | Admin | Private channel for full results with admins/mods included |
| `!awards name <award> <title>` / `reset` | Admin | Card title for an award (default: its role's name, e.g. "Kekkest") |
| `!awards priority [award] [position]` | Admin | Show or change which award wins when someone tops several (`reactking` for React King) |
| `!awards statbot #channel @role` / `off` | Admin | Link Statbot's weekly top-chatter announcement (and its role, as a fallback) |
| `!awards exclude #channel` | Admin | Never read that channel or its threads, for awards or `!reactking` (the command message is deleted so the name doesn't linger) |
| `!awards unexclude #channel` | Admin | Count it again |
| `!awards add :emoji: [@role]` | Admin | Add a weekly king award; the role moves to each week's winner |
| `!awards remove :emoji:` | Admin | Remove an award |
| `!awards time <day> <HH:MM> [timezone]` | Admin | When to post, e.g. `mon 12:00 America/Chicago` |
| `!awards toggle` | Admin | Turn weekly awards on/off |
| `!awards list` | Admin | Show award settings |
| `!awards overall on [@role]` / `off` | Admin | Also crown an overall React King (all award emotes combined) |
| `!awards preview` | Admin | Post this week's results here now as a test (no roles, no pings) |
| `!awards testrun` | Admin | Like preview, but gives/removes award roles for real (still no pings, nothing public) |

- Period like `24h`, `7d` (default) or `2w`, up to `365d`. Leave out the channel
  to count the whole server (plus active threads).
- Reacting to your own message and bots' reactions don't count.
- embedfix reposts count for the person in "shared by".
- Nothing is stored; it reads message history each time, so long periods on a
  busy server can take a little while. One scan at a time per server.
- The bot needs Read Message History in the channels it should count.
- Excluded channels (and their threads) and all private threads are never read,
  so they can't appear in any leaderboard, award or "most reacted" link. `!awards
  list` shows only how many channels are excluded, never their names.

**Weekly awards:** once a week (default Monday 12:00 Central):
1. 30 minutes early, the bot tallies the week (the slow part).
2. At the award time it posts the full results (staff included) to the mod channel
   only. Nothing public yet.
3. When Statbot posts its "Hear ye, hear ye! … Bow down to @winner" message (linked
   with `!awards statbot`), or 15 minutes later if it doesn't, the bot hands out the
   roles, then posts the public card (every award's top 3 plus "🏅 role → @winner"
   lines) with a "Congrats @winners! 👑" ping.

**One award per person**, in priority order: Statbot's top chatter (always first,
since Refbot can't take away Statbot's role) → then the order set with
`!awards priority` (default: 👑 React King if enabled, then the emoji awards in the
order they were added). The card lists awards in the same order. Each role goes to the
highest-ranked person on that leaderboard who doesn't already hold a higher award
that week (the card says who was skipped and why). Ties share a role; a week with
no eligible winner takes the role back. Admins and mods (Red's admin/mod roles,
Administrator permission, or the owner) can't win, but their reactions still
count for others; the full results, staff included and marked 🛡️, go to the mod
channel.
Award roles need **Manage Roles**, with the bot's role above the award roles.
If the bot was offline at award time it posts late, up to 12 hours; after
that it skips the week.

## modslash

Slash command versions of Red's moderation commands (Red's own Mod, Warnings
and Mutes cogs have no slash commands). Each one runs the matching Red command
underneath, so modlog cases, warning points, mute settings, DMs and permissions
work exactly like the `!` versions.

| Slash command | Same as |
| --- | --- |
| `/ban user [reason] [delete_messages]` | `!ban`, permanent (works for people not in the server). `delete_messages` is a menu: don't delete (default), last 24 hours, 3 days or 7 days |
| `/kick member [reason]` | `!kick` |
| `/tempban member [duration] [reason] [delete_messages]` | `!tempban`, ban for a set time (e.g. `1d`, `1w`) |
| `/softban member [reason]` | `!softban`: kick + delete their last day of messages |
| `/unban user_id [reason]` | `!unban` |
| `/warn member reason [points]` | `!warn` |
| `/warnings member` | `!warnings` |
| `/unwarn member warn_id [reason]` | `!unwarn` |
| `/mute member [duration] [reason]` | `!mute` |
| `/timeout member [duration] [reason]` | `!timeout` |
| `/unmute member [reason]` | `!unmute` |
| `/mutechannel member [duration] [reason]` | `!mutechannel` (this channel only) |
| `/unmutechannel member [reason]` | `!unmutechannel` |
| `/slowmode interval` | `!slowmode` |
| `/8ball question` | Red's `!8ball` (General cog), **public**, with the question shown above the answer |
| `/alert` | Defender's `!alert`: pings staff (helper roles like Assistant Coach, or mods). Once per channel every 2 minutes |
| Right-click a message → Apps → **Alert staff** | Same as `/alert`, but the staff ping links to that exact message. Enable with `!slash enable "Alert staff" message` |
| `/purge amount [user]` | deletes the last N messages (optionally one person's) |

**Invite back on unban:** when someone is unbanned some other way than Red's own
`unban` (an approved ban appeal, or a mod using Discord's Unban button), Refbot DMs
them "You've been unbanned from <server>. Here's an invite back: <link>" (the server's
permanent invite if it has one, else a new one-day invite). Red's `unban`/softban
send their own invite (`modset reinvite`) and expired tempbans already got one, so
those are skipped, told apart by the audit log. `!unbaninvite on|off` (admin, on by default);
`!unbaninvite message [text|reset]` shows or changes the wording (`{server}`, `{invite}`, which is required).

**End timeout button:** ExtendedModLog's "member updated" posts for a timeout starting get
an **End timeout** button (mods only, or anyone with Timeout Members). It lifts the timeout
and turns into a greyed-out "Timeout ended by <mod>". Works on posts from before a restart.

**Mute notice:** anyone who gets muted or timed out is DM'd "You've been muted (or timed out) in <server>
until <time>. Think this was a mistake? DM Refbot Modmail and the mods will take a look."
Covers Red's mutes (server, channel, voice) and timeouts, Defender's automatic timeouts and
Discord's own Timeout button; one notice per person per minute (Red's timeout fires twice).
`!mutenotice on|off`, `!mutenotice message [text|reset]` (`{action}` = muted / timed out, `{where}`, `{server}`, `{until}`; `<@bot id>` makes a clickable mention),
`!mutenotice test` DMs you a preview.

**Report reaction (needs Defender):** react to a message with the report emoji
(set with `!reportset emoji 🛎️`, a standard or custom server emoji, then
`!reportset toggle`). The bot removes the reaction at once. By default only
**Assistant Coaches (Defender helper roles) and mods** can report: their reaction
sends Defender's full alert, like `/alert`, and anyone else's is just removed.
`!reportset who members` opens it up to members, by the reactor's Defender rank:
Rank 1 (mods, Defender helper/trusted roles) sends Defender's full alert, like
`/alert`; Rank 2 (established members) sends a quiet "member report" to
Defender's notify channel with no ping (one per message every 6 hours); Rank 3-4
(new members) is ignored. A reported message gets one public reply (never says who
reported): "⚠️ Reported to the mods. Treat with caution." if the author is a new
account (Rank 3-4 or already left), otherwise "🚩 Reported to the mods."

**Auto-hide (spam):** once 3 different members (Rank 2+, not blocked) report a
message *from a new account* (Defender Rank 3-4, or someone who already left),
the bot deletes it, changes the note to "🧹 Removed after multiple reports.", and
tells staff (with the text and who reported it). Messages from established
members, mods, helpers, bots and link reposts can never be auto-hidden, only
reported. `!reportset autohide <n>` changes the number (0 = off). `!reportset block @user` ignores someone's reports after false reports
(`unblock` to undo); `!reportset who coaches|members` sets who can report; `!reportset show` shows the settings.

All replies are private ("Only you can see this"); modlog and log channels still record everything. Durations look like `10m`, `2h`, `1d`. Setup (bot owner): `!slash enablecog
modslash`, then `!slash sync`. Discord hides each command from members without
the matching Discord permission (e.g. Ban Members for `/ban`); change who sees
them in Server Settings → Integrations → the bot. Red's own mod/admin role
checks still apply either way.

## remindme

Reminders that come back as a reply in the same channel, pinging you.

| How | What happens |
| --- | --- |
| `!remindme 2h check the injury report` | ✅ on your message; the reminder replies to it |
| `!remindme 1d` sent as a reply | the reminder replies to the message you replied to |
| `!remindme` (nothing else) | a "⏰ Set a reminder" button (only you can use it, gone after 30 s) opens a form |
| `/remindme when what` | posts "⏰ You set a reminder · in 2 hours" + the note; the reminder replies to that |
| Right-click a message → Apps → **Remind me about this** | a form; the reminder replies to that message |
| `!reminders` / `!reminders cancel <number>` | list / cancel your reminders (private with slash) |

Times are lengths: `30m`, `2h`, `3d`, `1w`, `2h30m` (a bare number = minutes), up
to a year. Up to 25 active reminders per person. If the original message was
deleted, the reminder posts in the channel instead. Setup (bot owner):
`!slash enable remindme`, `!slash enable reminders`,
`!slash enable "Remind me about this" message`, then `!slash sync`.

## chants

When someone types a chant acronym anywhere in a message (any capitalisation,
whole word only), the bot replies with the full chant, pinging nobody:
FTP → Fuck the Packers, FTB → Fuck the Bears, FTL → Fuck the Lions,
FTR → Fuck the Refs, FSP → Fuck Sean Payton. Several in one message get one
reply. At most 3 replies per channel per minute by default (adjustable). Bots and link
reposts are ignored.

| Command | Who | What it does |
| --- | --- | --- |
| `!chant list` | Everyone | Show all chants |
| `!chant add <acronym> <phrase>` | Admin | Add or change one, e.g. `!chant add FTG Fuck the Giants` |
| `!chant remove <acronym>` | Admin | Remove one |
| `!chant limit <n> [channel\|server]` | Admin | Replies per minute, per channel (default) or across the server, 1-30 |
| `!chant toggle` | Admin | Turn chant replies on/off |

## avatar

Full-size profile pictures (handy for memes): the main avatar, the server-specific
avatar and the profile banner, whichever exist, as files you can save. PNG for
still images, GIF if animated, at the largest size they exist (never upscaled).

| How | Who sees the reply |
| --- | --- |
| `!avatar [@user]` (or `!pfp`) | everyone |
| `/avatar [user]` | only you |
| Right-click a user → Apps → **Get avatar** | only you |

Setup (bot owner): `!slash enable avatar`, `!slash enable "Get avatar" user`, then `!slash sync`.

## pickem

Weekly NFL pick'em, straight up (no spread), using ESPN's public scoreboard.

- A new week opens as soon as the last game of the previous week is final (normally
  right after Monday night): the bot posts last week's results, then the new week's
  **board** in the pick'em channel: one message with three cards, "Make your picks"
  (next lock time), **Players** (who's playing and how many games they've picked,
  never which teams) and the standings (this week and the season). The board shows
  the top 10 of each list. Buttons: **Make your picks**, **Where do I stand?** and
  **Full list**, which opens a private copy of every list, 10 per page, with Back /
  Next and a menu to switch between this week, the season and players. The board is edited in place as picks come in (about 30 s later) and as
  games finish.
- The channel is meant to be read-only for members (deny Send Messages for @everyone;
  buttons still work). The bot keeps it tidy: at most the current board, last week's
  results, and the tiebreaker while it's open. When results post, the previous
  results, that week's board and its tiebreaker are deleted. (The scoreboard still waits until Wednesday
  3 PM so Tuesday stays recap time.)
- The button (or `/pickem play`) opens a private picker: four games per page, one
  button per team with team logos. Picks save on click. Each game locks at its own
  kickoff, checked when the pick is saved, not just by greying out buttons.
- Right pick = win. Wrong pick or no pick = loss, once you've made at least one pick
  that week. An NFL tie is a push for everyone. Postponed games that aren't played
  that week don't count.
- **Tiebreaker:** once every game before the last game day (normally Monday) is final,
  anyone who could still finish tied for first is pinged to guess the total points of
  Monday night's game (both games added together on a Monday doubleheader). Guesses
  lock at kickoff. Closest guess wins a tie; if still tied, they share the week. Weeks
  where every game is on one day have no tiebreaker.
- **Where do I stand?** answers privately: your record, your rank, whether you can
  still win or tie, your remaining picks, tiebreaker guess, and season record.
- **Reminder:** 4 hours before the week's first game (normally Thursday 3:15 PM CT)
  the bot pings everyone who played the previous week plus an optional role (e.g.
  @Pickems) with a Make your picks button. It's deleted once that game kicks off.
  The role needs to be mentionable, or the bot needs Mention @everyone, @here and All Roles.
- **Sunday night check-in:** once every game before Monday is final, the bot pings
  everyone playing that week with a Where do I stand? button. If it's close, the same
  message is the tiebreaker (contenders get an Enter my guess button).
- The results ping everyone who played (with a Where do I stand? button), show the
  top 5 for the week and the season, name the winner and moves the optional winner role to them. Staff can
  play and win. The role is independent of the reaction awards, so someone can hold
  both.
- Other people's picks stay hidden until each game kicks off.
- Every `/pickem` reply is private ("Only you can see this"). The `!pickem` versions
  post in the channel.

| Command | Who | What it does |
| --- | --- | --- |
| `/pickem play` | Everyone | Open your private picker |
| `!pickem` | Everyone | Post a button that opens the picker |
| `/pickem picks [member]` | Everyone | Your picks in full (private); others' only for games that have started |
| `!pickem picks [@member]` | Everyone | Picks for games that have started |
| `/pickem standings` | Everyone | Full standings and players, private and paged (same as **Full list**) |
| `!pickem standings` | Everyone | Top 10 standings, posted in the channel |
| `!pickemset channel #channel` | Admin | Where the panel, tiebreaker and results go |
| `!pickemset role [@role]` | Admin | Weekly winner role (leave empty for none) |
| `!pickemset toggle` | Admin | Turn pick'em on or off |
| `!pickemset reminder <hours>` | Admin | Hours before the first game to send the reminder (default 4, 0 = off) |
| `!pickemset pingrole [@role]` | Admin | Also ping this role with the reminder (leave empty for none) |
| `!pickemset panel` | Admin | Repost this week's board at the bottom of the channel (the old one is deleted) |
| `!pickemset preview` | Admin | Show the results card as it stands (no pings, no roles) |
| `!pickemset show` | Admin | Settings, players this week, last problem |

Setup: `!pickemset channel #pickem`, optionally `!pickemset role @Role`, then
`!pickemset toggle`. Slash: `!slash enable pickem` then `!slash sync`.

## guide

An in-Discord guide to everything above: a panel with a "Pick a topic…" dropdown
(Scoreboard, Link fixer, Reporting & alerts, Reminders, Chants, Weekly awards,
Emotes & avatars, Pick'em, Fun, FAQ, Assistant Coaches, For mods). Each topic opens privately;
"Assistant Coaches" only opens for Defender helper roles and staff, "For mods" only for staff. The report emoji and chant list are filled in from the live
settings. The topic text lives in `guide/topics.py`.

Panels update themselves: after changing topics, `!reload guide` re-edits every panel
the bot remembers, and any older panel updates the first time someone uses it. No
reposting needed.

| Command | Who | What it does |
| --- | --- | --- |
| `!guide post #channel` | Admin | Post the permanent panel (keeps working after restarts) |
| `!guide` | Everyone | Show the panel here |
| `/guide` | Everyone | Show the panel privately |
