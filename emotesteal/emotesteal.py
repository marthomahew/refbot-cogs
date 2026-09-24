"""Copy custom emoji and stickers from a message into this server.

Reply to a message with `!steal` and the bot uploads every custom emoji and
sticker in it (ones from other servers, typically posted by Nitro users).
Or put the emoji straight in the command: `!steal <:pepelaugh:123...>`.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

import aiohttp
import discord
from redbot.core import commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.emotesteal")

# How a custom emoji looks inside message text: <:name:id> or <a:name:id> (animated).
EMOJI_RE = re.compile(r"<(a?):(\w{2,32}):(\d{15,21})>")
# Emoji and sticker names may only use letters, numbers and underscores.
NAME_RE = re.compile(r"^\w{2,32}$")

# Discord's upload size limits.
EMOJI_MAX_BYTES = 256 * 1024
STICKER_MAX_BYTES = 512 * 1024

# Don't let one command eat a pile of slots (and hit Discord's rate limits).
MAX_PER_COMMAND = 10


def emoji_url(emoji_id: str, animated: bool, size: Optional[int] = None) -> str:
    # Built by hand instead of discord.py's PartialEmoji.url, which gives .webp;
    # Discord's upload endpoint reliably takes PNG and GIF.
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"
    return f"{url}?size={size}" if size else url


class EmoteSteal(commands.Cog):
    """Add emoji and stickers from other servers to this one."""

    def __init__(self, bot: Red):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def cog_unload(self) -> None:
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # This cog stores no data at all, so there's nothing to delete.
        return

    async def _download(self, url: str) -> Optional[bytes]:
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            log.warning("Download failed for %s: %r", url, e)
            return None

    # ------------------------------------------------------------ emoji

    async def _add_emoji(self, guild: discord.Guild, name: str, emoji_id: str, animated: bool, who: str) -> str:
        """Upload one emoji. Returns a line for the result message."""
        if any(str(e.id) == emoji_id for e in guild.emojis):
            return f"`:{name}:` is already in this server."
        # A copied emoji gets a new id, so also check names to catch double steals.
        if any(e.name == name for e in guild.emojis):
            return f"❌ There's already an emoji called `:{name}:`. Use `!steal newname` to add it under another name."

        # Animated and regular emoji have separate slot counts.
        used = sum(1 for e in guild.emojis if e.animated == animated)
        kind = "animated" if animated else "emoji"
        if used >= guild.emoji_limit:
            return f"❌ `:{name}:`: no free {kind} slots ({used}/{guild.emoji_limit})."

        image = await self._download(emoji_url(emoji_id, animated))
        if image and len(image) > EMOJI_MAX_BYTES:
            # Too big for Discord's 256 KB limit; a smaller copy usually fits.
            image = await self._download(emoji_url(emoji_id, animated, size=128))
        if not image:
            return f"❌ `:{name}:`: couldn't download it."
        if len(image) > EMOJI_MAX_BYTES:
            return f"❌ `:{name}:`: the image is too big for Discord (over 256 KB)."

        try:
            new = await guild.create_custom_emoji(name=name, image=image, reason=f"!steal by {who}")
        except discord.HTTPException as e:
            log.warning("Emoji upload failed (%s): %r", name, e)
            return f"❌ `:{name}:`: Discord refused it ({e.text or e.status})."
        return f"✅ Added {new} `:{new.name}:` ({used + 1}/{guild.emoji_limit} {kind} slots)"

    # ------------------------------------------------------------ stickers

    async def _add_sticker(self, guild: discord.Guild, item: discord.StickerItem, name: str, who: str) -> str:
        if any(s.id == item.id for s in guild.stickers):
            return f"Sticker `{name}` is already in this server."
        if item.format is discord.StickerFormatType.lottie:
            # Lottie stickers are Discord's own built-in ones; servers can't upload them.
            return f"❌ Sticker `{name}`: Discord's built-in stickers can't be copied."
        used = len(guild.stickers)
        if used >= guild.sticker_limit:
            return f"❌ Sticker `{name}`: no free sticker slots ({used}/{guild.sticker_limit})."

        try:
            data = await item.read()
        except discord.HTTPException as e:
            log.warning("Sticker download failed (%s): %r", name, e)
            return f"❌ Sticker `{name}`: couldn't download it."
        if len(data) > STICKER_MAX_BYTES:
            return f"❌ Sticker `{name}`: the file is too big for Discord (over 512 KB)."

        # Discord requires a "related emoji" for every sticker; reuse the
        # original's if we can see it, otherwise a neutral one.
        related = "⭐"
        try:
            original = await item.fetch()
            related = getattr(original, "emoji", None) or related
        except discord.HTTPException:
            pass

        ext = "gif" if item.format is discord.StickerFormatType.gif else "png"
        try:
            new = await guild.create_sticker(
                name=name[:30],
                description=f"Added by {who}",
                emoji=related,
                file=discord.File(io.BytesIO(data), filename=f"{name}.{ext}"),
                reason=f"!steal by {who}",
            )
        except discord.HTTPException as e:
            log.warning("Sticker upload failed (%s): %r", name, e)
            return f"❌ Sticker `{name}`: Discord refused it ({e.text or e.status})."
        return f"✅ Added sticker **{new.name}** ({used + 1}/{guild.sticker_limit} sticker slots)"

    # ------------------------------------------------------------ command

    @commands.command(name="steal")
    @commands.guild_only()
    @commands.mod_or_permissions(manage_expressions=True)
    async def steal(self, ctx: commands.Context, name: Optional[str] = None):
        """Add the custom emoji and stickers from a message to this server.

        Reply to a message with `[p]steal`, or paste the emoji into the command:
        `[p]steal :pepelaugh:`. Give a new name when adding a single item:
        `[p]steal newname`.
        """
        # Only Manage Expressions counts. (Discord gives @everyone "Create
        # Expressions" by default, but that isn't enough for a bot's uploads.)
        if not ctx.guild.me.guild_permissions.manage_expressions:
            await ctx.send(
                "I need the **Manage Expressions** permission to add emoji and stickers. "
                "Server Settings → Roles → my role → turn on Manage Expressions."
            )
            return

        # Where to look: the replied-to message, otherwise this command message itself.
        source = ctx.message
        if ctx.message.reference and ctx.message.reference.message_id:
            source = ctx.message.reference.resolved
            if not isinstance(source, discord.Message):
                try:
                    source = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                except discord.HTTPException:
                    await ctx.send("I couldn't find the message you replied to.")
                    return

        # If the "name" is actually an emoji (e.g. `!steal <:pepe:123>`), it's not a rename.
        if name and EMOJI_RE.fullmatch(name):
            name = None
        if name and not NAME_RE.match(name):
            await ctx.send("Names can only use letters, numbers and underscores (2-32 characters).")
            return

        # Collect unique emoji from the text, then stickers.
        emojis: dict[str, tuple[str, bool]] = {}
        for animated, emoji_name, emoji_id in EMOJI_RE.findall(source.content):
            emojis.setdefault(emoji_id, (emoji_name, bool(animated)))
        stickers = list(source.stickers)

        total = len(emojis) + len(stickers)
        if total == 0:
            await ctx.send("No custom emoji or stickers found. Reply to a message that has some with `!steal`.")
            return
        if name and total > 1:
            await ctx.send("That message has more than one emoji/sticker, so I can't give them all one name. "
                           "Run `!steal` without a name.")
            return

        who = f"{ctx.author} ({ctx.author.id})"
        lines = []
        async with ctx.typing():
            for emoji_id, (emoji_name, animated) in list(emojis.items())[:MAX_PER_COMMAND]:
                lines.append(await self._add_emoji(ctx.guild, name or emoji_name, emoji_id, animated, who))
            for item in stickers[: max(0, MAX_PER_COMMAND - len(emojis))]:
                lines.append(await self._add_sticker(ctx.guild, item, name or item.name, who))
        if total > MAX_PER_COMMAND:
            lines.append(f"(Only the first {MAX_PER_COMMAND} were added. Run it again for the rest.)")
        await ctx.send("\n".join(lines))
