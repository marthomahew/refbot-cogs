"""An interactive guide to Refbot: a panel with a "Pick a topic…" dropdown.

- `[p]guide post #channel` (admin) posts the permanent panel. The dropdown keeps
  working after restarts (it's a "persistent view" with a fixed id).
- `/guide` opens the dropdown privately; `[p]guide` posts it where you are.
- Picking a topic shows it privately. "For mods" only opens for staff.
The topic text lives in topics.py.
"""

from __future__ import annotations

import logging
import weakref
from typing import Optional

import discord
from discord import app_commands
from redbot.core import commands
from redbot.core.bot import Red

from .topics import TEXT, TOPICS

log = logging.getLogger("red.refbot.guide")

SELECT_ID = "refbot_guide:topic"  # fixed id so old panels keep working after a restart
COLOR = discord.Color(0x4F2683)  # Vikings purple


class TopicSelect(discord.ui.Select):
    def __init__(self, cog: "Guide"):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, description=desc)
            for key, label, emoji, desc, _ in TOPICS
        ]
        super().__init__(placeholder="Pick a topic", options=options, custom_id=SELECT_ID)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.show_topic(interaction, self.values[0])


class GuideView(discord.ui.View):
    def __init__(self, cog: "Guide", timeout: Optional[float] = None):
        super().__init__(timeout=timeout)  # None = never expires
        self.add_item(TopicSelect(cog))


class Guide(commands.Cog):
    """How to use Refbot."""

    def __init__(self, bot: Red):
        self.bot = bot
        # The one registered "catch-all" menu that answers every guide panel,
        # including ones posted before a restart. It is NEVER sent in a message:
        # discord.py changes views it sends (e.g. private replies get a 15 minute
        # expiry), and if this one expired, every panel would stop answering.
        self.persistent = GuideView(self)
        # Fresh copies sent with messages, so they can be tidied up on reload.
        self._sent: "weakref.WeakSet[GuideView]" = weakref.WeakSet()

    async def cog_load(self) -> None:
        self.bot.add_view(self.persistent)

    async def cog_unload(self) -> None:
        # Stop everything this copy of the cog registered, so after a reload
        # every panel is answered by the new code.
        for view in list(self._sent):
            view.stop()
        self.persistent.stop()

    def _fresh_view(self, private: bool = False) -> GuideView:
        """A new menu for one message. Private (ephemeral) ones expire after 15
        minutes like all private menus; that only affects that one message."""
        view = GuideView(self, timeout=15 * 60 if private else None)
        self._sent.add(view)
        return view

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Stores nothing.
        return

    # ------------------------------------------------------------ content

    async def _live_values(self, guild: discord.Guild) -> dict:
        """Details read from the other cogs, so the guide matches the real settings."""
        values = {"report": "the report emoji", "chants": "- *(chants cog not loaded)*"}
        modslash = self.bot.get_cog("ModSlash")
        if modslash is not None:
            conf = await modslash.config.guild(guild).all()
            if conf.get("report_emoji_id"):
                emoji = self.bot.get_emoji(conf["report_emoji_id"])
                values["report"] = str(emoji) if emoji else values["report"]
            elif conf.get("report_emoji_unicode"):
                values["report"] = conf["report_emoji_unicode"]
        chants = self.bot.get_cog("Chants")
        if chants is not None:
            pairs = await chants.config.guild(guild).chants()
            values["chants"] = "\n".join(f"- **{a}** → {p}" for a, p in sorted(pairs)) or "- *(none set)*"
        return values

    async def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        return await self.bot.is_mod(member)

    async def show_topic(self, interaction: discord.Interaction, key: str) -> None:
        topic = next((t for t in TOPICS if t[0] == key), None)
        if topic is None:
            await interaction.response.send_message("That topic no longer exists.", ephemeral=True)
            return
        if topic[4] and not await self._is_staff(interaction.user):
            await interaction.response.send_message("That section is for mods.", ephemeral=True)
            return
        text = TEXT[key]
        if "{" in text:
            text = text.format(**await self._live_values(interaction.guild))
        embed = discord.Embed(description=text[:4096], color=COLOR)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _panel_embed(self) -> discord.Embed:
        lines = [
            "## How to use Refbot",
            "Pick a topic from the menu below. The answer is only visible to you.",
            "",
        ]
        lines += [f"**{label}** - {desc}" for _, label, emoji, desc, mods in TOPICS if not mods]
        return discord.Embed(description="\n".join(lines), color=COLOR)

    # ------------------------------------------------------------ commands

    @commands.group(name="guide", invoke_without_command=True, autohelp=False)
    @commands.guild_only()
    async def guide(self, ctx: commands.Context):
        """Show the Refbot guide here."""
        await ctx.send(embed=self._panel_embed(), view=self._fresh_view())

    @guide.command(name="post")
    @commands.admin_or_permissions(manage_guild=True)
    async def guide_post(self, ctx: commands.Context, channel: discord.TextChannel):
        """Post the permanent guide panel in a channel (e.g. #bot-guide)."""
        try:
            message = await channel.send(embed=self._panel_embed(), view=self._fresh_view())
        except discord.HTTPException as e:
            await ctx.send(f"Couldn't post there: {e.text or e.status}")
            return
        await ctx.send(f"Guide posted: {message.jump_url}. Pin it if you like.")

    @app_commands.command(name="guide", description="How to use Refbot")
    @app_commands.guild_only()
    async def guide_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._panel_embed(), view=self._fresh_view(private=True), ephemeral=True)
