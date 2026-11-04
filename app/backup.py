"""Regular snapshots of the database.

Each backup is a complete, self-contained SQLite file made with SQLite's online backup, so it is
consistent even while the poller is writing (copying the live file with `cp` is not). The newest few
are kept and older ones are removed.
"""
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

NAME = re.compile(r"radio-\d{8}-\d{6}\.db")
INITIAL_DELAY = 20      # seconds after start before the first check, so startup isn't competing with it
CHECK_EVERY = 600       # how often to see whether a backup is due
RETRY_AFTER_FAILURE = 3600


def list_backups(backup_dir):
    """Backup files, oldest first (their names sort by time)."""
    try:
        return sorted(p for p in Path(backup_dir).iterdir() if NAME.fullmatch(p.name))
    except OSError:
        return []


def newest_time(backup_dir):
    times = []
    for path in list_backups(backup_dir):
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(times) if times else None


def status(backup_dir, every_hours, keep):
    """A summary for the Import / Export page."""
    files = list_backups(backup_dir) if backup_dir else []
    size = 0
    for path in files:
        try:
            size += path.stat().st_size
        except OSError:
            pass
    last = newest_time(backup_dir) if backup_dir else None
    return {"enabled": bool(backup_dir) and every_hours > 0, "every_hours": every_hours, "keep": keep,
            "count": len(files), "last": int(last) if last else None, "bytes": size}


def due(backup_dir, every_seconds, now=None):
    now = time.time() if now is None else now
    newest = newest_time(backup_dir)
    return newest is None or now - newest >= every_seconds


def _remove_partial(partial):
    """The unfinished file, and the -wal / -shm files SQLite may have made beside it."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(partial) + suffix).unlink(missing_ok=True)


def prune(backup_dir, keep):
    for stray in Path(backup_dir).glob("*.partial*"):  # left behind by a crash mid-backup
        stray.unlink(missing_ok=True)
    for old in list_backups(backup_dir)[:-keep]:
        old.unlink(missing_ok=True)


def make_backup(db_path, backup_dir, keep, now=None):
    """Write a new snapshot, check it, then remove the oldest beyond `keep`. Returns its path."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() if now is None else now))  # UTC, sorts by time
    final = backup_dir / f"radio-{stamp}.db"
    partial = backup_dir / (final.name + ".partial")  # only a finished, checked file gets the real name
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(partial))
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # one standalone file, no -wal or -shm beside it
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("the new backup failed SQLite's integrity check")
    except BaseException:
        target.close()
        source.close()
        _remove_partial(partial)
        raise
    target.close()
    source.close()
    # The snapshot inherits the live database's WAL mode until it's converted above; closing can leave the
    # shared-memory file behind. The finished backup is standalone, so those files are just litter.
    for suffix in ("-wal", "-shm"):
        Path(str(partial) + suffix).unlink(missing_ok=True)
    os.replace(partial, final)
    prune(backup_dir, keep)
    return final


class BackupThread(threading.Thread):
    def __init__(self, db_path, backup_dir, every_seconds, keep, stop):
        super().__init__(name="backup", daemon=True)
        self.db_path, self.backup_dir = db_path, backup_dir
        self.every_seconds, self.keep, self.stop = every_seconds, keep, stop

    def run(self):
        if self.stop.wait(INITIAL_DELAY):
            return
        while not self.stop.is_set():
            delay = CHECK_EVERY
            try:
                if due(self.backup_dir, self.every_seconds):
                    path = make_backup(self.db_path, self.backup_dir, self.keep)
                    log.info("backup written: %s (%.1f MB); keeping the newest %d",
                             path.name, path.stat().st_size / 2**20, self.keep)
            except Exception:
                log.exception("backup failed")
                delay = RETRY_AFTER_FAILURE
            if self.stop.wait(delay):
                break
