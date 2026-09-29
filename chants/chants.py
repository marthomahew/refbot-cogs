"""Auto-replies for chant acronyms: someone types "FTP", the bot replies "Fuck the Packers".

- Triggers anywhere in a message, as a whole word, any capitalisation
  ("lol ftp" yes, "sftp" no).
- Several in one message get one combined reply.
- The reply pings nobody (the reply arrow already shows who said it).
- Flood control: at most N replies per minute, per channel (default) or per
  server, adjustable with `[p]chant limit`.
- The list is editable from Discord with `[p]chant add/remove/list`.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from typing import Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.chants")

DEFAULT_CHANTS = {
    "FTP": "Fuck the Packers",
    "FTB": "Fuck the Bears",
    "FTL": "Fuck the Lions",
    "FTR": "Fuck the Refs",
    "FSP": "Fuck Sean Payton",
}
DEFAULT_LIMIT = 3  # replies per minute (admins can change it with `chant limit`)
MAX_LIMIT = 30
RATE_WINDOW = 60  # seconds
ACRONYM_RE = re.compile(r"^[A-Za-z0-9]{2,10}$")


class Chants(commands.Cog):
    """FTP → Fuck the Packers, and friends."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D7, force_registration=True)
        self.config.register_guild(
            enabled=True,
            # A list of [acronym, phrase] pairs rather than a dict: Red merges dict
            # defaults back in, so removing a default chant wouldn't stick.
            chants=[[a, p] for a, p in DEFAULT_CHANTS.items()],
            rate_limit=DEFAULT_LIMIT,  # replies per minute...
            rate_scope="channel",  # ...per "channel" or per "server"
        )
        # ("channel", id) or ("server", id) -> times of recent replies
        self._recent: dict[tuple[str, int], deque] = defaultdict(deque)

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Stores no user data.
        return

    @staticmethod
    def _find(content: str, chants: list[list[str]]) -> list[str]:
        """Phrases for every chant acronym in the message, in the order they appear."""
        found = []
        for acronym, phrase in chants:
            # Whole word only: not part of a longer word or number.
            match = re.search(rf"(?<![A-Za-z0-9]){re.escape(acronym)}(?![A-Za-z0-9])", content, re.IGNORECASE)
            if match:
                found.append((match.start(), phrase))
        return [phrase for _, phrase in sorted(found)]

    def _allowed(self, message: discord.Message, limit: int, scope: str) -> bool:
        """Flood control: True if the bot may reply right now."""
        key = ("server", message.guild.id) if scope == "server" else ("channel", message.channel.id)
        now = time.monotonic()
        recent = self._recent[key]
        while recent and now - recent[0] > RATE_WINDOW:
            recent.popleft()
        if len(recent) >= limit:
            return False
        recent.append(now)
        return True

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return  # bots, and embedfix link reposts (the original already triggered)
        if not message.content:
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if not await self.bot.ignored_channel_or_guild(message):
            return
        if not await self.bot.allowed_by_whitelist_blacklist(message.author):
            return
        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"]:
            return
        phrases = self._find(message.content, conf["chants"])
        if not phrases or not self._allowed(message, conf["rate_limit"], conf["rate_scope"]):
            return
        try:
            await message.reply(
                "\n".join(phrases), mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as e:
            log.debug("Couldn't reply with a chant in #%s: %r", message.channel, e)

    # ------------------------------------------------------------ commands

    @commands.group(name="chant", aliases=["chants"])
    @commands.guild_only()
    async def chant(self, ctx: commands.Context):
        """Chant auto-replies (FTP → Fuck the Packers)."""

    @chant.command(name="list")
    async def chant_list(self, ctx: commands.Context):
        """Show all chants."""
        conf = await self.config.guild(ctx.guild).all()
        lines = [f"`{a}` → {p}" for a, p in sorted(conf["chants"])] or ["No chants yet."]
        status = "on" if conf["enabled"] else "off"
        lines.append(f"-# Chants are {status} · up to {conf['rate_limit']} replies per {conf['rate_scope']} per minute")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @chant.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_add(self, ctx: commands.Context, acronym: str, *, phrase: str):
        """Add or change a chant: `[p]chant add FTG Fuck the Giants`."""
        if not ACRONYM_RE.match(acronym):
            await ctx.send("The trigger should be 2-10 letters or numbers, like `FTG`.")
            return
        phrase = phrase.strip()[:300]
        async with self.config.guild(ctx.guild).chants() as chants:
            old = next((p for a, p in chants if a.upper() == acronym.upper()), None)
            chants[:] = [[a, p] for a, p in chants if a.upper() != acronym.upper()]
            chants.append([acronym.upper(), phrase])
        was = f" (was: {old})" if old else ""
        await ctx.send(f"`{acronym.upper()}` → {phrase}{was}", allowed_mentions=discord.AllowedMentions.none())

    @chant.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_remove(self, ctx: commands.Context, acronym: str):
        """Remove a chant."""
        async with self.config.guild(ctx.guild).chants() as chants:
            before = len(chants)
            chants[:] = [[a, p] for a, p in chants if a.upper() != acronym.upper()]
            removed = len(chants) < before
        await ctx.send(f"Removed `{acronym.upper()}`." if removed else f"There's no `{acronym.upper()}` chant.")

    @chant.command(name="limit")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_limit(self, ctx: commands.Context, per_minute: int, scope: str = "channel"):
        """How many chant replies per minute: `[p]chant limit 5` (per channel) or `[p]chant limit 5 server`."""
        scope = scope.lower()
        if scope not in ("channel", "server"):
            await ctx.send("Scope must be `channel` or `server`.")
            return
        if not 1 <= per_minute <= MAX_LIMIT:
            await ctx.send(f"Pick a number from 1 to {MAX_LIMIT}. (To turn chants off, use `{ctx.clean_prefix}chant toggle`.)")
            return
        conf = self.config.guild(ctx.guild)
        await conf.rate_limit.set(per_minute)
        await conf.rate_scope.set(scope)
        self._recent.clear()  # start counting fresh under the new rule
        where = "each channel" if scope == "channel" else "the whole server"
        await ctx.send(f"Chants: up to **{per_minute}** replies per minute in {where}.")

    @chant.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_toggle(self, ctx: commands.Context):
        """Turn chant replies on or off for this server."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        await conf.enabled.set(enabled)
        await ctx.send(f"Chants are now **{'on' if enabled else 'off'}**.")
