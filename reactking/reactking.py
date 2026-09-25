"""Leaderboards for who gets the most of one reaction emoji (the :kek: king).

`!reactking :kek:` reads message history for a period and totals, per member,
how many :kek: reactions their messages got.

`!awards` sets up a weekly post in an awards channel: every configured emoji
gets a king, and (optionally) a role that moves to each week's winner.

Fairness rules:
- Reacting to your own message doesn't count, and bots' reactions don't count.
- embedfix reposts (webhook messages ending in "-# shared by @name") count for
  the person who shared them, not for the webhook.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Collection, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.reactking")

# Same line embedfix puts at the end of every repost.
SHARED_BY_RE = re.compile(r"-# shared by <@!?(\d+)>\s*$")
PERIOD_RE = re.compile(r"^(\d{1,3})([hdw])$")
CUSTOM_EMOJI_RE = re.compile(r"^<(a?):(\w+):(\d+)>$")  # <:kek:123> or <a:kek:123>
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_DAYS = 365
TOP_N = 10
MAX_AWARDS = 10
# How many channels to read at once. Discord paces requests per channel, so
# reading a few in parallel is much faster than one after another.
SCAN_CONCURRENCY = 4
# If the bot was offline at award time, still post if it's back within this long.
# (Later than that, skip the week rather than announce Monday's awards on Wednesday.)
LATE_GRACE = timedelta(hours=12)

Target = Union[discord.PartialEmoji, str]  # a custom emoji, or a normal one like "😂"


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


def last_scheduled(now: datetime, day: int, hour: int, minute: int, tz) -> datetime:
    """The most recent award time (e.g. last Monday 12:00 in `tz`) at or before `now`."""
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate -= timedelta(days=(local_now.weekday() - day) % 7)
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate


@dataclass
class Tally:
    """Results for one emoji."""

    totals: dict[int, int] = field(default_factory=lambda: defaultdict(int))  # member id -> reactions
    messages: dict[int, int] = field(default_factory=lambda: defaultdict(int))  # member id -> messages
    # member id -> (count, jump url) of that member's most-reacted message
    best_by_owner: dict[int, tuple[int, str]] = field(default_factory=dict)

    def ranking(self, top: int = TOP_N, exclude: Collection[int] = ()) -> list[tuple[int, int]]:
        items = [(member, count) for member, count in self.totals.items() if member not in exclude]
        return sorted(items, key=lambda item: item[1], reverse=True)[:top]

    def best(self, exclude: Collection[int] = ()) -> tuple[int, str]:
        """(count, jump url) of the most-reacted message, skipping excluded members."""
        options = [value for member, value in self.best_by_owner.items() if member not in exclude]
        return max(options, default=(0, ""), key=lambda value: value[0])


class ReactKing(commands.Cog):
    """Who gets the most of a reaction emoji, with optional weekly awards."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D4, force_registration=True)
        self.config.register_guild(
            awards_channel=None,
            mod_channel=None,  # private channel for full results (staff included)
            overall=False,  # also crown an overall "React King" (all award emotes combined)
            overall_role_id=None,
            # Channels never read at all (and their threads), e.g. a private vent channel.
            # Used by both the awards and `!reactking`, so they can never show up anywhere.
            excluded_channels=[],
            # [{"emoji": "<:kek:123>" or "😂", "role_id": int or None}, ...]
            awards=[],
            day=0,  # 0 = Monday
            time="12:00",
            tz="America/Chicago",
            enabled=False,
            last_run=None,  # ISO time of the last award slot we posted for (so we never post twice)
        )
        self._task: Optional[asyncio.Task] = None
        self._scan_lock = asyncio.Lock()  # one history scan at a time
        self.last_scan: dict = {}  # stats from the most recent scan

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._award_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # No per-user data is stored; leaderboards are counted fresh from message history.
        return

    # ------------------------------------------------------------ helpers

    def _resolve_emoji(self, guild: discord.Guild, text: str) -> Optional[Target]:
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
    def _matches(reaction: discord.Reaction, target: Target) -> bool:
        if isinstance(target, str):
            return isinstance(reaction.emoji, str) and reaction.emoji == target
        return getattr(reaction.emoji, "id", None) == target.id

    @staticmethod
    def _is_excluded(chan, excluded: Collection[int]) -> bool:
        """Excluded channels and their threads, plus every private thread (invite-only by nature)."""
        if chan.id in excluded:
            return True
        if isinstance(chan, discord.Thread):
            return chan.parent_id in excluded or chan.type is discord.ChannelType.private_thread
        return False

    def _channels_to_scan(
        self, guild: discord.Guild, only: Optional[discord.TextChannel], excluded: Collection[int]
    ) -> list:
        me = guild.me
        if only is not None:
            candidates = [only] + [t for t in guild.threads if t.parent_id == only.id]
        else:
            candidates = list(guild.text_channels) + list(guild.threads)  # threads = active ones
        return [
            c for c in candidates
            if not self._is_excluded(c, excluded)
            and c.permissions_for(me).read_message_history and c.permissions_for(me).view_channel
        ]

    async def _count(
        self,
        guild: discord.Guild,
        targets: list[Target],
        cutoff: datetime,
        only: Optional[discord.TextChannel] = None,
        progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> list[Tally]:
        """Read history once and tally every target emoji. One Tally per target, same order.

        `progress(done, total)` is called as channels finish. Stats about the scan
        (messages read, reaction lookups, time taken) end up in self.last_scan.
        """
        tallies = [Tally() for _ in targets]
        excluded = await self.config.guild(guild).excluded_channels()
        channels = self._channels_to_scan(guild, only, excluded)
        stats = {"channels": len(channels), "done": 0, "messages": 0, "lookups": 0}
        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def scan(chan) -> None:
            async with semaphore:
                try:
                    async for message in chan.history(limit=None, after=cutoff):
                        stats["messages"] += 1
                        if not message.reactions:
                            continue
                        owner = owner_id(message)
                        if owner is None:
                            continue
                        for tally, target in zip(tallies, targets):
                            reaction = next((r for r in message.reactions if self._matches(r, target)), None)
                            if reaction is None:
                                continue
                            # Ask Discord who reacted, so self-reactions and bots can be left out.
                            # (This lookup is the slow part of a scan.)
                            stats["lookups"] += 1
                            count = 0
                            async for user in reaction.users():
                                if not user.bot and user.id != owner:
                                    count += 1
                            if count:
                                tally.totals[owner] += count
                                tally.messages[owner] += 1
                                if count > tally.best_by_owner.get(owner, (0, ""))[0]:
                                    tally.best_by_owner[owner] = (count, message.jump_url)
                except discord.HTTPException as e:
                    log.warning("Couldn't read history in channel %s: %r", chan.id, e)
            stats["done"] += 1
            if progress:
                try:
                    await progress(stats["done"], stats["channels"])
                except Exception:
                    pass  # a progress update failing must never break the count

        started = time.monotonic()
        async with self._scan_lock:
            await asyncio.gather(*(scan(chan) for chan in channels))
        stats["seconds"] = time.monotonic() - started
        self.last_scan = stats
        log.info("Reaction scan in guild %s: %s", guild.id, stats)
        return tallies

    @staticmethod
    def _stats_text(stats: dict) -> str:
        minutes, seconds = divmod(int(stats["seconds"]), 60)
        took = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        return (f"Scanned {stats['messages']:,} messages in {stats['channels']} channels in {took} "
                f"({stats['lookups']:,} reaction lookups)")

    async def _progress_reporter(self, channel: discord.abc.Messageable):
        """A "⏳ Counting…" message that updates as channels finish (at most every 5 s).
        Returns (callback, cleanup)."""
        message = await channel.send("⏳ Counting reactions…")
        last_edit = [0.0]

        async def update(done: int, total: int) -> None:
            if time.monotonic() - last_edit[0] >= 5 and done < total:
                last_edit[0] = time.monotonic()
                await message.edit(content=f"⏳ Counting reactions… {done}/{total} channels done")

        async def cleanup() -> None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

        return update, cleanup

    @staticmethod
    def _ranking_lines(
        tally: Tally, target: Target, top: int, exclude: Collection[int] = (), staff: Collection[int] = ()
    ) -> list[str]:
        """Ranked lines. `exclude` drops members entirely; `staff` just marks them 🛡️.

        With target "total" (the overall award) it shows totals without message counts.
        """
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        rank, previous = 0, None
        for position, (member_id, count) in enumerate(tally.ranking(top, exclude), start=1):
            if count != previous:  # ties share a rank (and medal): 1, 1, 3
                rank, previous = position, count
            who = f"{medals.get(rank, f'{rank}.')} <@{member_id}>{' 🛡️' if member_id in staff else ''}"
            if target == "total":
                lines.append(f"{who} — **{count}** reactions")
            else:
                msgs = tally.messages[member_id]
                lines.append(f"{who} — **{count}** {target} ({msgs} message{'s' if msgs != 1 else ''})")
        return lines

    # ------------------------------------------------------------ on-demand leaderboard

    @commands.hybrid_command(name="reactking")
    @commands.guild_only()
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
        if channel is not None and self._is_excluded(channel, await self.config.guild(ctx.guild).excluded_channels()):
            # Deliberately vague: don't confirm anything about the channel.
            await ctx.send("I can't count that channel.")
            return
        if self._scan_lock.locked():
            await ctx.send("I'm already counting something. Try again in a moment.")
            return

        await ctx.defer()
        update, cleanup = await self._progress_reporter(ctx.channel)
        try:
            (tally,) = await self._count(ctx.guild, [target], datetime.now(timezone.utc) - delta, channel, update)
        finally:
            await cleanup()

        where = channel.mention if channel else "the whole server"
        if not tally.totals:
            await ctx.send(f"Nobody got any {target} in {where} ({period_text(delta)}).")
            return

        lines = self._ranking_lines(tally, target, TOP_N)
        best_count, best_url = tally.best()
        lines += ["", f"Most {target}'d message: **{best_count}** — {best_url}"]
        embed = discord.Embed(title=f"👑 {target} King", description="\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text=f"{'#' + channel.name if channel else 'Whole server'} · {period_text(delta)} · "
                              "self-reacts and bots don't count")
        # Mentions inside an embed never ping anyone.
        await ctx.send(embed=embed)

    # ------------------------------------------------------------ weekly awards

    async def _award_loop(self) -> None:
        """Checks once a minute whether any server's weekly award time has come."""
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild_id, conf in (await self.config.all_guilds()).items():
                    guild = self.bot.get_guild(guild_id)
                    if guild is None or not conf["enabled"] or not conf["awards"] or not conf["awards_channel"]:
                        continue
                    slot = self._current_slot(conf)
                    if slot is None:
                        continue
                    if conf["last_run"] and datetime.fromisoformat(conf["last_run"]) >= slot:
                        continue  # already posted for this week
                    # Mark first, so a crash mid-post can't cause a double post.
                    await self.config.guild(guild).last_run.set(slot.isoformat())
                    if datetime.now(timezone.utc) - slot > LATE_GRACE:
                        log.info("Skipping awards for %s in guild %s (bot was offline too long)", slot, guild_id)
                        continue
                    await self._post_awards(guild, conf, mode="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Weekly awards check failed")
            await asyncio.sleep(60)

    @staticmethod
    def _current_slot(conf: dict) -> Optional[datetime]:
        try:
            tz = ZoneInfo(conf["tz"])
        except (ZoneInfoNotFoundError, ValueError):
            return None
        hour, minute = (int(x) for x in conf["time"].split(":"))
        return last_scheduled(datetime.now(timezone.utc), conf["day"], hour, minute, tz)

    async def _post_awards(
        self,
        guild: discord.Guild,
        conf: dict,
        mode: str = "scheduled",
        here: Optional[discord.abc.Messageable] = None,
        progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """Count the past week and post the results. Returns an error message, or None.

        mode "scheduled": public card in the awards channel (winners pinged), full
                          results to the mod channel, roles moved.
        mode "preview":   both cards posted `here`; no roles, no pings.
        mode "testrun":   both cards posted `here`; roles moved for real, no pings.
        """
        give_roles = mode in ("scheduled", "testrun")
        if mode == "scheduled":
            channel = guild.get_channel(conf["awards_channel"]) if conf["awards_channel"] else None
            mod_channel = guild.get_channel(conf["mod_channel"]) if conf["mod_channel"] else None
        else:
            channel = mod_channel = here
        if channel is None:
            return "The awards channel is gone. Set it again with `!awards channel #channel`."

        awards = [(self._resolve_emoji(guild, a["emoji"]), a.get("role_id")) for a in conf["awards"]]
        awards = [(t, r) for t, r in awards if t is not None]
        tallies = await self._count(
            guild, [t for t, _ in awards], datetime.now(timezone.utc) - timedelta(days=7), progress=progress
        )

        # Admins and mods can't win (but their reactions still count for others).
        everyone = {member for tally in tallies for member in tally.totals}
        staff = {member for member in everyone if await self._is_staff(guild, member)}

        embed = discord.Embed(title="👑 Weekly Reaction Kings", color=discord.Color.gold())
        full = discord.Embed(title="🛡️ Weekly Reaction Kings: full results (staff included)", color=discord.Color.dark_grey())
        winners: set[int] = set()
        role_notes: list[str] = []
        for (target, role_id), tally in zip(awards, tallies):
            # Private card: everyone, staff marked.
            full_lines = self._ranking_lines(tally, target, 5, staff=staff) or ["Nobody this week."]
            full.add_field(name=f"{target}", value="\n".join(full_lines)[:1024], inline=False)

            # Public card: staff left out, so the next person moves up.
            ranking = tally.ranking(3, exclude=staff)
            kings: list[int] = []
            if ranking:
                lines = self._ranking_lines(tally, target, 3, exclude=staff)
                best_count, best_url = tally.best(exclude=staff)
                if best_count:
                    lines.append(f"-# most {target}'d message: {best_count} — {best_url}")
                embed.add_field(name=f"{target} King", value="\n".join(lines)[:1024], inline=False)
                # Everyone tied for first is king.
                top_count = ranking[0][1]
                kings = [member_id for member_id, count in ranking if count == top_count]
                winners.update(kings)
            else:
                embed.add_field(name=f"{target} King", value="Nobody this week.", inline=False)
            # Also runs with no kings: the role is "of the week", so last week's holder loses it.
            if give_roles and role_id:
                note = await self._move_role(guild, role_id, kings, target)
                if note:
                    role_notes.append(note)

        # Overall React King: all award emotes added together. Goes at the top.
        if conf.get("overall") and tallies:
            overall = Tally()
            for tally in tallies:
                for member, count in tally.totals.items():
                    overall.totals[member] += count
            full.insert_field_at(
                0, name="👑 Overall", inline=False,
                value="\n".join(self._ranking_lines(overall, "total", 5, staff=staff)) or "Nobody this week.",
            )
            ranking = overall.ranking(3, exclude=staff)
            kings = [m for m, c in ranking if c == ranking[0][1]] if ranking else []
            winners.update(kings)
            embed.insert_field_at(
                0, name="👑 React King", inline=False,
                value="\n".join(self._ranking_lines(overall, "total", 3, exclude=staff)) or "Nobody this week.",
            )
            if give_roles and conf.get("overall_role_id"):
                note = await self._move_role(guild, conf["overall_role_id"], kings, "Emoji")
                if note:
                    role_notes.append(note)

        embed.set_footer(text="Last 7 days · self-reacts and bots don't count · staff can't win")
        full.set_footer(text="Last 7 days · 🛡️ = admin/mod (can't win the public award)")
        if mode == "scheduled":
            content = "Congrats " + ", ".join(f"<@{w}>" for w in sorted(winners)) + "! 👑" if winners else None
            mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        elif mode == "testrun":
            content = ("-# Test run: roles were given/removed for real. Nobody pinged, nothing posted publicly.\n"
                       f"-# {self._stats_text(self.last_scan)}")
            mentions = discord.AllowedMentions.none()
        else:
            content = f"-# Preview: no roles given, nobody pinged.\n-# {self._stats_text(self.last_scan)}"
            mentions = discord.AllowedMentions.none()
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=mentions)
        except discord.HTTPException as e:
            log.warning("Couldn't post awards in guild %s: %r", guild.id, e)
            return "I couldn't post in the awards channel. Check my permissions there."

        # Full results go to the private mod channel (or, for a preview/test run, right here).
        if mod_channel is not None:
            try:
                await mod_channel.send(embed=full, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Couldn't post full award results in guild %s: %r", guild.id, e)
        for note in role_notes:
            log.warning(note)
        return None

    async def _is_staff(self, guild: discord.Guild, member_id: int) -> bool:
        """Admins/mods: Red's admin & mod roles (`[p]set roles`), Administrator, or the owner."""
        member = guild.get_member(member_id)
        if member is None:  # left the server
            return False
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return True
        return await self.bot.is_mod(member)

    async def _move_role(self, guild: discord.Guild, role_id: int, kings: list[int], target: Target) -> Optional[str]:
        """Take the award role from last week's holders and give it to this week's kings."""
        role = guild.get_role(role_id)
        me = guild.me
        if role is None:
            return f"Award role for {target} no longer exists (guild {guild.id})."
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            return f"Can't manage role {role.name} (guild {guild.id}): needs Manage Roles and my role above it."
        reason = f"Weekly {getattr(target, 'name', target)} King"
        for member in list(role.members):
            if member.id not in kings:
                try:
                    await member.remove_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
        for member_id in kings:
            member = guild.get_member(member_id)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
        return None

    # ------------------------------------------------------------ awards commands

    @commands.group(name="awards")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def awards(self, ctx: commands.Context):
        """Weekly reaction king awards."""

    @awards.command(name="channel")
    async def awards_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where the weekly awards get posted."""
        await self.config.guild(ctx.guild).awards_channel.set(channel.id)
        await ctx.send(f"Weekly awards will be posted in {channel.mention}.")

    @awards.command(name="exclude")
    async def awards_exclude(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        """Never read a channel (or its threads) for awards or `!reactking`."""
        async with self.config.guild(ctx.guild).excluded_channels() as excluded:
            if channel.id not in excluded:
                excluded.append(channel.id)
        # Delete the command so the channel's name isn't left sitting in chat.
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("Done. That channel will never be counted.", delete_after=10)

    @awards.command(name="unexclude")
    async def awards_unexclude(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        """Count a previously excluded channel again."""
        async with self.config.guild(ctx.guild).excluded_channels() as excluded:
            if channel.id in excluded:
                excluded.remove(channel.id)
        await ctx.send(f"{channel.mention} will be counted again.", allowed_mentions=discord.AllowedMentions.none())

    @awards.command(name="modchannel")
    async def awards_modchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Private channel for the full weekly results, with admins and mods included."""
        await self.config.guild(ctx.guild).mod_channel.set(channel.id)
        await ctx.send(f"Full results (staff included) will go to {channel.mention}.")

    @awards.command(name="add")
    async def awards_add(self, ctx: commands.Context, emoji: str, role: Optional[discord.Role] = None):
        """Add an emoji award, optionally with a role for the winner: `add :kek: @Kek King`."""
        target = self._resolve_emoji(ctx.guild, emoji)
        if target is None:
            await ctx.send(f"I don't know the emoji `{emoji}`.")
            return
        if role is not None and (role >= ctx.guild.me.top_role or role.managed):
            await ctx.send(f"I can't give out {role.mention}: my role must be above it in the role list.")
            return
        async with self.config.guild(ctx.guild).awards() as awards:
            awards[:] = [a for a in awards if a["emoji"] != str(target)]  # re-adding replaces
            if len(awards) >= MAX_AWARDS:
                await ctx.send(f"That's the maximum of {MAX_AWARDS} awards.")
                return
            awards.append({"emoji": str(target), "role_id": role.id if role else None})
        extra = f" The winner gets {role.mention}." if role else ""
        await ctx.send(f"Added the {target} King award.{extra}", allowed_mentions=discord.AllowedMentions.none())

    @awards.command(name="remove")
    async def awards_remove(self, ctx: commands.Context, emoji: str):
        """Remove an emoji award."""
        target = self._resolve_emoji(ctx.guild, emoji)
        key = str(target) if target else emoji
        async with self.config.guild(ctx.guild).awards() as awards:
            before = len(awards)
            awards[:] = [a for a in awards if a["emoji"] != key]
            removed = len(awards) < before
        await ctx.send(f"Removed the {key} award." if removed else f"There's no {key} award.")

    @awards.command(name="time")
    async def awards_time(self, ctx: commands.Context, day: str, time: str, tz: Optional[str] = None):
        """When to post, e.g. `time mon 12:00` or `time mon 12:00 Europe/Berlin`.

        Time zone names are like America/Chicago, America/New_York, Europe/Berlin.
        """
        day_key = day.lower()[:3]
        match = TIME_RE.match(time)
        if day_key not in DAYS or not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            await ctx.send("Use a day and a 24-hour time, like `mon 12:00`.")
            return
        conf = self.config.guild(ctx.guild)
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                await ctx.send(f"Unknown time zone `{tz}`. Use a name like `America/Chicago` or `Europe/Berlin`.")
                return
            await conf.tz.set(tz)
        await conf.day.set(DAYS.index(day_key))
        await conf.time.set(f"{int(match.group(1)):02d}:{match.group(2)}")
        # Don't fire immediately for a slot that already passed today.
        slot = self._current_slot(await conf.all())
        await conf.last_run.set(slot.isoformat() if slot else None)
        await ctx.send(f"Awards will post every **{day_key.title()} at {time}** ({await conf.tz()}).")

    @awards.command(name="toggle")
    async def awards_toggle(self, ctx: commands.Context):
        """Turn the weekly awards on or off."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        if enabled:
            # Start from the next slot, not one that passed before turning on.
            slot = self._current_slot(await conf.all())
            await conf.last_run.set(slot.isoformat() if slot else None)
        await conf.enabled.set(enabled)
        await ctx.send(f"Weekly awards are now **{'on' if enabled else 'off'}**.")

    @awards.command(name="list")
    async def awards_list(self, ctx: commands.Context):
        """Show the award settings."""
        conf = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(conf["awards_channel"]) if conf["awards_channel"] else None
        lines = [
            f"**Status:** {'on' if conf['enabled'] else 'off'}",
            f"**Channel:** {channel.mention if channel else 'not set'}",
            f"**Mod channel (full results):** "
            f"{ctx.guild.get_channel(conf['mod_channel']).mention if conf['mod_channel'] and ctx.guild.get_channel(conf['mod_channel']) else 'not set'}",
            f"**When:** {DAYS[conf['day']].title()} {conf['time']} ({conf['tz']})",
            # A count only, never names, in case this is run somewhere public.
            f"**Excluded channels:** {len(conf['excluded_channels'])} (plus all private threads)",
            "**Awards:**",
        ]
        for a in conf["awards"]:
            role = ctx.guild.get_role(a["role_id"]) if a.get("role_id") else None
            lines.append(f"{a['emoji']} King" + (f" → {role.mention}" if role else ""))
        if not conf["awards"]:
            lines.append("none yet, add one with `!awards add :kek:`")
        if conf["overall"]:
            role = ctx.guild.get_role(conf["overall_role_id"]) if conf["overall_role_id"] else None
            lines.append("👑 Overall React King" + (f" → {role.mention}" if role else ""))
        if any(a.get("role_id") for a in conf["awards"]) and not ctx.guild.me.guild_permissions.manage_roles:
            lines.append("⚠️ I need **Manage Roles** to hand out award roles.")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @awards.command(name="preview")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def awards_preview(self, ctx: commands.Context):
        """Post this week's awards here now, as a test (no roles, no pings).

        Shows both the public card and the full staff-included results, so run it
        somewhere private.
        """
        conf = await self.config.guild(ctx.guild).all()
        if not conf["awards"]:
            await ctx.send("Add an award first, e.g. `!awards add :kek:`.")
            return
        update, cleanup = await self._progress_reporter(ctx.channel)
        try:
            error = await self._post_awards(ctx.guild, conf, mode="preview", here=ctx.channel, progress=update)
        finally:
            await cleanup()
        if error:
            await ctx.send(error)

    @awards.command(name="testrun")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def awards_testrun(self, ctx: commands.Context):
        """Like preview, but gives and removes the award roles for real.

        Results are posted here only; nobody is pinged and nothing goes to the
        awards channel. Handy with placeholder roles before switching to the real ones.
        """
        conf = await self.config.guild(ctx.guild).all()
        if not conf["awards"]:
            await ctx.send("Add an award first, e.g. `!awards add :kek:`.")
            return
        update, cleanup = await self._progress_reporter(ctx.channel)
        try:
            error = await self._post_awards(ctx.guild, conf, mode="testrun", here=ctx.channel, progress=update)
        finally:
            await cleanup()
        if error:
            await ctx.send(error)

    @awards.command(name="overall")
    async def awards_overall(self, ctx: commands.Context, state: str, role: Optional[discord.Role] = None):
        """Overall React King (all award emotes combined): `overall on @role` or `overall off`."""
        conf = self.config.guild(ctx.guild)
        if state.lower() == "off":
            await conf.overall.set(False)
            await ctx.send("Overall React King is off.")
            return
        if state.lower() != "on":
            await ctx.send("Use `!awards overall on @role` (role optional) or `!awards overall off`.")
            return
        if role is not None and (role >= ctx.guild.me.top_role or role.managed):
            await ctx.send(f"I can't give out {role.mention}: my role must be above it in the role list.",
                           allowed_mentions=discord.AllowedMentions.none())
            return
        await conf.overall.set(True)
        await conf.overall_role_id.set(role.id if role else None)
        extra = f" The winner gets {role.mention}." if role else ""
        await ctx.send(f"Overall React King is on.{extra}", allowed_mentions=discord.AllowedMentions.none())
