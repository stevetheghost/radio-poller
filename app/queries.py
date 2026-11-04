"""Read-side queries behind the JSON API. `params` is a dict of query-string values."""
import time
from datetime import datetime, timedelta

DAY_RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
MAX_HOURLY_SPAN = 14 * 86400


def int_param(params, name, default, lo, hi):
    try:
        return max(lo, min(hi, int(params.get(name, default))))
    except (TypeError, ValueError):
        return default


def _local_midnight(now, tz, days_back):
    day = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((day - timedelta(days=days_back)).timestamp())


def resolve_range(params, tz, now):
    """Returns (start, end) as unix seconds; None means unbounded. Day presets start
    at local midnight so the first day's bucket is complete."""
    rng = params.get("range", "all")
    start = end = None
    if rng == "24h":
        start = now - 86400
    elif rng in DAY_RANGES:
        start = _local_midnight(now, tz, DAY_RANGES[rng] - 1)
    if "from" in params:
        start = int_param(params, "from", start, 0, 10 ** 11)
    if "to" in params:
        end = int_param(params, "to", end, 0, 10 ** 11)
    return start, end


def build_where(params, tz, now, with_time=True):
    where, args = [], []
    if params.get("station"):
        where.append("p.station_id = ?")
        args.append(params["station"])
    if params.get("song_key"):
        where.append("p.song_key = ?")
        args.append(params["song_key"])
    for term in params.get("q", "")[:100].casefold().split()[:5]:  # bounded: every term is another scan
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("p.song_key LIKE ? ESCAPE '\\'")
        args.append("%" + escaped + "%")
    start = end = None
    if with_time:
        start, end = resolve_range(params, tz, now)
        if start is not None:
            where.append("p.played_at >= ?")
            args.append(start)
        if end is not None:
            where.append("p.played_at < ?")
            args.append(end)
    return (" WHERE " + " AND ".join(where)) if where else "", args, start, end


