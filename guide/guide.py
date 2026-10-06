"""An interactive guide to Refbot: a panel with a "Pick a topic…" dropdown.

- `[p]guide post #channel` (admin) posts the permanent panel. The dropdown keeps
  working after restarts (it's a "persistent view" with a fixed id).
- `/guide` opens the dropdown privately; `[p]guide` posts it where you are.
- Picking a topic shows it privately. "For mods" only opens for staff.
- Panels keep themselves up to date: the bot remembers the panels it posted (and
  any older panel the first time someone uses it) and re-edits them with the
  current topic list whenever the cog loads, so a reload is enough after
  changing topics.py. No reposting.
The topic text lives in topics.py.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from .topics import AUTOHIDE, COACHES_FAQ, COACHES_REPORT, MEMBERS_FAQ, MEMBERS_REPORT, TEXT, TOPICS

log = logging.getLogger("red.refbot.guide")

SELECT_ID = "refbot_guide:topic"  # fixed id so old panels keep working after a restart
COLOR = discord.Color(0x4F2683)  # Vikings purple
MAX_PANELS = 10  # remembered panels per server (oldest forgotten first)


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
        await self.cog.check_panel(interaction)


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
        # Panels to keep up to date: guild -> [[channel id, message id], ...]
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D9, force_registration=True)
        self.config.register_guild(panels=[])
        self._task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self.bot.add_view(self.persistent)
        self._task = asyncio.create_task(self._refresh_all())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
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
        # Only stores where the panels are (channel and message ids), nothing about users.
        return

    # ------------------------------------------------------------ keeping panels current

    async def _remember(self, message: discord.Message) -> None:
        pair = [message.channel.id, message.id]
        async with self.config.guild(message.guild).panels() as panels:
            if pair not in panels:
                panels.append(pair)
                del panels[:-MAX_PANELS]

    async def _refresh_all(self) -> None:
        """On load, re-edit every remembered panel with the current topics."""
        await self.bot.wait_until_red_ready()
        for guild_id, conf in (await self.config.all_guilds()).items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            gone = []
            for channel_id, message_id in conf["panels"]:
                channel = guild.get_channel_or_thread(channel_id)
                if channel is None:
                    gone.append([channel_id, message_id])  # channel deleted
                    continue
                try:
                    await channel.get_partial_message(message_id).edit(
                        embed=self._panel_embed(), view=self._fresh_view())
                except discord.NotFound:
                    gone.append([channel_id, message_id])  # deleted; forget it
                except discord.HTTPException as e:
                    log.info("Couldn't refresh guide panel %s: %r", message_id, e)
            if gone:
                async with self.config.guild(guild).panels() as panels:
                    panels[:] = [p for p in panels if p not in gone]

    def _is_current(self, message: discord.Message) -> bool:
        options = [o.value for row in message.components for c in getattr(row, "children", [])
                   for o in getattr(c, "options", [])]
        text = message.embeds[0].description if message.embeds else None
        return options == [t[0] for t in TOPICS] and text == self._panel_embed().description

    async def check_panel(self, interaction: discord.Interaction) -> None:
        """After someone uses a panel: remember it, and update it if it's out of date
        (e.g. a panel posted before this feature, or before a new topic was added)."""
        message = interaction.message
        if message is None or interaction.guild is None or message.flags.ephemeral:
            return  # private /guide copies can't be edited later, and expire anyway
        try:
            await self._remember(message)
            if not self._is_current(message):
                await message.edit(embed=self._panel_embed(), view=self._fresh_view())
        except discord.HTTPException as e:
            log.info("Couldn't update guide panel %s: %r", message.id, e)

    # ------------------------------------------------------------ content

    async def _live_values(self, guild: discord.Guild) -> dict:
        """Details read from the other cogs, so the guide matches the real settings."""
        values = {"report": "the report emoji", "chants": "- *(chants cog not loaded)*",
                  "report_who": COACHES_REPORT, "report_faq": COACHES_FAQ, "autohide": ""}
        modslash = self.bot.get_cog("ModSlash")
        if modslash is not None:
            conf = await modslash.config.guild(guild).all()
            if conf.get("report_emoji_id"):
                emoji = self.bot.get_emoji(conf["report_emoji_id"])
                values["report"] = str(emoji) if emoji else values["report"]
            elif conf.get("report_emoji_unicode"):
                values["report"] = conf["report_emoji_unicode"]
            if conf.get("auto_hide_threshold"):
                values["autohide"] = AUTOHIDE  # only mention auto-delete while it's on
            if conf.get("report_who") == "members":
                values["report_who"], values["report_faq"] = MEMBERS_REPORT, MEMBERS_FAQ
        chants = self.bot.get_cog("Chants")
        if chants is not None:
            conf = await chants.config.guild(guild).all()
            values["chants"] = "\n".join(f"- **{a}** → {p}" for a, p in sorted(conf["chants"])) or "- *(none set)*"
            reactions = conf.get("reactions") or []
            if reactions:
                values["chants"] += "\n\nAnd if your message has one of these anywhere in it, the bot reacts:\n" + \
                    "\n".join(f"- **{p}** → {e}" for p, e in reactions)
        values["report_who"] = values["report_who"].format(report=values["report"])
        return values

    async def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        return await self.bot.is_mod(member)

    async def _is_coach(self, member: discord.Member) -> bool:
        """Assistant Coaches = Defender's helper roles."""
        defender = self.bot.get_cog("Defender")
        return defender is not None and await defender.is_helper(member)

    async def show_topic(self, interaction: discord.Interaction, key: str) -> None:
        topic = next((t for t in TOPICS if t[0] == key), None)
        if topic is None:
            await interaction.response.send_message("That topic no longer exists.", ephemeral=True)
            return
        if topic[4] == "coaches":
            if not await self._is_staff(interaction.user) and not await self._is_coach(interaction.user):
                await interaction.response.send_message("That section is for Assistant Coaches and mods.", ephemeral=True)
                return
        elif topic[4] and not await self._is_staff(interaction.user):
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
        message = await ctx.send(embed=self._panel_embed(), view=self._fresh_view())
        await self._remember(message)

    @guide.command(name="post")
    @commands.admin_or_permissions(manage_guild=True)
    async def guide_post(self, ctx: commands.Context, channel: discord.TextChannel):
        """Post the permanent guide panel in a channel (e.g. #bot-guide)."""
        try:
            message = await channel.send(embed=self._panel_embed(), view=self._fresh_view())
        except discord.HTTPException as e:
            await ctx.send(f"Couldn't post there: {e.text or e.status}")
            return
        await self._remember(message)
        await ctx.send(f"Guide posted: {message.jump_url}. Pin it if you like.")

    @app_commands.command(name="guide", description="How to use Refbot")
    @app_commands.guild_only()
    async def guide_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._panel_embed(), view=self._fresh_view(private=True), ephemeral=True)
