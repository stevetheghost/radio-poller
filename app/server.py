import gzip
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import backup, config, db, detect, queries, transfer
from .security import FailureLimiter
from . import stations as station_admin

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/transfer": ("transfer.html", "text/html; charset=utf-8"),
    "/stations": ("stations.html", "text/html; charset=utf-8"),
}
# Front-end libraries are served from here, not a CDN, so no third party's code runs on these pages.
VENDOR_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
VENDOR = {f.name: f for f in (STATIC_DIR / "vendor").glob("*") if f.suffix in VENDOR_TYPES}
_gzipped = {}
JSON_CONTENT_TYPE = "application/json"
STATION_PATH = re.compile(r"/api/stations/([a-z0-9_-]{1,64})")
MAX_JSON_BYTES = 65536
IMPORT_CONTENT_TYPE = "application/x-ndjson"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# Pages load only their own scripts and styles. Album art may come from any https host the feeds name.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class _Body:
    """The request body, limited to its Content-Length, readable line by line."""

    def __init__(self, rfile, length):
        self.rfile, self.left = rfile, length

    def readline(self, limit=-1):
        n = self.left if limit < 0 else min(limit, self.left)
        data = self.rfile.readline(n)
        self.left -= len(data)
        return data

    def drain(self):
        while self.left > 0:
            chunk = self.rfile.read(min(65536, self.left))
            if not chunk:
                break
            self.left -= len(chunk)