def stations(conn):
    rows = conn.execute(
        """SELECT s.id, s.name, s.sort, s.color, s.active, s.provider, s.provider_id, s.poll_seconds, s.timezone,
                  s.last_poll_at, s.last_ok_at, s.last_error, COUNT(p.id) AS plays, MIN(p.played_at) AS first_play, MAX(p.played_at) AS last_play
           FROM stations s LEFT JOIN plays p ON p.station_id = s.id
           GROUP BY s.id ORDER BY s.sort, s.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def now_playing(conn, params):
    """The most recent play on each active station (or just the selected one)."""
    where, args = "WHERE s.active = 1", []
    if params.get("station"):  # a paused station can still be looked at on its own
        where, args = "WHERE s.id = ?", [params["station"]]
    rows = conn.execute(
        f"""SELECT s.id AS station_id, s.name AS station_name, s.sort, s.color,
                   p.artist, p.title, p.song_key, p.played_at, p.image_url
            FROM stations s LEFT JOIN plays p ON p.id =
                 (SELECT id FROM plays WHERE station_id = s.id ORDER BY played_at DESC, id DESC LIMIT 1)
            {where} ORDER BY s.sort, s.id""",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def summary(conn, params, tz, now):
    where, args, _, _ = build_where(params, tz, now)
    row = conn.execute(
        f"""SELECT COUNT(*) AS plays, COUNT(DISTINCT p.song_key) AS songs,
                   COUNT(DISTINCT lower(p.artist)) AS artists
            FROM plays p{where}""",
        args,
    ).fetchone()
    return dict(row)


def top_songs(conn, params, tz, now):
    where, args, _, _ = build_where(params, tz, now)
    limit = int_param(params, "limit", 25, 1, 500)
    rows = conn.execute(
        f"""SELECT MIN(p.artist) AS artist, MIN(p.title) AS title, p.song_key,
                   COUNT(*) AS plays, MIN(p.played_at) AS first_played, MAX(p.played_at) AS last_played
            FROM plays p{where}
            GROUP BY p.song_key ORDER BY plays DESC, last_played DESC LIMIT ?""",
        args + [limit],
    ).fetchall()
    songs = [dict(r) for r in rows]
    for song in songs:  # the station that played it last, so the page can show that time in the station's own timezone
        latest = conn.execute("SELECT station_id FROM plays WHERE song_key = ? AND played_at = ? LIMIT 1",
                              (song["song_key"], song["last_played"])).fetchone()
        song["last_station_id"] = latest["station_id"] if latest else None
    return songs


def top_artists(conn, params, tz, now):
    where, args, _, _ = build_where(params, tz, now)
    limit = int_param(params, "limit", 10, 1, 200)
    rows = conn.execute(
        f"""SELECT MIN(p.artist) AS artist, COUNT(*) AS plays, COUNT(DISTINCT p.song_key) AS songs
            FROM plays p{where}
            GROUP BY lower(p.artist) ORDER BY plays DESC, artist LIMIT ?""",
        args + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def recent(conn, params, tz, now):
    where, args, _, _ = build_where(params, tz, now)
    limit = int_param(params, "limit", 25, 1, 200)
    offset = int_param(params, "offset", 0, 0, 10 ** 7)
    rows = conn.execute(
        f"""SELECT p.station_id, s.name AS station_name, p.artist, p.title, p.song_key, p.played_at
            FROM plays p JOIN stations s ON s.id = p.station_id{where}
            ORDER BY p.played_at DESC, p.id DESC LIMIT ? OFFSET ?""",
        args + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows]


def song(conn, params):
    key = params.get("key", "")
    rows = conn.execute(
        """SELECT p.station_id, s.name AS station_name, s.sort, s.color, MIN(p.artist) AS artist, MIN(p.title) AS title,
                  COUNT(*) AS plays, MIN(p.played_at) AS first_played, MAX(p.played_at) AS last_played
           FROM plays p JOIN stations s ON s.id = p.station_id
           WHERE p.song_key = ? GROUP BY p.station_id ORDER BY s.sort""",
        (key,),
    ).fetchall()
    if not rows:
        return None
    first = min(rows, key=lambda r: r["first_played"])
    last = max(rows, key=lambda r: r["last_played"])
    return {
        "song_key": key,
        "artist": rows[0]["artist"],
        "title": rows[0]["title"],
        "plays": sum(r["plays"] for r in rows),
        "first_played": first["first_played"],
        "first_station_id": first["station_id"],
        "last_played": last["last_played"],
        "last_station_id": last["station_id"],
        "stations": [
            {k: r[k] for k in ("station_id", "station_name", "sort", "color", "plays", "first_played", "last_played")}
            for r in rows
        ],
    }


def _bucket_labels(start, end, tz, bucket):
    if bucket == "day":
        day = datetime.fromtimestamp(start, tz).date()
        last = datetime.fromtimestamp(end, tz).date()
        labels = []
        while day <= last:
            labels.append(day.isoformat())
            day += timedelta(days=1)
        return labels
    cursor = datetime.fromtimestamp(start, tz).replace(minute=0, second=0, microsecond=0).timestamp()
    labels = []
    while cursor <= end:
        label = datetime.fromtimestamp(cursor, tz).strftime("%Y-%m-%dT%H")
        if not labels or labels[-1] != label:  # a DST fall-back repeats an hour
            labels.append(label)
        cursor += 3600
    return labels


def timeseries(conn, params, tz, now):
    where, args, start, end = build_where(params, tz, now)
    if start is None:
        start = conn.execute(f"SELECT MIN(p.played_at) FROM plays p{where}", args).fetchone()[0]
    if start is None:
        return {"bucket": "day", "labels": [], "series": []}
    end = (end - 1) if end else now

    bucket = params.get("bucket") or ("hour" if params.get("range") == "24h" else "day")
    if bucket not in ("hour", "day") or (bucket == "hour" and end - start > MAX_HOURLY_SPAN):
        bucket = "day"
    labels = _bucket_labels(start, max(end, start), tz, bucket)
    index = {label: i for i, label in enumerate(labels)}

    fn = "localhour" if bucket == "hour" else "localday"
    rows = conn.execute(
        f"""SELECT p.station_id, s.name, s.sort, s.color, {fn}(p.played_at) AS b, COUNT(*) AS n
            FROM plays p JOIN stations s ON s.id = p.station_id{where}
            GROUP BY p.station_id, b""",
        args,
    ).fetchall()
    series = {}
    for r in rows:
        entry = series.setdefault(
            r["station_id"],
            {"station_id": r["station_id"], "name": r["name"], "sort": r["sort"], "color": r["color"],
             "counts": [0] * len(labels)},
        )
        if r["b"] in index:
            entry["counts"][index[r["b"]]] = r["n"]
    return {"bucket": bucket, "labels": labels, "series": sorted(series.values(), key=lambda s: s["sort"])}


def health(conn):
    row = conn.execute("SELECT COUNT(*) AS stations, MAX(last_ok_at) AS last_ok_at FROM stations WHERE active = 1").fetchone()
    return {"ok": True, "stations": row["stations"], "last_ok_at": row["last_ok_at"], "now": int(time.time())}
