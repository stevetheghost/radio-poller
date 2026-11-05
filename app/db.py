import logging
import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# A station can't legitimately play the same song twice this close together, so
# plays inside this window are treated as one play seen with a shifted timestamp.
DEDUPE_WINDOW = 90

# Columns added after the first release, for databases created before them.
STATION_COLUMNS = {"color": "INTEGER", "provider": "TEXT", "provider_id": "TEXT", "poll_seconds": "INTEGER",
                   "enabled_at": "INTEGER", "timezone": "TEXT"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    sort         INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,   -- 1 = polled, 0 = paused (history is kept)
    last_poll_at INTEGER,
    last_ok_at   INTEGER,
    last_error   TEXT,
    color        INTEGER,                      -- chart colour slot; never changes on reorder
    provider     TEXT,                         -- which adapter reads its feed (NULL = no feed set up)
    provider_id  TEXT,                         -- the station's id within that provider
    poll_seconds INTEGER,                      -- per-station poll interval override
    enabled_at   INTEGER,                      -- when it was last switched on; earlier plays in the feed are ignored
    timezone     TEXT                          -- IANA name of the station's local time (NULL = the server's TZ)
);

CREATE TABLE IF NOT EXISTS plays (
    id         INTEGER PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(id),
    artist     TEXT NOT NULL,
    title      TEXT NOT NULL,
    song_key   TEXT NOT NULL,
    played_at  INTEGER NOT NULL,
    image_url  TEXT,
    UNIQUE (station_id, played_at, song_key)
);

-- Spotify matches, filled in the first time a song's or artist's Spotify button is used (see spotify.py).
-- A NULL id means Spotify had nothing trustworthy; it's searched again after a while. Nothing here is a secret.
CREATE TABLE IF NOT EXISTS spotify_tracks (
    song_key   TEXT PRIMARY KEY,
    track_id   TEXT,
    name       TEXT,                           -- "Artist - Title" as Spotify names it, to check matches by eye
    matched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_artists (
    artist_key TEXT PRIMARY KEY,               -- the artist's name, case-folded
    artist_id  TEXT,
    name       TEXT,
    matched_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS plays_station_time ON plays (station_id, played_at);
CREATE INDEX IF NOT EXISTS plays_song_time ON plays (song_key, played_at);
CREATE INDEX IF NOT EXISTS plays_time ON plays (played_at);
"""


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


def song_key(artist, title):
    return f"{norm(artist)}|{norm(title)}".casefold()


def load_tz(name):
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("unknown timezone %r, falling back to UTC", name)
        return timezone.utc


def valid_timezone(name):
    """The name if it's a timezone this server knows (like America/Los_Angeles), otherwise None."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}", name) or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


def connect(path, tz=None):
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    if tz is not None:
        add_time_functions(conn, tz)
    return conn


def add_time_functions(conn, tz):
    """SQL functions that group a UTC timestamp into a calendar day or hour in `tz`."""
    conn.create_function("localday", 1, lambda t: datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d"), deterministic=True)
    conn.create_function("localhour", 1, lambda t: datetime.fromtimestamp(t, tz).strftime("%Y-%m-%dT%H"), deterministic=True)


def init(path):
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Databases created before album art was added lack this column.
        if "image_url" not in {r["name"] for r in conn.execute("PRAGMA table_info(plays)")}:
            conn.execute("ALTER TABLE plays ADD COLUMN image_url TEXT")
        have = {r["name"] for r in conn.execute("PRAGMA table_info(stations)")}
        for column, ddl in STATION_COLUMNS.items():
            if column not in have:
                conn.execute(f"ALTER TABLE stations ADD COLUMN {column} {ddl}")
        conn.execute("UPDATE stations SET color = sort WHERE color IS NULL")  # keep existing stations' colours
        conn.commit()
    finally:
        conn.close()


def add_station(conn, sid, name, provider=None, provider_id=None, poll_seconds=None, active=1, enabled_at=None, tz=None):
    """Add a station at the end of the list with the next free chart colour (no commit)."""
    sort, color = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1, COALESCE(MAX(color), -1) + 1 FROM stations").fetchone()
    conn.execute(
        """INSERT INTO stations (id, name, sort, color, active, provider, provider_id, poll_seconds, enabled_at, timezone)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, name, sort, color, int(bool(active)), provider,
         None if provider_id is None else str(provider_id), poll_seconds, enabled_at, valid_timezone(tz)),
    )


def seed_stations(conn, entries):
    """On the first run only, load the stations from stations.json. After that the
    database is the source of truth (edited from the Stations page), so the file is
    ignored. Returns True if it seeded."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return False
    for entry in entries:
        known = conn.execute("SELECT 1 FROM stations WHERE id = ?", (entry["id"],)).fetchone()
        if known:  # a database from before stations lived in it: attach the feed to the existing history
            conn.execute(
                "UPDATE stations SET name = ?, provider = ?, provider_id = ?, poll_seconds = ?, timezone = ?, active = 1 WHERE id = ?",
                (entry["name"], entry["provider"], str(entry["station_id"]), entry.get("poll_seconds"),
                 valid_timezone(entry.get("timezone")), entry["id"]),
            )
        else:
            add_station(conn, entry["id"], entry["name"], entry["provider"], entry["station_id"], entry.get("poll_seconds"),
                        tz=entry.get("timezone"))
    conn.commit()
    conn.execute("PRAGMA user_version = 2")
    return True


def fill_timezones(conn, entries):
    """For databases seeded before stations had timezones: give stations that have none the timezone
    from the stations file, once. Stations already given one (from the Stations page) are left alone."""
    if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
        return False
    for entry in entries:
        tz = valid_timezone(entry.get("timezone"))
        if tz:
            conn.execute("UPDATE stations SET timezone = ? WHERE id = ? AND timezone IS NULL", (tz, entry["id"]))
    conn.commit()
    conn.execute("PRAGMA user_version = 2")
    return True


def insert_play(conn, station_id, artist, title, played_at, image=None):
    """Record one play unless it's already there (no commit). Returns True if it was new."""
    key = song_key(artist, title)
    seen = conn.execute(
        "SELECT id, image_url FROM plays WHERE station_id = ? AND song_key = ? AND played_at BETWEEN ? AND ? LIMIT 1",
        (station_id, key, played_at - DEDUPE_WINDOW, played_at + DEDUPE_WINDOW),
    ).fetchone()
    if seen:
        if image and not seen["image_url"]:  # fill in art for plays logged before it was captured
            conn.execute("UPDATE plays SET image_url = ? WHERE id = ?", (image, seen["id"]))
        return False
    conn.execute(
        "INSERT OR IGNORE INTO plays (station_id, artist, title, song_key, played_at, image_url) VALUES (?, ?, ?, ?, ?, ?)",
        (station_id, norm(artist), norm(title), key, played_at, image),
    )
    return True


def insert_plays(conn, station_id, plays):
    """Insert plays not already recorded. Returns how many were new."""
    new = sum(insert_play(conn, station_id, p.artist, p.title, p.played_at, p.image) for p in plays)
    conn.commit()
    return new


def record_poll(conn, station_id, now, error=None):
    if error is None:
        conn.execute(
            "UPDATE stations SET last_poll_at = ?, last_ok_at = ?, last_error = NULL WHERE id = ?",
            (now, now, station_id),
        )
    else:
        conn.execute(
            "UPDATE stations SET last_poll_at = ?, last_error = ? WHERE id = ?",
            (now, error[:300], station_id),
        )
    conn.commit()
