"""Read-side queries behind the JSON API. `params` is a dict of query-string values."""
import time
from datetime import datetime, timedelta

DAY_RANGES = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
MAX_HOURLY_SPAN = 14 * 86400


def int_param(params, name, default, lo, hi):
    try:
        return max(lo, min(hi, int(params.get(name, default))))
    except (TypeError, ValueError):
        return default


def _local_midnight(now, tz, days_back):
    day = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((day - timedelta(days=days_back)).timestamp())


def _date_start(value, tz, days=0):
    """Local midnight of a YYYY-MM-DD date in `tz` (plus `days` days) as unix seconds, or None if it isn't a date."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=days)
        return max(0, min(10 ** 11, int(day.replace(tzinfo=tz).timestamp())))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def resolve_range(params, tz, now):
    """Returns (start, end) as unix seconds; None means unbounded. Day presets start
    at local midnight so the first day's bucket is complete. `from_date` / `to_date` (YYYY-MM-DD,
    both inclusive, in the zone) pick whole days; `from` / `to` (unix seconds) override everything."""
    rng = params.get("range", "all")
    start = end = None
    if rng == "24h":
        start = now - 86400
    elif rng in DAY_RANGES:
        start = _local_midnight(now, tz, DAY_RANGES[rng] - 1)
    if params.get("from_date"):
        start = _date_start(params["from_date"], tz)
    if params.get("to_date"):
        end = _date_start(params["to_date"], tz, days=1)
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


SONG_SORTS = {
    "plays": "plays DESC, last_played DESC",
    "recent": "last_played DESC",
    "az": "lower(title), lower(artist)",
}


def station_songs(conn, params, tz, now):
    """Every distinct song in the filtered range (usually one station), with its play count and when it was
    first and last heard. `total` and `plays` cover the whole range; `songs` is the first `limit` of them."""
    where, args, _, _ = build_where(params, tz, now)
    limit = int_param(params, "limit", 200, 1, 2000)
    order = SONG_SORTS.get(params.get("sort"), SONG_SORTS["plays"])
    counts = conn.execute(
        f"SELECT COUNT(*) AS plays, COUNT(DISTINCT p.song_key) AS songs FROM plays p{where}", args
    ).fetchone()
    rows = conn.execute(
        f"""SELECT g.*, (SELECT station_id FROM plays WHERE song_key = g.song_key AND played_at = g.last_played LIMIT 1) AS last_station_id
            FROM (SELECT MIN(p.artist) AS artist, MIN(p.title) AS title, p.song_key, COUNT(*) AS plays,
                         MIN(p.played_at) AS first_played, MAX(p.played_at) AS last_played
                  FROM plays p{where} GROUP BY p.song_key) g
            ORDER BY {order}, g.song_key LIMIT ?""",
        args + [limit],
    ).fetchall()
    return {"total": counts["songs"], "plays": counts["plays"], "songs": [dict(r) for r in rows]}


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


HEAT_BUCKET = 900           # seconds; every real UTC offset is a whole number of these, so the zone maths stays right
DROPPED_MIN_PLAYS = 2       # a song heard once in the earlier period was never really in rotation
OTHER_LIST = 5              # songs listed for "only on this station" in a comparison


def heatmap(conn, params, tz, now):
    """Plays by weekday (Monday first) and hour of day in `tz`, over the filtered range."""
    where, args, _, _ = build_where(params, tz, now)
    cells = [[0] * 24 for _ in range(7)]
    # Grouped in SQL by 15-minute slot, then each slot is placed once: far fewer zone conversions than one per play.
    for row in conn.execute(f"SELECT p.played_at / {HEAT_BUCKET} AS slot, COUNT(*) AS n FROM plays p{where} GROUP BY slot", args):
        local = datetime.fromtimestamp(row["slot"] * HEAT_BUCKET, tz)
        cells[local.weekday()][local.hour] += row["n"]
    return {"cells": cells, "max": max(max(day) for day in cells), "total": sum(sum(day) for day in cells)}


def _play_at(conn, key, at, station=None):
    """Artist, title and station of a song's play at an exact time (optionally on one station)."""
    sql, args = "SELECT artist, title, station_id FROM plays WHERE song_key = ? AND played_at = ?", [key, at]
    if station:
        sql, args = sql + " AND station_id = ?", args + [station]
    return conn.execute(sql + " LIMIT 1", args).fetchone()


