"""Slash command versions of Red's moderation commands.

Red's built-in mod cogs (Mod, Warnings, Mutes) only have `!` commands. Each
slash command here runs the *same* Red command underneath, so modlog cases,
warning points, mute settings, DMs and permissions all behave exactly like the
`!` version. Only `/purge` is written directly (see `purge` for why).

Also bridges Defender (x26-Cogs): `/alert`, a right-click "Alert staff" app,
and a report reaction (a custom emoji) whose effect depends on the reactor's
Defender rank.

Who can use them: Red's own checks (mod/admin roles or Discord permissions)
decide, same as `!`. Discord also hides each command from members without a
matching permission; server admins can change who sees them in Server
Settings → Integrations → the bot.
"""

from __future__ import annotations

import asyncio
import logging
import re
from copy import copy
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands, modlog
from redbot.core.bot import Red
from redbot.core.commands.converter import parse_timedelta
from redbot.core.utils.chat_formatting import pagify
from redbot.core.utils.mod import get_audit_reason

log = logging.getLogger("red.refbot.modslash")


# The "delete their messages" option on /ban and /tempban. A labelled menu, because
# a bare "days" number was easy to mistake for the ban's length.
DELETE_CHOICES = [
    app_commands.Choice(name="Don't delete anything", value=0),
    app_commands.Choice(name="Last 24 hours", value=1),
    app_commands.Choice(name="Last 3 days", value=3),
    app_commands.Choice(name="Last 7 days", value=7),
]


# Default wording for the DMs below. Admins can change them from Discord
# (`unbaninvite message` / `mutenotice message`); these are used until they do.
UNBAN_DEFAULT = "You've been unbanned from **{server}**. Here's an invite back: {invite}"
MUTE_DEFAULT = ("You've been {action} in {where} {until}. Think this was a mistake? "
                "DM **Refbot Modmail** and the mods will take a look.")


class _Blanks(dict):
    """For str.format_map: leaves unknown {placeholders} as they are instead of erroring."""

    def __missing__(self, key):
        return "{" + key + "}"


def fill(template: str, **values) -> str:
    return template.format_map(_Blanks(values))


def until_text(when: Optional[datetime]) -> str:
    """"until <Discord timestamp>" (shown in the reader's own time zone), or open-ended."""
    if when is None:
        return "until a mod lifts it"
    stamp = int(when.timestamp())
    return f"until <t:{stamp}:f> (<t:{stamp}:R>)"


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


END_TIMEOUT_ID = "refbot_modslash:end_timeout"  # fixed ids so old log posts keep working
UNMUTE_ID = "refbot_modslash:unmute"


class EndTimeoutButton(discord.ui.Button):
    """The "End timeout" button added to ExtendedModLog's timeout posts."""

    def __init__(self, cog: "ModSlash", label: str = "End timeout", disabled: bool = False):
        # A finished button gets no id: it can't be clicked, and a fixed id on it
        # could only cause trouble (see pickem's GuessButton).
        kwargs = {} if disabled else {"custom_id": END_TIMEOUT_ID}
        super().__init__(label=label, emoji="⏹️", style=discord.ButtonStyle.secondary, disabled=disabled, **kwargs)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.end_timeout(interaction)


class UnmuteButton(discord.ui.Button):
    """The "Unmute" button added to Red's modlog posts for server and channel mutes."""

    def __init__(self, cog: "ModSlash", label: str = "Unmute", disabled: bool = False):
        kwargs = {} if disabled else {"custom_id": UNMUTE_ID}  # see EndTimeoutButton
        super().__init__(label=label, emoji="🔊", style=discord.ButtonStyle.secondary, disabled=disabled, **kwargs)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.unmute_from_case(interaction)


def one_button(button: discord.ui.Button) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(button)
    return view


