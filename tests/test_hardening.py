import gzip
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import db, detect, providers, queries, server
from app.providers import Play
from app.security import FailureLimiter

NOW = 1_793_800_000
STATIC = Path(server.__file__).parent / "static"


class FailureLimiterTests(unittest.TestCase):
    def test_locks_out_after_the_limit_and_only_that_client(self):
        lim = FailureLimiter(max_failures=3, window=100)
        for i in range(2):
            lim.record_failure("a", now=1000 + i)
        self.assertEqual(lim.retry_after("a", now=1002), 0)
        lim.record_failure("a", now=1002)
        self.assertGreater(lim.retry_after("a", now=1003), 0)
        self.assertEqual(lim.retry_after("b", now=1003), 0)

    def test_lockout_ends_when_the_failures_age_out(self):
        lim = FailureLimiter(max_failures=3, window=100)
        for t in (1000, 1010, 1020):
            lim.record_failure("a", now=t)
        self.assertEqual(lim.retry_after("a", now=1021), 80)
        self.assertEqual(lim.retry_after("a", now=1101), 0)  # the first failure has aged out

    def test_success_clears_the_count(self):
        lim = FailureLimiter(max_failures=2, window=100)
        lim.record_failure("a", now=1)
        lim.clear("a")
        lim.record_failure("a", now=2)
        self.assertEqual(lim.retry_after("a", now=3), 0)

    def test_memory_stays_bounded(self):
        with mock.patch("app.security.MAX_TRACKED", 50):
            lim = FailureLimiter()
            for i in range(500):
                lim.record_failure("client-%d" % i, now=1000)
            self.assertLessEqual(len(lim._failures), 50)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        conn = db.connect(self.path)
        db.add_station(conn, "a", "A", "iheart", "2501")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", NOW)])
        conn.commit()
        conn.close()
        self.servers, self.sockets = [], []

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        self.tmp.cleanup()

    def start(self, **kwargs):
        httpd = server.make_server(0, self.path, "America/New_York", host="127.0.0.1", **kwargs)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        return "http://127.0.0.1:%d" % httpd.server_address[1], httpd.server_address[1]

    def call(self, base, method, path, body=None, headers=None, ctype="application/json"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        hdrs = dict(headers or {})
        if data is not None:
            hdrs["Content-Type"] = ctype
        try:
            resp = urlopen(Request(base + path, data=data, method=method, headers=hdrs), timeout=10)
            return resp.status, resp.read(), resp.headers
        except HTTPError as err:
            return err.code, err.read(), err.headers


class LockoutOverHttpTests(Base):
    def test_too_many_wrong_tokens_lock_the_client_out_even_with_the_right_one(self):
        base, _ = self.start(admin_token="s3cret")
        for _ in range(5):
            self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "nope"})[0], 401)
        status, body, headers = self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "s3cret"})
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 0)
        self.assertEqual(self.call(base, "GET", "/api/export", headers={"X-Admin-Token": "s3cret"})[0], 429)
        self.assertEqual(self.call(base, "GET", "/api/stations")[0], 200)  # everything else keeps working

    def test_a_correct_token_resets_the_count(self):
        base, _ = self.start(admin_token="s3cret")
        for _ in range(4):
            self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "nope"})
        self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "s3cret"})[0], 200)
        for _ in range(4):
            self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "nope"})[0], 401)

    def test_forwarded_header_is_only_trusted_when_asked_to_be(self):
        base, _ = self.start(admin_token="s3cret")  # not trusted: every request comes from 127.0.0.1
        for i in range(5):
            self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "x", "X-Forwarded-For": "10.0.0.%d" % i})
        self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "s3cret", "X-Forwarded-For": "10.9.9.9"})[0], 429)

        base, _ = self.start(admin_token="s3cret", trust_forwarded=True)  # trusted: clients are told apart
        for _ in range(5):
            self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "x", "X-Forwarded-For": "10.0.0.1"})
        self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "s3cret", "X-Forwarded-For": "10.0.0.1"})[0], 429)
        self.assertEqual(self.call(base, "POST", "/api/check-token", {}, {"X-Admin-Token": "s3cret", "X-Forwarded-For": "10.0.0.2"})[0], 200)
        # a client can't dodge the lockout by prepending addresses: only the proxy's (last) entry counts
        self.assertEqual(self.call(base, "POST", "/api/check-token", {},
                                   {"X-Admin-Token": "s3cret", "X-Forwarded-For": "1.2.3.4, 10.0.0.1"})[0], 429)


class ReadOnlyTests(Base):
    def test_nothing_can_be_changed_but_everything_can_be_read(self):
        base, _ = self.start(read_only=True)
        writes = [("POST", "/api/stations", {"name": "x", "provider": "iheart", "provider_id": "5"}),
                  ("PATCH", "/api/stations/a", {"name": "x"}), ("POST", "/api/stations/order", {"order": ["a"]}),
                  ("POST", "/api/stations/detect", {"url": "washfm.iheart.com"}), ("DELETE", "/api/stations/a?confirm=a", None),
                  ("POST", "/api/check-token", {})]
        for method, path, body in writes:
            self.assertEqual(self.call(base, method, path, body)[0], 403, path)
        self.assertEqual(self.call(base, "POST", "/api/import", b"{}", ctype="application/x-ndjson")[0], 403)
        conn = db.connect(self.path)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, name FROM stations")], [("a", "A")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 1)
        conn.close()
        for path in ("/", "/stations", "/transfer", "/api/stations", "/api/top-songs", "/api/export"):
            self.assertEqual(self.call(base, "GET", path)[0], 200, path)
        self.assertTrue(json.loads(self.call(base, "GET", "/api/transfer-info")[1])["read_only"])

    def test_read_only_wins_even_with_the_right_token(self):
        base, _ = self.start(read_only=True, admin_token="s3cret")
        self.assertEqual(self.call(base, "PATCH", "/api/stations/a", {"name": "x"}, {"X-Admin-Token": "s3cret"})[0], 403)


