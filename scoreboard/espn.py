"""Fetching and parsing ESPN's NFL scoreboard, plus building the Discord embeds.

ESPN's API is unofficial and can change without warning, so every parser here
is defensive: a game we can't understand is skipped (and logged) rather than
crashing the whole scoreboard, and every "extra" detail is optional.

Rough shape of the JSON (only the parts we use):

    {
      "season": {"type": 2, "year": 2026},        # 1=pre, 2=regular, 3=post
      "week": {"number": 3},
      "leagues": [{"calendar": [{"value": "2", "entries": [{"value": "3", "label": "Week 3"}]}]}],
      "events": [{
        "id": "...", "date": "2026-09-27T20:05Z",
        "weather": {"displayValue": "Sunny", "temperature": 88},
        "status": {"period": 2, "displayClock": "5:21",
                   "type": {"state": "pre|in|post", "name": "STATUS_...", "shortDetail": "Final/OT"}},
        "competitions": [{
          "neutralSite": false,
          "venue": {"fullName": "...", "indoor": false, "address": {"city": "Tampa", "state": "FL"}},
          "broadcast": "FOX",
          "odds": [{"details": "MIN -1.5", "overUnder": 42.5}],
          "headlines": [{"shortLinkText": "Recap headline"}],          # after the game
          "leaders": [{"shortDisplayName": "PASS",
                       "leaders": [{"displayValue": "11/20, 143 YDS",
                                    "athlete": {"shortName": "C. Wentz"}, "team": {"id": "16"}}]}],
          "competitors": [{"homeAway": "home", "score": "21",
                           "linescores": [{"value": 7.0}, ...],          # once the game starts
                           "records": [{"type": "total", "summary": "2-0"}],
                           "team": {"id": "16", "abbreviation": "MIN", "displayName": "Minnesota Vikings",
                                    "shortDisplayName": "Vikings", "color": "4f2683", "logo": "https://..."}}],
          "situation": {"possession": "16", "shortDownDistanceText": "3rd & 5", "possessionText": "MIN 45",
                        "lastPlay": {"text": "..."}}                   # only while live
        }]
      }]
    }

The "situation" block was not seen live when this was written (no games were on),
so it is parsed extra carefully.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

log = logging.getLogger("red.refbot.scoreboard")

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# Vikings purple: fallback color for the team card if ESPN doesn't send one.
TEAM_FALLBACK_COLOR = discord.Color(0x4F2683)
# NFL shield navy, for the league card.
LEAGUE_COLOR = discord.Color(0x013369)

# ESPN's abbreviations for all 32 teams (note: Washington is WSH, not WAS).
NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE",
    "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH",
}

try:
    CENTRAL = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    # Only happens if the server has no timezone database installed.
    # Fall back to a fixed UTC-5 (Central Daylight Time) so we still show something.
    log.warning("Timezone data for America/Chicago not found; using fixed UTC-5.")
    CENTRAL = timezone(timedelta(hours=-5), "CT")


@dataclass
class Team:
    id: str
    abbr: str
    name: str  # "Minnesota Vikings"
    short_name: str  # "Vikings"
    score: str  # ESPN sends scores as strings, e.g. "21"
    record: str = ""  # "2-0"
    logo: str = ""  # image URL
    color: str = ""  # hex without '#', e.g. "4f2683"
    linescores: list[int] = field(default_factory=list)  # points per quarter


@dataclass
class Leader:
    category: str  # "PASS", "RUSH", "REC"
    player: str  # "C. Wentz"
    team_abbr: str
    stats: str  # "11/20, 143 YDS"


@dataclass
class Game:
    id: str
    kickoff: datetime  # timezone-aware, UTC
    state: str  # "pre", "in" or "post"
    status_name: str  # e.g. "STATUS_HALFTIME"
    short_detail: str  # ESPN's own short status text, e.g. "Final/OT"
    period: int
    clock: str
    home: Team
    away: Team
    neutral_site: bool = False
    possession: Optional[str] = None  # abbreviation of the team with the ball
    down_distance: Optional[str] = None  # e.g. "3rd & 5 at MIN 45"
    # Extras, only shown on the highlighted team's card:
    last_play: str = ""
    tv: str = ""
    venue: str = ""
    weather: str = ""
    line: str = ""  # betting line, e.g. "MIN -1.5 · O/U 42.5"
    headline: str = ""  # recap headline after the game
    highlights_url: str = ""  # ESPN's highlight video (after the game)
    gamecast_url: str = ""  # ESPN's live game page
    leaders: list[Leader] = field(default_factory=list)

    def involves(self, abbr: str) -> bool:
        return abbr in (self.home.abbr, self.away.abbr)


@dataclass
class Week:
    label: str  # e.g. "Week 3" or "Wild Card"
    games: list[Game] = field(default_factory=list)


# ---------------------------------------------------------------- parsing


def _parse_time(text: str) -> datetime:
    # ESPN uses "2026-09-27T20:05Z" (no seconds), which fromisoformat accepts on 3.11.
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _optional(what: str, func, *args):
    """Run a parser for an optional extra; on any error, log at debug level and return None."""
    try:
        return func(*args)
    except Exception:
        log.debug("Couldn't parse optional %s", what, exc_info=True)
        return None


def _parse_team(competitor: dict) -> Team:
    team = competitor["team"]
    abbr = team["abbreviation"]
    record = next(
        (r.get("summary", "") for r in competitor.get("records") or []
         if r.get("type") == "total" or r.get("name") == "overall"),
        "",
    )
    linescores = [int(float(ls.get("value", 0))) for ls in competitor.get("linescores") or []]
    return Team(
        id=str(team.get("id", "")),
        abbr=abbr,
        name=team.get("displayName") or abbr,
        short_name=team.get("shortDisplayName") or team.get("name") or abbr,
        score=str(competitor.get("score") or "0"),
        record=record,
        logo=team.get("logo") or "",
        color=team.get("color") or "",
        linescores=linescores,
    )


def _parse_leaders(comp: dict, teams_by_id: dict[str, Team]) -> list[Leader]:
    leaders = []
    for cat in comp.get("leaders") or []:
        top = (cat.get("leaders") or [None])[0]
        if not top:
            continue
        team = teams_by_id.get(str((top.get("team") or {}).get("id", "")))
        leaders.append(Leader(
            category=cat.get("shortDisplayName") or cat.get("abbreviation") or "",
            player=(top.get("athlete") or {}).get("shortName", "?"),
            team_abbr=team.abbr if team else "",
            stats=top.get("displayValue", ""),
        ))
    return leaders


def _parse_venue(comp: dict) -> str:
    venue = comp.get("venue") or {}
    name = venue.get("fullName", "")
    addr = venue.get("address") or {}
    place = ", ".join(p for p in (addr.get("city"), addr.get("state")) if p)
    return f"{name}\n{place}" if name and place else name


def _parse_line(comp: dict) -> str:
    odds = (comp.get("odds") or [None])[0]
    if not odds:
        return ""
    parts = [odds.get("details", "")]
    if odds.get("overUnder") is not None:
        parts.append(f"O/U {odds['overUnder']}")
    return " · ".join(p for p in parts if p)


def _parse_weather(event: dict, comp: dict) -> str:
    if (comp.get("venue") or {}).get("indoor"):
        return "Indoors"
    weather = event.get("weather") or {}
    temp, desc = weather.get("temperature"), weather.get("displayValue")
    if temp is None and not desc:
        return ""
    return " ".join(p for p in (f"{temp}°F" if temp is not None else "", desc or "") if p)


def _parse_game(event: dict) -> Game:
    comp = event["competitions"][0]
    status = event.get("status") or comp.get("status") or {}
    stype = status.get("type") or {}

    teams = {c.get("homeAway"): _parse_team(c) for c in comp["competitors"]}
    home, away = teams["home"], teams["away"]
    teams_by_id = {home.id: home, away.id: away}

    game = Game(
        id=str(event.get("id", "")),
        kickoff=_parse_time(event["date"]),
        state=stype.get("state", "pre"),
        status_name=stype.get("name", ""),
        short_detail=stype.get("shortDetail", ""),
        period=int(status.get("period") or 0),
        clock=status.get("displayClock", ""),
        home=home,
        away=away,
        neutral_site=bool(comp.get("neutralSite")),
    )

    # Live-game extras. Any of these may be missing; that's fine.
    situation = comp.get("situation") or {}
    possession_team = teams_by_id.get(str(situation.get("possession") or ""))
    if possession_team:
        game.possession = possession_team.abbr
    down = situation.get("shortDownDistanceText") or situation.get("downDistanceText")
    where = situation.get("possessionText")
    if down and where:
        game.down_distance = f"{down} at {where}"
    elif down:
        game.down_distance = down
    game.last_play = ((situation.get("lastPlay") or {}).get("text") or "").strip()

    # Card extras.
    game.tv = comp.get("broadcast") or ", ".join(
        n for b in comp.get("broadcasts") or [] for n in b.get("names", [])
    )
    game.venue = _optional("venue", _parse_venue, comp) or ""
    game.weather = _optional("weather", _parse_weather, event, comp) or ""
    game.line = _optional("odds", _parse_line, comp) or ""
    game.headline = _optional("headline", lambda: (comp.get("headlines") or [{}])[0].get("shortLinkText", "")) or ""
    game.highlights_url = _optional(
        "highlights", lambda: (comp.get("highlights") or [{}])[0].get("links", {}).get("web", {}).get("href", "")
    ) or ""
    game.gamecast_url = next(
        (link.get("href", "") for link in event.get("links") or [] if "summary" in (link.get("rel") or [])), ""
    )
    # Before kickoff ESPN's "leaders" are season stats, which would be confusing
    # next to a 0-0 score, so only keep them once the game has started.
    if game.state != "pre":
        game.leaders = _optional("leaders", _parse_leaders, comp, teams_by_id) or []

    return game


def _week_label(data: dict) -> str:
    """Look up a friendly name like "Week 3" or "Wild Card" from ESPN's calendar."""
    week_num = str((data.get("week") or {}).get("number", ""))
    season_type = str((data.get("season") or {}).get("type", ""))
    try:
        for part in data["leagues"][0]["calendar"]:
            if str(part.get("value")) != season_type:
                continue
            for entry in part.get("entries", []):
                if str(entry.get("value")) == week_num:
                    return entry["label"]
    except (KeyError, IndexError, TypeError):
        pass
    return f"Week {week_num}" if week_num else "This Week"


