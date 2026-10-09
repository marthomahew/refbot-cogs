"""Auto-replies for chant acronyms: someone types "FTP", the bot replies "Fuck the Packers".

- Triggers anywhere in a message, as a whole word, any capitalisation
  ("lol ftp" yes, "sftp" no).
- Several in one message get one combined reply.
- The reply pings nobody (the reply arrow already shows who said it).
- Flood control: at most N replies per minute, per channel (default) or per
  server, adjustable with `[p]chant limit`.
- The list is editable from Discord with `[p]chant add/remove/list`.

Reaction triggers (`[p]chant react ...`): if a message contains a phrase anywhere
(any capitalisation, even inside a longer word), the bot reacts with that
phrase's emoji. No flood limit: reactions don't add messages to the chat.

Ignored channels (`[p]chant ignore #channel`): no chant replies or reactions
there, threads included. Never named in public output (privacy rule).
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
            reactions=[],  # [phrase, emoji] pairs, e.g. ["wild", "<:wild:123>"]
            ignored=[],  # channel IDs with no chants or reactions (their threads too)
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

    @staticmethod
    def _reactions_for(content: str, reactions: list[list[str]]) -> list[str]:
        """Emoji for every reaction phrase found anywhere in the message (each once)."""
        text = content.casefold()
        emojis = []
        for phrase, emoji in reactions:
            if phrase.casefold() in text and emoji not in emojis:
                emojis.append(emoji)
        return emojis

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message):
        if message.guild is None or not message.content:
            return
        if message.author.bot and message.webhook_id is None:
            return  # other bots
        reposted = message.webhook_id is not None  # e.g. an embedfix link repost
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        if not await self.bot.ignored_channel_or_guild(message):
            return
        if not await self.bot.allowed_by_whitelist_blacklist(message.author):
            return
        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"]:
            return
        channel = message.channel
        if channel.id in conf["ignored"] or getattr(channel, "parent_id", None) in conf["ignored"]:
            return
        # Reactions also go on reposts: embedfix deletes the original message, so a
        # reaction there would vanish with it.
        for emoji in self._reactions_for(message.content, conf["reactions"]):
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as e:
                log.debug("Couldn't react %s in #%s: %r", emoji, message.channel, e)
        if reposted:
            return  # chant replies: the original already triggered them
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
        if conf["reactions"]:
            lines.append("Reactions: " + ", ".join(f"`{p}` {e}" for p, e in conf["reactions"]))
        status = "on" if conf["enabled"] else "off"
        if conf["ignored"]:  # a count only: never name channels (some are private)
            n = len(conf["ignored"])
            status += f", off in {n} channel{'s' if n != 1 else ''}"
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
        """Turn chant replies and reaction triggers on or off for this server."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        await conf.enabled.set(enabled)
        await ctx.send(f"Chants are now **{'on' if enabled else 'off'}**.")

    @chant.command(name="ignore")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_ignore(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        """No chant replies or reactions in a channel (or its threads)."""
        async with self.config.guild(ctx.guild).ignored() as ignored:
            if channel.id not in ignored:
                ignored.append(channel.id)
        # Delete the command so the channel's name isn't left sitting in chat.
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("Done. No chants or reactions in that channel.", delete_after=10)

    @chant.command(name="unignore")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_unignore(self, ctx: commands.Context, channel: discord.abc.GuildChannel):
        """Turn chants and reactions back on in an ignored channel."""
        async with self.config.guild(ctx.guild).ignored() as ignored:
            found = channel.id in ignored
            if found:
                ignored.remove(channel.id)
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("Done. Chants and reactions are back on in that channel." if found
                       else "That channel wasn't ignored.", delete_after=10)

    # ------------------------------------------------------------ reaction triggers

    @chant.group(name="react", aliases=["reaction", "reactions"], invoke_without_command=True)
    async def chant_react(self, ctx: commands.Context):
        """Auto-reactions: a phrase anywhere in a message gets an emoji reaction."""
        reactions = await self.config.guild(ctx.guild).reactions()
        lines = [f"`{p}` → {e}" for p, e in sorted(reactions, key=lambda r: r[0].casefold())] or ["No reaction triggers yet."]
        lines.append(f"-# Add one with `{ctx.clean_prefix}chant react add wild :wild:`")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @chant_react.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_react_add(self, ctx: commands.Context, *, phrase_and_emoji: str):
        """React with an emoji whenever a message contains a phrase.

        The emoji goes last: `[p]chant react add wild :wild:` or
        `[p]chant react add let's go 🔥`. Matching ignores capitalisation and
        works inside longer words ("wild" also matches "wildin").
        """
        phrase, _, emoji = phrase_and_emoji.strip().rpartition(" ")
        phrase = phrase.strip()
        if not phrase or not emoji:
            await ctx.send(f"Put the phrase first and the emoji last, like `{ctx.clean_prefix}chant react add wild :wild:`.")
            return
        if not 2 <= len(phrase) <= 50:
            await ctx.send("The phrase should be 2-50 characters.")
            return
        # Try the emoji on the command message: proves it's real and usable by the bot.
        try:
            await ctx.message.add_reaction(emoji)
        except discord.HTTPException:
            await ctx.send("I can't use that emoji. Use a standard emoji or one from a server I'm in.")
            return
        async with self.config.guild(ctx.guild).reactions() as reactions:
            old = next((e for p, e in reactions if p.casefold() == phrase.casefold()), None)
            reactions[:] = [[p, e] for p, e in reactions if p.casefold() != phrase.casefold()]
            reactions.append([phrase, emoji])
        was = f" (was {old})" if old else ""
        await ctx.send(f"Messages containing `{phrase}` will get {emoji}{was}.",
                       allowed_mentions=discord.AllowedMentions.none())

    @chant_react.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def chant_react_remove(self, ctx: commands.Context, *, phrase: str):
        """Stop reacting to a phrase."""
        async with self.config.guild(ctx.guild).reactions() as reactions:
            before = len(reactions)
            reactions[:] = [[p, e] for p, e in reactions if p.casefold() != phrase.strip().casefold()]
            removed = len(reactions) < before
        await ctx.send(f"Removed `{phrase.strip()}`." if removed else f"There's no reaction for `{phrase.strip()}`.",
                       allowed_mentions=discord.AllowedMentions.none())
