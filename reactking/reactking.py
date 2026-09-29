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
# Weekly awards timeline: tally this long before the post time (the scan takes
# minutes on a busy server), post the card on time, then hand out roles when
# Statbot announces its top chatter, or after this long if it never does.
TALLY_LEAD = timedelta(minutes=30)
STATBOT_FALLBACK = timedelta(minutes=15)
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
            # Statbot's weekly "top chatter" announcement: where it's posted, and its role
            # (used as a fallback if the announcement is missed). Its winner outranks
            # every reaction award, so they can't also win one.
            statbot_channel=None,
            statbot_role=None,
            roles_run=None,  # ISO time of the award slot whose roles were handed out
            card=None,  # [channel id, message id] of this week's public card (to add role lines later)
            # Award priority below Statbot's top chatter: "reactking" and award emoji
            # strings, highest first. Anything missing is appended (React King first,
            # then awards in the order they were added).
            priority=[],
            # Custom card titles per award key (else the award role's name is used).
            names={},
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
        # guild id -> this week's in-progress award state (see _award_step)
        self._weekly: dict[int, dict] = {}
        # The timer and the Statbot listener can both advance a server's awards;
        # this makes sure only one does at a time (so the card is never posted twice).
        self._step_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
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
        """Checks every 20 seconds how each server's weekly awards are coming along."""
        await self.bot.wait_until_red_ready()
        while True:
            try:
                for guild_id, conf in (await self.config.all_guilds()).items():
                    guild = self.bot.get_guild(guild_id)
                    if guild is None or not conf["enabled"] or not conf["awards"] or not conf["awards_channel"]:
                        continue
                    await self._award_step(guild, conf)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Weekly awards check failed")
            await asyncio.sleep(20)

    @staticmethod
    def _slot_for(conf: dict, now: datetime) -> Optional[datetime]:
        """The award time this moment belongs to: from TALLY_LEAD before it, onwards."""
        try:
            tz = ZoneInfo(conf["tz"])
        except (ZoneInfoNotFoundError, ValueError):
            return None
        hour, minute = (int(x) for x in conf["time"].split(":"))
        return last_scheduled(now + TALLY_LEAD, conf["day"], hour, minute, tz)

    @staticmethod
    def _current_slot(conf: dict) -> Optional[datetime]:
        return ReactKing._slot_for(conf, datetime.now(timezone.utc) - TALLY_LEAD)

    async def _award_step(self, guild: discord.Guild, conf: dict) -> None:
        async with self._step_locks[guild.id]:
            # Re-read settings inside the lock: the other caller may have just moved things on.
            await self._award_step_locked(guild, await self.config.guild(guild).all())

    async def _award_step_locked(self, guild: discord.Guild, conf: dict) -> None:
        """One tick of the weekly timeline:
        1. from 30 min before: tally in the background (the slow part);
        2. at the award time: post the card (full results, no roles yet);
        3. when Statbot announces its top chatter (or 15 min later): hand out
           roles down the priority chain and add them to the card.
        Progress is saved in config, so a restart never double-posts or skips roles.
        """
        now = datetime.now(timezone.utc)
        slot = self._slot_for(conf, now)
        if slot is None:
            return
        posted = conf["last_run"] and datetime.fromisoformat(conf["last_run"]) >= slot
        roles_done = conf["roles_run"] and datetime.fromisoformat(conf["roles_run"]) >= slot
        if posted and roles_done:
            return
        conf_group = self.config.guild(guild)
        if now - slot > LATE_GRACE:  # bot was offline far too long: skip this week
            log.info("Skipping awards for %s in guild %s (bot was offline too long)", slot, guild.id)
            await conf_group.last_run.set(slot.isoformat())
            await conf_group.roles_run.set(slot.isoformat())
            return

        state = self._weekly.get(guild.id)
        if state is None or state["slot"] != slot:
            state = self._weekly[guild.id] = {"slot": slot, "task": None, "results": None, "chat_king": None}

        # 1. Tally (starts TALLY_LEAD early; also after a restart if needed).
        if state["results"] is None:
            if state["task"] is None:
                state["task"] = asyncio.create_task(self._compute(guild, conf))
            if not state["task"].done():
                return
            try:
                state["results"] = state["task"].result()
            except Exception:
                log.exception("Award tally failed in guild %s", guild.id)
                state["task"] = None  # try again next tick
                return

        # 2. Post the card at the award time.
        if not posted:
            if now < slot:
                return
            await conf_group.last_run.set(slot.isoformat())  # mark first: never double-post
            card = await self._publish_card(guild, conf, state["results"])
            await conf_group.card.set([card.channel.id, card.id] if card else None)

        # 3. Roles: as soon as Statbot has crowned its top chatter, or at the fallback time.
        if not roles_done:
            if conf["statbot_channel"] and state["chat_king"] is None and now < slot + STATBOT_FALLBACK:
                return
            await conf_group.roles_run.set(slot.isoformat())
            chat_king = state["chat_king"]
            if chat_king is None:  # no announcement seen: use whoever holds Statbot's role
                chat_king = self._role_holders(guild, conf["statbot_role"])
            plan = await self._plan_roles(guild, conf, state["results"], chat_king)
            notes = await self._apply_roles(guild, plan)
            await self._finish_card(guild, conf, state["results"], plan)
            for note in notes:
                log.warning(note)

    @staticmethod
    def _role_holders(guild: discord.Guild, role_id: Optional[int]) -> set[int]:
        role = guild.get_role(role_id) if role_id else None
        return {m.id for m in role.members} if role else set()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Statbot's "Hear ye, hear ye! … Bow down to @winner" announcement: note the
        winner, and hand out this week's reaction roles right away."""
        if message.guild is None or not message.author.bot or not message.mentions:
            return
        if "hear ye" not in message.content.lower():
            return
        conf = await self.config.guild(message.guild).all()
        if message.channel.id != conf["statbot_channel"]:
            return
        state = self._weekly.get(message.guild.id)
        now = datetime.now(timezone.utc)
        slot = self._slot_for(conf, now)
        if slot is None or not (slot - TALLY_LEAD <= now <= slot + LATE_GRACE):
            return  # not around award time
        if state is None or state["slot"] != slot:
            state = self._weekly[message.guild.id] = {"slot": slot, "task": None, "results": None, "chat_king": None}
        # The first mention is the new top chatter ("Bow down to @Grem"); a second
        # message mentions last week's, which we ignore.
        state["chat_king"] = {message.mentions[0].id}
        log.info("Statbot crowned %s; handing out reaction roles", message.mentions[0].id)
        if conf["enabled"] and conf["awards"] and conf["awards_channel"]:
            await self._award_step(message.guild, conf)

    # ---- the parts: tally -> plan roles -> cards -> apply

    async def _compute(
        self, guild: discord.Guild, conf: dict, progress: Optional[Callable[[int, int], Awaitable[None]]] = None
    ) -> dict:
        """Tally the past 7 days for every award (the slow part)."""
        awards = [(self._resolve_emoji(guild, a["emoji"]), a.get("role_id"), a["emoji"]) for a in conf["awards"]]
        awards = [(t, r, k) for t, r, k in awards if t is not None]
        tallies = await self._count(
            guild, [t for t, _, _ in awards], datetime.now(timezone.utc) - timedelta(days=7), progress=progress
        )
        overall = None
        if conf.get("overall") and tallies:
            overall = Tally()
            for tally in tallies:
                for member, count in tally.totals.items():
                    overall.totals[member] += count
        # Admins and mods can't win (but their reactions still count for others).
        everyone = {member for tally in tallies for member in tally.totals}
        staff = {member for member in everyone if await self._is_staff(guild, member)}
        return {"awards": awards, "tallies": tallies, "overall": overall, "staff": staff, "stats": dict(self.last_scan)}

    @staticmethod
    def _priority_keys(conf: dict) -> list[str]:
        """Award keys, highest priority first: "reactking" and each award's emoji."""
        valid = ["reactking"] + [a["emoji"] for a in conf["awards"]]
        saved = [k for k in conf.get("priority", []) if k in valid]
        return saved + [k for k in valid if k not in saved]

    @staticmethod
    def _award_title(guild: discord.Guild, conf: dict, key: str, role_id: Optional[int]) -> str:
        """An award's display name: a custom name (`awards name`), else its role's
        name ("Kekkest"), else "<emoji> King". Emoji awards show their emoji first."""
        role = guild.get_role(role_id) if role_id else None
        name = conf.get("names", {}).get(key) or (role.name if role else None)
        if key == "reactking":
            name = name or "React King"
            return name if "👑" in name else f"👑 {name}"
        return f"{key} {name}" if name else f"{key} King"

    def _entries(self, guild: discord.Guild, conf: dict, results: dict) -> list[dict]:
        """This week's awards in priority order, each with its title, tally and role."""
        by_key = {}
        if results["overall"] is not None:
            role_id = conf.get("overall_role_id")
            by_key["reactking"] = {"label": self._award_title(guild, conf, "reactking", role_id),
                                   "full_label": "👑 Overall", "target": "total",
                                   "tally": results["overall"], "role_id": role_id}
        for (target, role_id, key), tally in zip(results["awards"], results["tallies"]):
            by_key[key] = {"label": self._award_title(guild, conf, key, role_id), "full_label": f"{target}",
                           "target": target, "tally": tally, "role_id": role_id}
        return [by_key[k] for k in self._priority_keys(conf) if k in by_key]

    async def _plan_roles(self, guild: discord.Guild, conf: dict, results: dict, chat_king: set[int]) -> list[dict]:
        """Who gets which role, one award per person, in priority order:
        Statbot's top chatter (already has theirs) -> React King (if enabled) -> the
        emoji awards in the order they were added. Each role goes to the highest-
        ranked person on that leaderboard who isn't staff and doesn't hold a higher
        award this week. Ties for first share the role."""
        chat_role = guild.get_role(conf["statbot_role"]) if conf["statbot_role"] else None
        taken: dict[int, str] = {m: (chat_role.name if chat_role else "top chatter") for m in chat_king}
        plan = []
        for entry in self._entries(guild, conf, results):
            label, target, tally, role_id = entry["label"], entry["target"], entry["tally"], entry["role_id"]
            role = guild.get_role(role_id) if role_id else None
            ranking = tally.ranking(len(tally.totals), exclude=results["staff"])
            eligible = [(m, c) for m, c in ranking if m not in taken]
            kings = [m for m, c in eligible if c == eligible[0][1]] if eligible else []
            # People ranked above the winner who were passed over, and why.
            first_count = eligible[0][1] if eligible else 0
            skipped = [(m, taken[m]) for m, c in ranking if m in taken and c >= first_count][:3]
            award_name = role.name if role else label
            for m in kings:
                taken[m] = award_name
            plan.append({"label": label, "target": target, "role_id": role_id, "role_name": role.name if role else None,
                         "kings": kings, "skipped": skipped})
        return plan

    async def _apply_roles(self, guild: discord.Guild, plan: list[dict]) -> list[str]:
        notes = []
        for item in plan:
            if item["role_id"]:  # also runs with no kings: last week's holder loses it
                note = await self._move_role(guild, item["role_id"], item["kings"], item["target"])
                if note:
                    notes.append(note)
        return notes

    def _cards(
        self, guild: discord.Guild, conf: dict, results: dict, plan: Optional[list[dict]] = None
    ) -> tuple[discord.Embed, discord.Embed]:
        """The public card (staff left out, optional role lines) and the full mod card."""
        staff = results["staff"]
        embed = discord.Embed(title="👑 Weekly Reaction Kings", color=discord.Color.gold())
        full = discord.Embed(title="🛡️ Weekly Reaction Kings: full results (staff included)", color=discord.Color.dark_grey())
        planned = {item["label"]: item for item in plan or []}
        for entry in self._entries(guild, conf, results):  # priority order, highest first
            label, full_label, target, tally = entry["label"], entry["full_label"], entry["target"], entry["tally"]
            full_lines = self._ranking_lines(tally, target, 5, staff=staff) or ["Nobody this week."]
            full.add_field(name=full_label, value="\n".join(full_lines)[:1024], inline=False)

            lines = self._ranking_lines(tally, target, 3, exclude=staff) or ["Nobody this week."]
            if target != "total":
                best_count, best_url = tally.best(exclude=staff)
                if best_count:
                    lines.append(f"-# most {target}'d message: {best_count} — {best_url}")
            item = planned.get(label)
            if item and item["role_name"]:
                if item["kings"]:
                    line = f"-# 🏅 {item['role_name']} → " + ", ".join(f"<@{k}>" for k in item["kings"])
                else:
                    line = f"-# 🏅 {item['role_name']} → nobody this week"
                if item["skipped"]:
                    line += " (" + "; ".join(f"<@{m}> already has {why}" for m, why in item["skipped"]) + ")"
                lines.append(line)
            embed.add_field(name=label, value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Last 7 days · self-reacts and bots don't count · staff can't win · one award per person")
        full.set_footer(text="Last 7 days · 🛡️ = admin/mod (can't win the public award)")
        return embed, full

    async def _publish_card(self, guild: discord.Guild, conf: dict, results: dict) -> Optional[discord.Message]:
        """Post the public card (no roles yet) and the full mod card."""
        channel = guild.get_channel(conf["awards_channel"])
        mod_channel = guild.get_channel(conf["mod_channel"]) if conf["mod_channel"] else None
        embed, full = self._cards(guild, conf, results)
        card = None
        if channel is not None:
            try:
                card = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Couldn't post awards in guild %s: %r", guild.id, e)
        if mod_channel is not None:
            try:
                await mod_channel.send(embed=full, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Couldn't post full award results in guild %s: %r", guild.id, e)
        return card

    async def _finish_card(self, guild: discord.Guild, conf: dict, results: dict, plan: list[dict]) -> None:
        """Add the role lines to this week's card and congratulate the winners."""
        embed, _ = self._cards(guild, conf, results, plan)
        winners = sorted({k for item in plan for k in item["kings"]})
        card_ref = await self.config.guild(guild).card()
        card = None
        if card_ref:
            channel = guild.get_channel(card_ref[0])
            if channel is not None:
                try:
                    card = await channel.fetch_message(card_ref[1])
                    await card.edit(embed=embed)
                except discord.HTTPException as e:
                    log.warning("Couldn't update the awards card in guild %s: %r", guild.id, e)
                    card = None
        if not winners:
            return
        text = "Congrats " + ", ".join(f"<@{w}>" for w in winners) + "! 👑"
        mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        try:
            if card is not None:
                await card.reply(text, allowed_mentions=mentions, mention_author=False)
            else:
                channel = guild.get_channel(conf["awards_channel"])
                if channel is not None:
                    await channel.send(embed=embed, content=text, allowed_mentions=mentions)
        except discord.HTTPException as e:
            log.warning("Couldn't congratulate award winners in guild %s: %r", guild.id, e)

    async def _post_awards(
        self,
        guild: discord.Guild,
        conf: dict,
        mode: str = "preview",
        here: Optional[discord.abc.Messageable] = None,
        progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """Preview / test run: tally now and post both cards `here`.

        The roles are planned as if Statbot's current role holder is this week's
        top chatter. "preview" gives no roles; "testrun" moves them for real.
        Nobody is pinged either way.
        """
        results = await self._compute(guild, conf, progress=progress)
        chat_king = self._role_holders(guild, conf["statbot_role"])
        plan = await self._plan_roles(guild, conf, results, chat_king)
        notes = await self._apply_roles(guild, plan) if mode == "testrun" else []
        embed, full = self._cards(guild, conf, results, plan)
        if mode == "testrun":
            content = "-# Test run: roles were given/removed for real. Nobody pinged, nothing posted publicly."
        else:
            content = "-# Preview: no roles given, nobody pinged. Role lines show what *would* happen."
        content += f"\n-# {self._stats_text(results['stats'])}"
        try:
            await here.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await here.send(embed=full, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Couldn't post award preview in guild %s: %r", guild.id, e)
            return "I couldn't post here."
        for note in notes:
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

    @awards.command(name="priority")
    async def awards_priority(self, ctx: commands.Context, award: Optional[str] = None, position: Optional[int] = None):
        """Show or change which award wins when someone tops several.

        `priority` shows the order. `priority :doubt: 1` moves an award to #1;
        use `reactking` for React King. Statbot's top chatter is always above all of
        them, because Refbot can't take away Statbot's role.
        """
        conf_group = self.config.guild(ctx.guild)
        conf = await conf_group.all()
        keys = self._priority_keys(conf)

        if award is not None:
            key = "reactking" if award.lower() in ("reactking", "react_king", "overall") else None
            if key is None:
                target = self._resolve_emoji(ctx.guild, award)
                key = str(target) if target else award
            if key not in keys:
                await ctx.send(f"`{award}` isn't one of the awards. See `{ctx.clean_prefix}awards priority`.")
                return
            if position is None or not 1 <= position <= len(keys):
                await ctx.send(f"Give a position from 1 to {len(keys)}, e.g. `{ctx.clean_prefix}awards priority {award} 1`.")
                return
            keys.remove(key)
            keys.insert(position - 1, key)
            await conf_group.priority.set(keys)
            conf["priority"] = keys

        roles = {a["emoji"]: a.get("role_id") for a in conf["awards"]}
        roles["reactking"] = conf.get("overall_role_id")
        chat_role = ctx.guild.get_role(conf["statbot_role"]) if conf["statbot_role"] else None
        lines = ["**Award priority** (someone who tops several gets the highest one):",
                 f"**0.** Statbot's top chatter" + (f" · {chat_role.mention}" if chat_role else "") + " · always first"]
        for n, key in enumerate(self._priority_keys(conf), start=1):
            name = self._award_title(ctx.guild, conf, key, roles.get(key))
            if key == "reactking" and not conf.get("overall"):
                name += " (off)"
            role = ctx.guild.get_role(roles.get(key)) if roles.get(key) else None
            lines.append(f"**{n}.** {name}" + (f" · {role.mention}" if role else ""))
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @awards.command(name="name")
    async def awards_name(self, ctx: commands.Context, award: str, *, name: str):
        """Set an award's title on the card: `name :kek: The Kekkest` (`reset` to use the role's name).

        Use `reactking` for React King. By default each award is titled with its role's name.
        """
        conf_group = self.config.guild(ctx.guild)
        conf = await conf_group.all()
        key = "reactking" if award.lower() in ("reactking", "react_king", "overall") else None
        if key is None:
            target = self._resolve_emoji(ctx.guild, award)
            key = str(target) if target else award
        if key not in self._priority_keys(conf):
            await ctx.send(f"`{award}` isn't one of the awards. See `{ctx.clean_prefix}awards priority`.")
            return
        async with conf_group.names() as names:
            if name.strip().lower() == "reset":
                names.pop(key, None)
            else:
                names[key] = name.strip()[:60]
        conf = await conf_group.all()
        role_id = conf.get("overall_role_id") if key == "reactking" else next(
            (a.get("role_id") for a in conf["awards"] if a["emoji"] == key), None)
        await ctx.send(f"Card title: **{self._award_title(ctx.guild, conf, key, role_id)}**",
                       allowed_mentions=discord.AllowedMentions.none())

    @awards.command(name="statbot")
    async def awards_statbot(self, ctx: commands.Context, channel: str, role: Optional[discord.Role] = None):
        """Link Statbot's weekly top-chatter award: `statbot #free-talk @RoleName` (or `statbot off`).

        When Statbot posts its "Hear ye, hear ye! … Bow down to @winner" message in
        that channel, the reaction roles are handed out right away, and that winner
        can't also win a reaction award. The role is the fallback: if no
        announcement arrives within 15 minutes, whoever holds it counts as the winner.
        """
        conf = self.config.guild(ctx.guild)
        if channel.lower() == "off":
            await conf.statbot_channel.set(None)
            await conf.statbot_role.set(None)
            await ctx.send("Statbot link removed. Roles will be handed out right when the card posts.")
            return
        try:
            text_channel = await commands.TextChannelConverter().convert(ctx, channel)
        except commands.BadArgument:
            await ctx.send("Use a channel, like `!awards statbot #free-talk @RoleName`.")
            return
        await conf.statbot_channel.set(text_channel.id)
        await conf.statbot_role.set(role.id if role else None)
        extra = f" Fallback role: {role.mention}." if role else " (No fallback role set.)"
        await ctx.send(
            f"Watching {text_channel.mention} for Statbot's announcement.{extra}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        await conf.roles_run.set(slot.isoformat() if slot else None)
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
            await conf.roles_run.set(slot.isoformat() if slot else None)
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
            f"**When:** {DAYS[conf['day']].title()} {conf['time']} ({conf['tz']}) · tally starts "
            f"{int(TALLY_LEAD.total_seconds() // 60)} min early",
            f"**Statbot:** "
            + (f"<#{conf['statbot_channel']}>, fallback role "
               f"{ctx.guild.get_role(conf['statbot_role']).mention if conf['statbot_role'] and ctx.guild.get_role(conf['statbot_role']) else 'none'}"
               if conf["statbot_channel"] else "not linked"),
            # A count only, never names, in case this is run somewhere public.
            f"**Excluded channels:** {len(conf['excluded_channels'])} (plus all private threads)",
            "**Awards:**",
        ]
        for a in conf["awards"]:
            role = ctx.guild.get_role(a["role_id"]) if a.get("role_id") else None
            lines.append(f"{a['emoji']} King" + (f" → {role.mention}" if role else ""))
        if not conf["awards"]:
            lines.append("none yet, add one with `!awards add :kek:`")
        lines.append(f"-# One award per person; see the priority order with `{ctx.clean_prefix}awards priority`")
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