# Event ids we've already complained about, so a broken event doesn't spam the log every minute.
_bad_events: set[str] = set()


def parse_scoreboard(data: dict) -> Week:
    """Turn ESPN's JSON into a Week. Raises only if the top-level shape is unusable."""
    events = data["events"]  # KeyError here means the API changed; caller handles it
    games = []
    for event in events:
        try:
            games.append(_parse_game(event))
        except (KeyError, IndexError, TypeError, ValueError):
            event_id = str(event.get("id"))
            if event_id not in _bad_events:
                _bad_events.add(event_id)
                log.warning("Skipping an ESPN event we couldn't parse (id=%s)", event_id, exc_info=True)
    return Week(label=_week_label(data), games=games)


# ---------------------------------------------------------------- text helpers


def _clock(dt: datetime) -> str:
    """e.g. "3:05 PM" (written by hand so it works the same on any OS)."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def central_time_text(dt: datetime) -> str:
    """e.g. "Sun 9/27 · 3:05 PM CT"."""
    local = dt.astimezone(CENTRAL)
    return f"{local:%a} {local.month}/{local.day} · {_clock(local)} CT"


def _quarter(period: int) -> str:
    if period <= 4:
        return f"Q{period}"
    extra = period - 4  # 5 = OT, 6 = 2OT (playoffs)
    return "OT" if extra == 1 else f"{extra}OT"


def _is_postponed(game: Game) -> bool:
    return game.status_name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_DELAYED")


def clock_text(game: Game) -> str:
    """Just the game clock part: "Q3 5:21", "Halftime", "Final/OT", or kickoff time."""
    if game.state == "post":
        return game.short_detail if game.short_detail.startswith("Final") else "Final"
    if game.state == "in":
        if game.status_name == "STATUS_HALFTIME":
            return "Halftime"
        if game.status_name == "STATUS_IN_PROGRESS" and game.period:
            return f"{_quarter(game.period)} {game.clock}"
        return game.short_detail or "In progress"  # end of quarter, weather delay, etc.
    if _is_postponed(game):
        return game.short_detail or "Postponed"
    return central_time_text(game.kickoff)


def ball_text(game: Game) -> str:
    """e.g. "🏈 MIN · 3rd & 5 at TB 32", or "" if nobody has the ball."""
    if game.state != "in" or not game.possession:
        return ""
    text = f"🏈 {game.possession}"
    if game.down_distance:
        text += f" · {game.down_distance}"
    return text


def _scores(game: Game) -> tuple[str, str]:
    """Away and home "ABBR SCORE" strings, with the leader in bold."""
    away = f"{game.away.abbr} {game.away.score}"
    home = f"{game.home.abbr} {game.home.score}"
    try:
        away_pts, home_pts = int(game.away.score), int(game.home.score)
    except ValueError:
        return away, home
    if away_pts > home_pts:
        away = f"**{away}**"
    elif home_pts > away_pts:
        home = f"**{home}**"
    return away, home


def _sep(game: Game) -> str:
    return "vs" if game.neutral_site else "@"


# ---------------------------------------------------------------- team card


def _linescore_block(game: Game) -> str:
    """A little fixed-width table of points per quarter:

        ```
              1  2  3  4    T
        MIN   3  3  0  3    9
        CHI   3  0  0  0    3
        ```
    """
    periods = max(len(game.away.linescores), len(game.home.linescores))
    if periods == 0:
        return ""
    heads = [str(i + 1) if i < 4 else ("OT" if i == 4 else f"{i - 3}OT") for i in range(periods)]
    rows = ["    " + "".join(f"{h:>4}" for h in heads) + "     T"]
    for team in (game.away, game.home):
        points = team.linescores + [0] * (periods - len(team.linescores))
        rows.append(f"{team.abbr:<4}" + "".join(f"{p:>4}" for p in points) + f"{team.score:>6}")
    return "```\n" + "\n".join(rows) + "\n```"


def _team_color(team: Team) -> discord.Color:
    try:
        return discord.Color(int(team.color, 16))
    except ValueError:
        return TEAM_FALLBACK_COLOR


def build_team_embed(
    game: Optional[Game], team_abbr: str, week_label: str, logos: Optional[dict[str, str]] = None
) -> discord.Embed:
    """The big card for the highlighted team's game."""
    logos = logos or {}

    def logo(abbr: str) -> str:
        return f"{logos[abbr]} " if abbr in logos else ""

    if game is None:
        return discord.Embed(
            title=f"⭐ {team_abbr}: bye week",
            description=f"No {team_abbr} game in {week_label}.",
            color=TEAM_FALLBACK_COLOR,
        )

    ours = game.home if game.home.abbr == team_abbr else game.away

    embed = discord.Embed(
        title=f"{game.away.short_name} {_sep(game)} {game.home.short_name}",
        color=_team_color(ours),
    )
    # Discord only has one big image slot (top-right), so it goes to our team.
    embed.set_author(name=f"⭐ {ours.name} · {week_label}")
    if ours.logo:
        embed.set_thumbnail(url=ours.logo)

    # Big headline line: records before kickoff, the score after.
    def rec(t: Team) -> str:
        return f" ({t.record})" if t.record else ""

    if game.state == "pre":
        head = (f"### {logo(game.away.abbr)}{game.away.abbr}{rec(game.away)}  {_sep(game)}  "
                f"{logo(game.home.abbr)}{game.home.abbr}{rec(game.home)}")
    else:
        # No bold here: Discord headings are already bold.
        head = (f"## {logo(game.away.abbr)}{game.away.abbr} {game.away.score}  {_sep(game)}  "
                f"{logo(game.home.abbr)}{game.home.abbr} {game.home.score}")

    status_icon = {"pre": "🕐", "in": "🔴", "post": "✅"}.get(game.state, "")
    status_lines = [f"{status_icon} **{clock_text(game)}**"]
    if ball_text(game):
        status_lines.append(ball_text(game))
    embed.description = head + "\n" + "\n".join(status_lines)

    if game.state == "in" and game.last_play:
        embed.add_field(name="Last play", value=game.last_play[:1024], inline=False)

    if game.state != "pre":
        linescore = _linescore_block(game)
        if linescore:
            embed.add_field(name="Scoring by quarter", value=linescore, inline=False)
        if game.leaders:
            lines = [f"`{ld.category:<4}` **{ld.player}** ({ld.team_abbr}) · {ld.stats}" for ld in game.leaders]
            embed.add_field(name="Leaders", value="\n".join(lines)[:1024], inline=False)

    if game.state == "post" and game.headline:
        embed.add_field(name="Recap", value=game.headline[:1024], inline=False)

    # Small inline facts along the bottom.
    if game.tv:
        embed.add_field(name="📺 TV", value=game.tv[:1024], inline=True)
    if game.state == "pre":
        if game.venue:
            embed.add_field(name="📍 Venue", value=game.venue[:1024], inline=True)
        if game.line:
            embed.add_field(name="📈 Line", value=game.line[:1024], inline=True)
        if game.weather:
            embed.add_field(name="🌤️ Weather", value=game.weather[:1024], inline=True)
    return embed


