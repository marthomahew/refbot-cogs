# refbot-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for Refbot.
Built for Red 3.5.24 / Python 3.11.

## Install

In Discord (prefix `!`):

```
!repo add refbot-cogs https://github.com/marthomahew/refbot-cogs
!cog install refbot-cogs scoreboard
!load scoreboard
```

Updating after pushing changes:

```
!cog update
!reload scoreboard
```

Slash commands (optional): as bot owner, run `!slash enable scoreboard` then `!slash sync`.

## scoreboard

One NFL scoreboard message that the bot keeps editing in place, using ESPN's public
scoreboard data. It has two cards:

- **Team card** (Vikings by default), in team colors with both logos. Before kickoff it
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
