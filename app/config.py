import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
STATIONS_FILE = Path(os.environ.get("STATIONS_FILE", ROOT / "config" / "stations.json"))
PORT = _env_int("PORT", 5555)
TZ_NAME = os.environ.get("TZ", "America/New_York")

# Seconds between polls of each station (a station can override with "poll_seconds").
POLL_SECONDS = max(15, _env_int("POLL_SECONDS", 60))
# How many recent plays to request on each poll. Providers return recent history,
# so a value above 1 lets the poller backfill after an outage or restart.
HISTORY_COUNT = max(1, min(50, _env_int("HISTORY_COUNT", 10)))

# Importing writes to the database. If set, imports must supply this token.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_IMPORT_BYTES = max(1, _env_int("MAX_IMPORT_MB", 1024)) * 1024 * 1024

# Automatic snapshots of the database (see the README's Backups section).
BACKUP_EVERY_HOURS = max(0, _env_int("BACKUP_EVERY_HOURS", 24))  # 0 turns them off
BACKUP_KEEP = max(1, _env_int("BACKUP_KEEP", 7))                  # how many of the newest to keep
BACKUP_COMPRESS = os.environ.get("BACKUP_COMPRESS", "1").strip().lower() not in ("0", "false", "no", "off")  # gzip them
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DATA_DIR / "backups"))

# For a server that strangers can reach (see the README's section on public servers).
READ_ONLY = _env_bool("READ_ONLY")                  # the web interface can't change anything
MAX_CONNECTIONS = max(4, _env_int("MAX_CONNECTIONS", 64))  # simultaneous requests; more get a 503
REQUEST_TIMEOUT = max(5, _env_int("REQUEST_TIMEOUT", 30))  # seconds a client may stay silent before it's dropped
TRUST_FORWARDED_FOR = _env_bool("TRUST_FORWARDED_FOR")     # take the client address from a reverse proxy's X-Forwarded-For

# Optional: a Spotify developer app, so the dashboard's Spotify buttons can go straight to the song or artist
# (see the README's Spotify section). Without both, the buttons open a Spotify search instead.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