# ---------------------------------------------------------------- league card


def build_league_embed(
    games: list[Game],
    week_label: str,
    updated: datetime,
    logos: Optional[dict[str, str]] = None,
    youtube: Optional[dict[str, str]] = None,
) -> discord.Embed:
    """Everyone else, as a grid of small game cards (three per row on desktop).

    Each card: status as its title, then logo + team name (+ score) for each team,
    the possession line while live, and a link: Highlights for finished games
    (NFL YouTube if we found it, else ESPN's video), Gamecast otherwise.
    `logos` maps abbreviations to logo emoji; `youtube` maps ESPN game ids to
    YouTube highlight links. Missing logos just show the team name.
    """
    logos = logos or {}
    youtube = youtube or {}

    def team_line(game: Game, team: Team, other: Team) -> str:
        logo = f"{logos[team.abbr]} " if team.abbr in logos else ""
        if game.state == "pre":
            return f"{logo}{team.short_name}"
        text = f"{team.short_name} {team.score}"
        try:
            if int(team.score) > int(other.score):
                text = f"**{text}**"  # leader / winner in bold
        except ValueError:
            pass
        return f"{logo}{text}"

    def link(game: Game) -> str:
        if game.state == "post":
            url = youtube.get(game.id) or game.highlights_url or game.gamecast_url
            return f"[Highlights]({url})" if url else ""
        return f"[Gamecast]({game.gamecast_url})" if game.gamecast_url else ""

    embed = discord.Embed(title=f"🏈 Around the NFL · {week_label}", color=LEAGUE_COLOR)
    sections = [
        ("🔴 Live", "in"),
        ("🗓️ Upcoming", "pre"),
        ("✅ Final", "post"),
    ]
    for heading, state in sections:
        section = sorted((g for g in games if g.state == state), key=lambda g: g.kickoff)
        if not section or len(embed.fields) >= 24:  # room for a header + at least one card
            continue
        # A full-width header field starts a new row of cards. (Field values can't
        # be empty, so it uses an invisible character.)
        embed.add_field(name=heading, value="\u200b", inline=False)
        for game in section:
            lines = [team_line(game, game.away, game.home), team_line(game, game.home, game.away)]
            if ball_text(game):
                lines.append(ball_text(game))
            if link(game):
                lines.append(link(game))
            # Live and upcoming cards keep their own clock / kickoff time; finals
            # don't need a title (the section header says it).
            title = clock_text(game) if state != "post" else "\u200b"
            embed.add_field(name=title, value="\n".join(lines)[:1024], inline=True)
            if len(embed.fields) >= 25:  # Discord's field limit
                break
    if not games:
        embed.description = "No other games this week."
    embed.set_footer(text=f"Last updated {central_time_text(updated)} · Data: ESPN")
    return embed


