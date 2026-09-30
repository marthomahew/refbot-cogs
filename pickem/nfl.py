"""ESPN schedule/results for pick'em, and all the scoring rules.

Everything here is plain data in and out (no Discord), so it can be tested
without a bot. A week is stored in Config as a dict:

    {
      "label": "Week 4", "season": 2026,
      "games": {game_id: {"away": "PIT", "home": "CLE", "away_name": "Steelers",
                          "home_name": "Browns", "kickoff": "2026-10-02T00:15:00+00:00",
                          "state": "pre|in|post", "status": "STATUS_FINAL",
                          "away_score": 24, "home_score": 20,
                          "winner": None | "PIT" | "TIE" | "VOID"}},
      "picks": {user_id: {game_id: "PIT"}},
      "tb": {...tiebreaker, see TIEBREAK below...},
      "opened": False, "posted": False,
    }

Rules (straight up, no spread):
- A pick locks at that game's kickoff (or as soon as ESPN says it started).
- Right pick = a win. Wrong pick OR no pick = a loss, once you've played that
  week (made at least one pick). An NFL tie is a push for everybody.
- Postponed / canceled games that never finish that week don't count ("VOID").
- The most wins takes the week. If that's a tie, the Monday night tiebreaker
  (total points) decides it; if it's still tied, they share the week.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("red.refbot.pickem")

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

try:
    CENTRAL = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    CENTRAL = timezone(timedelta(hours=-5), "CT")

NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE",
    "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH",
}

# ESPN statuses for games that aren't going to be played as scheduled.
NOT_PLAYING = ("STATUS_POSTPONED", "STATUS_CANCELED")


# ---------------------------------------------------------------- ESPN


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_week(data: dict) -> Optional[dict]:
    """ESPN's JSON -> {"key", "label", "season", "type", "games"}.
    Returns None for preseason (not played) or if the shape is unusable."""
    try:
        season = int(data["season"]["year"])
        stype = str(data["season"]["type"])
        number = str(data["week"]["number"])
        events = data["events"]
    except (KeyError, TypeError, ValueError):
        log.warning("ESPN scoreboard shape changed; can't read the week")
        return None
    if stype == "1":  # preseason
        return None

    games = {}
    for event in events:
        try:
            comp = event["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            away, home = teams["away"], teams["home"]
            stype_ = (event.get("status") or {}).get("type") or {}
            game = {
                "away": away["team"]["abbreviation"],
                "home": home["team"]["abbreviation"],
                "away_name": away["team"].get("shortDisplayName") or away["team"]["abbreviation"],
                "home_name": home["team"].get("shortDisplayName") or home["team"]["abbreviation"],
                "kickoff": _time(event["date"]).isoformat(),
                "state": stype_.get("state", "pre"),
                "status": stype_.get("name", ""),
                "away_score": int(float(away.get("score") or 0)),
                "home_score": int(float(home.get("score") or 0)),
                "winner": None,
            }
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Skipping an ESPN game we couldn't read (id=%s)", event.get("id"))
            continue
        if game["state"] == "post" and game["status"] not in NOT_PLAYING:
            if away.get("winner"):
                game["winner"] = game["away"]
            elif home.get("winner"):
                game["winner"] = game["home"]
            elif game["away_score"] == game["home_score"]:
                game["winner"] = "TIE"
        if game["away"] not in NFL_TEAMS or game["home"] not in NFL_TEAMS:
            continue  # e.g. the Pro Bowl (AFC vs NFC)
        games[str(event["id"])] = game

    label = f"Week {number}"
    try:
        for part in data["leagues"][0]["calendar"]:
            if str(part.get("value")) == stype:
                for entry in part.get("entries", []):
                    if str(entry.get("value")) == number:
                        label = entry["label"]
    except (KeyError, IndexError, TypeError):
        pass
    if not games:
        return None  # nothing to pick (Pro Bowl week, or an empty week)
    return {"key": f"{season}-{stype}-{number}", "label": label, "season": season, "type": stype, "games": games}


def _calendar(data: dict) -> list[tuple[str, str]]:
    """Every week in ESPN's calendar as (season type, week), in date order."""
    weeks = []
    for part in (data.get("leagues") or [{}])[0].get("calendar") or []:
        for entry in part.get("entries") or []:
            try:
                weeks.append((_time(entry["startDate"]), str(part["value"]), str(entry["value"])))
            except (KeyError, TypeError, ValueError):
                continue
    return [(t, w) for _, t, w in sorted(weeks)]


def next_week(data: dict) -> Optional[tuple[str, str]]:
    """(season type, week) of the week after the one ESPN sent, or None.

    Pick'em moves on as soon as a week's last game is final (normally right
    after Monday night) instead of waiting for ESPN to switch on Wednesday."""
    try:
        this = (str(data["season"]["type"]), str(data["week"]["number"]))
    except (KeyError, TypeError):
        return None
    weeks = _calendar(data)
    if this in weeks and weeks.index(this) + 1 < len(weeks):
        return weeks[weeks.index(this) + 1]
    return None


def merge_games(stored: dict, fresh: dict) -> bool:
    """Copy ESPN's latest into the stored games. Returns True if anything changed.
    Games ESPN dropped from the week are kept (so picks on them still show)."""
    changed = False
    for gid, game in fresh.items():
        if stored.get(gid) != game:
            stored[gid] = game
            changed = True
    return changed


# ---------------------------------------------------------------- times


def kickoff(game: dict) -> datetime:
    return datetime.fromisoformat(game["kickoff"])


def is_locked(game: dict, now: datetime) -> bool:
    return game["state"] != "pre" or now >= kickoff(game) or game["winner"] is not None


