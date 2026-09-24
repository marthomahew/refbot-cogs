"""A single live-updating NFL scoreboard embed.

How it works:
- One background task (started in cog_load, cancelled in cog_unload) fetches
  ESPN's scoreboard, then edits one stored message in each enabled server.
- How long it sleeps between fetches depends on whether games are live
  (see espn.poll_interval).
- If ESPN fails, we log it once and leave the last good embed alone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import espn

log = logging.getLogger("red.refbot.scoreboard")


class Scoreboard(commands.Cog):
    """A live NFL scoreboard that edits one message in place."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D1, force_registration=True)
        self.config.register_guild(
            channel_id=None,  # where the scoreboard lives
            message_id=None,  # the message we keep editing
            running=False,  # is the update loop enabled for this server?
            team="MIN",  # highlighted team
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        # Setting this event wakes the loop early (e.g. after `start`).
        self._wake = asyncio.Event()
        # Stops the loop and a command from editing at the same moment.
        self._lock = asyncio.Lock()

        self._week: Optional[espn.Week] = None  # last good data from ESPN
        self._fetch_failing = False  # so we only log a failure streak once
        self._last_content: dict[int, str] = {}  # guild id -> what we last showed
        self._warned: set[str] = set()  # one-time warnings already logged
        self.interval, self.interval_reason = 300, "starting up"

    # ------------------------------------------------------------ lifecycle

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # This cog stores no data about users, so there's nothing to delete.
        return

    # ------------------------------------------------------------ the loop

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            self._wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad tick kill the loop.
                log.exception("Unexpected error in scoreboard loop")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        guilds = await self.config.all_guilds()
        active = [gid for gid, conf in guilds.items() if conf["running"] and conf["channel_id"]]
        if not active:
            # Nobody wants updates; don't bother ESPN. `start` will wake us.
            self.interval, self.interval_reason = 3600, "no servers running"
            return

        async with self._lock:
            await self._fetch()
            for gid in active:
                guild = self.bot.get_guild(gid)
                if guild is not None:
                    await self._update_guild(guild)

        self.interval, self.interval_reason = espn.poll_interval(self._week, datetime.now(timezone.utc))

    # ------------------------------------------------------------ ESPN

    async def _fetch(self) -> None:
        """Fetch fresh data into self._week. On failure, keep the old data."""
        try:
            async with self._session.get(espn.ESPN_URL) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            week = espn.parse_scoreboard(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._fetch_failing:
                log.warning("ESPN scoreboard fetch failed; keeping last good data: %r", e)
                self._fetch_failing = True
            return

        if self._fetch_failing:
            log.info("ESPN scoreboard fetch working again")
            self._fetch_failing = False
        self._week = week

    # ------------------------------------------------------------ Discord

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    async def _update_guild(self, guild: discord.Guild, force: bool = False) -> Optional[discord.Message]:
        """Edit (or post) this server's scoreboard. Returns the message if we touched it.

        Skips the edit when nothing on the board has changed, unless `force`.
        """
        conf = self.config.guild(guild)
        channel_id = await conf.channel_id()
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            self._warn_once(f"nochan-{guild.id}", "Scoreboard channel %s missing in guild %s", channel_id, guild.id)
            return None

        team = await conf.team()
        # Two cards in one message: the highlighted team's game, then everyone else.
        # Editing both is still a single API call.
        embeds = espn.build_embeds(self._week, team, datetime.now(timezone.utc))
        # Compare everything except the footer, whose "last updated" time always changes.
        content = repr([{k: v for k, v in e.to_dict().items() if k != "footer"} for e in embeds])
        if not force and self._last_content.get(guild.id) == content:
            return None

        message_id = await conf.message_id()
        message = None
        try:
            if message_id:
                try:
                    message = await channel.get_partial_message(message_id).edit(embeds=embeds)
                except discord.NotFound:
                    message = None  # someone deleted it; post a new one below
            if message is None:
                message = await channel.send(embeds=embeds)
                await conf.message_id.set(message.id)
        except discord.Forbidden:
            self._warn_once(f"forbidden-{guild.id}", "No permission to post/edit scoreboard in #%s (%s)", channel, guild.id)
            return None
        except discord.HTTPException as e:
            log.warning("Couldn't update scoreboard in guild %s: %r", guild.id, e)
            return None

        self._last_content[guild.id] = content
        self._warned.discard(f"forbidden-{guild.id}")
        self._warned.discard(f"nochan-{guild.id}")
        return message

    async def _refresh_now(self, guild: discord.Guild) -> Optional[discord.Message]:
        """Fetch right now and force an edit. Used by commands."""
        async with self._lock:
            await self._fetch()
            return await self._update_guild(guild, force=True)

    @staticmethod
    def _message_link(guild_id: int, channel_id: int, message_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    # ------------------------------------------------------------ commands

    @commands.hybrid_group(name="scoreboard")
    @commands.guild_only()
    async def scoreboard(self, ctx: commands.Context):
        """Live NFL scoreboard."""

    @scoreboard.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def sb_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the scoreboard channel and post the scoreboard there."""
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await ctx.send(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
            return
        await ctx.defer()

        conf = self.config.guild(ctx.guild)
        old_channel_id, old_message_id = await conf.channel_id(), await conf.message_id()
        if old_channel_id == channel.id and old_message_id:
            # Same channel: just refresh the existing message.
            pass
        else:
            # Moving channels: tidy up the old board so there aren't two.
            old_channel = ctx.guild.get_channel(old_channel_id) if old_channel_id else None
            if old_channel is not None and old_message_id:
                try:
                    await old_channel.get_partial_message(old_message_id).delete()
                except discord.HTTPException:
                    pass
            await conf.channel_id.set(channel.id)
            await conf.message_id.set(None)

        message = await self._refresh_now(ctx.guild)
        if message is None:
            await ctx.send("Couldn't post the scoreboard; check the bot's permissions and logs.")
            return
        running = await conf.running()
        hint = "" if running else f" Run `{ctx.clean_prefix}scoreboard start` to keep it updating."
        await ctx.send(f"Scoreboard posted: {message.jump_url}{hint}")

    @scoreboard.command(name="start")
    @commands.admin_or_permissions(manage_guild=True)
    async def sb_start(self, ctx: commands.Context):
        """Start updating the scoreboard automatically."""
        conf = self.config.guild(ctx.guild)
        if not await conf.channel_id():
            await ctx.send(f"Set a channel first with `{ctx.clean_prefix}scoreboard channel #channel`.")
            return
        await conf.running.set(True)
        self._wake.set()  # update right away instead of waiting out the current sleep
        await ctx.send("Scoreboard updates started.")

    @scoreboard.command(name="stop")
    @commands.admin_or_permissions(manage_guild=True)
    async def sb_stop(self, ctx: commands.Context):
        """Stop updating the scoreboard. The last embed stays in place."""
        await self.config.guild(ctx.guild).running.set(False)
        await ctx.send("Scoreboard updates stopped.")

    @scoreboard.command(name="refresh")
    @commands.mod_or_permissions(manage_messages=True)
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def sb_refresh(self, ctx: commands.Context):
        """Fetch scores and update the scoreboard right now."""
        if not await self.config.guild(ctx.guild).channel_id():
            await ctx.send(f"Set a channel first with `{ctx.clean_prefix}scoreboard channel #channel`.")
            return
        await ctx.defer()
        message = await self._refresh_now(ctx.guild)
        if message is None:
            await ctx.send("Couldn't update the scoreboard; check the bot's permissions and logs.")
        elif self._fetch_failing:
            await ctx.send(f"ESPN isn't responding, so the board still shows the last good scores: {message.jump_url}")
        else:
            await ctx.send(f"Scoreboard refreshed: {message.jump_url}")

    @scoreboard.command(name="settings")
    @commands.admin_or_permissions(manage_guild=True)
    async def sb_settings(self, ctx: commands.Context):
        """Show the current scoreboard settings."""
        conf = await self.config.guild(ctx.guild).all()
        channel_id, message_id = conf["channel_id"], conf["message_id"]
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        link = self._message_link(ctx.guild.id, channel_id, message_id) if channel_id and message_id else "none"
        espn_state = "failing (showing last good data)" if self._fetch_failing else "OK"

        lines = [
            f"**Channel:** {channel.mention if channel else 'not set'}",
            f"**Message:** {link}",
            f"**Running:** {'yes' if conf['running'] else 'no'}",
            f"**Highlighted team:** {conf['team']}",
            f"**Current poll interval:** {self.interval}s ({self.interval_reason})",
            f"**ESPN:** {espn_state}",
        ]
        await ctx.send("\n".join(lines))

    @scoreboard.command(name="team")
    @commands.admin_or_permissions(manage_guild=True)
    async def sb_team(self, ctx: commands.Context, abbreviation: str):
        """Change the highlighted team (e.g. MIN, GB, DET)."""
        abbr = abbreviation.upper()
        if abbr not in espn.NFL_TEAMS:
            await ctx.send(f"Unknown team `{abbr}`. Use one of: {', '.join(sorted(espn.NFL_TEAMS))}")
            return
        await self.config.guild(ctx.guild).team.set(abbr)
        if await self.config.guild(ctx.guild).channel_id():
            await ctx.defer()
            await self._refresh_now(ctx.guild)
        await ctx.send(f"Highlighted team set to {abbr}.")
