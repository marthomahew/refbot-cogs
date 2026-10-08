"""Reading game results out of people's share messages. Plain logic, no Discord.

Each game turns a message into a Result (or None if the message isn't that
game's share). Results always use the date written in the share itself, so a
late post still counts for the right day's puzzle.

Built in: Worldle, Maptap and DailyOrbs. Admins can add simple-score games from
Discord (`dailygames learn`); those are LearnedGame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


@dataclass
class Result:
    game: str  # display name, e.g. "Worldle"
    day: date  # the puzzle's date
    sort: tuple  # lower sorts first (best)
    text: str  # e.g. "1/6", "954", "9 orbs · 7 misses"
    title: Optional[str] = None  # e.g. "Worldle #1721" (card title), else the game name


def _year_near(month: int, day: int, posted: date) -> Optional[date]:
    """A month + day with no year: the one closest to when it was posted."""
    for year in (posted.year, posted.year - 1, posted.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if abs((candidate - posted).days) <= 180:
            return candidate
    return None


def find_date(text: str, posted: date) -> Optional[date]:
    """A date written in a share, in any of the usual formats."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)  # 2026-10-08
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)  # 08.10.2026
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            pass
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:,?\s+(\d{4}))?", text, re.IGNORECASE)
    if m:  # October 8 [2026]
        month, day = MONTHS[m[1].lower()], int(m[2])
        if m[3]:
            try:
                return date(int(m[3]), month, day)
            except ValueError:
                return None
        return _year_near(month, day, posted)
    return None


# ---------------------------------------------------------------- built-in games

WORLDLE_RE = re.compile(r"#Worldle\s*#(\d+)\s*\((\d{1,2})\.(\d{1,2})\.(\d{4})\)\s*([X\d])/(\d)", re.IGNORECASE)


def worldle(text: str, posted: date) -> Optional[Result]:
    """#Worldle #1721 (08.10.2026) 1/6 (100%). Fewest guesses wins; X/6 last."""
    m = WORLDLE_RE.search(text)
    if not m:
        return None
    try:
        day = date(int(m[4]), int(m[3]), int(m[2]))
    except ValueError:
        return None
    total = int(m[6])
    guesses = total + 1 if m[5].upper() == "X" else int(m[5])
    return Result("Worldle", day, (guesses,), f"{m[5].upper()}/{total}", title=f"Worldle #{m[1]}")


MAPTAP_RE = re.compile(r"maptap\.gg\S*\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2})", re.IGNORECASE)
MAPTAP_SCORE_RE = re.compile(r"Final score:\s*(\d+)", re.IGNORECASE)


def maptap(text: str, posted: date) -> Optional[Result]:
    """www.maptap.gg October 8 / ... / Final score: 954. Highest wins."""
    m, score = MAPTAP_RE.search(text), MAPTAP_SCORE_RE.search(text)
    if not m or not score:
        return None
    day = _year_near(MONTHS[m[1].lower()], int(m[2]), posted)
    if day is None:
        return None
    points = int(score[1])
    return Result("Maptap", day, (-points,), str(points))


DAILYORBS_RE = re.compile(r"DailyOrbs\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s+(\d{4})", re.IGNORECASE)
ORBS = set("🟠🟢🔵🟣")  # an orb earned
MISSES = set("🧡💚💙💜")  # a heart lost (a mistake)


def dailyorbs(text: str, posted: date) -> Optional[Result]:
    """DailyOrbs October 8 2026 + a row of orbs (circles), mistakes (hearts) and a 🏆
    at the 6th orb. Most orbs wins, then fewest mistakes. No row = not counted."""
    m = DAILYORBS_RE.search(text)
    if not m:
        return None
    try:
        day = date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
    except ValueError:
        return None
    orbs = sum(1 for ch in text if ch in ORBS)
    misses = sum(1 for ch in text if ch in MISSES)
    if orbs == 0 and misses == 0:
        return None  # shared without the result row
    return Result("DailyOrbs", day, (-orbs, misses),
                  f"{orbs} orb{'s' if orbs != 1 else ''} · {misses} miss{'es' if misses != 1 else ''}")


BUILT_IN = [worldle, maptap, dailyorbs]

