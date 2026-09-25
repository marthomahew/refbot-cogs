"""Leaderboards for who gets the most of one reaction emoji (the :kek: king).

`!reactking :kek:` reads message history for a period and totals, per member,
how many :kek: reactions their messages got. Nothing is stored; every run reads
the history fresh.

Fairness rules:
- Reacting to your own message doesn't count, and bots' reactions don't count.
- embedfix reposts (webhook messages ending in "-# shared by @name") count for
  the person who shared them, not for the webhook.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from redbot.core import commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.reactking")

# Same line embedfix puts at the end of every repost.
SHARED_BY_RE = re.compile(r"-# shared by <@!?(\d+)>\s*$")
PERIOD_RE = re.compile(r"^(\d{1,3})([hdw])$")
CUSTOM_EMOJI_RE = re.compile(r"^<(a?):(\w+):(\d+)>$")  # <:kek:123> or <a:kek:123>
MAX_DAYS = 365
TOP_N = 10


def parse_period(text: str) -> Optional[timedelta]:
    """"24h", "7d", "2w" -> timedelta. None if it doesn't look like a period."""
    match = PERIOD_RE.match(text.lower())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
    return min(delta, timedelta(days=MAX_DAYS))


def period_text(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"last {hours} hours"
    return f"last {hours // 24} days"


def owner_id(message: discord.Message) -> Optional[int]:
    """Who gets credit for a message: the "shared by" person for reposts,
    otherwise the author. None for other bots' messages."""
    if message.webhook_id is not None:
        match = SHARED_BY_RE.search(message.content)
        return int(match.group(1)) if match else None
    if message.author.bot:
        return None
    return message.author.id


class ReactKing(commands.Cog):
    """Who gets the most of a reaction emoji."""

    def __init__(self, bot: Red):
        self.bot = bot

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Nothing is stored; leaderboards are counted fresh from message history.
        return

    def _resolve_emoji(self, guild: discord.Guild, text: str) -> Optional[Union[discord.PartialEmoji, str]]:
        """Accepts <:kek:123>, :kek:, kek (a server emoji's name), or a normal emoji like 😂."""
        text = text.strip()
        custom = CUSTOM_EMOJI_RE.match(text)
        if custom:
            animated, name, emoji_id = custom.groups()
            return discord.PartialEmoji(name=name, id=int(emoji_id), animated=bool(animated))
        name = text.strip(":")
        found = discord.utils.get(guild.emojis, name=name)
        if found:
            return discord.PartialEmoji(name=found.name, id=found.id, animated=found.animated)
        # Anything else that isn't plain letters is treated as a normal emoji (😂, 🔥).
        if not re.fullmatch(r"\w+", name):
            return text
        return None

    @staticmethod
    def _matches(reaction: discord.Reaction, target: Union[discord.PartialEmoji, str]) -> bool:
        if isinstance(target, str):
            return isinstance(reaction.emoji, str) and reaction.emoji == target
        return getattr(reaction.emoji, "id", None) == target.id

    def _channels_to_scan(self, guild: discord.Guild, only: Optional[discord.TextChannel]) -> list:
        me = guild.me
        if only is not None:
            candidates = [only] + [t for t in guild.threads if t.parent_id == only.id]
        else:
            candidates = list(guild.text_channels) + list(guild.threads)  # threads = active ones
        return [
            c for c in candidates
            if c.permissions_for(me).read_message_history and c.permissions_for(me).view_channel
        ]

    @commands.hybrid_command(name="reactking")
    @commands.guild_only()
    @commands.max_concurrency(1, commands.BucketType.guild)  # one scan at a time per server
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def reactking(
        self,
        ctx: commands.Context,
        emoji: str,
        period: str = "7d",
        channel: Optional[discord.TextChannel] = None,
    ):
        """Who got the most of one reaction emoji, e.g. `[p]reactking :kek: 7d`.

        period: like 24h, 7d, 2w (default 7d, max 365d).
        channel: count only one channel (default: the whole server).
        Self-reactions and bots' reactions don't count. embedfix reposts count
        for the person who shared them.
        """
        target = self._resolve_emoji(ctx.guild, emoji)
        if target is None:
            await ctx.send(f"I don't know the emoji `{emoji}`. Use one from this server, like `:kek:`.")
            return
        delta = parse_period(period)
        if delta is None and channel is None:
            # Allow `!reactking :kek: #vikings` (channel without a period).
            try:
                channel = await commands.TextChannelConverter().convert(ctx, period)
                delta = parse_period("7d")
            except commands.BadArgument:
                pass
        if delta is None:
            await ctx.send("Period should look like `24h`, `7d` or `2w`.")
            return

        cutoff = datetime.now(timezone.utc) - delta
        channels = self._channels_to_scan(ctx.guild, channel)
        totals: dict[int, int] = defaultdict(int)  # member id -> reactions received
        message_counts: dict[int, int] = defaultdict(int)  # member id -> messages that got at least one
        best: tuple[int, str] = (0, "")  # (count, jump url) of the single most-reacted message

        await ctx.defer()
        async with ctx.typing():
            for chan in channels:
                try:
                    async for message in chan.history(limit=None, after=cutoff):
                        reaction = next((r for r in message.reactions if self._matches(r, target)), None)
                        if reaction is None:
                            continue
                        owner = owner_id(message)
                        if owner is None:
                            continue
                        # Count reactors one by one so self-reactions and bots can be left out.
                        count = 0
                        async for user in reaction.users():
                            if not user.bot and user.id != owner:
                                count += 1
                        if count:
                            totals[owner] += count
                            message_counts[owner] += 1
                            if count > best[0]:
                                best = (count, message.jump_url)
                except discord.HTTPException as e:
                    log.warning("Couldn't read history in #%s: %r", chan, e)

        where = channel.mention if channel else "the whole server"
        title = f"👑 {target} King"
        if not totals:
            await ctx.send(f"Nobody got any {target} in {where} ({period_text(delta)}).")
            return

        ranking = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:TOP_N]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(rank, f'{rank}.')} <@{member_id}> — **{count}** {target} "
            f"({message_counts[member_id]} message{'s' if message_counts[member_id] != 1 else ''})"
            for rank, (member_id, count) in enumerate(ranking, start=1)
        ]
        lines += ["", f"Most {target}'d message: **{best[0]}** — {best[1]}"]

        embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text=f"{'#' + channel.name if channel else 'Whole server'} · {period_text(delta)} · "
                              "self-reacts and bots don't count")
        # Mentions inside an embed never ping anyone.
        await ctx.send(embed=embed)
