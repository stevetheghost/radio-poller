import json
import logging
import re
import threading
import time

from . import db, providers

log = logging.getLogger(__name__)

MAX_BACKOFF = 900
MIN_INTERVAL = 15


def parse_stations(raw):
    """Validate the stations seed file, skipping (and logging) bad entries."""
    stations, seen = [], set()
    for entry in raw:
        try:
            sid = entry["id"]
            if not re.fullmatch(r"[a-z0-9_-]+", sid) or sid in seen:
                raise ValueError("id must be unique lowercase letters, digits, - or _")
            if entry["provider"] not in providers.PROVIDERS:
                raise ValueError("unknown provider %r" % entry["provider"])
            if entry.get("timezone") is not None and not db.valid_timezone(entry["timezone"]):
                raise ValueError("unknown timezone %r" % entry["timezone"])
            entry["name"], entry["station_id"]
        except (KeyError, TypeError, ValueError) as exc:
            log.error("skipping invalid station entry %r: %s", entry, exc)
            continue
        seen.add(sid)
        stations.append(entry)
    return stations


def seed_from_file(conn, stations_file):
    """First run only: load stations.json into the database."""
    try:
        raw = json.loads(stations_file.read_text())
    except (OSError, ValueError) as exc:
        log.error("cannot read %s, so no stations were seeded: %s", stations_file, exc)
        return
    entries = parse_stations(raw)
    if db.seed_stations(conn, entries):
        log.info("first run: loaded stations from %s (manage them from the Stations page from now on)", stations_file)
    elif db.fill_timezones(conn, entries):
        log.info("gave stations their timezones from %s (change them on the Stations page)", stations_file)


class Poller(threading.Thread):
    def __init__(self, db_path, interval, history, stop):
        super().__init__(name="poller", daemon=True)
        self.db_path = db_path
        self.interval = interval
        self.history = history
        self.stop = stop
        self._next_due = {}
        self._failures = {}

    @staticmethod
    def active_stations(conn):
        """Stations that are switched on and have a feed. Read every cycle, so changes
        made on the Stations page take effect within a few seconds."""
        rows = conn.execute(
            """SELECT id, name, provider, provider_id, poll_seconds, enabled_at FROM stations
               WHERE active = 1 AND provider IS NOT NULL AND provider_id IS NOT NULL ORDER BY sort, id"""
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "provider": r["provider"], "station_id": r["provider_id"],
                 "poll_seconds": r["poll_seconds"], "enabled_at": r["enabled_at"]} for r in rows if r["provider"] in providers.PROVIDERS]

    def poll(self, conn, station):
        sid = station["id"]
        try:
            plays = providers.fetch(station, self.history)
            if station.get("enabled_at"):  # see stations.update: don't record songs from before it was switched on
                plays = [p for p in plays if p.played_at >= station["enabled_at"]]
            new = db.insert_plays(conn, sid, plays)
            db.record_poll(conn, sid, int(time.time()))
            self._failures[sid] = 0
            if new:
                log.info("%s: %d new play(s)", sid, new)
        except Exception as exc:
            self._failures[sid] = self._failures.get(sid, 0) + 1
            log.warning("%s: poll failed (%s: %s)", sid, type(exc).__name__, exc)
            try:
                db.record_poll(conn, sid, int(time.time()), error="%s: %s" % (type(exc).__name__, exc))
            except Exception:
                log.exception("could not record poll failure")

    def delay(self, station):
        base = max(MIN_INTERVAL, int(station.get("poll_seconds") or self.interval))
        fails = self._failures.get(station["id"], 0)
        return min(base * (2 ** min(fails, 5)), MAX_BACKOFF) if fails else base

    def run(self):
        conn = db.connect(self.db_path)
        try:
            while not self.stop.is_set():
                try:
                    stations = self.active_stations(conn)
                except Exception:
                    log.exception("could not read the stations list")
                    stations = []
                now = time.monotonic()
                live = {st["id"] for st in stations}
                for sid in [k for k in self._next_due if k not in live]:
                    del self._next_due[sid]  # switched off: poll right away if it's switched back on
                for i, st in enumerate(stations):
                    due = self._next_due.setdefault(st["id"], now + i * 2)  # stagger the first round
                    if now >= due:
                        self.poll(conn, st)
                        self._next_due[st["id"]] = time.monotonic() + self.delay(st)
                waits = [d - time.monotonic() for d in self._next_due.values()] or [5]
                self.stop.wait(max(0.5, min(5, min(waits))))
        finally:
            conn.close()
