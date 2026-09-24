# refbot-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for Refbot.
Built for Red 3.5.24 / Python 3.11.

## Install

In Discord (prefix `!`):

```
!repo add refbot-cogs https://github.com/marthomahew/refbot-cogs
!cog install refbot-cogs scoreboard embedfix emotesteal
!load scoreboard embedfix emotesteal
```

Updating after pushing changes:

```
!cog update
!reload scoreboard embedfix emotesteal
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

It reports how many slots are left, skips ones already in the server, and adds up
to 10 at a time. Discord's own built-in stickers can't be copied. The bot needs
the **Manage Expressions** permission. `!steal` is a text command only (not a
slash command), because it works by replying to a message.