def build_embeds(
    week: Optional[Week],
    team: str,
    updated: datetime,
    logos: Optional[dict[str, str]] = None,
    youtube: Optional[dict[str, str]] = None,
) -> list[discord.Embed]:
    """Both cards for the scoreboard message. `week` is None if we've never had data."""
    if week is None:
        embed = discord.Embed(
            title="🏈 NFL Scoreboard",
            description="Waiting for scores from ESPN…",
            color=LEAGUE_COLOR,
        )
        embed.set_footer(text=f"Last updated {central_time_text(updated)}")
        return [embed]

    ours = next((g for g in week.games if g.involves(team)), None)
    others = [g for g in week.games if g is not ours]
    return [
        build_team_embed(ours, team, week.label, logos),
        build_league_embed(others, week.label, updated, logos, youtube),
    ]


# ---------------------------------------------------------------- week turnover


def _calendar(data: dict) -> list[tuple[str, str, datetime, datetime]]:
    """Every week in ESPN's calendar as (season type, week, start, end), in order."""
    weeks = []
    for part in (data.get("leagues") or [{}])[0].get("calendar") or []:
        for entry in part.get("entries") or []:
            try:
                weeks.append((
                    str(part["value"]), str(entry["value"]),
                    _parse_time(entry["startDate"]), _parse_time(entry["endDate"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(weeks, key=lambda w: w[2])


def held_week(data: dict, now: datetime) -> Optional[tuple[str, str]]:
    """ESPN starts each new week at 2:00 AM Central on Wednesday. We keep showing
    the previous week until 3:00 PM Central that Wednesday (around the first injury
    reports), so Wednesday morning is still recap time. Weeks that don't start on
    a Wednesday (Week 1, preseason) aren't held.

    Returns (season type, week) of the week to show instead, or None to use
    ESPN's current week. Works across preseason / regular season / playoffs
    because it follows ESPN's own calendar.
    """
    weeks = _calendar(data)
    for i, (_, _, start, end) in enumerate(weeks):
        if start <= now <= end:
            break
    else:
        return None  # not inside any calendar week (e.g. deep offseason)
    if i == 0:
        return None
    local_start = start.astimezone(CENTRAL)
    if local_start.weekday() != 2:  # Monday=0, Wednesday=2
        # Only ordinary weeks start on Wednesday. Week 1 (a Sunday) and preseason
        # weeks (Thursdays) switch straight away, so their games are never hidden.
        return None
    turnover = local_start.replace(hour=15, minute=0, second=0, microsecond=0)
    if now >= turnover:
        return None
    previous_type, previous_week, _, _ = weeks[i - 1]
    return previous_type, previous_week


# ---------------------------------------------------------------- cadence


def poll_interval(week: Optional[Week], now: datetime) -> tuple[int, str]:
    """How many seconds to wait before the next fetch, and a short reason why.

    - 60 s while any game is live (or should have kicked off by now)
    - 15 min when a game kicks off within the next 6 hours
    - 1 hour otherwise (overnight, weekdays, bye weeks)
    Never sleeps past the next kickoff, so the first live update is on time.
    """
    if week is None:
        return 300, "no data yet"

    if any(g.state == "in" for g in week.games):
        return 60, "game live"

    upcoming = [g.kickoff for g in week.games if g.state == "pre"
                and g.status_name not in ("STATUS_POSTPONED", "STATUS_CANCELED")]
    if any(k <= now for k in upcoming):
        return 60, "kickoff due"

    if not upcoming:
        return 3600, "no upcoming games"

    until_next = (min(upcoming) - now).total_seconds()
    if until_next <= 6 * 3600:
        base, reason = 900, "game day"
    else:
        base, reason = 3600, "no game soon"
    return max(60, min(base, int(until_next))), reason
