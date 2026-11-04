"""Changes to the stations list, made from the Stations page."""
import re
import time

from . import db, providers

MAX_NAME = 60
MIN_POLL, MAX_POLL = 15, 3600
STATION_ID = re.compile(r"[a-z0-9_-]{1,64}")
PROVIDER_ID = {
    "iheart": (re.compile(r"\d{1,10}"), "iHeart station ids are numbers, like 2501"),
    "audacy": (re.compile(r"\d{1,6}-\d{1,8}"), "Audacy station codes look like 101-988"),
}


class StationError(ValueError):
    """A problem with the request, worded for the user. `status` is the HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise StationError("give the station a name")
    return value.strip()[:MAX_NAME]


def _timezone(value):
    """A timezone name like America/Los_Angeles, or None to use the server's."""
    if value in (None, ""):
        return None
    tz = db.valid_timezone(value)
    if tz is None:
        raise StationError("unknown timezone; use a name like America/New_York")
    return tz


def _poll_seconds(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_POLL <= value <= MAX_POLL:
        raise StationError("poll seconds must be a whole number from %d to %d" % (MIN_POLL, MAX_POLL))
    return value


def source(provider, provider_id):
    """Validate a provider and station id, returning the id as a string."""
    if provider not in providers.PROVIDERS:
        raise StationError("choose iHeart or Audacy as the provider")
    pattern, hint = PROVIDER_ID[provider]
    provider_id = str(provider_id if provider_id is not None else "").strip()
    if not pattern.fullmatch(provider_id):
        raise StationError(hint)
    return provider_id


def test_feed(provider, provider_id):
    """Fetch the feed once. Returns the newest play (or None for an empty feed); raises if it fails."""
    try:
        plays = providers.fetch({"provider": provider, "station_id": provider_id}, 3)
    except Exception as exc:
        raise StationError("that station's feed didn't work (%s: %s)" % (type(exc).__name__, exc), 422) from exc
    return max(plays, key=lambda p: p.played_at) if plays else None


def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "station"
    sid, n = base, 2
    while sid in taken:
        sid = "%s-%d" % (base, n)
        n += 1
    return sid


def create(conn, data):
    """Add a station, after checking its feed works. If a station with the same name exists
    but has no feed (its history came from an import), the feed is attached to it instead."""
    name = _name(data.get("name"))
    provider = data.get("provider")
    provider_id = source(provider, data.get("provider_id"))
    poll = _poll_seconds(data.get("poll_seconds"))
    tz = _timezone(data.get("timezone"))
    wanted = data.get("id")
    if wanted is not None and not (isinstance(wanted, str) and STATION_ID.fullmatch(wanted)):
        raise StationError("station ids use lowercase letters, digits, - and _")

    # The same feed can be added under another name (say, a station's Christmas-music version). Two copies that
    # are both switched on would log every song twice, so a copy starts paused unless asked otherwise.
    duplicate = conn.execute("SELECT 1 FROM stations WHERE provider = ? AND provider_id = ?", (provider, provider_id)).fetchone()
    enabled = data.get("enabled", duplicate is None)
    if not isinstance(enabled, bool):
        raise StationError("enabled must be true or false")
    newest = test_feed(provider, provider_id)
    # A copy only records songs from after it's switched on, so it doesn't pick up what the other copy logged.
    enabled_at = int(time.time()) if duplicate else None

    rows = {r["id"]: r["provider"] for r in conn.execute("SELECT id, provider FROM stations")}
    if wanted:
        sid = wanted
    else:
        base = _slug(name, set())
        sid = base if base in rows and rows[base] is None else _slug(name, set(rows))
    if sid in rows:
        if rows[sid] is not None:
            raise StationError("a station with the id \"%s\" already exists" % sid, 409)
        conn.execute("UPDATE stations SET name = ?, provider = ?, provider_id = ?, poll_seconds = ?, active = ?, enabled_at = ?, "
                     "timezone = COALESCE(?, timezone) WHERE id = ?",
                     (name, provider, provider_id, poll, int(enabled), enabled_at, tz, sid))
    else:
        db.add_station(conn, sid, name, provider, provider_id, poll, active=enabled, enabled_at=enabled_at, tz=tz)
    conn.commit()
    return sid, newest


def update(conn, sid, data):
    row = conn.execute("SELECT provider, active FROM stations WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise StationError("no such station", 404)
    sets, args = [], []
    if "name" in data:
        sets.append("name = ?")
        args.append(_name(data["name"]))
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise StationError("enabled must be true or false")
        if data["enabled"] and row["provider"] is None:
            raise StationError("this station has no feed set up, so it can't be switched on. Add it again by URL to reconnect it")
        sets.append("active = ?")
        args.append(int(data["enabled"]))
        if data["enabled"] and not row["active"]:
            # Switching a station back on starts it fresh: the feed's recent history was played while
            # it was off (and may be logged by another copy), so only songs from now on are recorded.
            sets.append("enabled_at = ?")
            args.append(int(time.time()))
    if "poll_seconds" in data:
        sets.append("poll_seconds = ?")
        args.append(_poll_seconds(data["poll_seconds"]))
    if "timezone" in data:
        sets.append("timezone = ?")
        args.append(_timezone(data["timezone"]))
    if sets:
        conn.execute("UPDATE stations SET %s WHERE id = ?" % ", ".join(sets), args + [sid])
        conn.commit()


def reorder(conn, ids):
    current = [r["id"] for r in conn.execute("SELECT id FROM stations ORDER BY sort, id")]
    if not isinstance(ids, list) or any(i not in current for i in ids):
        raise StationError("that isn't a list of known stations")
    ordered = list(dict.fromkeys(ids)) + [i for i in current if i not in ids]
    conn.executemany("UPDATE stations SET sort = ? WHERE id = ?", [(n, i) for n, i in enumerate(ordered)])
    conn.commit()


def delete(conn, sid, confirm):
    if not conn.execute("SELECT 1 FROM stations WHERE id = ?", (sid,)).fetchone():
        raise StationError("no such station", 404)
    if confirm != sid:
        raise StationError("deleting needs ?confirm=%s" % sid)
    plays = conn.execute("DELETE FROM plays WHERE station_id = ?", (sid,)).rowcount
    conn.execute("DELETE FROM stations WHERE id = ?", (sid,))
    conn.commit()
    return plays
