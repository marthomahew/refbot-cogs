"""Which game channels should be open right now. Plain logic, no Discord.

Rules:
- The highlighted team's game (Vikings by default) gets two channels, the game
  ("min-at-chi") and "delayed-min-at-chi" for people watching later, whatever
  time it kicks off. A Vikings primetime game gets only these two.
- Every other primetime game (kickoff 5 PM Central or later: Thursday, Sunday
  and Monday night, Monday doubleheaders) gets one channel named for the game.
- RedZone opens whenever two or more daytime games kick off together (Sunday's
  noon and late windows, Week 18, ...), from before the window's first kickoff
  until after its last game ends.
- A channel opens `before` its kickoff and closes `after` the game is final
  (the delayed channel stays open longer, `delayed_after`, for late watchers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("red.refbot.gameday")

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NOT_PLAYING = ("STATUS_POSTPONED", "STATUS_CANCELED")
PRIMETIME_HOUR = 17  # kickoffs at 5 PM Central or later count as primetime
WINDOW_GAP = timedelta(minutes=45)  # kickoffs this close share a RedZone window (3:05 + 3:25)

try:
    CENTRAL = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    CENTRAL = timezone(timedelta(hours=-5), "CT")


@dataclass
class Game:
    id: str
    away: str
    home: str
    kickoff: datetime  # UTC
    state: str  # pre / in / post
    status: str

    @property
    def name(self) -> str:
        """Channel name, e.g. "pit-at-cle"."""
        return f"{self.away.lower()}-at-{self.home.lower()}"

    @property
    def playing(self) -> bool:
        return self.status not in NOT_PLAYING

    @property
    def primetime(self) -> bool:
        return self.kickoff.astimezone(CENTRAL).hour >= PRIMETIME_HOUR

    def involves(self, team: str) -> bool:
        return team in (self.away, self.home)


def parse_games(data: dict) -> list[Game]:
    games = []
    for event in data.get("events") or []:
        try:
            comp = event["competitions"][0]
            teams = {c["homeAway"]: c["team"]["abbreviation"] for c in comp["competitors"]}
            status = (event.get("status") or {}).get("type") or {}
            games.append(Game(
                id=str(event["id"]),
                away=teams["away"],
                home=teams["home"],
                kickoff=datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(timezone.utc),
                state=status.get("state", "pre"),
                status=status.get("name", ""),
            ))
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Skipping an ESPN game we couldn't read (id=%s)", event.get("id"))
    return sorted(games, key=lambda g: (g.kickoff, g.id))


def redzone_windows(games: list[Game], team: str) -> list[list[Game]]:
    """Groups of daytime games kicking off together (2+ games each)."""
    daytime = [g for g in games if g.playing and not g.primetime]
    windows: list[list[Game]] = []
    for game in daytime:
        if windows and game.kickoff - windows[-1][-1].kickoff <= WINDOW_GAP:
            windows[-1].append(game)
        else:
            windows.append([game])
    return [w for w in windows if len(w) >= 2]


@dataclass
class Want:
    """A channel that should be open: which slot, its name, and a key that says
    which game(s) it's for (so we know when it changes)."""

    slot: str  # "vikings", "delayed", "redzone" or "primetime"
    name: str
    key: str
    opens: datetime
    closes: Optional[datetime]  # None = not decided yet (games not final)
    label: str  # for `gameday show`


def _closes(games: list[Game], finals: dict[str, str], after: timedelta) -> Optional[datetime]:
    """When to close: `after` the last of these games went final, or None if any
    of them isn't final yet."""
    times = []
    for game in games:
        if not game.playing:
            continue
        if game.id not in finals:
            return None
        times.append(datetime.fromisoformat(finals[game.id]))
    return max(times) + after if times else None


def plan(games: list[Game], finals: dict[str, str], team: str, before: timedelta,
         after: timedelta, delayed_after: timedelta) -> list[Want]:
    """Every channel this week's games call for, open or not."""
    wants = []
    for game in games:
        if not game.playing:
            continue
        opens = game.kickoff - before
        if game.involves(team):
            wants.append(Want("vikings", game.name, f"team:{game.id}", opens,
                              _closes([game], finals, after), f"{game.name} ({team} game)"))
            wants.append(Want("delayed", f"delayed-{game.name}", f"delayed:{game.id}", opens,
                              _closes([game], finals, delayed_after), f"delayed-{game.name}"))
        elif game.primetime:
            wants.append(Want("primetime", game.name, f"prime:{game.id}", opens,
                              _closes([game], finals, after), f"{game.name} (primetime)"))
    for window in redzone_windows(games, team):
        wants.append(Want("redzone", "redzone", f"redzone:{window[0].id}", window[0].kickoff - before,
                          _closes(window, finals, after), f"redzone ({len(window)} games)"))
    return sorted(wants, key=lambda w: w.opens)


def open_now(wants: list[Want], now: datetime) -> list[Want]:
    return [w for w in wants if w.opens <= now and (w.closes is None or now < w.closes)]