def is_done(game: dict) -> bool:
    """Finished, or not being played this week."""
    return game["winner"] is not None or game["status"] in NOT_PLAYING


def kickoff_text(game: dict) -> str:
    """e.g. "Thu 7:15 PM"."""
    local = kickoff(game).astimezone(CENTRAL)
    hour = local.hour % 12 or 12
    return f"{local:%a} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def ordered(games: dict) -> list[tuple[str, dict]]:
    """Games in kickoff order (then by id, so the order never jumps around)."""
    return sorted(games.items(), key=lambda kv: (kv[1]["kickoff"], kv[0]))


# ---------------------------------------------------------------- scoring


def record(week: dict, uid: str, only: Optional[set] = None) -> tuple[int, int, int]:
    """(wins, losses, pushes) for one player so far. `only` limits it to some games."""
    picks = week["picks"].get(uid, {})
    wins = losses = pushes = 0
    for gid, game in week["games"].items():
        if only is not None and gid not in only:
            continue
        winner = game["winner"]
        if winner is None or winner == "VOID":
            continue
        if winner == "TIE":
            pushes += 1
        elif picks.get(gid) == winner:
            wins += 1
        else:
            losses += 1  # wrong pick, or no pick
    return wins, losses, pushes


def players(week: dict) -> list[str]:
    return [uid for uid, picks in week["picks"].items() if picks]


def record_text(rec: tuple[int, int, int]) -> str:
    wins, losses, pushes = rec
    return f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")


def standings(week: dict) -> list[tuple[str, tuple[int, int, int]]]:
    """Players sorted by wins (then fewest losses)."""
    rows = [(uid, record(week, uid)) for uid in players(week)]
    return sorted(rows, key=lambda r: (-r[1][0], r[1][1]))


def season_standings(weeks: dict, season: int) -> list[tuple[str, tuple[int, int, int], int]]:
    """(uid, total record, weeks played), sorted by wins."""
    totals: dict[str, list[int]] = {}
    for week in weeks.values():
        if week.get("season") != season:
            continue
        for uid in players(week):
            w, l, p = record(week, uid)
            t = totals.setdefault(uid, [0, 0, 0, 0])
            t[0] += w
            t[1] += l
            t[2] += p
            t[3] += 1
    rows = [(uid, (t[0], t[1], t[2]), t[3]) for uid, t in totals.items()]
    return sorted(rows, key=lambda r: (-r[1][0], r[1][1]))


# ---------------------------------------------------------------- tiebreaker
#
# The tiebreaker is the total points of the week's last game day (normally
# Monday night; both games if there are two). Once every earlier game is final,
# anyone who could still finish tied for first gets asked for a guess, which
# locks at the first Monday kickoff. Stored in week["tb"]:
#   {"games": [ids], "contenders": [uids], "guesses": {uid: points},
#    "message_id": id, "done": True once we've checked}


def tiebreak_games(week: dict) -> Optional[list[str]]:
    """Ids of the last game day's games, or None if the whole week is one day
    (then there's nothing earlier to base a tie on, so tied players share)."""
    games = ordered(week["games"])
    playing = [(gid, g) for gid, g in games if g["status"] not in NOT_PLAYING]
    if not playing:
        return None
    last_day = kickoff(playing[-1][1]).astimezone(CENTRAL).date()
    last = [gid for gid, g in playing if kickoff(g).astimezone(CENTRAL).date() == last_day]
    if len(last) == len(playing):
        return None
    return last


def tiebreak_due(week: dict, now: datetime) -> Optional[list[str]]:
    """If it's time to ask for tiebreaker guesses, the ids of the tiebreaker games."""
    tb_ids = tiebreak_games(week)
    if not tb_ids:
        return None
    earlier_done = all(is_done(g) for gid, g in week["games"].items() if gid not in tb_ids)
    not_started = not any(is_locked(week["games"][gid], now) for gid in tb_ids)
    return tb_ids if earlier_done and not_started else None


def contenders(week: dict, tb_ids: list[str]) -> list[str]:
    """Players who could still finish tied for (or in) first place: their wins
    so far plus a win in every remaining game reach the current leader."""
    earlier = set(week["games"]) - set(tb_ids)
    wins = {uid: record(week, uid, only=earlier)[0] for uid in players(week)}
    if not wins:
        return []
    leader = max(wins.values())
    return [uid for uid, w in wins.items() if w + len(tb_ids) >= leader]


def tiebreak_total(week: dict) -> Optional[int]:
    """Total points of the tiebreaker games, once they're all final."""
    tb_ids = (week.get("tb") or {}).get("games") or []
    if not tb_ids:
        return None
    games = [week["games"].get(gid) for gid in tb_ids]
    if any(g is None or g["winner"] in (None, "VOID") for g in games):
        return None
    return sum(g["away_score"] + g["home_score"] for g in games)


def winners(week: dict) -> tuple[list[str], list[str], Optional[int]]:
    """(winners, everyone tied on wins, tiebreaker total). Winners is more than
    one person only if they're still tied after the tiebreaker."""
    table = standings(week)
    if not table:
        return [], [], None
    top = table[0][1][0]
    tied = [uid for uid, rec in table if rec[0] == top]
    if len(tied) == 1:
        return tied, tied, None
    total = tiebreak_total(week)
    guesses = (week.get("tb") or {}).get("guesses") or {}
    guessed = {uid: abs(guesses[uid] - total) for uid in tied if uid in guesses}
    if total is None or not guessed:
        return tied, tied, total
    best = min(guessed.values())
    return [uid for uid, d in guessed.items() if d == best], tied, total
