"""Game channels that open and close themselves.

A fixed set of channels lives in a parking category ("gameday placeholder").
When a game is coming up, the bot moves the right channel into the live
category ("game threads"), renames it for the game ("min-at-chi") and syncs
it to that category's permissions. After the game it renames it back, moves it
back to parking and syncs again. The same channels are reused every week, so
their history stays for people to look back on.

Which channels open when is decided in schedule.py. A background task checks
ESPN every few minutes (every minute around game time).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import schedule

log = logging.getLogger("red.refbot.gameday")

# The kinds of game channel. Admins point each at one of their existing channels
# with `gameday use` ("primetime" can have several, for Monday doubleheaders).
SLOTS = [("vikings", "vikings-game"), ("delayed", "vikings-delayed"), ("redzone", "redzone"),
         ("primetime", "primetime-1"), ("primetime", "primetime-2")]
LIVE_CATEGORY = "game threads"
PARK_CATEGORY = "gameday placeholder"
EDIT_TIMEOUT = 60  # seconds; Discord allows 2 renames per channel per 10 minutes


def pretty(perm: str, channel: bool = False) -> str:
    """A permission's name as Discord's settings show it. (Manage Roles is called
    Manage Permissions in a channel's or category's settings.)"""
    if channel and perm == "manage_roles":
        return "Manage Permissions"
    return perm.replace("_", " ").title()


class Gameday(commands.Cog):
    """Game channels that open and close themselves."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2DA, force_registration=True)
        self.config.register_guild(
            enabled=False,
            team="MIN",
            live_category=None,
            park_category=None,
            # [[channel id, slot, parked name], ...] (a list: Red merges dict defaults)
            channels=[],
            assigned={},  # channel id (str) -> key of what it's open for (see schedule.Want)
            finals={},  # ESPN game id -> when we first saw it final (ISO time)
            before_hours=2,
            after_hours=1,
            delayed_after_hours=6,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._games: list[schedule.Game] = []
        self.interval = 300
        self.last_error: dict[int, str] = {}

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Stores no user data.
        return

    # ------------------------------------------------------------ loop

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            self._wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Gameday loop failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        guilds = {gid: c for gid, c in (await self.config.all_guilds()).items() if c["enabled"]}
        if not guilds:
            self.interval = 3600
            return
        try:
            async with self._session.get(schedule.ESPN_URL) as resp:
                resp.raise_for_status()
                self._games = schedule.parse_games(await resp.json(content_type=None))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("ESPN fetch failed, keeping the last schedule: %r", e)
        now = datetime.now(timezone.utc)
        for gid in guilds:
            guild = self.bot.get_guild(gid)
            if guild is not None:
                async with self._lock:
                    await self._update(guild, now)
        # Every minute when something opens or closes soon, otherwise every 10.
        soon = any(abs((g.kickoff - now).total_seconds()) < 4 * 3600 or g.state == "in" for g in self._games)
        self.interval = 60 if soon else 600

    def _wants(self, conf: dict) -> list[schedule.Want]:
        return schedule.plan(self._games, conf["finals"], conf["team"], timedelta(hours=conf["before_hours"]),
                             timedelta(hours=conf["after_hours"]), timedelta(hours=conf["delayed_after_hours"]))

    async def _update(self, guild: discord.Guild, now: datetime) -> None:
        conf_group = self.config.guild(guild)
        conf = await conf_group.all()
        # Remember when each game went final (ESPN doesn't say), for closing times.
        finals = dict(conf["finals"])
        for game in self._games:
            if game.state == "post" and game.id not in finals:
                finals[game.id] = now.isoformat()
        if finals != conf["finals"]:
            current = {g.id for g in self._games}
            await conf_group.finals.set({k: v for k, v in finals.items() if k in current})
            conf["finals"] = finals

        live = guild.get_channel(conf["live_category"] or 0)
        park = guild.get_channel(conf["park_category"] or 0)
        if not isinstance(live, discord.CategoryChannel) or not isinstance(park, discord.CategoryChannel):
            self.last_error[guild.id] = "Categories not set. Run `gameday setup`."
            return

        self.last_error.pop(guild.id, None)  # categories are fine now
        targets = self._targets(conf, schedule.open_now(self._wants(conf), now))
        assigned = dict(conf["assigned"])
        channels = [list(c) for c in conf["channels"]]
        for entry in channels:
            channel_id, slot, parked_name = entry
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            target = targets.get(channel_id)
            current = assigned.get(str(channel_id))
            if target and target.key != current:
                if current and channel.category_id == live.id:
                    # Already open, now for the next window (RedZone): keep whatever
                    # name it has, including one the mods gave it.
                    assigned[str(channel_id)] = target.key
                    continue
                # Remember its name as it is now, so renaming a parked channel sticks.
                if channel.category_id != live.id and channel.name != parked_name:
                    entry[2] = parked_name = channel.name
                if await self._edit(channel, live, target.name):
                    assigned[str(channel_id)] = target.key
                    log.info("Opened #%s in guild %s", target.name, guild.id)
            elif not target and current:
                # Back to its parked name, whatever it was renamed to during the game.
                if await self._edit(channel, park, parked_name):
                    assigned.pop(str(channel_id), None)
                    log.info("Parked #%s in guild %s", parked_name, guild.id)
        if assigned != conf["assigned"]:
            await conf_group.assigned.set(assigned)
        if channels != conf["channels"]:
            await conf_group.channels.set(channels)

    def _targets(self, conf: dict, wants: list[schedule.Want]) -> dict[int, schedule.Want]:
        """Which channel each open Want goes in. Primetime games share a pool of
        channels; a game keeps the channel it already has."""
        by_slot: dict[str, list[int]] = {}
        for channel_id, slot, _ in conf["channels"]:
            by_slot.setdefault(slot, []).append(channel_id)
        targets: dict[int, schedule.Want] = {}
        waiting = []
        for want in wants:
            pool = by_slot.get(want.slot, [])
            mine = next((c for c in pool if conf["assigned"].get(str(c)) == want.key), None)
            if mine is not None and mine not in targets:
                targets[mine] = want
            else:
                waiting.append(want)
        for want in waiting:
            free = next((c for c in by_slot.get(want.slot, []) if c not in targets), None)
            if free is not None:
                targets[free] = want
        return targets

    async def _edit(self, channel: discord.TextChannel, category: discord.CategoryChannel, name: str) -> bool:
        """Move, rename and sync permissions in one edit."""
        try:
            await asyncio.wait_for(
                channel.edit(category=category, name=name, sync_permissions=True,
                             reason="Game channel schedule"),
                timeout=EDIT_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            # Discord's rename limit: try again next check rather than block.
            log.info("Discord is rate limiting edits to #%s; will retry", channel)
        except discord.HTTPException as e:
            self.last_error[channel.guild.id] = f"Couldn't move #{channel}: {e.text or e.status}"
            log.warning("Couldn't move #%s: %r", channel, e)
        return False

    # ------------------------------------------------------------ commands

    @commands.group(name="gameday")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def gameday(self, ctx: commands.Context):
        """Game channels that open and close themselves."""

    @gameday.command(name="setup")
    async def gameday_setup(self, ctx: commands.Context):
        """Find the "game threads" (live) and "gameday placeholder" (parking) categories.

        Then tell the bot which of your existing channels to use with `gameday use`.
        """
        guild = ctx.guild
        cats = {c.name.casefold(): c for c in guild.categories}
        live, park = cats.get(LIVE_CATEGORY), cats.get(PARK_CATEGORY)
        if live is None or park is None:
            await ctx.send(f"I need categories named **{LIVE_CATEGORY}** and **{PARK_CATEGORY}**.")
            return
        for cat in (live, park):
            perms = cat.permissions_for(guild.me)
            if not (perms.manage_channels and perms.manage_roles):
                await ctx.send(f"I need **Manage Channels** and **Manage Permissions** in **{cat.name}**.")
                return
        conf = self.config.guild(guild)
        await conf.live_category.set(live.id)
        await conf.park_category.set(park.id)
        self.last_error.pop(guild.id, None)
        self._wake.set()
        p = ctx.clean_prefix
        slots = {slot for _, slot, _ in await conf.channels()}
        if {"vikings", "delayed", "redzone", "primetime"} <= slots:
            state = "It's on." if await conf.enabled() else f"Turn it on with `{p}gameday toggle`."
            await ctx.send(f"Live: **{live.name}** · Parking: **{park.name}**\n"
                           f"All the game channels are set. {state} `{p}gameday show` has this week's schedule.")
            return
        await ctx.send(
            f"Live: **{live.name}** · Parking: **{park.name}**\n"
            f"Now tell me which channels to use (each is renamed back to its current name when parked):\n"
            f"`{p}gameday use vikings #channel` (the Vikings game)\n"
            f"`{p}gameday use delayed #channel` (the delayed Vikings channel)\n"
            f"`{p}gameday use redzone #channel`\n"
            f"`{p}gameday use primetime #channel` (run it twice for two, for Monday doubleheaders)\n"
            f"Then check `{p}gameday show` and turn it on with `{p}gameday toggle`."
        )

    @gameday.command(name="use")
    async def gameday_use(self, ctx: commands.Context, slot: str, channel: discord.TextChannel):
        """Use an existing channel for a slot: vikings, delayed, redzone or primetime.

        When parked, the channel goes back to the name it has now.
        """
        slot = slot.lower()
        if slot not in {s for s, _ in SLOTS}:
            await ctx.send("The slot must be `vikings`, `delayed`, `redzone` or `primetime`.")
            return
        conf = self.config.guild(ctx.guild)
        async with self._lock:
            channels = [c for c in await conf.channels() if c[0] != channel.id]
            if slot != "primetime":
                # One channel per slot (primetime can have several): replace the old one.
                channels = [c for c in channels if c[1] != slot]
            channels.append([channel.id, slot, channel.name])
            await conf.channels.set(channels)
        count = sum(1 for c in channels if c[1] == slot)
        extra = f" ({count} primetime channel{'s' if count != 1 else ''} now)" if slot == "primetime" else ""
        await ctx.send(f"{channel.mention} is the **{slot}** channel{extra}. When parked it's called `{channel.name}`.")

    @gameday.command(name="unuse")
    async def gameday_unuse(self, ctx: commands.Context, channel: discord.TextChannel):
        """Stop using a channel for game days (it stays where it is)."""
        conf = self.config.guild(ctx.guild)
        async with self._lock:
            channels = await conf.channels()
            kept = [c for c in channels if c[0] != channel.id]
            await conf.channels.set(kept)
            async with conf.assigned() as assigned:
                assigned.pop(str(channel.id), None)
        await ctx.send(f"Stopped using {channel.mention}." if len(kept) < len(channels)
                       else f"{channel.mention} wasn't a game channel.")

    @gameday.command(name="toggle")
    async def gameday_toggle(self, ctx: commands.Context):
        """Turn the automatic game channels on or off."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        await conf.enabled.set(enabled)
        self._wake.set()
        await ctx.send("Game channels are on." if enabled
                       else f"Game channels are off. Open ones stay where they are; `{ctx.clean_prefix}gameday park` puts them back.")

    @gameday.command(name="times")
    async def gameday_times(self, ctx: commands.Context, before: int, after: int, delayed_after: Optional[int] = None):
        """Hours to open before kickoff, close after the final, and keep the delayed channel open after the final.

        Example: `[p]gameday times 2 1 6`.
        """
        if not (0 <= before <= 24 and 0 <= after <= 24 and (delayed_after is None or 0 <= delayed_after <= 48)):
            await ctx.send("Use hours from 0 to 24 (delayed: up to 48).")
            return
        conf = self.config.guild(ctx.guild)
        await conf.before_hours.set(before)
        await conf.after_hours.set(after)
        if delayed_after is not None:
            await conf.delayed_after_hours.set(delayed_after)
        self._wake.set()
        delayed = delayed_after if delayed_after is not None else await conf.delayed_after_hours()
        await ctx.send(f"Channels open {before}h before kickoff and close {after}h after the final "
                       f"(the delayed channel {delayed}h after).")

    @gameday.command(name="show")
    async def gameday_show(self, ctx: commands.Context):
        """Settings and this week's schedule of channels."""
        conf = await self.config.guild(ctx.guild).all()
        guild = ctx.guild
        live = guild.get_channel(conf["live_category"] or 0)
        park = guild.get_channel(conf["park_category"] or 0)
        lines = [
            f"Running: {'yes' if conf['enabled'] else 'no'} · Team: {conf['team']}",
            f"Live: {live.name if live else 'not set'} · Parking: {park.name if park else 'not set'}",
            f"Open {conf['before_hours']}h before kickoff, close {conf['after_hours']}h after the final "
            f"(delayed: {conf['delayed_after_hours']}h)",
            "Channels: " + (", ".join(f"<#{cid}> ({slot})" for cid, slot, _ in conf["channels"]) or "none, run setup"),
        ]
        if self._games:
            now = datetime.now(timezone.utc)
            open_keys = {w.key for w in schedule.open_now(self._wants(conf), now)}
            lines.append("\n**This week**")
            for want in self._wants(conf):
                local = want.opens.astimezone(schedule.CENTRAL)
                hour = local.hour % 12 or 12
                when = f"{local:%a} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
                state = " · **open now**" if want.key in open_keys else ""
                lines.append(f"{when} opens #{want.name}{state}")
        if guild.id in self.last_error:
            lines.append(f"\nLast problem: {self.last_error[guild.id]}")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @gameday.command(name="test")
    async def gameday_test(self, ctx: commands.Context, channel: discord.TextChannel, seconds: int = 60):
        """Dry run: open a game channel now, then park it again after `seconds` (default 60).

        Does exactly what a real game does (move to the live category, rename, sync
        permissions, then back) and reports each step. Discord allows 2 renames per
        channel per 10 minutes, so test each channel at most once per 10 minutes.
        """
        guild = ctx.guild
        conf = await self.config.guild(guild).all()
        live = guild.get_channel(conf["live_category"] or 0)
        park = guild.get_channel(conf["park_category"] or 0)
        entry = next((c for c in conf["channels"] if c[0] == channel.id), None)
        if live is None or park is None or entry is None:
            await ctx.send(f"Run `{ctx.clean_prefix}gameday setup` and add {channel.mention} with `gameday use` first.")
            return
        if str(channel.id) in conf["assigned"]:
            await ctx.send(f"{channel.mention} is open for a real game right now, so I won't touch it.")
            return
        seconds = max(10, min(seconds, 600))

        def report(step: str) -> str:
            fresh = guild.get_channel(channel.id)
            synced = "yes" if fresh.permissions_synced else "**no**"
            return (f"{step}: in **{fresh.category.name if fresh.category else 'no category'}**, named "
                    f"`{fresh.name}`, permissions synced with the category: {synced}")

        async with self._lock:
            await ctx.send(f"Opening {channel.mention} like a game would...")
            if not await self._edit(channel, live, "gameday-test"):
                await ctx.send(f"That failed: {self.last_error.get(guild.id, 'Discord said no')}")
                return
            await asyncio.sleep(2)  # let Discord's update reach us
            await ctx.send(report("Opened") + f"\nParking it again in {seconds} seconds.")
        await asyncio.sleep(seconds)
        async with self._lock:
            if not await self._edit(channel, park, entry[2]):
                await ctx.send(f"Parking failed: {self.last_error.get(guild.id, 'Discord said no')}. "
                               f"`{ctx.clean_prefix}gameday park` will retry.")
                return
            await asyncio.sleep(2)
            await ctx.send(report("Parked"))

    @gameday.command(name="perms")
    async def gameday_perms(self, ctx: commands.Context):
        """Which permissions Refbot needs to move and sync the game channels.

        Syncing copies every permission rule from the category onto the channel,
        and Discord only lets a bot set rules for permissions it has itself
        (unless it has Manage Permissions as a rule on that category/channel).
        This lists what's missing either way.
        """
        guild = ctx.guild
        conf = await self.config.guild(guild).all()
        places = [guild.get_channel(i) for i in (conf["live_category"], conf["park_category"])]
        places += [guild.get_channel(c[0]) for c in conf["channels"]]
        places = [p for p in places if p is not None]
        if not places:
            await ctx.send(f"Run `{ctx.clean_prefix}gameday setup` first.")
            return
        me = guild.me
        # Refbot's own role permissions, as if it didn't have Administrator.
        role_perms = discord.Permissions.none()
        for role in me.roles:
            role_perms.value |= role.permissions.value
        role_perms.administrator = False
        basics = discord.Permissions(view_channel=True, manage_channels=True, manage_roles=True)
        used = discord.Permissions.none()  # every permission any rule in these places allows or denies
        lines = []
        for place in places:
            for target, overwrite in place.overwrites.items():
                allow, deny = overwrite.pair()
                used.value |= allow.value | deny.value
            own_rule = any(place.overwrites_for(r).manage_roles for r in me.roles)
            # A basic is fine if Refbot's role has it server-wide or a rule here allows it.
            missing_basics = [pretty(n, channel=True) for n, v in basics if v and not getattr(role_perms, n)
                              and not any(getattr(place.overwrites_for(r), n) for r in me.roles)]
            kind = "category" if isinstance(place, discord.CategoryChannel) else "channel"
            status = "has Manage Permissions as a rule here" if own_rule else "no Manage Permissions rule here"
            extra = f", missing: {', '.join(missing_basics)}" if missing_basics else ""
            lines.append(f"- {kind} **{place.name}**: {status}{extra}")
        needed = [pretty(n) for n, v in used if v and not getattr(role_perms, n)]
        msg = ["**Refbot's access to the game categories and channels:**", *lines, ""]
        msg.append("Easiest fix: on both categories, give the Refbot role **View Channel**, **Manage Channels** "
                   "and **Manage Permissions**. The channels inherit it when they sync.")
        if needed:
            msg.append("Without those category rules, Refbot's role would need these server-wide (they're used "
                       "in the categories' or channels' permission settings): " + ", ".join(sorted(needed)))
        await ctx.send("\n".join(msg)[:2000], allowed_mentions=discord.AllowedMentions.none())

    @gameday.command(name="park")
    async def gameday_park(self, ctx: commands.Context):
        """Put every game channel back in parking now (it reopens on schedule if still on)."""
        guild = ctx.guild
        conf = await self.config.guild(guild).all()
        park = guild.get_channel(conf["park_category"] or 0)
        if park is None:
            await ctx.send("Run `gameday setup` first.")
            return
        async with self._lock:
            for channel_id, _, parked_name in conf["channels"]:
                channel = guild.get_channel(channel_id)
                if channel and str(channel_id) in conf["assigned"]:
                    await self._edit(channel, park, parked_name)
            await self.config.guild(guild).assigned.set({})
            await self.config.guild(guild).enabled.set(False)
        await ctx.send(f"All parked, and game channels are off. `{ctx.clean_prefix}gameday toggle` turns them back on.")
