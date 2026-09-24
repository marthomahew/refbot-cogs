# refbot-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for Refbot.
Built for Red 3.5.24 / Python 3.11.

## Install

In Discord (prefix `!`):

```
!repo add refbot-cogs https://github.com/marthomahew/refbot-cogs
!cog install refbot-cogs scoreboard embedfix
!load scoreboard embedfix
```

Updating after pushing changes:

```
!cog update
!reload scoreboard embedfix
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

When someone posts a Twitter/X, Instagram, TikTok or Reddit link, the bot replies
(without pinging them) with a proxy link that embeds properly, and hides the
original message's embed so there aren't two previews. It's on as soon as it's loaded.

Links are left alone when they're wrapped in `<...>`, inside `||spoilers||` or
`code`, posted by bots, or in an ignored channel.

| Command | Who | What it does |
| --- | --- | --- |
| `!embedfix toggle` | Admin | Turn link fixing on or off |
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

The bot needs Send Messages, Embed Links and Read Message History to reply, and
Manage Messages to hide the original embed (without it, it still replies).
