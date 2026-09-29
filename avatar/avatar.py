"""Grab someone's profile pictures at full size (handy for memes).

- `!avatar [@user]`                      public reply with the images as files.
- `/avatar user`                         private reply ("Only you can see this").
- Right-click a user → Apps → "Get avatar"   private reply.

Sends, where they exist: the main avatar, the server-specific avatar, and the
profile banner, at the largest size they exist (never upscaled): PNG for still
images, GIF for animated ones.
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Union

import aiohttp
import discord
from discord import app_commands
from redbot.core import commands
from redbot.core.bot import Red

log = logging.getLogger("red.refbot.avatar")


def original_url(asset: discord.Asset) -> str:
    """The asset's URL at the largest size it exists at: PNG for still images, GIF
    if animated. Asking Discord's CDN for size=4096 (its maximum) returns the
    original upload size and never upscales (tested: a 355 px upload comes back at
    355 px). With no size at all it returns a tiny 128 px copy, so don't drop it."""
    fmt = "gif" if asset.is_animated() else "png"
    return asset.replace(format=fmt, size=4096).url


class Avatar(commands.Cog):
    """Full-size profile pictures and banners."""

    def __init__(self, bot: Red):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        # Right-click a user → Apps → "Get avatar".
        self.menu = app_commands.ContextMenu(name="Get avatar", callback=self.avatar_from_menu)

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Stores nothing.
        return

    # ------------------------------------------------------------ gathering

    async def _pictures(self, user: Union[discord.Member, discord.User], guild: Optional[discord.Guild]):
        """[(label, url, filename)] for every picture this person has."""
        name = user.name
        pictures = []
        # Main avatar (Discord's default one if they never set any).
        pictures.append(("Avatar", original_url(user.avatar or user.default_avatar), f"{name}_avatar"))
        # Server-specific avatar, if they set one for this server.
        member = guild.get_member(user.id) if guild else None
        if member is not None and member.guild_avatar is not None:
            pictures.append(("Server avatar", original_url(member.guild_avatar), f"{name}_server_avatar"))
        # Banners aren't included in the member list; fetching the user gets them.
        try:
            full_user = await self.bot.fetch_user(user.id)
            if full_user.banner is not None:
                pictures.append(("Banner", original_url(full_user.banner), f"{name}_banner"))
        except discord.HTTPException:
            pass
        return pictures

    async def _files(self, pictures, size_limit: int) -> tuple[list[discord.File], list[str]]:
        """Download each picture as a file. Anything too big to upload becomes a link."""
        files, lines = [], []
        for label, url, filename in pictures:
            ext = url.split("?")[0].rsplit(".", 1)[-1]
            try:
                async with self._session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            except Exception as e:
                log.info("Couldn't download %s: %r", url, e)
                lines.append(f"**{label}:** [open]({url})")
                continue
            if len(data) > size_limit:
                lines.append(f"**{label}:** [open full size]({url}) (too big to upload here)")
                continue
            files.append(discord.File(io.BytesIO(data), filename=f"{filename}.{ext}"))
            lines.append(f"**{label}:** `{filename}.{ext}` · [open]({url})")
        return files, lines

    async def _build(self, user, guild) -> tuple[str, list[discord.File]]:
        pictures = await self._pictures(user, guild)
        limit = guild.filesize_limit if guild else 10 * 1024 * 1024
        files, lines = await self._files(pictures, limit)
        header = f"🖼️ **{discord.utils.escape_markdown(user.display_name)}** · full size"
        return "\n".join([header] + lines), files

    # ------------------------------------------------------------ commands

    @commands.command(name="avatar", aliases=["pfp"])
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def avatar(self, ctx: commands.Context, *, user: Optional[Union[discord.Member, discord.User]] = None):
        """Post someone's profile pictures at full size: `[p]avatar @user` (yours if no name)."""
        user = user or ctx.author
        async with ctx.typing():
            text, files = await self._build(user, ctx.guild)
        await ctx.send(text, files=files, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)

    @app_commands.command(name="avatar", description="Get someone's profile pictures at full size")
    @app_commands.describe(user="Whose pictures (leave empty for yours)")
    @app_commands.guild_only()
    async def avatar_slash(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await self._reply_private(interaction, user or interaction.user)

    async def avatar_from_menu(self, interaction: discord.Interaction, user: discord.User):
        """Right-click a user → Apps → Get avatar."""
        await self._reply_private(interaction, user)

    async def _reply_private(self, interaction: discord.Interaction, user) -> None:
        # Downloads can take a moment; Discord only waits 3 seconds for a first reply.
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, files = await self._build(user, interaction.guild)
        await interaction.followup.send(
            text, files=files, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True
        )
