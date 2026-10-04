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

import logging
from copy import copy
from datetime import timedelta
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands.converter import parse_timedelta
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.refbot.modslash")


# The "delete their messages" option on /ban and /tempban. A labelled menu, because
# a bare "days" number was easy to mistake for the ban's length.
DELETE_CHOICES = [
    app_commands.Choice(name="Don't delete anything", value=0),
    app_commands.Choice(name="Last 24 hours", value=1),
    app_commands.Choice(name="Last 3 days", value=3),
    app_commands.Choice(name="Last 7 days", value=7),
]


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
        )
        self._review_notes: dict[int, discord.Message] = {}  # reported message id -> our public note
        self._reporters: dict[int, set[int]] = {}  # reported message id -> ids of members who reported it

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.alert_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.alert_menu.name, type=self.alert_menu.type)

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