# Where the "Play" buttons go. DailyOrbs uses dated pages.
PLAY_LINKS = {
    "Worldle": lambda today: "https://worldle.teuteuf.fr/",
    "Maptap": lambda today: "https://www.maptap.gg/",
    "DailyOrbs": lambda today: f"https://dailyorbs.com/{today.isoformat()}",
}


# ---------------------------------------------------------------- learned games
#
# A learned game is stored as a dict:
#   {"name": "Costcodle", "match": "costcodle.com", "kind": "fraction" | "label" | "percent",
#    "label": "Final score" (for "label"), "denominator": 6 (for "fraction"),
#    "better": "lower" | "higher", "link": "https://costcodle.com"}

FRACTION_RE = re.compile(r"(?<![\w/])([X\d]{1,3})/(\d{1,3})(?![\w/])", re.IGNORECASE)
LABEL_RE = re.compile(r"([A-Za-z][A-Za-z ]{1,24}?):\s*(\d[\d,]*(?:\.\d+)?)")
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
URL_RE = re.compile(r"https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", re.IGNORECASE)
BARE_DOMAIN_RE = re.compile(r"\b(?:www\.)?([a-z0-9-]+\.(?:com|gg|io|net|org|app|fr|co|dev|xyz|me|game|games))\b",
                            re.IGNORECASE)


def score_candidates(text: str) -> list[dict]:
    """Every number in a share that could be the score, for the admin to pick."""
    found = []
    for m in FRACTION_RE.finditer(text):
        found.append({"kind": "fraction", "denominator": int(m[2]), "example": f"{m[1]}/{m[2]}",
                      "better": "lower"})
    for m in LABEL_RE.finditer(text):
        label = m[1].strip()
        found.append({"kind": "label", "label": label, "example": f"{label}: {m[2]}", "better": "higher"})
    for m in PERCENT_RE.finditer(text):
        found.append({"kind": "percent", "example": f"{m[1]}%", "better": "higher"})
    seen, unique = set(), []
    for c in found:  # drop exact repeats
        key = (c["kind"], c.get("denominator"), c.get("label", "").lower())
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique[:25]


def identify(text: str) -> tuple[str, str, Optional[str]]:
    """(suggested name, text that identifies this game's shares, link) from a share.
    Uses the game's website if the share has one, else the first words."""
    url = URL_RE.search(text) or BARE_DOMAIN_RE.search(text)
    if url:
        domain = url[1].lower()
        name = domain.split(".")[0].capitalize()
        return name, domain, f"https://{domain}"
    first = re.sub(r"[^A-Za-z ]", " ", text.strip().splitlines()[0] if text.strip() else "").split()
    name = first[0] if first else "Game"
    return name.capitalize(), name.lower(), None


def learned(spec: dict, text: str, posted: date) -> Optional[Result]:
    if spec["match"].lower() not in text.lower():
        return None
    value, shown = None, None
    if spec["kind"] == "fraction":
        for m in FRACTION_RE.finditer(text):
            if int(m[2]) == spec["denominator"]:
                value = spec["denominator"] + 1 if m[1].upper() == "X" else int(m[1])
                shown = f"{m[1].upper()}/{m[2]}"
                break
    elif spec["kind"] == "label":
        for m in LABEL_RE.finditer(text):
            if m[1].strip().lower() == spec["label"].lower():
                value = float(m[2].replace(",", ""))
                shown = m[2]
                break
    elif spec["kind"] == "percent":
        m = PERCENT_RE.search(text)
        if m:
            value, shown = float(m[1]), f"{m[1]}%"
    if value is None:
        return None
    day = find_date(text, posted) or posted  # no date in the share: the day it was posted
    sort = (value,) if spec["better"] == "lower" else (-value,)
    return Result(spec["name"], day, sort, shown)


def read_all(text: str, posted: date, learned_specs: list[dict]) -> list[Result]:
    """Every game result in a message (people sometimes paste two at once)."""
    results = [r for parse in BUILT_IN if (r := parse(text, posted))]
    built_in_names = {r.game for r in results}
    for spec in learned_specs:
        r = learned(spec, text, posted)
        if r and r.game not in built_in_names:
            results.append(r)
    return results


def yesterday(today: date) -> date:
    return today - timedelta(days=1)