class ModSlash(commands.Cog):
    """Slash versions of Red's ban, kick, warn, mute and more."""

    def __init__(self, bot: Red):
        self.bot = bot
        # Right-click a message → Apps → "Alert staff". Context menus can't be
        # declared inside a cog class like slash commands, so it's built here
        # and added to the bot in cog_load.
        self.alert_menu = app_commands.ContextMenu(name="Alert staff", callback=self.alert_from_message)
        self.alert_menu.guild_only = True
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D5, force_registration=True)
        self.config.register_guild(
            report_enabled=False,
            # The report emoji: a custom server emoji (by id) OR a standard one like 🛎️.
            report_emoji_id=None,
            report_emoji_unicode=None,
            report_blocked=[],  # member ids whose report reactions are ignored (false reporters)
            # Who can report with the reaction: "coaches" = mods + Defender helper roles
            # (Assistant Coach) only; "members" = anyone Defender ranks 1-2.
            report_who="coaches",
            # Auto-hide: remove a message once this many different members report it,
            # but ONLY if its author is a new account (Defender Rank 3-4). 0 = off.
            auto_hide_threshold=3,
            # DM people an invite back when they're unbanned (e.g. an approved ban
            # appeal), except where Red already sends its own (see on_member_unban).
            unban_invite=True,
            unban_message=None,  # None = UNBAN_DEFAULT
            # DM people who get muted or timed out, pointing them to Modmail.
            mute_notice=True,
            mute_message=None,  # None = MUTE_DEFAULT
        )
        self._review_notes: dict[int, discord.Message] = {}  # reported message id -> our public note
        self._reporters: dict[int, set[int]] = {}
        self._mute_notified: dict[tuple[int, int], datetime] = {}  # (guild, user) -> last mute DM  # reported message id -> ids of members who reported it

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.alert_menu)
        # Answers the "End timeout" / "Unmute" buttons on log posts, including ones
        # posted before a restart. Never sent itself; every post gets its own copy.
        self._log_buttons_view = discord.ui.View(timeout=None)
        self._log_buttons_view.add_item(EndTimeoutButton(self))
        self._log_buttons_view.add_item(UnmuteButton(self))
        self.bot.add_view(self._log_buttons_view)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.alert_menu.name, type=self.alert_menu.type)
        self._log_buttons_view.stop()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        # The only user data here is the report block list (member IDs).
        # (The underlying Red cogs manage their own data.)
        for guild_id, conf in (await self.config.all_guilds()).items():
            if user_id in conf.get("report_blocked", []):
                async with self.config.guild_from_id(guild_id).report_blocked() as blocked:
                    blocked.remove(user_id)

    async def _run(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *args,
        prepare: Optional[Callable[[commands.Context], Awaitable[None]]] = None,
        public: bool = False,
        header: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Run an existing Red text command on behalf of a slash command.

        `prepare(ctx)` can adjust the context first (used by /alert).
        Replies are private ("Only you can see this") unless `public` (used by
        /8ball); `header` is added above the first reply (e.g. the question).
        """
        command = self.bot.get_command(command_name)
        if command is None or command.cog is None:
            cog = {"warn": "Warnings", "warnings": "Warnings", "unwarn": "Warnings",
                   "mute": "Mutes", "unmute": "Mutes", "timeout": "Mutes",
                   "mutechannel": "Mutes", "unmutechannel": "Mutes",
                   "alert": "Defender", "8": "General"}.get(command_name, "Mod")
            await interaction.response.send_message(
                f"That needs Red's **{cog}** cog, which isn't loaded (`!load {cog.lower()}`).", ephemeral=True
            )
            return

        # Mod actions can take a few seconds (DMs, modlog); Discord only waits 3.
        # ephemeral = "Only you can see this": confirmations stay private to the mod.
        await interaction.response.defer(ephemeral=not public, thinking=True)
        ctx = await self.bot.get_context(interaction)
        ctx.command = command
        ctx.invoked_with = command.name
        ctx.modslash = True  # lets the Mutes patch below recognise our slash commands
        if prepare is not None:
            await prepare(ctx)
        if command.cog.qualified_name == "Mutes":
            self._patch_mute_issues(command.cog)

        # Red commands often confirm with a ✅ reaction on the command message.
        # A slash command has no real message, so that silently does nothing.
        # Track whether the command said anything, and say "Done" if not.
        # Every reply is forced private, since some Red commands send several.
        replied = False
        original_send = ctx.send

        async def tracked_send(content=None, **kw):
            nonlocal replied
            if header and not replied:
                content = f"{header}\n{content}" if content else header
            replied = True
            kw["ephemeral"] = not public
            return await original_send(content, **kw)

        ctx.send = tracked_send

        # Announce the command like Red does for `!` commands, so loggers that
        # listen for it (e.g. ExtendedModLog's "commands used" log) record it.
        # discord.py also announces before checking permissions, so do the same.
        self.bot.dispatch("command", ctx)
        try:
            if not await command.can_run(ctx):
                raise commands.CheckFailure()
            # Respect the command's cooldown like `!` does (e.g. !alert: once per
            # channel every 2 minutes). Running a command from code skips it otherwise.
            command._prepare_cooldowns(ctx)
            await ctx.invoke(command, *args, **kwargs)
            self.bot.dispatch("command_completion", ctx)
        except commands.CheckFailure as e:
            await ctx.send(str(e) or "You don't have permission to do that.")
        except commands.CommandError as e:
            await ctx.send(str(e) or "That didn't work.")
        except Exception:
            log.exception("/%s failed", command_name)
            await ctx.send("Something went wrong running that. It's in the bot's log.")
        if not replied:
            await ctx.send("✅ Done.")

    @staticmethod
    def _patch_mute_issues(mutes) -> None:
        """When a mute partly fails, Red asks "see who, where and why?" and waits
        for a ✅/❎ reaction. Private (ephemeral) replies can't have reactions, so
        for our slash commands, skip the question and just show the details.
        `!mute` is untouched. Re-applied if the Mutes cog is reloaded."""
        if getattr(mutes, "_modslash_patched", False):
            return
        original = mutes.handle_issues

        async def handle_issues(ctx, issue_list):
            if not getattr(ctx, "modslash", False):
                return await original(ctx, issue_list)
            await ctx.send("Some users couldn't be fully muted or unmuted:")
            for page in pagify(mutes.parse_issues(issue_list)):
                await ctx.send(page)

        mutes.handle_issues = handle_issues
        mutes._modslash_patched = True

    # ------------------------------------------------------------ Mod cog

    @app_commands.command(name="ban", description="Ban a user permanently (use /tempban for a set time)")
    @app_commands.describe(
        user="Who to ban (works for people not in the server too)",
        reason="Why (shown to them in the ban DM)",
        delete_messages="Also delete their recent messages? (default: keep them)",
    )
    @app_commands.choices(delete_messages=DELETE_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: Optional[str] = None,
        delete_messages: Optional[app_commands.Choice[int]] = None,
    ):
        # Red's ban takes a member, or a plain user ID for people not in the server.
        target = user if isinstance(user, discord.Member) else user.id
        days = delete_messages.value if delete_messages else None
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
        duration="How long the ban lasts, e.g. 1d, 12h, 1w (default: Red's tempban setting)",
        reason="Why (shown to them in the ban DM)",
        delete_messages="Also delete their recent messages? (default: keep them)",
    )
    @app_commands.choices(delete_messages=DELETE_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def tempban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: Optional[str] = None,
        reason: Optional[str] = None,
        delete_messages: Optional[app_commands.Choice[int]] = None,
    ):
        days = delete_messages.value if delete_messages else None
        try:
            length = to_timedelta(duration)
        except commands.BadArgument as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self._run(interaction, "tempban", member, length, days, reason=reason)

    @app_commands.command(name="softban", description="Kick someone AND delete their last day of messages (ban + instant unban)")
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

    @app_commands.command(name="slowmode", description="Set slowmode in this channel")
    @app_commands.describe(interval="Time between messages, e.g. 30s, 5m, 1h (0 turns it off, max 6h)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, interval: str):
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
        # Build exactly what Red's own MuteTime converter produces. Red reads the
        # end time from "until"; with only "duration", a timeout silently did
        # nothing and a mute had no expiry.
        time_and_reason = {}
        if length:
            if length <= timedelta(0):
                await interaction.response.send_message("The duration has to be longer than 0.", ephemeral=True)
                return
            time_and_reason["duration"] = length
            time_and_reason["until"] = interaction.created_at + length
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

    # ------------------------------------------------------------ General (fun)

    @app_commands.command(name="8ball", description="Ask the Magic 8-Ball a question")
    @app_commands.describe(question="Your question (end it with a ?)")
    @app_commands.guild_only()
    async def eightball(self, interaction: discord.Interaction, question: str):
        # Runs Red's own !8ball (General cog), so the answers and the "?" rule are
        # the same. Public, and the question is shown because slash commands hide
        # what was typed. No mentions from the question can ping anyone.
        question = discord.utils.escape_mentions(question.strip())[:200]
        await self._run(interaction, "8", question=question, public=True, header=f"🎱 **{question}**")

    # ------------------------------------------------------------ Defender

    async def _alert_about(self, interaction: discord.Interaction, target: Optional[discord.Message]) -> None:
        """Run Defender's alert, with its "Click to jump" link pointing at `target`.

        Defender links to the command message; slash commands and right-click
        apps have none, so we hand it a copy of `target`, credited to the person
        raising the alert.
        """

        async def point_at_target(ctx: commands.Context) -> None:
            if target is not None:
                stand_in = copy(target)
                stand_in.author = interaction.user
                ctx.message = stand_in

        await self._run(interaction, "alert", prepare=point_at_target)

    @app_commands.command(name="alert", description="Alert the staff (Defender)")
    @app_commands.guild_only()
    async def alert(self, interaction: discord.Interaction):
        # No default_permissions: Defender's helper role (e.g. Assistant Coach)
        # usually has no mod permissions. Defender itself checks who's allowed.
        # A slash command can't be a reply, so link to the channel's latest message.
        try:
            latest = [m async for m in interaction.channel.history(limit=1)]
        except discord.HTTPException:
            latest = []
        await self._alert_about(interaction, latest[0] if latest else None)

    async def alert_from_message(self, interaction: discord.Interaction, message: discord.Message):
        """Right-click → Apps → Alert staff: the alert links to that exact message."""
        await self._alert_about(interaction, message)

    # ------------------------------------------------------------ report reaction

    # Public note on a reported message. New accounts get the cautionary one
    # (spam bots asking people to friend/DM them); nobody established is labelled.
    REVIEW_NOTE = "🚩 Reported to the mods."
    REVIEW_NOTE_NEW_ACCOUNT = "⚠️ Reported to the mods. Treat with caution."
    REMOVED_NOTE = "🧹 Removed after multiple reports."

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Reacting with the report emoji reports that message, based on Defender rank:

        By default only mods and Defender helpers (Assistant Coaches) can report
        (`reportset who`); everyone else's reaction is just removed. Otherwise:
        Rank 1 (mods, Defender helper/trusted roles): Defender's full alert, like /alert.
        Rank 2 (established members): a quiet "member report" to Defender's notify
                channel, no staff ping, no emergency mode.
        Rank 3-4 (recent joins / new accounts) and blocked members: ignored.
        The reaction is removed right away. A reported message gets one public
        reply saying it's being reviewed (it never says who reported it).
        """
        if payload.guild_id is None or payload.member is None:
            return
        if payload.member.bot:
            return
        conf = await self.config.guild_from_id(payload.guild_id).all()
        if not conf["report_enabled"] or not self._is_report_emoji(payload.emoji, conf):
            return
        guild = payload.member.guild
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        defender = self.bot.get_cog("Defender")
        channel = guild.get_channel_or_thread(payload.channel_id)
        if defender is None or channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        member = payload.member

        # Take the reaction off first, so nobody sees who reported.
        try:
            await message.remove_reaction(payload.emoji, member)
        except discord.HTTPException:
            log.warning("Couldn't remove a report reaction in #%s (needs Manage Messages)", channel)

        if member.id in conf["report_blocked"]:
            log.info("Ignored report reaction from blocked member %s", member.id)
            return
        if conf["report_who"] == "coaches" and not await self._can_report(defender, member):
            log.info("Ignored report reaction from %s (only coaches and mods can report)", member.id)
            return
        rank = int(await defender.rank_user(member))
        if rank <= 2:
            self._reporters.setdefault(message.id, set()).add(member.id)
        if rank == 1:
            if not await self._reaction_alert(member, message):
                # Alert on cooldown (staff were pinged about this channel moments
                # ago): still flag this message, quietly.
                await self._member_report(defender, member, message, note="Raised while an alert was already active")
        elif rank == 2:
            await self._member_report(defender, member, message)
        else:
            log.info("Ignored report reaction from new member %s (Defender rank %s)", member.id, rank)
            return
        await self._mark_under_review(message, await self._author_is_new(defender, message))
        await self._maybe_auto_hide(defender, message, conf["auto_hide_threshold"])

    async def _can_report(self, defender, member: discord.Member) -> bool:
        """Mods, admins and Defender's helper roles (Assistant Coach)."""
        if member.guild_permissions.administrator or member.id == member.guild.owner_id:
            return True
        return await self.bot.is_mod(member) or await defender.is_helper(member)

    @staticmethod
    def _is_report_emoji(emoji: discord.PartialEmoji, conf: dict) -> bool:
        if conf["report_emoji_id"]:
            return emoji.id == conf["report_emoji_id"]
        if conf["report_emoji_unicode"] and emoji.id is None:
            # Discord sometimes adds/drops the invisible "emoji style" marker (U+FE0F).
            strip = lambda text: (text or "").replace("\ufe0f", "")
            return strip(emoji.name) == strip(conf["report_emoji_unicode"])
        return False

    @staticmethod
    async def _author_is_new(defender, message: discord.Message) -> bool:
        """Is this message from a new account (Defender Rank 3-4, or someone who
        already left)? Used for the cautionary note and for auto-hide.
        Link reposts (webhooks) and bots never count as new accounts."""
        if message.webhook_id is not None or message.author.bot:
            return False
        author = message.guild.get_member(message.author.id)
        if author is None:
            return True  # already left the server: typical of spam bots
        return int(await defender.rank_user(author)) >= 3

    async def _mark_under_review(self, message: discord.Message, new_account: bool) -> None:
        """Reply once to the reported message so everyone (including the reporter)
        can see it's being looked at."""
        if message.id in self._review_notes:
            return
        try:
            self._review_notes[message.id] = await message.reply(
                self.REVIEW_NOTE_NEW_ACCOUNT if new_account else self.REVIEW_NOTE,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't post the review note in #%s: %r", message.channel, e)

    async def _maybe_auto_hide(self, defender, message: discord.Message, threshold: int) -> None:
        """Remove a reported message once enough members report it, but only if
        it's from a new account. Established members can never be silenced this way."""
        reporters = self._reporters.get(message.id, set())
        if not threshold or len(reporters) < threshold:
            return
        if not await self._author_is_new(defender, message):
            return  # established members, link reposts and bots: report only, never auto-hide
        try:
            await message.delete()
        except discord.HTTPException as e:
            log.warning("Auto-hide couldn't delete %s: %r", message.jump_url, e)
            return
        self._reporters.pop(message.id, None)
        await defender.send_notification(
            message.guild,
            f"A message by {message.author.mention} in {message.channel.mention} was removed "
            f"after {len(reporters)} reports.",
            title="🧹 • Auto-removed (reported spam)",
            fields=[
                {"name": "Author", "value": f"`{message.author}` ({message.author.id})"},
                # Listed so mods can spot a pile-on and `!reportset block` the people behind it.
                {"name": "Reported by", "value": ", ".join(f"<@{r}>" for r in sorted(reporters))[:1000], "inline": False},
                {"name": "Message", "value": (message.content or "(no text; attachment or embed)")[:1000], "inline": False},
            ],
            ping=False,
        )
        note = self._review_notes.get(message.id)
        if note is not None:
            try:
                await note.edit(content=self.REMOVED_NOTE)
            except discord.HTTPException:
                pass

    async def _reaction_alert(self, member: discord.Member, message: discord.Message) -> bool:
        """Run Defender's own alert command, as if `member` used it on `message`.
        Returns False if it didn't go out (e.g. the channel's alert cooldown)."""
        command = self.bot.get_command("alert")
        if command is None or command.cog is None:
            return False
        # A stand-in for the command message: the reported message, credited to
        # the person reporting it, so Defender's "Click to jump" goes to it.
        stand_in = copy(message)
        stand_in.author = member
        stand_in.content = "[report reaction]"  # what command loggers show instead of the reported text
        ctx = await self.bot.get_context(stand_in)
        ctx.command = command
        ctx.invoked_with = command.name

        # Defender would reply in the channel ("staff has been notified"); the
        # public review note replaces that, so swallow its replies.
        async def quiet_send(*args, **kwargs):
            return None

        ctx.send = quiet_send
        self.bot.dispatch("command", ctx)
        try:
            if not await command.can_run(ctx):
                return False
            command._prepare_cooldowns(ctx)
            await ctx.invoke(command)
            self.bot.dispatch("command_completion", ctx)
            return True
        except commands.CommandOnCooldown:
            return False
        except commands.CommandError as e:
            log.info("Reaction alert refused for %s: %r", member.id, e)
            return False
        except Exception:
            log.exception("Reaction alert failed")
            return False

    async def _member_report(self, defender, member: discord.Member, message: discord.Message, note: str = "") -> None:
        """A quiet report to Defender's notify channel (no staff ping)."""
        text = message.content or "(no text; attachment or embed)"
        fields = [
            {"name": "Reporter", "value": f"`{member}` ({member.id})"},
            {"name": "Reported user", "value": f"`{message.author}` ({message.author.id})"},
            # Kept in the report in case the message gets deleted.
            {"name": "Message", "value": text[:1000], "inline": False},
        ]
        if note:
            fields.append({"name": "Note", "value": note, "inline": False})
        await defender.send_notification(
            member.guild,
            f"{member.mention} reported a message by {message.author.mention} in {message.channel.mention}.",
            title="📣 • Member report",
            fields=fields,
            ping=False,
            jump_to=message,
            # One report per message every 6 hours, however many people react.
            heat_key=f"modslash-report-{message.id}",
            no_repeat_for=timedelta(hours=6),
        )

    # ------------------------------------------------------------ invite back on unban

    # Red's own unbans (`unban`, softban) are labelled like this in the audit log,
    # and Red sends those people an invite itself (Mod's "reinvite on unban").
    RED_REASON_PREFIX = "Action requested by "
    # Tempbans that run out: the invite was already in the tempban DM.
    RED_TEMPBAN_REASON = "Tempban finished"

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        """DM an invite back to anyone unbanned some other way: an approved ban
        appeal (the Appeals cog unbans directly, so Red's reinvite never runs) or a
        mod using Discord's own Unban button."""
        if user.bot or not await self.config.guild(guild).unban_invite():
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        reason = await self._unban_reason(guild, user)
        if reason and (reason.startswith(self.RED_REASON_PREFIX) or reason == self.RED_TEMPBAN_REASON):
            return  # Red handles these
        invite = await self._invite_for(guild)
        if not invite:
            log.warning("Couldn't make an invite to send %s after their unban (guild %s)", user.id, guild.id)
            return
        template = await self.config.guild(guild).unban_message() or UNBAN_DEFAULT
        try:
            await user.send(fill(template, server=guild.name, invite=invite))
        except discord.HTTPException:
            # Usually: DMs closed, or we no longer share a server with them.
            log.info("Couldn't DM an invite to %s after their unban (guild %s)", user.id, guild.id)

    @staticmethod
    async def _unban_reason(guild: discord.Guild, user: discord.User) -> Optional[str]:
        """The audit log reason for this unban, or None if we can't tell."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(2)  # the audit log entry can lag a moment behind the event
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.unban):
                if entry.target and entry.target.id == user.id and entry.created_at >= cutoff:
                    return entry.reason or ""
        except discord.HTTPException:
            pass
        return None

    @staticmethod
    async def _invite_for(guild: discord.Guild) -> Optional[str]:
        """A permanent invite if the server has one (or its vanity URL), else a new
        one-day invite. Same approach as Red's own reinvite."""
        me = guild.me.guild_permissions
        if me.manage_guild or me.administrator:
            if guild.vanity_url:
                return guild.vanity_url
            try:
                for inv in await guild.invites():
                    if not (inv.max_uses or inv.max_age or inv.temporary):
                        return inv.url
            except discord.HTTPException:
                pass
        for channel in [guild.rules_channel, guild.system_channel, *guild.text_channels]:
            if channel and channel.permissions_for(guild.me).create_instant_invite:
                try:
                    return (await channel.create_invite(max_age=86400, reason="Invite back after unban")).url
                except discord.HTTPException:
                    continue
        return None

    @commands.group(name="unbaninvite", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def unbaninvite(self, ctx: commands.Context, on_or_off: Optional[bool] = None):
        """DM people an invite back when they're unbanned (on by default).

        Covers approved ban appeals and Discord's own Unban button. Red's `unban`
        and softban send their own invite (`modset reinvite`), so they're skipped.
        Change the wording with `unbaninvite message`.
        """
        conf = self.config.guild(ctx.guild)
        if on_or_off is None:
            on_or_off = not await conf.unban_invite()
        await conf.unban_invite.set(on_or_off)
        await ctx.send("Unbanned people will get a DM with an invite back." if on_or_off
                       else "No more invite DMs on unban (Red's own `unban` still follows `modset reinvite`).")

    @unbaninvite.command(name="message")
    async def unbaninvite_message(self, ctx: commands.Context, *, text: Optional[str] = None):
        """Show or change the unban DM. Use {server} and {invite}; `reset` for the default."""
        await self._edit_template(ctx, "unban_message", UNBAN_DEFAULT, text, required=["{invite}"],
                                  example=dict(server=ctx.guild.name, invite="https://discord.gg/example"))

    # ------------------------------------------------------------ "End timeout" on log posts

    @staticmethod
    def _timed_out_member_id(message: discord.Message) -> Optional[int]:
        """If this is ExtendedModLog's "member updated" post for a timeout starting,
        the member's ID. (Its embed has an "After" field with "Timeout until: <time>"
        and a "Member ID" field.)"""
        if not message.embeds:
            return None
        fields = {f.name: f.value or "" for f in message.embeds[0].fields}
        after, member_id = fields.get("After", ""), fields.get("Member ID", "")
        if "Timeout until" not in after or "Timeout until: None" in after:
            return None
        digits = "".join(ch for ch in member_id if ch.isdigit())
        return int(digits) if digits else None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Add an "End timeout" button to ExtendedModLog's timeout posts."""
        if message.guild is None or message.author.id != self.bot.user.id:
            return
        if self._timed_out_member_id(message) is None:
            return
        if await self.bot.cog_disabled_in_guild(self, message.guild):
            return
        try:
            await message.edit(view=one_button(EndTimeoutButton(self)))
        except discord.HTTPException as e:
            log.info("Couldn't add the End timeout button: %r", e)

    async def end_timeout(self, interaction: discord.Interaction) -> None:
        guild, mod = interaction.guild, interaction.user
        if guild is None:
            return
        if not (mod.guild_permissions.moderate_members or await self.bot.is_mod(mod)):
            await interaction.response.send_message("Only mods can end timeouts.", ephemeral=True)
            return
        member_id = self._timed_out_member_id(interaction.message)
        member = guild.get_member(member_id) if member_id else None
        if member is None:
            await interaction.response.send_message("They're not in the server any more.", ephemeral=True)
            return
        if not member.is_timed_out():
            label = "Timeout already over"
        else:
            try:
                await member.edit(timed_out_until=None, reason=f"Timeout ended by {mod} ({mod.id}) from the log")
            except discord.HTTPException:
                await interaction.response.send_message(
                    "I couldn't end it. I need Timeout Members and my role above theirs.", ephemeral=True)
                return
            label = f"Timeout ended by {mod.display_name}"[:80]
        # Swap the button for a greyed-out note of who ended it.
        await interaction.response.edit_message(view=one_button(EndTimeoutButton(self, label=label, disabled=True)))

    async def _add_unmute_button(self, case) -> None:
        """Red posts the case to the modlog channel right after announcing it; wait
        for that post (up to ~10 s), then add the Unmute button to it."""
        for _ in range(10):
            await asyncio.sleep(1)
            if case.message is not None:
                break
        else:
            return  # no modlog channel set, or Red couldn't post
        try:
            await case.message.edit(view=one_button(UnmuteButton(self)))
        except discord.HTTPException as e:
            log.info("Couldn't add the Unmute button to case %s: %r", case.case_number, e)

    async def unmute_from_case(self, interaction: discord.Interaction) -> None:
        """Undo the mute in this modlog case using Red's own unmute (so roles,
        timeouts, channel permissions and Red's records are all handled), and log
        an unmute case like `unmute` does."""
        guild, mod = interaction.guild, interaction.user
        if guild is None:
            return
        if not await self.bot.is_mod(mod) and not mod.guild_permissions.administrator:
            await interaction.response.send_message("Only mods can unmute.", ephemeral=True)
            return
        mutes = self.bot.get_cog("Mutes")
        message = interaction.message
        text = (message.embeds[0].title or "") if message.embeds else (message.content or "")
        number = re.search(r"Case #(\d+)", text)
        if mutes is None or number is None:
            await interaction.response.send_message("I can't find that mute (is the Mutes cog loaded?).", ephemeral=True)
            return
        try:
            case = await modlog.get_case(int(number.group(1)), guild, self.bot)
        except RuntimeError:
            await interaction.response.send_message("That case no longer exists.", ephemeral=True)
            return
        user_id = case.user if isinstance(case.user, int) else case.user.id
        member = guild.get_member(user_id)
        if member is None:
            await interaction.response.send_message("They're not in the server any more.", ephemeral=True)
            return
        # Defer as an update of the log post itself, so editing the "original
        # response" below changes the post's button. Messages to the mod go out as
        # private follow-ups.
        await interaction.response.defer()
        reason = get_audit_reason(mod, "Unmuted from the modlog", shorten=True)
        channel = None
        if case.action_type == "cmute":
            channel_id = case.channel if isinstance(case.channel, int) else getattr(case.channel, "id", None)
            channel = guild.get_channel_or_thread(channel_id) if channel_id else None
            if channel is None:
                await interaction.followup.send("That channel is gone.", ephemeral=True)
                return
            result = await mutes.channel_unmute_user(guild, channel, mod, member, reason)
        else:
            result = await mutes.unmute_user(guild, mod, member, reason)
        if not result.success:
            await interaction.followup.send(result.reason or "Couldn't unmute them.", ephemeral=True)
            return
        await modlog.create_case(self.bot, guild, interaction.created_at,
                                 "cunmute" if channel else "sunmute", member, mod,
                                 "Unmuted from the modlog", until=None, channel=channel)
        await interaction.edit_original_response(
            view=one_button(UnmuteButton(self, label=f"Unmuted by {mod.display_name}"[:80], disabled=True)))
        await interaction.followup.send(f"Unmuted {member.mention}.", ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ mute notice

    async def _edit_template(self, ctx, key: str, default: str, text: Optional[str],
                             required: list[str], example: dict) -> None:
        value = self.config.guild(ctx.guild).get_attr(key)
        if text is None:
            current = await value() or default
            await ctx.send(f"Current message:\n>>> {current}", allowed_mentions=discord.AllowedMentions.none())
            return
        if text.strip().lower() == "reset":
            await value.set(None)
            text = default
        else:
            missing = [r for r in required if r not in text]
            if missing:
                await ctx.send(f"The message needs {', '.join(missing)} in it.")
                return
            if len(text) > 1500:
                await ctx.send("That's too long. Keep it under 1500 characters.")
                return
            await value.set(text)
        await ctx.send(f"Saved. It will look like this:\n>>> {fill(text, **example)}",
                       allowed_mentions=discord.AllowedMentions.none())

    async def _send_mute_notice(self, guild: discord.Guild, user: discord.abc.User,
                                until: Optional[datetime], channel: Optional[discord.abc.GuildChannel] = None,
                                action: str = "muted") -> None:
        """DM someone who just got muted or timed out, once per minute at most (Red's
        timeout both starts a Discord timeout and logs a mute, so this can fire twice)."""
        if user.bot or not await self.config.guild(guild).mute_notice():
            return
        if await self.bot.cog_disabled_in_guild(self, guild):
            return
        key = (guild.id, user.id)
        now = datetime.now(timezone.utc)
        last = self._mute_notified.get(key)
        if last and now - last < timedelta(minutes=1):
            return
        self._mute_notified[key] = now
        where = f"**{guild.name}**" if channel is None else f"#{channel.name} in **{guild.name}**"
        template = await self.config.guild(guild).mute_message() or MUTE_DEFAULT
        try:
            await user.send(fill(template, action=action, server=guild.name, where=where, until=until_text(until)))
        except discord.HTTPException:
            log.info("Couldn't DM a mute notice to %s (guild %s)", user.id, guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """A Discord timeout just started: from Red's timeout, Defender, or the
        Timeout button in Discord itself."""
        now = datetime.now(timezone.utc)
        started = after.timed_out_until and after.timed_out_until > now
        was = before.timed_out_until and before.timed_out_until > now
        if started and not was:
            await self._send_mute_notice(after.guild, after, after.timed_out_until, action="timed out")

    @commands.Cog.listener()
    async def on_modlog_case_create(self, case):
        """Red's mutes (server, channel and voice) log a case; that's our cue."""
        if case.action_type not in ("smute", "cmute", "vmute") or case.guild is None:
            return
        if case.action_type in ("smute", "cmute"):
            asyncio.create_task(self._add_unmute_button(case))
        user = case.user if isinstance(case.user, discord.abc.User) else self.bot.get_user(case.user)
        if user is None:
            return
        until = datetime.fromtimestamp(case.until, timezone.utc) if case.until else None
        channel = case.channel if case.action_type in ("cmute", "vmute") else None
        await self._send_mute_notice(case.guild, user, until, channel)

    @commands.group(name="mutenotice", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def mutenotice(self, ctx: commands.Context, on_or_off: Optional[bool] = None):
        """DM people who get muted or timed out, pointing them to Modmail (on by default).

        Covers Red's mutes and timeouts, Defender's automatic timeouts, and Discord's
        own Timeout button. Change the wording with `mutenotice message`.
        """
        conf = self.config.guild(ctx.guild)
        if on_or_off is None:
            on_or_off = not await conf.mute_notice()
        await conf.mute_notice.set(on_or_off)
        await ctx.send("Muted and timed out people will get a DM pointing them to Modmail." if on_or_off
                       else "No more mute notices.")

    @mutenotice.command(name="message")
    async def mutenotice_message(self, ctx: commands.Context, *, text: Optional[str] = None):
        """Show or change the mute DM. Use {action} ("muted" / "timed out"), {where} (or {server})
        and {until}; `reset` for the default."""
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await self._edit_template(ctx, "mute_message", MUTE_DEFAULT, text, required=[],
                                  example=dict(action="timed out", server=ctx.guild.name, where=f"**{ctx.guild.name}**",
                                               until=until_text(later)))

    @mutenotice.command(name="test")
    async def mutenotice_test(self, ctx: commands.Context):
        """DM yourself the mute notice as it is now (nobody gets muted)."""
        self._mute_notified.pop((ctx.guild.id, ctx.author.id), None)
        await self._send_mute_notice(ctx.guild, ctx.author, datetime.now(timezone.utc) + timedelta(hours=1),
                                     action="timed out")
        await ctx.send("Sent you a DM (if your DMs are open).")

    @commands.group(name="reportset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def reportset(self, ctx: commands.Context):
        """Report reaction settings (uses Defender)."""

    @reportset.command(name="emoji")
    async def reportset_emoji(self, ctx: commands.Context, emoji: str):
        """Set the emoji that reports a message: a custom server emoji or a standard one like 🛎️."""
        conf = self.config.guild(ctx.guild)
        try:
            custom = await commands.EmojiConverter().convert(ctx, emoji)
        except commands.BadArgument:
            custom = None
        if custom is not None:
            if custom.guild_id != ctx.guild.id:
                await ctx.send("Use an emoji from this server.")
                return
            await conf.report_emoji_id.set(custom.id)
            await conf.report_emoji_unicode.set(None)
            shown = str(custom)
        elif emoji.startswith("<") or any(ch.isalnum() for ch in emoji) or len(emoji) > 10:
            await ctx.send("That doesn't look like an emoji. Pick one from the emoji menu.")
            return
        else:
            await conf.report_emoji_unicode.set(emoji)
            await conf.report_emoji_id.set(None)
            shown = emoji
        await ctx.send(f"Reacting with {shown} will report a message. Turn it on with `{ctx.clean_prefix}reportset toggle`.")

    @reportset.command(name="toggle")
    async def reportset_toggle(self, ctx: commands.Context):
        """Turn the report reaction on or off."""
        conf = self.config.guild(ctx.guild)
        if not await conf.report_emoji_id() and not await conf.report_emoji_unicode():
            await ctx.send(f"Set the emoji first: `{ctx.clean_prefix}reportset emoji :youremoji:`")
            return
        enabled = not await conf.report_enabled()
        await conf.report_enabled.set(enabled)
        warning = ""
        if enabled and self.bot.get_cog("Defender") is None:
            warning = "\n⚠️ Defender isn't loaded, so reports won't go anywhere until it is."
        await ctx.send(f"Report reaction is now **{'on' if enabled else 'off'}**.{warning}")

    @reportset.command(name="block")
    async def reportset_block(self, ctx: commands.Context, member: discord.Member):
        """Ignore someone's report reactions (e.g. after false reports)."""
        async with self.config.guild(ctx.guild).report_blocked() as blocked:
            if member.id not in blocked:
                blocked.append(member.id)
        await ctx.send(f"{member.mention}'s report reactions will be ignored.",
                       allowed_mentions=discord.AllowedMentions.none())

    @reportset.command(name="unblock")
    async def reportset_unblock(self, ctx: commands.Context, member: discord.Member):
        """Let someone report again."""
        async with self.config.guild(ctx.guild).report_blocked() as blocked:
            if member.id in blocked:
                blocked.remove(member.id)
        await ctx.send(f"{member.mention} can report again.", allowed_mentions=discord.AllowedMentions.none())

    @reportset.command(name="who")
    async def reportset_who(self, ctx: commands.Context, who: str):
        """Who can report with the reaction: `coaches` (Assistant Coaches and mods) or `members` (everyone established)."""
        who = who.lower()
        if who not in ("coaches", "members"):
            await ctx.send("Use `coaches` (Assistant Coaches and mods only) or `members` (any established member).")
            return
        await self.config.guild(ctx.guild).report_who.set(who)
        await ctx.send("Only Assistant Coaches and mods can report now. Everyone else's reaction is just removed."
                       if who == "coaches" else
                       "Any established member can report now (brand new accounts are still ignored).")

    @reportset.command(name="autohide")
    async def reportset_autohide(self, ctx: commands.Context, reports: int):
        """How many different members' reports remove a new account's message (0 = off).

        Only messages from new accounts (Defender Rank 3-4) can be auto-removed.
        """
        if reports < 0 or reports > 20:
            await ctx.send("Use a number from 0 (off) to 20.")
            return
        await self.config.guild(ctx.guild).auto_hide_threshold.set(reports)
        if reports:
            await ctx.send(f"New accounts' messages will be removed after **{reports}** different members report them.")
        else:
            await ctx.send("Auto-hide is off. Reports still go to the mods.")

    @reportset.command(name="show")
    async def reportset_show(self, ctx: commands.Context):
        """Show the report reaction settings."""
        conf = await self.config.guild(ctx.guild).all()
        emoji = self.bot.get_emoji(conf["report_emoji_id"]) if conf["report_emoji_id"] else conf["report_emoji_unicode"]
        blocked = ", ".join(f"<@{i}>" for i in conf["report_blocked"]) or "nobody"
        await ctx.send(
            f"**Report reaction:** {'on' if conf['report_enabled'] else 'off'}\n"
            f"**Emoji:** {emoji or 'not set'}\n"
            f"**Who can report:** "
            f"{'Assistant Coaches (Defender helper roles) and mods' if conf['report_who'] == 'coaches' else 'any established member'}\n"
            f"**Blocked from reporting:** {blocked}\n"
            f"**Auto-hide:** "
            f"{str(conf['auto_hide_threshold']) + ' reports (new accounts only)' if conf['auto_hide_threshold'] else 'off'}\n"
            "**What it does:** coaches and mods = full alert (pings staff). With `who members`, "
            "established members = quiet report and new accounts = ignored. "
            "Reported messages get a public \"don't click links\" reply.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