def changes(conn, params, tz, now):
    """Songs that entered rotation in the range, and songs that fell out of it.

    New: first heard (on the chosen station, or any) inside the range. Dropped: played at least twice in
    the equally long period before the range and not at all since. Both need history older than the range."""
    start, _ = resolve_range(params, tz, now)
    scope, args, _, _ = build_where(params, tz, now, with_time=False)
    station = params.get("station") or None
    limit = int_param(params, "limit", 10, 1, 100)
    if start is None:
        return {"applicable": False, "reason": "range"}
    earliest = conn.execute(f"SELECT MIN(p.played_at) FROM plays p{scope}", args).fetchone()[0]
    if earliest is None or earliest >= start:
        return {"applicable": False, "reason": "history", "history_start": earliest}
    span = max(1, now - start)
    previous = start - span

    fresh = conn.execute(
        f"""SELECT p.song_key, COUNT(*) AS plays, MIN(p.played_at) AS first_played, MAX(p.played_at) AS last_played
            FROM plays p{scope} GROUP BY p.song_key HAVING MIN(p.played_at) >= ?
            ORDER BY plays DESC, first_played DESC""",
        args + [start],
    ).fetchall()
    gone = conn.execute(
        f"""SELECT p.song_key, SUM(p.played_at < ?) AS plays, MAX(p.played_at) AS last_played
            FROM plays p{scope}{" AND" if scope else " WHERE"} p.played_at >= ?
            GROUP BY p.song_key HAVING SUM(p.played_at >= ?) = 0 AND plays >= ?
            ORDER BY plays DESC, last_played DESC""",
        [start] + args + [previous, start, DROPPED_MIN_PLAYS],
    ).fetchall()

    def songs(rows, time_key):
        out = []
        for r in rows[:limit]:  # `at` is when it was first heard (new) or last heard (dropped)
            play = _play_at(conn, r["song_key"], r[time_key], station)
            out.append({"song_key": r["song_key"], "artist": play["artist"], "title": play["title"],
                        "plays": r["plays"], "at": r[time_key], "station_id": play["station_id"]})
        return out

    return {"applicable": True, "since": start, "previous_start": previous, "history_start": earliest,
            "baseline": start - earliest,
            "new": {"total": len(fresh), "songs": songs(fresh, "first_played")},
            "dropped": {"total": len(gone), "songs": songs(gone, "last_played"), "min_plays": DROPPED_MIN_PLAYS}}


def _station_songs(conn, where, args, station_id):
    glue = " AND" if where else " WHERE"
    rows = conn.execute(
        f"""SELECT p.song_key, MIN(p.artist) AS artist, MIN(p.title) AS title, COUNT(*) AS n
            FROM plays p{where}{glue} p.station_id = ? GROUP BY p.song_key""",
        args + [station_id],
    )
    return {r["song_key"]: (r["artist"], r["title"], r["n"]) for r in rows}


def _first_plays(conn, station_id):
    rows = conn.execute("SELECT song_key, MIN(played_at) AS t FROM plays WHERE station_id = ? GROUP BY song_key", (station_id,))
    return {r["song_key"]: r["t"] for r in rows}


def compare(conn, params, tz, now):
    """Two stations side by side over the range: how much they overlap, and who played shared songs first.

    "First" is the first time we logged the song on each station, so it's only claimed when both stations
    were being logged by then; a song already playing when the later station was added is "already in rotation"."""
    a, b = params.get("a", ""), params.get("b", "")
    info = {r["id"]: r for r in conn.execute("SELECT id, name, color, timezone FROM stations WHERE id IN (?, ?)", (a, b))}
    if a == b or a not in info or b not in info:
        return None
    where, args, _, _ = build_where({k: v for k, v in params.items() if k != "station"}, tz, now)
    limit = int_param(params, "limit", 10, 1, 200)
    mine, theirs = _station_songs(conn, where, args, a), _station_songs(conn, where, args, b)
    shared_keys = mine.keys() & theirs.keys()

    def side(sid, songs):
        plays = sum(n for _, _, n in songs.values())
        return {"id": sid, "name": info[sid]["name"], "color": info[sid]["color"], "timezone": info[sid]["timezone"],
                "plays": plays, "songs": len(songs), "avg_plays": round(plays / len(songs), 1) if songs else 0,
                "shared_pct": round(100 * len(shared_keys) / len(songs)) if songs else 0}

    first = {"a": 0, "b": 0, "tie": 0, "unknown": 0}
    songs_out, common_start = [], None
    if shared_keys:
        began = [conn.execute("SELECT MIN(played_at) FROM plays WHERE station_id = ?", (sid,)).fetchone()[0] for sid in (a, b)]
        common_start = max(began)
        firsts_a, firsts_b = _first_plays(conn, a), _first_plays(conn, b)
        ordered = sorted(shared_keys, key=lambda k: (-(mine[k][2] + theirs[k][2]), mine[k][1].casefold(), k))
        for rank, key in enumerate(ordered):
            ta, tb = firsts_a[key], firsts_b[key]
            who = "unknown" if min(ta, tb) < common_start else "tie" if ta == tb else "a" if ta < tb else "b"
            first[who] += 1
            if rank < limit:
                songs_out.append({"song_key": key, "artist": mine[key][0], "title": mine[key][1], "plays_a": mine[key][2],
                                  "plays_b": theirs[key][2], "first_a": ta, "first_b": tb,
                                  "first": None if who == "unknown" else who, "lead": abs(ta - tb)})

    def only(own, other):
        keys = sorted(own.keys() - other.keys(), key=lambda k: (-own[k][2], own[k][1].casefold(), k))
        return {"total": len(keys), "songs": [{"song_key": k, "artist": own[k][0], "title": own[k][1], "plays": own[k][2]}
                                              for k in keys[:OTHER_LIST]]}

    return {"a": side(a, mine), "b": side(b, theirs), "shared": {"songs": len(shared_keys),
            "plays_a": sum(mine[k][2] for k in shared_keys), "plays_b": sum(theirs[k][2] for k in shared_keys)},
            "first": first, "common_start": common_start, "songs": songs_out,
            "only_a": only(mine, theirs), "only_b": only(theirs, mine)}


def health(conn):
    row = conn.execute("SELECT COUNT(*) AS stations, MAX(last_ok_at) AS last_ok_at FROM stations WHERE active = 1").fetchone()
    return {"ok": True, "stations": row["stations"], "last_ok_at": row["last_ok_at"], "now": int(time.time())}