class ExportProtectionTests(Base):
    def test_export_needs_the_token_when_one_is_set(self):
        base, _ = self.start(admin_token="s3cret")
        self.assertEqual(self.call(base, "GET", "/api/export")[0], 401)
        self.assertEqual(self.call(base, "GET", "/api/export?plays=0", headers={"X-Admin-Token": "wrong"})[0], 401)
        status, body, _ = self.call(base, "GET", "/api/export", headers={"X-Admin-Token": "s3cret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.splitlines()[0])["plays"], 1)

    def test_export_stays_open_when_no_token_is_set(self):
        base, _ = self.start()
        self.assertEqual(self.call(base, "GET", "/api/export")[0], 200)

    def test_the_csv_endpoint_is_gone(self):
        base, _ = self.start()
        self.assertEqual(self.call(base, "GET", "/api/plays.csv")[0], 404)


class LimitsTests(Base):
    def open_idle(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sockets.append(sock)
        return sock

    def test_connections_beyond_the_cap_get_a_503_and_the_slots_come_back(self):
        base, port = self.start(max_connections=2)
        held = [self.open_idle(port), self.open_idle(port)]
        time.sleep(0.3)  # let the server pick both up
        self.assertEqual(self.call(base, "GET", "/health")[0], 503)
        for sock in held:
            sock.close()
        for _ in range(20):  # the freed threads finish asynchronously
            if self.call(base, "GET", "/health")[0] == 200:
                break
            time.sleep(0.1)
        self.assertEqual(self.call(base, "GET", "/health")[0], 200)

    def test_a_silent_client_is_dropped_after_the_timeout(self):
        base, port = self.start(request_timeout=1)
        sock = self.open_idle(port)
        sock.settimeout(5)
        started = time.time()
        self.assertEqual(sock.recv(100), b"")  # the server hung up on it
        self.assertLess(time.time() - started, 4)
        self.assertEqual(self.call(base, "GET", "/health")[0], 200)

    def test_search_terms_are_capped(self):
        where, args, _, _ = queries.build_where({"q": " ".join("t%d" % i for i in range(40)) + "x" * 500}, None, NOW, with_time=False)
        self.assertEqual(where.count("LIKE"), 5)
        where, args, _, _ = queries.build_where({"q": "a" * 500}, None, NOW, with_time=False)
        self.assertEqual(len(args[0]), 102)  # 100 characters plus the two % wildcards


class HeadersTests(Base):
    def test_pages_and_api_carry_security_headers(self):
        base, _ = self.start()
        for path in ("/", "/stations", "/transfer"):
            _, _, headers = self.call(base, "GET", path)
            csp = headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("http", csp.replace("https:", ""))  # no third-party hosts are allowed to run scripts
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        for path in ("/api/stations", "/api/export", "/vendor/daisyui-5.7.50.css", "/nope"):
            _, _, headers = self.call(base, "GET", path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", path)
        self.assertIsNone(self.call(base, "GET", "/api/stations")[2]["Content-Security-Policy"])

    def test_the_server_does_not_announce_what_it_runs_on(self):
        base, _ = self.start()
        _, _, headers = self.call(base, "GET", "/health")
        self.assertEqual(headers["Server"], "radio-poller")


class VendorTests(Base):
    def files(self):
        return sorted(p.name for p in (STATIC / "vendor").iterdir())

    def test_pinned_libraries_are_served_from_here_with_long_caching_and_gzip(self):
        base, _ = self.start()
        name = next(n for n in self.files() if n.endswith(".css"))
        status, body, headers = self.call(base, "GET", "/vendor/" + name)
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/css"))
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(body, (STATIC / "vendor" / name).read_bytes())
        status, packed, headers = self.call(base, "GET", "/vendor/" + name, headers={"Accept-Encoding": "gzip"})
        self.assertEqual((status, headers["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(packed), body)
        self.assertLess(len(packed), len(body) // 3)

    def test_only_files_in_the_vendor_folder_are_served(self):
        base, _ = self.start()
        for path in ("/vendor/nope.js", "/vendor/../db.py", "/vendor/%2e%2e/app.js", "/vendor/..%2f..%2fapp%2fdb.py", "/vendor/"):
            self.assertEqual(self.call(base, "GET", path)[0], 404, path)

    def test_no_page_loads_anything_from_a_third_party(self):
        vendor = set(self.files())
        for page in ("index.html", "stations.html", "transfer.html"):
            html = (STATIC / page).read_text()
            self.assertNotRegex(html, r"""(?:src|href)=["']https?://""", page)
            used = set(re.findall(r'/vendor/([\w.\-]+)', html))
            self.assertTrue(used, page)
            self.assertLessEqual(used, vendor, page)  # every file a page asks for exists


if __name__ == "__main__":
    unittest.main()
