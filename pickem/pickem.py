"""Weekly NFL pick'em, straight up.

How a week goes:
- As soon as a week's last game is final (normally right after Monday night),
  the bot posts that week's results and a "Make your picks" panel for the next.
- The button (or `/pickem play`) opens a private picker: four games per page,
  one button per team. Picks save on click and each game locks at its kickoff.
- Once every game before Monday is final, anyone who could still finish tied
  for first gets pinged to guess Monday night's total points (see nfl.py).
- The results ping the winner and move the (optional) winner role to them.

The rules and scoring live in nfl.py. A background task started in cog_load
fetches ESPN every 2 to 30 minutes, depending on whether games are on.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.bot import Red

from . import nfl

log = logging.getLogger("red.refbot.pickem")

OPEN_ID = "refbot_pickem:open"  # fixed ids so old panels keep working after a restart
GUESS_ID = "refbot_pickem:guess"
PER_PAGE = 4  # games per picker page (one row each; the 5th row is for page buttons)
TABLE_SIZE = 10  # rows shown in standings
CARD_SIZE = 5  # rows shown on the results card
COLOR = discord.Color(0x4F2683)  # Vikings purple


def mention(uid: str) -> str:
    return f"<@{uid}>"


def names(uids: list[str]) -> str:
    """"@A", "@A and @B", "@A, @B and @C"."""
    parts = [mention(u) for u in uids]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def matchup(game: dict) -> str:
    return f"{game['away']} @ {game['home']}"


# ------------------------------------------------------------------ views


class PanelView(discord.ui.View):
    """The "Make your picks" button on the weekly panel."""

    def __init__(self, cog: "Pickem", timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.cog = cog

    @discord.ui.button(label="Make your picks", emoji="🏈", style=discord.ButtonStyle.primary, custom_id=OPEN_ID)
    async def open_picker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_picker(interaction)


class GuessView(discord.ui.View):
    """The button on the tiebreaker message."""

    def __init__(self, cog: "Pickem", timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.cog = cog

    @discord.ui.button(label="Enter my guess", emoji="🔢", style=discord.ButtonStyle.primary, custom_id=GUESS_ID)
    async def guess(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_guess(interaction)


class GuessForm(discord.ui.Modal, title="Tiebreaker guess"):
    points = discord.ui.TextInput(label="Total points scored", placeholder="e.g. 45", max_length=3)

    def __init__(self, cog: "Pickem", key: str, current: Optional[int]):
        super().__init__()
        self.cog, self.key = cog, key
        if current is not None:
            self.points.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.points.value.strip()
        if not text.isdigit() or int(text) > 200:
            await interaction.response.send_message("That needs to be a number of points, like 45.", ephemeral=True)
            return
        await self.cog.save_guess(interaction, self.key, int(text))


class PickButton(discord.ui.Button):
    def __init__(self, gid: str, team: str, **kwargs):
        super().__init__(**kwargs)
        self.gid, self.team = gid, team

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog.pick(interaction, self.view, self.gid, self.team)


class PageButton(discord.ui.Button):
    def __init__(self, step: int, **kwargs):
        super().__init__(**kwargs)
        self.step = step

    async def callback(self, interaction: discord.Interaction):
        self.view.page += self.step
        await self.view.cog.refresh_picker(interaction, self.view)


class PickerView(discord.ui.View):
    """One person's private picker. Rebuilt after every click."""

    def __init__(self, cog: "Pickem", uid: str, key: str, page: int):
        super().__init__(timeout=15 * 60)  # private messages' buttons expire anyway
        self.cog, self.uid, self.key, self.page = cog, uid, key, page

    def build(self, week: dict, now: datetime, logos: dict) -> None:
        self.clear_items()
        games = nfl.ordered(week["games"])
        pages = max(1, math.ceil(len(games) / PER_PAGE))
        self.page = max(0, min(self.page, pages - 1))
        picks = week["picks"].get(self.uid, {})
        for row, (gid, game) in enumerate(games[self.page * PER_PAGE:(self.page + 1) * PER_PAGE]):
            locked = nfl.is_locked(game, now)
            for side in ("away", "home"):
                team = game[side]
                picked = picks.get(gid) == team
                if game["winner"] not in (None, "TIE", "VOID") and picked:
                    style = discord.ButtonStyle.success if game["winner"] == team else discord.ButtonStyle.danger
                elif picked:
                    style = discord.ButtonStyle.primary
                else:
                    style = discord.ButtonStyle.secondary
                label = game[f"{side}_name"] if side == "away" else f"@ {game[f'{side}_name']}"
                self.add_item(PickButton(gid, team, label=label[:80], emoji=logos.get(team),
                                         style=style, disabled=locked, row=row))
        if pages > 1:
            self.add_item(PageButton(-1, label="Back", style=discord.ButtonStyle.secondary,
                                     disabled=self.page == 0, row=4))
            self.add_item(discord.ui.Button(label=f"Page {self.page + 1} of {pages}", disabled=True, row=4))
            self.add_item(PageButton(1, label="Next", style=discord.ButtonStyle.secondary,
                                     disabled=self.page >= pages - 1, row=4))


