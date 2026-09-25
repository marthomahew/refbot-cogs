# refbot-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for Refbot.
Built for Red 3.5.24 / Python 3.11.

## Install

In Discord (prefix `!`):

```
!repo add refbot-cogs https://github.com/marthomahew/refbot-cogs
!cog install refbot-cogs scoreboard embedfix emotesteal reactking
!load scoreboard embedfix emotesteal reactking
```

Updating after pushing changes:

```
!cog update
!reload scoreboard embedfix emotesteal reactking
```

Slash commands (optional): as bot owner, run `!slash enable scoreboard`, `!slash enable embedfix`, then `!slash sync`.

## scoreboard

One NFL scoreboard message that the bot keeps editing in place, using ESPN's public
scoreboard data. It has two cards:

- **Team card** (Vikings by default), in team colors with its logo. Before kickoff it
  shows records, TV, venue, betting line and weather. During and after the game it shows
  the score, clock, who has the ball and down & distance, the last play, scoring by
  quarter, stat leaders, and a recap headline when it's over.
- **Around the NFL card** with every other game: live scores with clock, ball and
  down & distance; upcoming games grouped by kickoff time; and final scores.

It updates every minute while games are live, every 15 minutes on game days, and hourly
otherwise. If ESPN is down, the last good scoreboard stays up.

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
| `!awards add :emoji: [@role]` | Admin | Add a weekly king award; the role moves to each week's winner |
| `!awards remove :emoji:` | Admin | Remove an award |
| `!awards time <day> <HH:MM> [timezone]` | Admin | When to post, e.g. `mon 12:00 America/Chicago` |
| `!awards toggle` | Admin | Turn weekly awards on/off |
| `!awards list` | Admin | Show award settings |
| `!awards overall on [@role]` / `off` | Admin | Also crown an overall Emoji King (all award emotes combined) |
| `!awards preview` | Admin | Post this week's results here now as a test (no roles, no pings) |
| `!awards testrun` | Admin | Like preview, but gives/removes award roles for real (still no pings, nothing public) |

- Period like `24h`, `7d` (default) or `2w`, up to `365d`. Leave out the channel
  to count the whole server (plus active threads).
- Reacting to your own message and bots' reactions don't count.
- embedfix reposts count for the person in "shared by".
- Nothing is stored; it reads message history each time, so long periods on a
  busy server can take a little while. One scan at a time per server.
- The bot needs Read Message History in the channels it should count.

**Weekly awards:** once a week (default Monday 12:00 Central) the bot posts one
card with every award's top 3 for the last 7 days, pings the winners, and moves
each award role from last week's king to this week's (ties share the crown; a
week with no winner takes the role back). Admins and mods (Red's admin/mod roles,
Administrator permission, or the owner) can't win, but their reactions still
count for others; the full results, staff included and marked 🛡️, go to the mod
channel.
Award roles need **Manage Roles**, with the bot's role above the award roles.
If the bot was offline at award time it posts late, up to 12 hours; after
that it skips the week.
