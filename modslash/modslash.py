"""Slash command versions of Red's moderation commands.

Red's built-in mod cogs (Mod, Warnings, Mutes) only have `!` commands. Each
slash command here runs the *same* Red command underneath, so modlog cases,
warning points, mute settings, DMs and permissions all behave exactly like the
`!` version. Only `/purge` is written directly (see `purge` for why).

Who can use them: Red's own checks (mod/admin roles or Discord permissions)
decide, same as `!`. Discord also hides each command from members without a
matching permission; server admins can change who sees them in Server
Settings → Integrations → the bot.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.commands.converter import parse_timedelta

log = logging.getLogger("red.refbot.modslash")


def to_timedelta(text: Optional[str], maximum: Optional[timedelta] = None) -> Optional[timedelta]:
    """"10m", "2h", "1d12h" -> timedelta. A bare number means seconds. None if blank.
    Raises BadArgument if it can't be read."""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        text += "s"
    duration = parse_timedelta(text, maximum=maximum)
    if duration is None:
        raise commands.BadArgument(f"I couldn't read `{text}` as a length of time. Try `10m`, `2h` or `1d`.")
    return duration


class ModSlash(commands.Cog):
    """Slash versions of Red's ban, kick, warn, mute and more."""

    def __init__(self, bot: Red):
        self.bot = bot

    async def red_delete_data_for_user(self, **kwargs) -> None:
        # Stores nothing; the underlying Red cogs manage their own data.
        return

    async def _run(self, interaction: discord.Interaction, command_name: str, *args, **kwargs) -> None:
        """Run an existing Red text command on behalf of a slash command."""
        command = self.bot.get_command(command_name)
        if command is None or command.cog is None:
            cog = {"warn": "Warnings", "warnings": "Warnings", "unwarn": "Warnings",
                   "mute": "Mutes", "unmute": "Mutes", "timeout": "Mutes",
                   "mutechannel": "Mutes", "unmutechannel": "Mutes"}.get(command_name, "Mod")
            await interaction.response.send_message(
                f"That needs Red's **{cog}** cog, which isn't loaded (`!load {cog.lower()}`).", ephemeral=True
            )
            return

        # Mod actions can take a few seconds (DMs, modlog); Discord only waits 3.
        await interaction.response.defer(thinking=True)
        ctx = await self.bot.get_context(interaction)
        ctx.command = command
        ctx.invoked_with = command.name

        # Red commands often confirm with a ✅ reaction on the command message.
        # A slash command has no real message, so that silently does nothing.
        # Track whether the command said anything, and say "Done" if not.
        replied = False
        original_send = ctx.send

        async def tracked_send(*a, **kw):
            nonlocal replied
            replied = True
            return await original_send(*a, **kw)

        ctx.send = tracked_send

        try:
            if not await command.can_run(ctx):
                raise commands.CheckFailure()
            await ctx.invoke(command, *args, **kwargs)
        except commands.CheckFailure as e:
            await ctx.send(str(e) or "You don't have permission to do that.")
        except commands.CommandError as e:
            await ctx.send(str(e) or "That didn't work.")
        except Exception:
            log.exception("/%s failed", command_name)
            await ctx.send("Something went wrong running that. It's in the bot's log.")
        if not replied:
            await ctx.send("✅ Done.")

    # ------------------------------------------------------------ Mod cog

    @app_commands.command(name="ban", description="Ban a user (works for people not in the server too)")
    @app_commands.describe(user="Who to ban", days="Delete their messages from the last N days (0-7)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        days: Optional[app_commands.Range[int, 0, 7]] = None,
        reason: Optional[str] = None,
    ):
        # Red's ban takes a member, or a plain user ID for people not in the server.
        target = user if isinstance(user, discord.Member) else user.id
        await self._run(interaction, "ban", target, days, reason=reason)

    @app_commands.command(name="kick", description="Kick a member")
    @app_commands.describe(member="Who to kick", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        await self._run(interaction, "kick", member, reason=reason)

    @app_commands.command(name="tempban", description="Ban a member for a while")
    @app_commands.describe(
        member="Who to ban",
        duration="How long, e.g. 1d, 12h, 1w (default: Red's tempban setting)",
        days="Delete their messages from the last N days (0-7)",
        reason="Why",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: Optional[str] = None,
        days: Optional[app_commands.Range[int, 0, 7]] = None,
        reason: Optional[str] = None,
    ):
        try:
            length = to_timedelta(duration)
        except commands.BadArgument as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self._run(interaction, "tempban", member, length, days, reason=reason)

    @app_commands.command(name="softban", description="Ban and immediately unban, to clear someone's recent messages")
    @app_commands.describe(member="Who to softban", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def softban(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        await self._run(interaction, "softban", member, reason=reason)

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="The banned user's ID (right-click them → Copy User ID)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: Optional[str] = None):
        # IDs are too big for Discord's number option, so it's text here.
        if not user_id.strip().isdigit():
            await interaction.response.send_message("That doesn't look like a user ID (numbers only).", ephemeral=True)
            return
        await self._run(interaction, "unban", int(user_id.strip()), reason=reason)

    @app_commands.command(name="slow", description="Set slowmode in this channel")
    @app_commands.describe(interval="Time between messages, e.g. 30s, 5m, 1h (0 turns it off, max 6h)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def slow(self, interaction: discord.Interaction, interval: str):
        try:
            length = to_timedelta(interval, maximum=timedelta(hours=6)) or timedelta(0)
        except commands.BadArgument as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self._run(interaction, "slowmode", interval=length)

    # ------------------------------------------------------------ Warnings cog

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="Who to warn", reason="Why (required)", points="Warning points (default 1)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        points: Optional[app_commands.Range[int, 1, 100]] = 1,
    ):
        await self._run(interaction, "warn", member, points or 1, reason=reason)

    @app_commands.command(name="warnings", description="List a member's warnings")
    @app_commands.describe(member="Whose warnings to show")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        await self._run(interaction, "warnings", member)

    @app_commands.command(name="unwarn", description="Remove one of a member's warnings")
    @app_commands.describe(member="Whose warning", warn_id="The warning's ID (see /warnings)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def unwarn(
        self, interaction: discord.Interaction, member: discord.Member, warn_id: str, reason: Optional[str] = None
    ):
        await self._run(interaction, "unwarn", member, warn_id, reason=reason)

    # ------------------------------------------------------------ Mutes cog

    async def _mute_like(self, interaction, command_name, member, duration, reason):
        # Red's mute commands take their time and reason as one combined value.
        try:
            length = to_timedelta(duration)
        except commands.BadArgument as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        time_and_reason = {}
        if length:
            time_and_reason["duration"] = length
        if reason:
            time_and_reason["reason"] = reason
        await self._run(interaction, command_name, [member], time_and_reason=time_and_reason)

    @app_commands.command(name="mute", description="Mute a member (uses the server's Red mute settings)")
    @app_commands.describe(member="Who to mute", duration="How long, e.g. 10m, 2h, 1d (blank = default)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        await self._mute_like(interaction, "mute", member, duration, reason)

    @app_commands.command(name="timeout", description="Time out a member (Discord's built-in timeout)")
    @app_commands.describe(member="Who to time out", duration="How long, e.g. 10m, 2h, 1d (max 28d)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        await self._mute_like(interaction, "timeout", member, duration, reason)

    @app_commands.command(name="unmute", description="Unmute a member")
    @app_commands.describe(member="Who to unmute", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        await self._run(interaction, "unmute", [member], reason=reason)

    @app_commands.command(name="mutechannel", description="Mute a member in this channel only")
    @app_commands.describe(member="Who to mute here", duration="How long, e.g. 10m, 2h, 1d (blank = default)", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    async def mutechannel(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        await self._mute_like(interaction, "mutechannel", member, duration, reason)

    @app_commands.command(name="unmutechannel", description="Unmute a member in this channel")
    @app_commands.describe(member="Who to unmute here", reason="Why")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    async def unmutechannel(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        await self._run(interaction, "unmutechannel", [member], reason=reason)

    # ------------------------------------------------------------ purge

    @app_commands.command(name="purge", description="Delete recent messages in this channel")
    @app_commands.describe(amount="How many messages to check (1-200)", user="Only delete this person's messages")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 200],
        user: Optional[discord.Member] = None,
    ):
        # Written directly instead of running Red's cleanup: those commands also
        # delete the `!` command message, which a slash command doesn't have.
        member = interaction.user
        allowed = member.guild_permissions.manage_messages or await self.bot.is_mod(member)
        if not allowed:
            await interaction.response.send_message("You don't have permission to do that.", ephemeral=True)
            return
        channel = interaction.channel
        me = interaction.guild.me
        if not channel.permissions_for(me).manage_messages:
            await interaction.response.send_message("I need **Manage Messages** in this channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        check = (lambda m: m.author.id == user.id) if user else (lambda m: not m.pinned)
        try:
            deleted = await channel.purge(
                limit=amount, check=check, reason=f"/purge by {member} ({member.id})"
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"Couldn't delete messages: {e.text or e.status}", ephemeral=True)
            return
        log.info("%s (%s) purged %s messages in #%s", member, member.id, len(deleted), channel)
        who = f" from {user.display_name}" if user else ""
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages{who}.", ephemeral=True)