# ------------------------------------------------------------------ the cog


class Pickem(commands.Cog):
    """Weekly NFL pick'em."""

    pickem_slash = app_commands.Group(name="pickem", description="Weekly NFL pick'em", guild_only=True)

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5C0BE0A2D8, force_registration=True)
        self.config.register_guild(
            enabled=False,
            channel_id=None,  # where the panel, tiebreaker and results go
            role_id=None,  # optional weekly winner role
            weeks={},  # week key ("2026-2-4") -> week dict, see nfl.py
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # per server
        self._current: Optional[dict] = None  # the week being played (parsed ESPN data)
        # The week before it, while ESPN still calls it "this week" (from the end
        # of Monday night until ESPN switches early Wednesday). Only its final
        # results are needed from it.
        self._finishing: Optional[dict] = None
        self._fetch_failing = False
        self._logos: dict[str, discord.PartialEmoji] = {}  # the scoreboard's team logo emoji
        self._logos_loaded_for: Optional[str] = None
        self.interval = 300
        self.last_error: dict[int, str] = {}  # guild id -> last problem, shown in `pickemset show`
        # Registered "catch-all" views that answer old panels after a restart.
        # Never sent: every message gets a fresh copy (see guide.py for why).
        self._persistent = [PanelView(self), GuessView(self)]

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        for view in self._persistent:
            self.bot.add_view(view)
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        for view in self._persistent:
            view.stop()
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        uid = str(user_id)
        for guild_id in await self.config.all_guilds():
            async with self._locks[guild_id]:
                async with self.config.guild_from_id(guild_id).weeks() as weeks:
                    for week in weeks.values():
                        week["picks"].pop(uid, None)
                        tb = week.get("tb") or {}
                        tb.get("guesses", {}).pop(uid, None)
                        if uid in tb.get("contenders", []):
                            tb["contenders"].remove(uid)
                        if uid in week.get("winners", []):
                            week["winners"].remove(uid)

    # ------------------------------------------------------------ ESPN loop

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            self._wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error in pick'em loop")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        guilds = await self.config.all_guilds()
        active = [gid for gid, conf in guilds.items() if conf["enabled"] and conf["channel_id"]]
        if not active:
            self.interval = 3600
            return
        await self._fetch()
        now = datetime.now(timezone.utc)
        self.interval = self._next_interval(now)
        if self._current is None:
            return
        await self._load_logos()
        for gid in active:
            guild = self.bot.get_guild(gid)
            if guild is not None:
                try:
                    await self._update_guild(guild, now)
                except Exception:
                    log.exception("Pick'em update failed in guild %s", gid)

    async def _get_json(self, params: Optional[dict] = None) -> dict:
        async with self._session.get(nfl.ESPN_URL, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _fetch(self) -> None:
        try:
            data = await self._get_json()
            current, finishing = nfl.parse_week(data), None
            # Once every game of ESPN's week is final, move on to the next week
            # right away (ESPN itself waits until Wednesday).
            if current is None or all(nfl.is_done(g) for g in current["games"].values()):
                ahead = nfl.next_week(data)
                upcoming = nfl.parse_week(await self._get_json({"seasontype": ahead[0], "week": ahead[1]})) if ahead else None
                if upcoming is not None:
                    current, finishing = upcoming, current
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._fetch_failing:
                log.warning("ESPN fetch failed; keeping last data: %r", e)
                self._fetch_failing = True
            return
        if self._fetch_failing:
            log.info("ESPN fetch working again")
            self._fetch_failing = False
        self._current, self._finishing = current, finishing

    def _next_interval(self, now: datetime) -> int:
        """2 min while games are on (results and the tiebreaker prompt go out
        quickly), 15 min when a game is coming up, 30 min otherwise."""
        if self._current is None:
            return 300
        games = self._current["games"].values()
        if any(not nfl.is_done(g) and nfl.kickoff(g) <= now for g in games):
            return 120
        upcoming = [nfl.kickoff(g) for g in games if g["state"] == "pre" and g["status"] not in nfl.NOT_PLAYING]
        if not upcoming:
            return 1800
        return int(max(60, min(900, (min(upcoming) - now).total_seconds() + 5)))

    async def _load_logos(self) -> None:
        """The scoreboard cog creates team logos as the bot's own emoji
        (nfl_min etc.); reuse them on the buttons. Reloaded once a week."""
        if self._logos_loaded_for == self._current["key"]:
            return
        self._logos_loaded_for = self._current["key"]
        try:
            emojis = await self.bot.fetch_application_emojis()
        except discord.HTTPException as e:
            log.info("Couldn't load team logo emoji: %r", e)
            return
        self._logos = {
            e.name[4:].upper(): discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)
            for e in emojis if e.name.startswith("nfl_")
        }

    # ------------------------------------------------------------ weekly steps

    @staticmethod
    def _new_week(current: dict) -> dict:
        return {"label": current["label"], "season": current["season"], "games": {}, "picks": {},
                "tb": {}, "opened": False, "posted": False, "winners": []}

    async def _week(self, guild: discord.Guild) -> Optional[tuple[str, dict]]:
        """This week's stored data (created from ESPN's if it's new)."""
        if self._current is None:
            return None
        key = self._current["key"]
        async with self._locks[guild.id]:
            weeks = await self.config.guild(guild).weeks()
            week = weeks.get(key)
            if week is None:
                week = self._new_week(self._current)
                nfl.merge_games(week["games"], self._current["games"])
                await self.config.guild(guild).set_raw("weeks", key, value=week)
        return key, week

    async def _save(self, guild: discord.Guild, key: str, **changes) -> None:
        async with self._locks[guild.id]:
            for field, value in changes.items():
                await self.config.guild(guild).set_raw("weeks", key, field, value=value)

    async def _update_guild(self, guild: discord.Guild, now: datetime) -> None:
        conf = await self.config.guild(guild).all()
        channel = guild.get_channel(conf["channel_id"])
        if channel is None:
            self.last_error[guild.id] = "The pick'em channel is gone. Set it again with `pickemset channel`."
            return
        key = self._current["key"]

        # 1. Save ESPN's latest schedule and results (including last week's final
        #    scores if we've just moved on from it).
        async with self._locks[guild.id]:
            weeks = await self.config.guild(guild).weeks()
            for espn_week in (self._finishing, self._current):
                if espn_week is None:
                    continue
                k = espn_week["key"]
                if k not in weeks:
                    if espn_week is self._finishing:
                        continue  # a week nobody played here
                    weeks[k] = self._new_week(espn_week)
                    nfl.merge_games(weeks[k]["games"], espn_week["games"])
                    await self.config.guild(guild).set_raw("weeks", k, value=weeks[k])
                elif nfl.merge_games(weeks[k]["games"], espn_week["games"]):
                    await self.config.guild(guild).set_raw("weeks", k, "games", value=weeks[k]["games"])
            week = weeks[key]
            old = [k for k, w in weeks.items() if k != key and not w.get("posted")]

        # 2. Results for the week that just ended (and any earlier week that never
        #    got them, e.g. the bot was down or a game was postponed out of the
        #    week). Unfinished games don't count.
        for old_key in old:
            await self._post_results(guild, channel, conf, old_key, weeks[old_key])

        # 3. New week: post the panel while there's still something to pick.
        if not week.get("opened") and any(not nfl.is_locked(g, now) for g in week["games"].values()):
            if await self._post_panel(channel, week):
                await self._save(guild, key, opened=True)

        # 4. Tiebreaker: once every game before Monday is final.
        if not week.get("tb", {}).get("done"):
            tb_ids = nfl.tiebreak_due(week, now)
            if tb_ids:
                await self._start_tiebreak(guild, channel, key, week, tb_ids)

        # 5. Results, if this is the last week of the season (otherwise step 2
        #    does it once we've moved on to the next week).
        games = week["games"].values()
        if not week.get("posted") and games and all(nfl.is_done(g) for g in games):
            await self._post_results(guild, channel, conf, key, week)

    def _panel_embed(self, week: dict) -> discord.Embed:
        games = [g for _, g in nfl.ordered(week["games"]) if g["status"] not in nfl.NOT_PLAYING]
        lines = [
            "Pick the winner of every game this week. Each pick locks when that game kicks off, "
            "so you can wait on the Sunday games until Sunday.",
            "",
        ]
        if games:
            lines.append(f"First game: **{nfl.kickoff_text(games[0])}**, {matchup(games[0])}")
        lines.append("Most wins takes the week. If it's close after Sunday night, "
                     "the people still in it get a tiebreaker on Monday night's total points.")
        return discord.Embed(title=f"{week['label']} Pick'em", description="\n".join(lines), color=COLOR)

    async def _post_panel(self, channel: discord.TextChannel, week: dict) -> bool:
        try:
            await channel.send(embed=self._panel_embed(week), view=PanelView(self))
        except discord.HTTPException as e:
            self.last_error[channel.guild.id] = f"Couldn't post the panel: {e.text or e.status}"
            log.warning("Couldn't post pick'em panel in guild %s: %r", channel.guild.id, e)
            return False
        return True

    async def _start_tiebreak(self, guild, channel, key: str, week: dict, tb_ids: list[str]) -> None:
        who = nfl.contenders(week, tb_ids)
        tb = {"games": tb_ids, "contenders": who, "guesses": {}, "done": True, "message_id": None}
        if len(who) >= 2:
            tb_games = [week["games"][gid] for gid in tb_ids]
            first = min(tb_games, key=nfl.kickoff)
            day = nfl.kickoff(first).astimezone(nfl.CENTRAL).strftime("%A")
            if len(tb_games) == 1:
                what = f"{day} night's game, {matchup(first)}"
            else:
                what = f"{day}'s games ({', '.join(matchup(g) for g in tb_games)}) added together"
            embed = discord.Embed(
                title=f"{week['label']} Pick'em tiebreaker",
                description=(
                    f"It's close. {len(who)} of you could still finish tied for first, so guess the total "
                    f"points scored in {what}. If the week ends in a tie, the closest guess wins it.\n\n"
                    f"Guesses lock at kickoff, **{nfl.kickoff_text(first)}**. You can still change your "
                    f"{day} pick in the picker until then."
                ),
                color=COLOR,
            )
            try:
                message = await channel.send(
                    " ".join(mention(u) for u in who), embed=embed, view=GuessView(self),
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(u)) for u in who]),
                )
                tb["message_id"] = message.id
            except discord.HTTPException as e:
                log.warning("Couldn't post pick'em tiebreaker in guild %s: %r", guild.id, e)
                return  # try again next tick
        await self._save(guild, key, tb=tb)

    def results_embed(self, week: dict, weeks: dict) -> tuple[discord.Embed, list[str]]:
        """The results card and the winners. Used for the real post and previews."""
        won, tied, total = nfl.winners(week)
        table = nfl.standings(week)
        lines = []
        if won:
            rec = nfl.record_text(dict(table)[won[0]])
            verb = "wins" if len(won) == 1 else "share"
            lines.append(f"🏆 {names(won)} {verb} {week['label']} at {rec}.")
        if len(tied) > 1:
            guesses = (week.get("tb") or {}).get("guesses") or {}
            if total is None:
                lines.append(f"{names(tied)} tied on wins, and there was no tiebreaker to split them.")
            else:
                said = ", ".join(f"{mention(u)} guessed {guesses[u]}" if u in guesses else f"{mention(u)} didn't guess"
                                 for u in tied)
                lines.append(f"Tiebreaker: {total} total points. {said}.")
        embed = discord.Embed(title=f"{week['label']} Pick'em results", description="\n".join(lines) or None, color=COLOR)
        if table:
            embed.add_field(name="This week", value=self._table(table, CARD_SIZE), inline=False)
        season = nfl.season_standings(weeks, week["season"])
        if season:
            embed.add_field(name=f"{week['season']} season", value=self._season_table(season, CARD_SIZE), inline=False)
        return embed, won

    @staticmethod
    def _table(rows, size: int) -> str:
        return "\n".join(f"{i}. {mention(uid)} {nfl.record_text(rec)}" for i, (uid, rec) in enumerate(rows[:size], 1))

    @staticmethod
    def _season_table(rows, size: int) -> str:
        return "\n".join(
            f"{i}. {mention(uid)} {nfl.record_text(rec)} ({played} week{'s' if played != 1 else ''})"
            for i, (uid, rec, played) in enumerate(rows[:size], 1)
        )

    async def _post_results(self, guild, channel, conf: dict, key: str, week: dict) -> None:
        if not nfl.players(week):
            await self._save(guild, key, posted=True)  # nobody played; nothing to announce
            return
        weeks = await self.config.guild(guild).weeks()
        embed, won = self.results_embed(week, weeks)
        try:
            await channel.send(
                " ".join(mention(u) for u in won), embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(u)) for u in won]),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't post pick'em results in guild %s: %r", guild.id, e)
            return  # try again next tick
        await self._save(guild, key, posted=True, winners=won)
        if conf["role_id"]:
            problem = await self._move_role(guild, conf["role_id"], won)
            if problem:
                self.last_error[guild.id] = problem
                log.warning("%s (guild %s)", problem, guild.id)

    async def _move_role(self, guild: discord.Guild, role_id: int, winners: list[str]) -> Optional[str]:
        """Take the winner role from last week's winners and give it to this week's."""
        role = guild.get_role(role_id)
        if role is None:
            return "The pick'em winner role no longer exists."
        if not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
            return f"I can't hand out {role.name}: I need Manage Roles and my role above it."
        keep = {int(u) for u in winners}
        reason = "Weekly pick'em winner"
        for member in list(role.members):
            if member.id not in keep:
                try:
                    await member.remove_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
        for member_id in keep:
            member = guild.get_member(member_id)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason=reason)
                except discord.HTTPException:
                    pass
        return None

    # ------------------------------------------------------------ picker

    async def _playable(self, interaction: discord.Interaction) -> Optional[tuple[str, dict]]:
        """This week's data if pick'em is on in this server, else tells the user why not."""
        guild = interaction.guild
        if guild is None or not await self.config.guild(guild).enabled():
            await interaction.response.send_message("Pick'em isn't running in this server.", ephemeral=True)
            return None
        found = await self._week(guild)
        if found is None:
            await interaction.response.send_message("I don't have this week's games yet. Try again in a minute.",
                                                    ephemeral=True)
        return found

    def _picker_embed(self, week: dict, uid: str, page: int, now: datetime) -> discord.Embed:
        games = nfl.ordered(week["games"])
        picks = week["picks"].get(uid, {})
        lines = []
        for gid, game in games[page * PER_PAGE:(page + 1) * PER_PAGE]:
            line = f"**{nfl.kickoff_text(game)}** · {matchup(game)}"
            winner = game["winner"]
            if game["status"] in nfl.NOT_PLAYING:
                line += " · postponed"
            elif winner is not None:
                line += f" · final {game['away']} {game['away_score']}, {game['home']} {game['home_score']}"
                pick = picks.get(gid)
                line += " ✅" if pick == winner else (" (tie)" if winner == "TIE" else " ❌")
            elif nfl.is_locked(game, now):
                line += " · 🔒 locked"
            lines.append(line)
        open_games = [gid for gid, g in games if not nfl.is_locked(g, now)]
        made = sum(1 for gid in open_games if gid in picks)
        embed = discord.Embed(title=f"{week['label']} Pick'em", description="\n".join(lines), color=COLOR)
        rec = nfl.record(week, uid)
        footer = f"{made} of {len(open_games)} open games picked"
        if sum(rec):
            footer += f" · you're {nfl.record_text(rec)} so far"
        embed.set_footer(text=footer)
        return embed

    def _first_page(self, week: dict, uid: str, now: datetime) -> int:
        """Start on the first page with an open game you haven't picked."""
        picks = week["picks"].get(uid, {})
        games = nfl.ordered(week["games"])
        for wanted in (lambda gid, g: not nfl.is_locked(g, now) and gid not in picks,
                       lambda gid, g: not nfl.is_locked(g, now)):
            for i, (gid, game) in enumerate(games):
                if wanted(gid, game):
                    return i // PER_PAGE
        return 0

    async def open_picker(self, interaction: discord.Interaction) -> None:
        found = await self._playable(interaction)
        if found is None:
            return
        key, week = found
        now = datetime.now(timezone.utc)
        uid = str(interaction.user.id)
        view = PickerView(self, uid, key, self._first_page(week, uid, now))
        view.build(week, now, self._logos)
        await interaction.response.send_message(
            embed=self._picker_embed(week, uid, view.page, now), view=view, ephemeral=True
        )

    async def refresh_picker(self, interaction: discord.Interaction, view: PickerView, note: str = "") -> None:
        now = datetime.now(timezone.utc)
        week = await self.config.guild(interaction.guild).get_raw("weeks", view.key, default=None)
        if week is None:
            await interaction.response.edit_message(content="That week is over.", embed=None, view=None)
            return
        view.build(week, now, self._logos)
        await interaction.response.edit_message(
            content=note or None, embed=self._picker_embed(week, view.uid, view.page, now), view=view
        )

    async def pick(self, interaction: discord.Interaction, view: PickerView, gid: str, team: str) -> None:
        guild = interaction.guild
        now = datetime.now(timezone.utc)
        note = ""
        async with self._locks[guild.id]:
            game = await self.config.guild(guild).get_raw("weeks", view.key, "games", gid, default=None)
            # Checked here, not just by disabling buttons: an old picker can still be clicked.
            if game is None or nfl.is_locked(game, now):
                note = "That game already kicked off, so its pick is locked."
            else:
                await self.config.guild(guild).set_raw("weeks", view.key, "picks", view.uid, gid, value=team)
        await self.refresh_picker(interaction, view, note)

    # ------------------------------------------------------------ tiebreaker

    async def open_guess(self, interaction: discord.Interaction) -> None:
        found = await self._playable(interaction)
        if found is None:
            return
        key, week = found
        tb = week.get("tb") or {}
        uid = str(interaction.user.id)
        problem = None
        if not tb.get("games"):
            problem = "There's no tiebreaker this week."
        elif uid not in tb.get("contenders", []):
            problem = "The tiebreaker is only for the people tagged in it. Everyone else is out of reach this week."
        elif any(nfl.is_locked(week["games"][gid], datetime.now(timezone.utc)) for gid in tb["games"]):
            problem = "Guesses closed at kickoff."
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(GuessForm(self, key, tb.get("guesses", {}).get(uid)))

    async def save_guess(self, interaction: discord.Interaction, key: str, points: int) -> None:
        guild = interaction.guild
        async with self._locks[guild.id]:
            week = await self.config.guild(guild).get_raw("weeks", key, default=None)
            games = (week or {}).get("tb", {}).get("games") or []
            if not games or any(nfl.is_locked(week["games"][gid], datetime.now(timezone.utc)) for gid in games):
                problem = "Guesses closed at kickoff."
            else:
                problem = None
                await self.config.guild(guild).set_raw("weeks", key, "tb", "guesses", str(interaction.user.id), value=points)
        await interaction.response.send_message(problem or f"Got it: {points} points. You can change it until kickoff.",
                                                ephemeral=True)

    # ------------------------------------------------------------ member commands

    def _picks_text(self, week: dict, uid: str, show_all: bool, now: datetime) -> str:
        picks = week["picks"].get(uid, {})
        lines, hidden = [], 0
        for gid, game in nfl.ordered(week["games"]):
            if not show_all and not nfl.is_locked(game, now):
                hidden += gid in picks
                continue
            pick = picks.get(gid)
            mark = ""
            if game["winner"] not in (None, "VOID"):
                mark = " (tie)" if game["winner"] == "TIE" else (" ✅" if pick == game["winner"] else " ❌")
            lines.append(f"{nfl.kickoff_text(game)} · {matchup(game)} · **{pick or 'no pick'}**{mark}")
        if hidden:
            lines.append(f"\nPlus {hidden} pick{'s' if hidden != 1 else ''} for games that haven't started, "
                         "which stay hidden until kickoff.")
        return "\n".join(lines) or "No games have started yet, so there's nothing to show."

    async def _picks_embed(self, guild, member: discord.Member, private: bool) -> Optional[discord.Embed]:
        found = await self._week(guild)
        if found is None:
            return None
        _, week = found
        now = datetime.now(timezone.utc)
        uid = str(member.id)
        text = self._picks_text(week, uid, show_all=private, now=now)
        embed = discord.Embed(title=f"{member.display_name}'s {week['label']} picks", description=text[:4096], color=COLOR)
        rec = nfl.record(week, uid)
        if sum(rec):
            embed.set_footer(text=f"{nfl.record_text(rec)} so far")
        return embed

    async def _standings_embed(self, guild) -> Optional[discord.Embed]:
        found = await self._week(guild)
        if found is None:
            return None
        _, week = found
        weeks = await self.config.guild(guild).weeks()
        embed = discord.Embed(title="Pick'em standings", color=COLOR)
        table = nfl.standings(week)
        embed.add_field(name=week["label"], value=self._table(table, TABLE_SIZE) or "No picks yet.", inline=False)
        season = nfl.season_standings(weeks, week["season"])
        embed.add_field(name=f"{week['season']} season", value=self._season_table(season, TABLE_SIZE) or "Nothing yet.",
                        inline=False)
        return embed

    NOT_READY = "Pick'em isn't running here yet, or I don't have this week's games. Try again in a minute."

    @commands.group(name="pickem", invoke_without_command=True)
    @commands.guild_only()
    async def pickem(self, ctx: commands.Context):
        """Make your picks for this week."""
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send("Pick'em isn't running in this server.")
            return
        # Typed commands can't open private messages, so hand over the button.
        await ctx.send("Your picks are private, so they open from this button.", view=PanelView(self, timeout=120),
                       delete_after=120)

    @pickem.command(name="picks")
    async def pickem_picks(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show picks for games that have kicked off. Use /pickem picks to see all of your own."""
        embed = await self._picks_embed(ctx.guild, member or ctx.author, private=False)
        await ctx.send(embed=embed) if embed else await ctx.send(self.NOT_READY)

    @pickem.command(name="standings")
    async def pickem_standings(self, ctx: commands.Context):
        """This week's and the season's standings."""
        embed = await self._standings_embed(ctx.guild)
        await ctx.send(embed=embed) if embed else await ctx.send(self.NOT_READY)

    @pickem_slash.command(name="play", description="Make your picks for this week")
    async def slash_play(self, interaction: discord.Interaction):
        await self.open_picker(interaction)

    @pickem_slash.command(name="picks", description="See picks. Yours show in full; others' show once games kick off")
    @app_commands.describe(member="Whose picks (leave empty for yours)")
    async def slash_picks(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        embed = await self._picks_embed(interaction.guild, target, private=target.id == interaction.user.id)
        if embed is None:
            await interaction.response.send_message(self.NOT_READY, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @pickem_slash.command(name="standings", description="This week's and the season's pick'em standings")
    async def slash_standings(self, interaction: discord.Interaction):
        embed = await self._standings_embed(interaction.guild)
        if embed is None:
            await interaction.response.send_message(self.NOT_READY, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ admin commands

    @commands.group(name="pickemset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def pickemset(self, ctx: commands.Context):
        """Pick'em settings."""

    @pickemset.command(name="channel")
    async def pickemset_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Where the weekly panel, tiebreaker and results are posted."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        self.last_error.pop(ctx.guild.id, None)
        await ctx.send(f"Pick'em will post in {channel.mention}." +
                       ("" if await self.config.guild(ctx.guild).enabled() else " Turn it on with `pickemset toggle`."))

    @pickemset.command(name="role")
    async def pickemset_role(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """Set the weekly winner role, or leave empty for no role."""
        await self.config.guild(ctx.guild).role_id.set(role.id if role else None)
        if role is None:
            await ctx.send("No winner role. Winners just get announced.")
        else:
            await ctx.send(f"Weekly winners will get **{role.name}** (taken from last week's winner).",
                           allowed_mentions=discord.AllowedMentions.none())

    @pickemset.command(name="toggle")
    async def pickemset_toggle(self, ctx: commands.Context):
        """Turn pick'em on or off."""
        conf = self.config.guild(ctx.guild)
        enabled = not await conf.enabled()
        if enabled:
            if not await conf.channel_id():
                await ctx.send("Set a channel first: `pickemset channel #channel`.")
                return
            # Don't announce weeks from before it was switched on.
            async with self._locks[ctx.guild.id]:
                async with conf.weeks() as weeks:
                    current = self._current["key"] if self._current else None
                    for key, week in weeks.items():
                        if key != current:
                            week["posted"] = True
        await conf.enabled.set(enabled)
        self._wake.set()
        await ctx.send("Pick'em is on. The panel goes up as soon as there are games to pick." if enabled
                       else "Pick'em is off. Picks so far are kept.")

    @pickemset.command(name="panel")
    async def pickemset_panel(self, ctx: commands.Context):
        """Post this week's "Make your picks" panel again (e.g. if it was deleted)."""
        channel = ctx.guild.get_channel(await self.config.guild(ctx.guild).channel_id() or 0)
        found = await self._week(ctx.guild)
        if channel is None or found is None:
            await ctx.send("Set a channel first, and give me a minute to load the games.")
            return
        key, week = found
        if await self._post_panel(channel, week):
            await self._save(ctx.guild, key, opened=True)
            await ctx.tick()
        else:
            await ctx.send(self.last_error.get(ctx.guild.id, "Couldn't post the panel."))

    @pickemset.command(name="preview")
    async def pickemset_preview(self, ctx: commands.Context):
        """Show this week's results card here, as it stands (no pings, no roles)."""
        found = await self._week(ctx.guild)
        if found is None:
            await ctx.send(self.NOT_READY)
            return
        _, week = found
        weeks = await self.config.guild(ctx.guild).weeks()
        embed, _ = self.results_embed(week, weeks)
        tb = week.get("tb") or {}
        notes = []
        if tb.get("games"):
            who = tb.get("contenders", [])
            notes.append(f"Tiebreaker: {len(who)} contender(s), {len(tb.get('guesses', {}))} guess(es) in."
                         if len(who) >= 2 else "Tiebreaker: not needed this week.")
        else:
            tb_ids = nfl.tiebreak_games(week)
            notes.append("Tiebreaker: prompt goes out once every game before "
                         f"{nfl.kickoff_text(week['games'][tb_ids[0]]).split()[0]} is final." if tb_ids
                         else "Tiebreaker: none this week (all games on one day).")
        await ctx.send("Preview only, nothing was posted.\n" + "\n".join(notes), embed=embed,
                       allowed_mentions=discord.AllowedMentions.none())

    @pickemset.command(name="show")
    async def pickemset_show(self, ctx: commands.Context):
        """Show the pick'em settings."""
        conf = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(conf["channel_id"] or 0)
        role = ctx.guild.get_role(conf["role_id"] or 0)
        week = conf["weeks"].get(self._current["key"]) if self._current else None
        lines = [
            f"Running: {'yes' if conf['enabled'] else 'no'}",
            f"Channel: {channel.mention if channel else 'not set'}",
            f"Winner role: {role.name if role else 'none'}",
            f"This week: {self._current['label'] if self._current else 'not loaded yet'}"
            + (f", {len(nfl.players(week))} playing" if week else ""),
            f"Next ESPN check in about {self.interval // 60 or 1} min",
        ]
        if ctx.guild.id in self.last_error:
            lines.append(f"Last problem: {self.last_error[ctx.guild.id]}")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
