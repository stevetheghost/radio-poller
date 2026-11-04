"""Export and import of the whole play history, for moving to another server.

The file format is JSON Lines: a header, then one line per station, one per play,
and an end marker. Import merges into the existing database, skipping plays that
are already there, so it is safe to run more than once.
"""
import json
import re

from . import db, stations

FORMAT = "radio-poller"
VERSION = 2          # 2 added each station's feed settings
READABLE_VERSIONS = (1, 2)
MAX_LINE = 65536
BATCH = 2000
STATION_ID = re.compile(r"[a-z0-9_-]{1,64}")
MAX_TEXT = 300
MAX_TIME = 4102444800  # year 2100


class FormatError(ValueError):
    """The file isn't a radio-poller export this version can read."""


def _line(obj):
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def export_lines(conn, now, station=None, plays=True):
    """Yield the export as chunks of bytes, read from one consistent snapshot.
    Pass a station id to export just that station, and plays=False for the stations' settings only."""
    args = (station,) if station else ()
    conn.execute("BEGIN")
    try:
        total = conn.execute("SELECT COUNT(*) FROM plays" + (" WHERE station_id = ?" if station else ""), args).fetchone()[0] if plays else 0
        yield _line({"type": "header", "format": FORMAT, "version": VERSION, "exported_at": now, "plays": total})
        rows = conn.execute(
            "SELECT id, name, sort, active, provider, provider_id, poll_seconds, timezone FROM stations"
            + (" WHERE id = ?" if station else "") + " ORDER BY sort, id", args)
        for s in rows:
            yield _line({"type": "station", "id": s["id"], "name": s["name"], "sort": s["sort"], "active": s["active"],
                         "provider": s["provider"], "provider_id": s["provider_id"], "poll_seconds": s["poll_seconds"],
                         "timezone": s["timezone"]})
        cur = conn.execute(
            "SELECT station_id, artist, title, played_at, image_url FROM plays"
            + (" WHERE station_id = ?" if station else "") + " ORDER BY played_at, id", args) if plays else None
        while cur is not None:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            yield b"".join(
                _line({"type": "play", "station_id": r["station_id"], "artist": r["artist"], "title": r["title"],
                       "played_at": r["played_at"], "image_url": r["image_url"]})
                for r in rows
            )
        yield _line({"type": "end", "plays": total})
    finally:
        conn.rollback()


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()[:MAX_TEXT]


def _feed(obj):
    """The feed settings in a station record, or (None, None, None) if absent or not usable here."""
    try:
        provider_id = stations.source(obj.get("provider"), obj.get("provider_id"))
    except stations.StationError:
        return None, None, None
    poll = obj.get("poll_seconds")
    ok = isinstance(poll, int) and not isinstance(poll, bool) and stations.MIN_POLL <= poll <= stations.MAX_POLL
    return obj["provider"], provider_id, poll if ok else None


def _station_id(value):
    if not isinstance(value, str) or not STATION_ID.fullmatch(value):
        raise ValueError("bad station id")
    return value


def import_stream(conn, reader):
    """Merge an export read from `reader` (anything with readline(limit)) into the database.

    Raises FormatError if the file isn't a usable export. Individual bad lines are
    counted and skipped. Plays are committed in batches, so an interrupted import
    keeps what was done and can simply be run again."""
    result = {"stations_added": 0, "plays_added": 0, "plays_skipped": 0,
              "invalid_lines": 0, "errors": [], "complete": False}
    known = {r["id"] for r in conn.execute("SELECT id FROM stations")}
    seen_header = False
    pending = 0

    def ensure_station(sid, name, feed=(None, None, None), enabled=False, tz=None):
        if sid in known:  # stations this server already has keep their own settings
            return
        provider, provider_id, poll = feed
        # Without a usable feed a station can't be polled, so it arrives paused with its history.
        db.add_station(conn, sid, name or sid, provider, provider_id, poll, active=enabled and provider is not None, tz=tz)
        known.add(sid)
        result["stations_added"] += 1

    try:
        for raw in iter(lambda: reader.readline(MAX_LINE + 1), b""):
            if not raw.strip():
                continue
            try:
                if len(raw) > MAX_LINE:
                    raise ValueError("line too long")
                try:
                    obj = json.loads(raw)
                except ValueError:  # bad JSON or bad UTF-8
                    raise ValueError("a line isn't valid JSON") from None
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
            except ValueError as exc:
                if not seen_header:
                    raise FormatError("this isn't a radio-poller export file") from exc
                result["invalid_lines"] += 1
                if len(result["errors"]) < 5:
                    result["errors"].append(str(exc))
                continue

            if not seen_header:
                if obj.get("type") != "header" or obj.get("format") != FORMAT:
                    raise FormatError("this isn't a radio-poller export file")
                if obj.get("version") not in READABLE_VERSIONS:
                    raise FormatError("unsupported export version %r (this server reads versions %s)"
                                      % (obj.get("version"), " and ".join(map(str, READABLE_VERSIONS))))
                seen_header = True
                continue

            kind = obj.get("type")
            try:
                if kind == "station":
                    ensure_station(_station_id(obj.get("id")), _text(obj.get("name"), "name"), _feed(obj), obj.get("active") == 1, obj.get("timezone"))
                elif kind == "play":
                    sid = _station_id(obj.get("station_id"))
                    artist, title = _text(obj.get("artist"), "artist"), _text(obj.get("title"), "title")
                    played_at = obj.get("played_at")
                    if not isinstance(played_at, int) or isinstance(played_at, bool) or not 0 < played_at < MAX_TIME:
                        raise ValueError("bad played_at")
                    image = obj.get("image_url")
                    if not (isinstance(image, str) and re.match(r"https?://", image) and len(image) <= 2000):
                        image = None
                    ensure_station(sid, None)
                    if db.insert_play(conn, sid, artist, title, played_at, image):
                        result["plays_added"] += 1
                    else:
                        result["plays_skipped"] += 1
                    pending += 1
                    if pending >= BATCH:
                        conn.commit()
                        pending = 0
                elif kind == "end":
                    result["complete"] = True
                else:
                    raise ValueError("unknown record type %r" % (kind,))
            except ValueError as exc:
                result["invalid_lines"] += 1
                if len(result["errors"]) < 5:
                    result["errors"].append(str(exc))
        if not seen_header:
            raise FormatError("the file is empty")
        conn.commit()
    except BaseException:
        conn.commit()  # keep completed batches; a rerun will skip them
        raise
    return result
