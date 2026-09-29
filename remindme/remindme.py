"""Reminders that come back as a reply in the same channel.

Ways to set one:
- `/remindme when what`      posts a public "⏰ X set a reminder" message; the
                             reminder later replies to it.
- `!remindme 2h what`        reacts ✅; the reminder later replies to that message
                             (or, if `!remindme` was sent as a reply, to the
                             message it replied to).
- bare `!remindme`           replies with a "⏰ Set a reminder" button (deleted
                             after 30 s) that opens a form.
- right-click a message → Apps → "Remind me about this": opens the same form;
                             the reminder replies to that message.

Reminders are saved to Red's config (so they survive restarts) and deleted once
they fire. A background loop checks every 15 seconds for due ones.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.commands.converter import parse_timedelta

log = logging.getLogger("red.refbot.remindme")

MAX_PER_USER = 25  # active reminders per person, per server
MAX_NOTE = 500
MAX_WAIT = timedelta(days=365)
CHECK_EVERY = 15  # seconds between checks for due reminders
TIME_HELP = "Use a length like `30m`, `2h`, `3d`, `1w` or `2h30m`."


def parse_when(text: Optional[str]) -> Optional[timedelta]:
    """"2h", "30m", "1d12h" -> timedelta. A bare number means minutes. None if unreadable."""
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():
        text += "m"
    try:
        delta = parse_timedelta(text, maximum=MAX_WAIT, minimum=timedelta(minutes=1))
    except commands.BadArgument:
        return None
    return delta


class ReminderForm(discord.ui.Modal, title="Set a reminder"):
    """The pop-up form used by the button and the right-click app."""

    when = discord.ui.TextInput(label="When?", placeholder="e.g. 30m, 2h, 3d, 1w", max_length=20)
    what = discord.ui.TextInput(
        label="Remind you about what?",
        placeholder="e.g. check the injury report",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=MAX_NOTE,
    )

    def __init__(self, cog: "RemindMe", channel_id: int, reply_to: Optional[int]):
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id
        self.reply_to = reply_to

    async def on_submit(self, interaction: discord.Interaction):
        delta = parse_when(self.when.value)
        if delta is None:
            await interaction.response.send_message(f"I couldn't read that time. {TIME_HELP}", ephemeral=True)
            return
        error = await self.cog.add_reminder(
            interaction.guild, interaction.user, self.channel_id, self.reply_to, delta, self.what.value
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        due = int(time.time() + delta.total_seconds())
        await interaction.response.send_message(f"⏰ Got it, I'll remind you <t:{due}:R>.", ephemeral=True)


class SetReminderButton(discord.ui.View):
    """Shown for a bare `!remindme`. Only the person who typed it can use it."""

    def __init__(self, cog: "RemindMe", owner_id: int, channel_id: int, reply_to: Optional[int]):
        super().__init__(timeout=30)
        self.cog, self.owner_id, self.channel_id, self.reply_to = cog, owner_id, channel_id, reply_to

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("That button's for someone else. Type `!remindme` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Set a reminder", emoji="⏰", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReminderForm(self.cog, self.channel_id, self.reply_to))


class RemindMe(commands.Cog):
    """Reminders that come back as a reply in the same channel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D6, force_registration=True)
        # [{"user": id, "channel": id, "reply_to": message id or None, "due": unix time,
        #   "note": str, "created": unix time}, ...]
        self.config.register_guild(reminders=[])
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()  # the loop and commands edit the same list
        # Right-click a message → Apps → "Remind me about this".
        self.menu = app_commands.ContextMenu(name="Remind me about this", callback=self.remind_about_message)
        self.menu.guild_only = True

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)
        if self._task:
            self._task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        # Reminders are user data: remove every reminder this person set.
        async with self._lock:
            for guild_id, conf in (await self.config.all_guilds()).items():
                if any(r["user"] == user_id for r in conf["reminders"]):
                    async with self.config.guild_from_id(guild_id).reminders() as reminders:
                        reminders[:] = [r for r in reminders if r["user"] != user_id]

    # ------------------------------------------------------------ storing

    async def add_reminder(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        channel_id: int,
        reply_to: Optional[int],
        delta: timedelta,
        note: str,
    ) -> Optional[str]:
        """Save a reminder. Returns an error message, or None if it worked."""
        async with self._lock:
            async with self.config.guild(guild).reminders() as reminders:
                if sum(1 for r in reminders if r["user"] == user.id) >= MAX_PER_USER:
                    return f"You already have {MAX_PER_USER} reminders. Cancel one with `!reminders cancel <number>`."
                now = time.time()
                reminders.append({
                    "user": user.id,
                    "channel": channel_id,
                    "reply_to": reply_to,
                    "due": now + delta.total_seconds(),
                    "note": (note or "").strip()[:MAX_NOTE],
                    "created": now,
                })
        return None

    # ------------------------------------------------------------ firing

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder check failed")
            await asyncio.sleep(CHECK_EVERY)

    async def _fire_due(self) -> None:
        now = time.time()
        for guild_id, conf in (await self.config.all_guilds()).items():
            due = [r for r in conf["reminders"] if r["due"] <= now]
            if not due:
                continue
            async with self._lock:
                async with self.config.guild_from_id(guild_id).reminders() as reminders:
                    reminders[:] = [r for r in reminders if r["due"] > now]
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            for reminder in due:
                try:
                    await self._deliver(guild, reminder)
                except Exception:
                    log.exception("Couldn't deliver a reminder in guild %s", guild_id)

    async def _deliver(self, guild: discord.Guild, reminder: dict) -> None:
        channel = guild.get_channel_or_thread(reminder["channel"])
        if channel is None:
            return  # channel deleted: nowhere to post
        note = reminder["note"]
        text = f"⏰ <@{reminder['user']}>, reminder" + (f": {note}" if note else "!")
        # Only ping the person the reminder is for, whatever the note says.
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(reminder["user"])])
        if reminder["reply_to"]:
            try:
                target = await channel.fetch_message(reminder["reply_to"])
                await target.reply(text, mention_author=False, allowed_mentions=mentions)
                return
            except discord.HTTPException:
                pass  # original message deleted: post in the channel instead
        await channel.send(text, allowed_mentions=mentions)

    # ------------------------------------------------------------ commands

    @commands.hybrid_command(name="remindme")
    @commands.guild_only()
    @app_commands.describe(when="How long from now, e.g. 30m, 2h, 3d, 1w", what="What to remind you about")
    async def remindme(self, ctx: commands.Context, when: Optional[str] = None, *, what: Optional[str] = None):
        """Set a reminder, e.g. `[p]remindme 2h check the injury report`.

        Send it as a reply to a message and the reminder will reply to that message.
        """
        delta = parse_when(when)

        if ctx.interaction is not None:  # /remindme
            if delta is None:
                problem = f"I couldn't read `{when}`." if when else "Add a time."
                await ctx.send(f"{problem} {TIME_HELP}", ephemeral=True)
                return
            due = int(time.time() + delta.total_seconds())
            # A public anchor in the channel; the reminder will reply to it.
            anchor = await ctx.send(
                f"⏰ **{ctx.author.display_name}** set a reminder · <t:{due}:R>" + (f"\n-# {what}" if what else ""),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            error = await self.add_reminder(ctx.guild, ctx.author, ctx.channel.id, anchor.id, delta, what or "")
            if error:
                await anchor.edit(content=error)
            return

        # !remindme: reply to the message it replied to, or else to the !remindme itself.
        reply_to = ctx.message.reference.message_id if ctx.message.reference else ctx.message.id

        if when is None:  # bare !remindme: offer the form
            view = SetReminderButton(self, ctx.author.id, ctx.channel.id, reply_to)
            await ctx.reply("Want a reminder about this?", view=view, delete_after=30, mention_author=False)
            return
        if delta is None:
            await ctx.reply(f"I couldn't read `{when}`. {TIME_HELP}", delete_after=15, mention_author=False)
            return
        error = await self.add_reminder(ctx.guild, ctx.author, ctx.channel.id, reply_to, delta, what or "")
        if error:
            await ctx.reply(error, delete_after=15, mention_author=False)
            return
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    async def remind_about_message(self, interaction: discord.Interaction, message: discord.Message):
        """Right-click → Apps → Remind me about this."""
        await interaction.response.send_modal(ReminderForm(self, message.channel.id, message.id))

    @commands.hybrid_group(name="reminders", invoke_without_command=True, autohelp=False)
    @commands.guild_only()
    async def reminders(self, ctx: commands.Context):
        """List your pending reminders."""
        await self._list(ctx)

    @reminders.command(name="list")
    async def reminders_list(self, ctx: commands.Context):
        """List your pending reminders."""
        await self._list(ctx)

    async def _mine(self, guild: discord.Guild, user_id: int) -> list[dict]:
        reminders = await self.config.guild(guild).reminders()
        return sorted((r for r in reminders if r["user"] == user_id), key=lambda r: r["due"])

    async def _list(self, ctx: commands.Context):
        mine = await self._mine(ctx.guild, ctx.author.id)
        if not mine:
            await ctx.send("You have no reminders set.", ephemeral=True)
            return
        def jump(r: dict) -> str:
            # Link to the message the reminder will reply to (if any).
            if not r["reply_to"]:
                return ""
            return f" · [jump](https://discord.com/channels/{ctx.guild.id}/{r['channel']}/{r['reply_to']})"

        lines = [
            f"**{n}.** <t:{int(r['due'])}:R> in <#{r['channel']}>{jump(r)}" + (f": {r['note'][:80]}" if r["note"] else "")
            for n, r in enumerate(mine, start=1)
        ]
        lines.append(f"-# Cancel one with `{ctx.clean_prefix}reminders cancel <number>`")
        await ctx.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @reminders.command(name="cancel")
    @app_commands.describe(number="The reminder's number from the list")
    async def reminders_cancel(self, ctx: commands.Context, number: int):
        """Cancel one of your reminders by its number in `[p]reminders`."""
        async with self._lock:
            mine = await self._mine(ctx.guild, ctx.author.id)
            if not 1 <= number <= len(mine):
                await ctx.send(f"You don't have a reminder #{number}. See `{ctx.clean_prefix}reminders`.", ephemeral=True)
                return
            target = mine[number - 1]
            async with self.config.guild(ctx.guild).reminders() as reminders:
                reminders[:] = [r for r in reminders if r != target]
        await ctx.send(f"Cancelled reminder #{number}.", ephemeral=True)