class BoundedServer(ThreadingHTTPServer):
    """A thread per request, but only up to `max_connections` at once: past that, new connections
    get a 503 straight away instead of piling up threads."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "radio-poller"
    timeout = config.REQUEST_TIMEOUT  # an idle client is dropped after this many seconds

    def version_string(self):  # don't announce the Python version
        return self.server_version

    def log_message(self, fmt, *args):  # no per-request access log
        pass

    def _client_ip(self):
        if self.server.trust_forwarded:  # the reverse proxy appends the real client address last
            last = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            if last:
                return last
        return self.client_address[0]

    def _security_headers(self, content_type=""):
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)

    def _send(self, status, body, content_type, extra=None):
        headers = {"Cache-Control": "no-store", **(extra or {})}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self._security_headers(content_type)
        self.end_headers()
        self.wfile.write(body)

    def _vendor(self, name):
        path = VENDOR.get(name)
        if path is None:
            return self._json({"error": "not found"}, 404)
        body = path.read_bytes()
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "Vary": "Accept-Encoding"}  # names carry the version
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = _gzipped.setdefault(name, gzip.compress(body, 6))
            headers["Content-Encoding"] = "gzip"
        self._send(200, body, VENDOR_TYPES[path.suffix], headers)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path.startswith("/vendor/"):
                return self._vendor(url.path[len("/vendor/"):])
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
            if url.path.startswith("/api/") or url.path == "/health":
                return self._api(url.path, params)
            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("error handling %s", self.path)
            self._json({"error": "internal error"}, 500)

    def _handle_write(self, method, handler):
        if self.server.read_only:
            self.close_connection = True
            return self._json({"error": "this server is read-only"}, 403)
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("error handling %s %s", method, self.path)
            self._json({"error": "internal error"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        routes = {"/api/import": self._import, "/api/check-token": self._check_token, "/api/stations": self._create_station,
                  "/api/stations/detect": self._detect_station, "/api/stations/order": self._reorder_stations}
        if path in routes:
            return self._handle_write("POST", routes[path])
        self._json({"error": "not found"}, 404)

    def do_PATCH(self):
        match = STATION_PATH.fullmatch(urlparse(self.path).path)
        if not match:
            return self._json({"error": "not found"}, 404)
        self._handle_write("PATCH", lambda: self._station_write(lambda conn, data: station_admin.update(conn, match.group(1), data)))

    def do_DELETE(self):
        url = urlparse(self.path)
        match = STATION_PATH.fullmatch(url.path)
        if not match:
            return self._json({"error": "not found"}, 404)
        confirm = parse_qs(url.query).get("confirm", [""])[0]
        sid = match.group(1)

        def delete():
            if not self._admin_ok():
                return
            conn = db.connect(self.server.db_path)
            try:
                try:
                    plays = station_admin.delete(conn, sid, confirm)
                except station_admin.StationError as exc:
                    return self._json({"error": str(exc)}, exc.status)
                log.info("deleted station %s and its %d plays", sid, plays)
                self._json({"deleted_plays": plays, **self._stations_payload(conn)})
            finally:
                conn.close()
        self._handle_write("DELETE", delete)

    # ---- helpers for the endpoints that change things ------------------------

    def _admin_ok(self):
        """True if no admin token is set or the request carries it; otherwise sends the error. Too many
        wrong tokens from one client lock it out for a while, so the token can't be guessed."""
        token = self.server.admin_token
        if not token:
            return True
        client = self._client_ip()
        wait = self.server.limiter.retry_after(client)
        if wait:
            self.close_connection = True
            self._send(429, json.dumps({"error": "too many wrong tokens; try again in %d seconds" % wait}).encode(),
                       "application/json", {"Retry-After": str(wait)})
            return False
        if hmac.compare_digest(self.headers.get("X-Admin-Token", "").encode(), token.encode()):
            self.server.limiter.clear(client)
            return True
        self.server.limiter.record_failure(client)
        self.close_connection = True
        self._json({"error": "wrong or missing admin token"}, 401)
        return False

    def _read_json(self):
        """The JSON object in the request body, or None after sending an error. A JSON content
        type can't be sent by another website's form without a CORS preflight, which this
        server never grants, so requiring it keeps other sites from changing things."""
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_JSON_BYTES:
            self._json({"error": "a JSON body up to %d KB is required" % (MAX_JSON_BYTES // 1024)}, 411 if not length.isdigit() else 413)
            return None
        raw = self.rfile.read(int(length))
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != JSON_CONTENT_TYPE:
            self._json({"error": "unsupported content type"}, 415)
            return None
        if not self._admin_ok():
            return None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._json({"error": "the body must be a JSON object"}, 400)
            return None
        return data

    def _stations_payload(self, conn):
        return {"now": int(time.time()), "tz": self.server.tz_name, "stations": queries.stations(conn)}

    def _station_write(self, action):
        """Run `action(conn, data)` for a station change and reply with the updated list."""
        data = self._read_json()
        if data is None:
            return
        conn = db.connect(self.server.db_path)
        try:
            try:
                action(conn, data)
            except station_admin.StationError as exc:
                return self._json({"error": str(exc)}, exc.status)
            self._json(self._stations_payload(conn))
        finally:
            conn.close()

    def _create_station(self):
        data = self._read_json()
        if data is None:
            return
        conn = db.connect(self.server.db_path)
        try:
            try:
                sid, newest = station_admin.create(conn, data)
            except station_admin.StationError as exc:
                return self._json({"error": str(exc)}, exc.status)
            log.info("added station %s", sid)
            self._json({"created": sid, "sample": newest._asdict() if newest else None, **self._stations_payload(conn)}, 201)
        finally:
            conn.close()

    def _check_token(self):
        if self._read_json() is not None:
            self._json({"ok": True})

    def _reorder_stations(self):
        self._station_write(lambda conn, data: station_admin.reorder(conn, data.get("order")))

    def _detect_station(self):
        data = self._read_json()
        if data is None:
            return
        try:
            found = detect.detect(data.get("url"))
        except detect.DetectError as exc:
            return self._json({"error": str(exc)}, 422)
        except OSError as exc:
            return self._json({"error": "couldn't load that page (%s)" % exc}, 502)
        found.update(feed_ok=True, sample=None, feed_error=None, existing=None)
        try:
            newest = station_admin.test_feed(found["provider"], found["provider_id"])
            found["sample"] = {"artist": newest.artist, "title": newest.title} if newest else None
        except station_admin.StationError as exc:
            found.update(feed_ok=False, feed_error=str(exc))
        conn = db.connect(self.server.db_path)
        try:
            row = conn.execute("SELECT name FROM stations WHERE provider = ? AND provider_id = ?",
                               (found["provider"], found["provider_id"])).fetchone()
            found["existing"] = row["name"] if row else None
        finally:
            conn.close()
        self._json(found)

    def _import(self):
        """Merge an uploaded export. The body is read as a stream, never held in memory."""
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self._json({"error": "Content-Length is required"}, 411)
        body = _Body(self.rfile, int(length))
        if body.left > self.server.max_import_bytes:
            self.close_connection = True
            return self._json({"error": "file is larger than the %d MB import limit" % (self.server.max_import_bytes // 2**20)}, 413)

        # A custom content type can't be sent by another website's form or script
        # without a CORS preflight, which this server never grants.
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != IMPORT_CONTENT_TYPE:
            self.close_connection = True
            return self._json({"error": "unsupported content type"}, 415)
        if not self._admin_ok():
            return

        conn = db.connect(self.server.db_path)
        try:
            result = transfer.import_stream(conn, body)
        except transfer.FormatError as exc:
            body.drain()
            return self._json({"error": str(exc)}, 400)
        finally:
            conn.close()
        log.info("import finished: %s", {k: v for k, v in result.items() if k != "errors"})
        self._json(result)

    def _export(self, conn, now, station=None, plays=True):
        if station and not conn.execute("SELECT 1 FROM stations WHERE id = ?", (station,)).fetchone():
            return self._json({"error": "no such station"}, 404)
        stamp = datetime.fromtimestamp(now, self.server.tz).strftime("%Y-%m-%d")
        name = "radio-poller-%s%s%s.jsonl" % ("stations-" if not plays else "", station + "-" if station else "", stamp)  # ids are slugs
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()  # no Content-Length: the stream ends when the connection closes
        for chunk in transfer.export_lines(conn, now, station, plays):
            self.wfile.write(chunk)

    def _timezone_for(self, conn, p):
        """Days and hours are grouped in the picked station's own timezone; across all stations, in the server's."""
        if p.get("station"):
            row = conn.execute("SELECT timezone FROM stations WHERE id = ?", (p["station"],)).fetchone()
            if row and row["timezone"]:
                return db.load_tz(row["timezone"]), row["timezone"]
        return self.server.tz, self.server.tz_name

    def _api(self, path, p):
        now = int(time.time())
        conn = db.connect(self.server.db_path)
        tz, tz_name = self._timezone_for(conn, p)
        db.add_time_functions(conn, tz)
        try:
            if path == "/health":
                return self._json(queries.health(conn))
            if path == "/api/stations":
                return self._json({"tz": self.server.tz_name, "now": now, "stations": queries.stations(conn)})
            if path == "/api/export":  # a full copy of the data, and the heaviest request the server serves
                if not self._admin_ok():
                    return
                return self._export(conn, now, p.get("station") or None, p.get("plays") != "0")
            if path == "/api/transfer-info":
                row = conn.execute("SELECT COUNT(*) AS plays, MIN(played_at) AS first_play, MAX(played_at) AS last_play FROM plays").fetchone()
                info = dict(row)
                info["stations"] = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
                info["station_list"] = [
                    {k: st[k] for k in ("id", "name", "active", "plays", "first_play", "last_play")}
                    for st in queries.stations(conn)
                ]
                info["token_required"] = bool(self.server.admin_token)
                info["read_only"] = self.server.read_only
                info["backup"] = backup.status(self.server.backup_dir, self.server.backup_every_hours, self.server.backup_keep)
                info["max_bytes"] = self.server.max_import_bytes
                return self._json(info)
            if path == "/api/now-playing":
                return self._json({"now": now, "stations": queries.now_playing(conn, p)})
            if path == "/api/summary":
                return self._json(queries.summary(conn, p, tz, now))
            if path == "/api/top-songs":
                return self._json(queries.top_songs(conn, p, tz, now))
            if path == "/api/top-artists":
                return self._json(queries.top_artists(conn, p, tz, now))
            if path == "/api/recent":
                return self._json(queries.recent(conn, p, tz, now))
            if path == "/api/timeseries":
                return self._json({**queries.timeseries(conn, p, tz, now), "tz": tz_name})
            if path == "/api/song":
                result = queries.song(conn, p)
                return self._json(result, 200) if result else self._json({"error": "no such song"}, 404)
            self._json({"error": "not found"}, 404)
        finally:
            conn.close()


def make_server(port, db_path, tz_name, host="0.0.0.0", admin_token="", max_import_bytes=config.MAX_IMPORT_BYTES,
                read_only=False, max_connections=config.MAX_CONNECTIONS, request_timeout=config.REQUEST_TIMEOUT,
                trust_forwarded=config.TRUST_FORWARDED_FOR, backup_dir=None, backup_every_hours=0, backup_keep=7):
    handler = type("TimedHandler", (Handler,), {"timeout": request_timeout})
    httpd = BoundedServer((host, port), handler, max_connections)
    httpd.db_path = db_path
    httpd.tz_name = tz_name
    httpd.tz = db.load_tz(tz_name)
    httpd.admin_token = admin_token
    httpd.max_import_bytes = max_import_bytes
    httpd.read_only = read_only
    httpd.trust_forwarded = trust_forwarded
    httpd.limiter = FailureLimiter()
    httpd.backup_dir, httpd.backup_every_hours, httpd.backup_keep = backup_dir, backup_every_hours, backup_keep
    return httpd
