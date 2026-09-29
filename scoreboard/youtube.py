"""Find NFL game highlight videos on the NFL's official YouTube channel.

YouTube publishes a public feed of a channel's latest 15 videos (no API key
needed). The NFL titles its highlight videos like:

    "Philadelphia Eagles vs. Chicago Bears Game Highlights | 2026 NFL Season Week 3"

so we match the two full team names from ESPN's data. The feed only holds the
latest 15 videos, so the cog checks it every ~10 minutes on game days and saves
each link as it appears. Anything missed falls back to ESPN's highlight link.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Iterable

NFL_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UCDVYQ4Zhbm3S2dlz7P1GBDg"

# "<Team> vs. <Team> Game Highlights" (tolerates "vs" without the dot, and "Highlights" alone)
TITLE_RE = re.compile(r"^(?P<a>.+?)\s+vs\.?\s+(?P<b>.+?)\s+(?:Game\s+)?Highlights\b", re.IGNORECASE)
_NS = {"atom": "http://www.w3.org/2005/Atom"}


def parse_feed(xml_text: str) -> list[tuple[str, str, datetime]]:
    """(title, url, published) for each video in the feed."""
    videos = []
    root = ET.fromstring(xml_text)
    for entry in root.findall("atom:entry", _NS):
        title = entry.findtext("atom:title", default="", namespaces=_NS)
        link = entry.find("atom:link[@rel='alternate']", _NS)
        published = entry.findtext("atom:published", default="", namespaces=_NS)
        if link is None or not published:
            continue
        url = link.get("href", "")
        if "/shorts/" in url:
            continue  # vertical shorts are never the full game highlights
        videos.append((title, url, datetime.fromisoformat(published)))
    return videos


def match_highlights(videos: Iterable[tuple[str, str, datetime]], games) -> dict[str, str]:
    """{ESPN game id: YouTube url} for finished games whose highlights are in `videos`.

    A video only counts if it was published after that game's kickoff (and within
    3 days), so a rematch later in the season can't pick up the older game's video.
    """
    found: dict[str, str] = {}
    videos = list(videos)
    for game in games:
        if game.state != "post":
            continue
        names = {game.away.name.lower(), game.home.name.lower()}
        for title, url, published in videos:
            match = TITLE_RE.match(title)
            if not match:
                continue
            if {match["a"].strip().lower(), match["b"].strip().lower()} != names:
                continue
            if not (game.kickoff <= published <= game.kickoff + timedelta(days=3)):
                continue
            found[game.id] = url
            break
    return found
