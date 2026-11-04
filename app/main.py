import logging
import signal
import threading

from . import config, db
from .poller import Poller, seed_from_file
from .server import make_server

log = logging.getLogger("radio-poller")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = config.DATA_DIR / "radio.db"
    db.init(db_path)
    conn = db.connect(db_path)
    try:
        seed_from_file(conn, config.STATIONS_FILE)
    finally:
        conn.close()

    stop = threading.Event()
    Poller(db_path, config.POLL_SECONDS, config.HISTORY_COUNT, stop).start()

    httpd = make_server(config.PORT, db_path, config.TZ_NAME, admin_token=config.ADMIN_TOKEN,
                        max_import_bytes=config.MAX_IMPORT_BYTES, read_only=config.READ_ONLY,
                        max_connections=config.MAX_CONNECTIONS, request_timeout=config.REQUEST_TIMEOUT,
                        trust_forwarded=config.TRUST_FORWARDED_FOR)

    if config.READ_ONLY:
        log.info("READ_ONLY is on: the web interface can't change stations or import data")
    elif not config.ADMIN_TOKEN:
        log.warning("no ADMIN_TOKEN is set, so anyone who can reach this server can change stations and import data; "
                    "set one (or READ_ONLY=1) before exposing it beyond a network you trust")
    elif len(config.ADMIN_TOKEN) < 16:
        log.warning("ADMIN_TOKEN is short (%d characters); use 16 or more random characters", len(config.ADMIN_TOKEN))

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()  # must not run on the serving thread

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("serving on :%d, polling every %ds, db at %s", config.PORT, config.POLL_SECONDS, db_path)
    httpd.serve_forever()
    log.info("stopped")


if __name__ == "__main__":
    main()
