"""A morning recap of yesterday's daily games (Worldle, Maptap, DailyOrbs, ...).

- People post their game shares in the daily games channel as usual. The bot
  reads each share (games.py) and saves the result under the date written in
  the share. Only someone's first share per game per day counts.
- Each morning, right after the Wordle app posts its daily results (or at
  10 AM Central if Wordle hasn't), the bot posts one recap: a card per game
  with yesterday's scores, best first, names shown without pinging, and
  buttons to today's games.
- A game nobody has played for 3 days in a row drops out of the recap until
  someone plays it again.
- Admins can add simple-score games by replying to a share with
  `dailygames learn`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import games

log = logging.getLogger("red.refbot.dailygames")

CENTRAL = ZoneInfo("America/Chicago")
FALLBACK_HOUR = 10  # post at 10 AM Central if Wordle hasn't posted by then
HIDE_AFTER = 3  # empty days in a row before a game's card is left out
KEEP_DAYS = 35  # days of results kept
WORDLE_MARKERS = ("yesterday", "results")  # words in the Wordle app's daily post
BUILT_IN_NAMES = ["Worldle", "Maptap", "DailyOrbs"]
COLOR = discord.Color(0x4F2683)


def central_today() -> date:
    return datetime.now(CENTRAL).date()


class LearnView(discord.ui.View):
    """`dailygames learn`: pick which number is the score, then which way is better."""

    def __init__(self, cog: "DailyGames", author_id: int, name: str, match: str, link: Optional[str],
                 candidates: list[dict]):
        super().__init__(timeout=300)
        self.cog, self.author_id = cog, author_id
        self.name, self.match, self.link, self.candidates = name, match, link, candidates
        self.choice: Optional[dict] = None
        menu = discord.ui.Select(
            placeholder="Which one is the score?",
            options=[discord.SelectOption(label=c["example"][:100], value=str(i)) for i, c in enumerate(candidates)],
        )
        menu.callback = self.picked
        self.add_item(menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the admin who ran it can answer this.", ephemeral=True)
            return False
        return True

    async def picked(self, interaction: discord.Interaction):
        self.choice = dict(self.candidates[int(interaction.data["values"][0])])
        self.clear_items()
        for better, label in (("higher", "Higher is better"), ("lower", "Lower is better")):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary
                                       if better == self.choice["better"] else discord.ButtonStyle.secondary)
            button.callback = self._direction(better)
            self.add_item(button)
        await interaction.response.edit_message(
            content=f"Score: **{self.choice['example']}**. Is a higher or a lower score better?", view=self)

    def _direction(self, better: str):
        async def callback(interaction: discord.Interaction):
            spec = {"name": self.name, "match": self.match, "link": self.link, "better": better,
                    **{k: v for k, v in self.choice.items() if k in ("kind", "label", "denominator")}}
            await self.cog.save_learned(interaction.guild, spec)
            self.stop()
            await interaction.response.edit_message(
                content=(f"Added **{self.name}**. Shares mentioning `{self.match}` count from now on, "
                         f"scored by {self.choice['example'].split(':')[0] if self.choice['kind'] == 'label' else self.choice['example']} "
                         f"({better} is better)."),
                view=None)
        return callback


class DailyGames(commands.Cog):
    """A morning recap of yesterday's daily games."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2DB, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel_id=None,
            results={},  # "2026-10-08" -> game name -> user id -> [sort list, text, title]
            empty_days={},  # game name -> how many recaps in a row it had no players
            last_posted=None,  # Central date of the last recap ("2026-10-09")
            learned=[],  # learned game specs, see games.py
        )
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        uid = str(user_id)
        for guild_id in await self.config.all_guilds():
            async with self.config.guild_from_id(guild_id).results() as results:
                for day in results.values():
                    for scores in day.values():
                        scores.pop(uid, None)

    # ------------------------------------------------------------ collecting shares

    async def _record(self, message: discord.Message) -> None:
        conf = await self.config.guild(message.guild).all()
        posted = message.created_at.astimezone(CENTRAL).date()
        found = games.read_all(message.content, posted, conf["learned"])
        if not found:
            return
        uid = str(message.author.id)
        async with self._lock:
            async with self.config.guild(message.guild).results() as results:
                for r in found:
                    day = results.setdefault(r.day.isoformat(), {})
                    scores = day.setdefault(r.game, {})
                    if uid not in scores:  # only the first share per person per game per day
                        scores[uid] = [list(r.sort), r.text, r.title]
                cutoff = (central_today() - timedelta(days=KEEP_DAYS)).isoformat()
                for key in [k for k in results if k < cutoff]:
                    del results[key]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or not message.content:
            return
        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"] or message.channel.id != conf["channel_id"]:
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if message.author.bot:
            # The Wordle app's daily results are our cue to post.
            text = message.content.lower()
            if message.author.id != self.bot.user.id and all(w in text for w in WORDLE_MARKERS):
                await self._post_if_due(message.guild)
            return
        await self._record(message)

    async def _backfill(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        """Read the last two days of the channel, for shares posted while the bot was offline."""
        after = datetime.now(timezone.utc) - timedelta(days=2)
        try:
            async for message in channel.history(after=after, limit=500, oldest_first=True):
                if not message.author.bot and message.content:
                    await self._record(message)
        except discord.HTTPException as e:
            log.info("Couldn't read #%s history: %r", channel, e)

    # ------------------------------------------------------------ the recap

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        for guild_id, conf in (await self.config.all_guilds()).items():
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(conf["channel_id"] or 0) if guild else None
            if conf["enabled"] and channel is not None:
                await self._backfill(guild, channel)
        while True:
            try:
                if datetime.now(CENTRAL).hour >= FALLBACK_HOUR:
                    for guild_id, conf in (await self.config.all_guilds()).items():
                        guild = self.bot.get_guild(guild_id)
                        if guild and conf["enabled"]:
                            await self._post_if_due(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Daily games check failed")
            await asyncio.sleep(300)

    async def _post_if_due(self, guild: discord.Guild) -> None:
        async with self._lock:
            conf = await self.config.guild(guild).all()
            today = central_today()
            if conf["last_posted"] == today.isoformat():
                return
            await self.config.guild(guild).last_posted.set(today.isoformat())
        channel = guild.get_channel(conf["channel_id"] or 0)
        if channel is None:
            return
        embed, view, empty = self.recap(conf, today)
        async with self.config.guild(guild).empty_days() as empty_days:
            for name in self._game_names(conf):
                empty_days[name] = empty_days.get(name, 0) + 1 if name in empty else 0
        if embed is None:
            return  # nobody played anything yesterday: nothing to post
        try:
            await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post the daily games recap in guild %s: %r", guild.id, e)

    @staticmethod
    def _game_names(conf: dict) -> list[str]:
        return BUILT_IN_NAMES + [s["name"] for s in conf["learned"] if s["name"] not in BUILT_IN_NAMES]

    def recap(self, conf: dict, today: date) -> tuple[Optional[discord.Embed], discord.ui.View, set]:
        """(the recap embed or None if nobody played anything, its buttons, games nobody played)."""
        day = games.yesterday(today)
        played = conf["results"].get(day.isoformat(), {})
        embed = discord.Embed(title=f"Yesterday's daily games · {day:%b} {day.day}", color=COLOR)
        view = discord.ui.View(timeout=None)
        empty = set()
        for name in self._game_names(conf):
            scores = played.get(name, {})
            link = self._link(conf, name, today)
            if not scores:
                empty.add(name)
                if conf["empty_days"].get(name, 0) + 1 >= HIDE_AFTER:
                    continue  # nobody for 3 days in a row: leave it out
                embed.add_field(name=name, value="Nobody played yesterday", inline=True)
            else:
                title = next((v[2] for v in scores.values() if v[2]), None) or name
                embed.add_field(name=title, value=self._ranking(scores)[:1024], inline=True)
            if link and len(view.children) < 25:
                view.add_item(discord.ui.Button(label=f"Play {name}", url=link))
        if len(empty) == len(self._game_names(conf)):
            return None, view, empty
        return embed, view, empty

    @staticmethod
    def _ranking(scores: dict) -> str:
        """Best first; people with the same score share a line, like Wordle's."""
        lines: list[tuple[list, str, list[str]]] = []
        for uid, (sort, text, _) in sorted(scores.items(), key=lambda kv: (kv[1][0], kv[0])):
            if lines and lines[-1][0] == sort:
                lines[-1][2].append(f"<@{uid}>")
            else:
                lines.append((sort, text, [f"<@{uid}>"]))
        return "\n".join(f"{text} {' '.join(people)}" for _, text, people in lines)

    @staticmethod
    def _link(conf: dict, name: str, today: date) -> Optional[str]:
        if name in games.PLAY_LINKS:
            return games.PLAY_LINKS[name](today)
        spec = next((s for s in conf["learned"] if s["name"] == name), None)
        return spec.get("link") if spec else None

    async def save_learned(self, guild: discord.Guild, spec: dict) -> None:
        async with self.config.guild(guild).learned() as learned:
            learned[:] = [s for s in learned if s["name"].lower() != spec["name"].lower()]
            learned.append(spec)

    # ------------------------------------------------------------ commands

    @commands.group(name="dailygames", aliases=["dailygame"], invoke_without_command=True)
    @commands.guild_only()
    async def dailygames(self, ctx: commands.Context):
        """Yesterday's daily game scores, every morning."""
        await ctx.send_help()

    @dailygames.command(name="help")
    async def dailygames_help(self, ctx: commands.Context):
        """Show these commands."""
        await ctx.send_help(self.dailygames)

    @dailygames.command(name="games")
    async def dailygames_games(self, ctx: commands.Context):
        """Which games the recap counts."""
        learned = await self.config.guild(ctx.guild).learned()
        lines = ["**Worldle**: fewest guesses (X/6 last)", "**Maptap**: highest final score",
                 "**DailyOrbs**: most orbs, then fewest misses"]
        lines += [f"**{s['name']}**: {'lowest' if s['better'] == 'lower' else 'highest'} score "
                  f"(shares mentioning `{s['match']}`)" for s in learned]
        await ctx.send("Post your share in the daily games channel and it's counted:\n" + "\n".join(lines),
                       allowed_mentions=discord.AllowedMentions.none())

    @dailygames.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def dailygames_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the daily games channel and turn the recap on."""
        conf = self.config.guild(ctx.guild)
        await conf.channel_id.set(channel.id)
        await conf.enabled.set(True)
        await ctx.send(f"Watching {channel.mention}. The recap posts there right after Wordle's, "
                       f"or at {FALLBACK_HOUR} AM Central. Reading the last two days of shares now...")
        await self._backfill(ctx.guild, channel)
        await ctx.tick()

    @dailygames.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def dailygames_toggle(self, ctx: commands.Context):
        """Turn the recap on or off."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        if enabled and not await conf.channel_id():
            await ctx.send(f"Set the channel first: `{ctx.clean_prefix}dailygames channel #daily-games`.")
            return
        await conf.enabled.set(enabled)
        await ctx.send(f"Daily games recap is {'on' if enabled else 'off'}.")

    @dailygames.command(name="preview")
    @commands.admin_or_permissions(manage_guild=True)
    async def dailygames_preview(self, ctx: commands.Context):
        """Show this morning's recap here (doesn't count as the day's post)."""
        conf = await self.config.guild(ctx.guild).all()
        embed, view, _ = self.recap(conf, central_today())
        if embed is None:
            await ctx.send("Nobody has posted any shares for yesterday yet.")
            return
        await ctx.send("-# Preview", embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    @dailygames.command(name="learn")
    @commands.admin_or_permissions(manage_guild=True)
    async def dailygames_learn(self, ctx: commands.Context, *, name: Optional[str] = None):
        """Add a game: reply to someone's share of it with this command.

        I'll find the numbers that could be the score and ask which one it is and
        whether higher or lower is better. Works for games that share a plain score
        (like 4/6, Score: 950 or 87%). Optionally give the game's name.
        """
        ref = ctx.message.reference
        source = ref.resolved if ref and isinstance(ref.resolved, discord.Message) else None
        if source is None or not source.content:
            await ctx.send("Reply to someone's share of the game with this command.")
            return
        guessed, match, link = games.identify(source.content)
        name = (name or guessed).strip()[:40]
        if name.lower() in (n.lower() for n in BUILT_IN_NAMES):
            await ctx.send(f"{name} is already built in.")
            return
        candidates = games.score_candidates(source.content)
        if not candidates:
            await ctx.send("I can't find a score in that share (like 4/6, Score: 950 or 87%). "
                           "Games with emoji-only results need adding by hand.")
            return
        await ctx.send(f"Adding **{name}** (I'll spot its shares by `{match}`). Which one is the score?",
                       view=LearnView(self, ctx.author.id, name, match, link, candidates))

    @dailygames.command(name="forget")
    @commands.admin_or_permissions(manage_guild=True)
    async def dailygames_forget(self, ctx: commands.Context, *, name: str):
        """Remove a game added with `learn`."""
        async with self.config.guild(ctx.guild).learned() as learned:
            before = len(learned)
            learned[:] = [s for s in learned if s["name"].lower() != name.strip().lower()]
            removed = len(learned) < before
        await ctx.send(f"Removed **{name}**." if removed else f"There's no learned game called **{name}**.",
                       allowed_mentions=discord.AllowedMentions.none())
