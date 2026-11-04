"""Adapters that fetch a station's recent plays from its public now-playing feed.

To support a new kind of feed, write a function taking (station, count) and
returning a list of Play, then register it in PROVIDERS. Stations in
config/stations.json select an adapter with their "provider" field.
"""
import base64
import json
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

USER_AGENT = "radio-poller/1.0 (personal song-play logger)"
TIMEOUT = 10


class Play(NamedTuple):
    artist: str
    title: str
    played_at: int  # unix seconds
    image: Optional[str] = None  # album art URL, if the feed has one


def get_json(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def _iheart_image(url, size=300):
    """iHeart's feed links the original artwork (often 2-3 MB); their image service
    can serve a thumbnail of it instead."""
    if not url:
        return None
    token = base64.urlsafe_b64encode(url.replace("http://", "https://", 1).encode()).decode().rstrip("=")
    return f"https://i.iheart.com/v3/url/{token}?ops=cover({size},{size})"


def iheart(station, count) -> List[Play]:
    """iHeartRadio live-meta track history. `station_id` is the numeric iHeart
    station id (e.g. 2501 for 97.1 WASH-FM)."""
    sid = quote(str(station["station_id"]), safe="")
    data = get_json(f"https://us.api.iheart.com/api/v3/live-meta/stream/{sid}/trackHistory?limit={count}")
    if "data" not in data:
        raise ValueError("unexpected iHeart response: %s" % str(data)[:120])
    plays = []
    for item in data["data"]:
        artist, title, start = item.get("artist"), item.get("title"), item.get("startTime")
        if artist and title and start:
            plays.append(Play(artist, title, int(start), _iheart_image(item.get("imagePath"))))
    return plays


def audacy(station, count) -> List[Play]:
    """Audacy now-playing feed. `station_id` is Audacy's station code (e.g.
    "101-988" for 101.3 2WD, found as STATION_ROC_ID in the station page source)."""
    sid = quote(str(station["station_id"]), safe="")
    data = get_json(f"https://api.audacy.com/experience/v2/stations/{sid}/nowplaying?count={count}")
    if "performances" not in data:
        raise ValueError("unexpected Audacy response: %s" % str(data)[:120])
    plays = []
    for item in data["performances"]:
        artist, title, when = item.get("artist"), item.get("title"), item.get("time")
        if not (artist and title and when):
            continue
        try:
            ts = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        plays.append(Play(artist, title, int(ts.timestamp()), item.get("mediumimage") or None))
    return plays


PROVIDERS = {"iheart": iheart, "audacy": audacy}


def fetch(station, count) -> List[Play]:
    return PROVIDERS[station["provider"]](station, count)
