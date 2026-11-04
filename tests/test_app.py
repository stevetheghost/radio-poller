import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from app import db, poller, providers, queries, server, transfer
from app.providers import Play

NOW = 1_793_800_000  # 2026-11-04 ~13:46 UTC


def add_stations(conn, *ids, feed=False):
    """Add stations named after their ids. `feed` gives them an iHeart feed so they can be polled."""
    for sid in ids:
        db.add_station(conn, sid, sid.title(), "iheart" if feed else None, "2501" if feed else None)
    conn.commit()


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        self.conn = db.connect(self.path, db.load_tz("America/New_York"))
        add_stations(self.conn, "a", "b")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_song_key_ignores_case_and_spacing(self):
        self.assertEqual(db.song_key("Madonna ", "Like  A Virgin"), db.song_key("madonna", "like a virgin"))

    def test_same_play_is_not_counted_twice(self):
        plays = [Play("Adele", "Hello", NOW), Play("Roxette", "Listen To Your Heart", NOW - 300)]
        self.assertEqual(db.insert_plays(self.conn, "a", plays), 2)
        self.assertEqual(db.insert_plays(self.conn, "a", plays), 0)
        # a shifted timestamp for the same song is the same play
        self.assertEqual(db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW + 20)]), 0)

    def test_repeat_play_later_is_counted(self):
        db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW)])
        self.assertEqual(db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW + 4 * 3600)]), 1)

    def test_same_song_on_two_stations_counts_separately(self):
        db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW)])
        self.assertEqual(db.insert_plays(self.conn, "b", [Play("Adele", "Hello", NOW)]), 1)

    def test_queries(self):
        db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW - 4 * 3600), Play("Adele", "Hello", NOW - 3600),
                                         Play("Roxette", "The Look", NOW - 600)])
        db.insert_plays(self.conn, "b", [Play("Adele", "Hello", NOW - 1800)])
        tz = db.load_tz("America/New_York")

        top = queries.top_songs(self.conn, {}, tz, NOW)
        self.assertEqual((top[0]["title"], top[0]["plays"]), ("Hello", 3))
        self.assertEqual(queries.summary(self.conn, {"station": "a"}, tz, NOW), {"plays": 3, "songs": 2, "artists": 2})
        self.assertEqual(queries.summary(self.conn, {"q": "adele hel"}, tz, NOW)["plays"], 3)
        self.assertEqual(queries.summary(self.conn, {"q": "100%"}, tz, NOW)["plays"], 0)  # % is not a wildcard
        self.assertEqual(queries.summary(self.conn, {"range": "24h"}, tz, NOW + 30 * 3600)["plays"], 0)

        ts = queries.timeseries(self.conn, {"range": "24h"}, tz, NOW)
        self.assertEqual(ts["bucket"], "hour")
        self.assertEqual(sum(sum(s["counts"]) for s in ts["series"]), 4)
        ts = queries.timeseries(self.conn, {"range": "7d"}, tz, NOW)
        self.assertEqual((ts["bucket"], len(ts["labels"])), ("day", 7))
        self.assertEqual(sum(sum(s["counts"]) for s in ts["series"]), 4)

        info = queries.song(self.conn, {"key": "adele|hello"})
        self.assertEqual((info["plays"], len(info["stations"])), (3, 2))
        self.assertIsNone(queries.song(self.conn, {"key": "nobody|nothing"}))
        self.assertEqual(queries.recent(self.conn, {"limit": "2"}, tz, NOW)[0]["title"], "The Look")


    def test_art_is_stored_and_backfilled_onto_existing_play(self):
        db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW)])
        self.assertIsNone(self.conn.execute("SELECT image_url FROM plays").fetchone()[0])
        self.assertEqual(db.insert_plays(self.conn, "a", [Play("Adele", "Hello", NOW, "https://img/x.jpg")]), 0)
        self.assertEqual(self.conn.execute("SELECT image_url FROM plays").fetchone()[0], "https://img/x.jpg")

    def test_now_playing_is_latest_play_per_active_station(self):
        db.insert_plays(self.conn, "a", [Play("Old", "Song", NOW - 900), Play("New", "Song", NOW - 60, "https://img/n.jpg")])
        rows = {r["station_id"]: r for r in queries.now_playing(self.conn, {})}
        self.assertEqual((rows["a"]["artist"], rows["a"]["image_url"]), ("New", "https://img/n.jpg"))
        self.assertIsNone(rows["b"]["title"])  # no plays yet
        self.assertEqual([r["station_id"] for r in queries.now_playing(self.conn, {"station": "b"})], ["b"])

    def test_old_database_gets_image_column(self):
        import sqlite3
        old = Path(self.tmp.name) / "old.db"
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY, station_id TEXT, artist TEXT, title TEXT, song_key TEXT, played_at INTEGER)")
        raw.commit(); raw.close()
        db.init(old)
        conn = db.connect(old)
        self.assertIn("image_url", {r["name"] for r in conn.execute("PRAGMA table_info(plays)")})
        conn.close()


class ProviderTests(unittest.TestCase):
    def test_iheart_parsing(self):
        payload = {"data": [{"artist": "Madonna", "title": "Like a Virgin", "startTime": 1793801506},
                            {"artist": "", "title": "Station ID", "startTime": 1793801400}]}
        with mock.patch.object(providers, "get_json", return_value=payload) as get:
            plays = providers.iheart({"station_id": 2501}, 5)
        self.assertEqual([(p.artist, p.title, p.played_at) for p in plays], [("Madonna", "Like a Virgin", 1793801506)])
        self.assertIsNone(plays[0].image)
        self.assertIn("/stream/2501/trackHistory?limit=5", get.call_args[0][0])

    def test_iheart_art_is_a_resized_https_thumbnail(self):
        payload = {"data": [{"artist": "A", "title": "T", "startTime": 1, "imagePath": "http://image.iheart.com/x/y.jpg"}]}
        with mock.patch.object(providers, "get_json", return_value=payload):
            image = providers.iheart({"station_id": 1}, 1)[0].image
        self.assertTrue(image.startswith("https://i.iheart.com/v3/url/"))
        self.assertTrue(image.endswith("?ops=cover(300,300)"))
        token = image.split("/v3/url/")[1].split("?")[0]
        import base64
        self.assertEqual(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)), b"https://image.iheart.com/x/y.jpg")

    def test_audacy_parsing(self):
        payload = {"performances": [{"artist": "Billy Joel", "title": "Uptown Girl", "time": "2026-11-04T14:05:37Z",
                                     "mediumimage": "https://enrichment.example/m"}]}
        with mock.patch.object(providers, "get_json", return_value=payload):
            plays = providers.audacy({"station_id": "101-988"}, 5)
        self.assertEqual(plays, [Play("Billy Joel", "Uptown Girl", 1793801137, "https://enrichment.example/m")])

    def test_unexpected_response_is_an_error(self):
        with mock.patch.object(providers, "get_json", return_value={"code": 410}):
            with self.assertRaises(ValueError):
                providers.iheart({"station_id": 1}, 5)


class PollerTests(unittest.TestCase):
    def test_invalid_station_entries_are_skipped(self):
        good = {"id": "ok", "name": "OK", "provider": "iheart", "station_id": 1}
        bad = [{"id": "Bad Id", "name": "x", "provider": "iheart", "station_id": 1},
               {"id": "nop", "name": "x", "provider": "nope", "station_id": 1},
               {"id": "missing", "provider": "iheart"}, good, dict(good)]
        self.assertEqual(poller.parse_stations(bad), [good])

    def test_failure_is_recorded_and_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db.init(path)
            conn = db.connect(path)
            st = {"id": "a", "name": "A", "provider": "iheart", "station_id": 1}
            add_stations(conn, "a")
            p = poller.Poller(path, 60, 10, threading.Event())
            with mock.patch.object(providers, "fetch", side_effect=OSError("boom")):
                p.poll(conn, st)
            row = conn.execute("SELECT last_error, last_ok_at FROM stations").fetchone()
            self.assertIn("boom", row["last_error"])
            self.assertIsNone(row["last_ok_at"])
            self.assertEqual(p.delay(st), 120)
            with mock.patch.object(providers, "fetch", return_value=[Play("A", "B", NOW)]):
                p.poll(conn, st)
            self.assertEqual(p.delay(st), 60)
            conn.close()


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = self._db("src")
        add_stations(self.src, "a", "old")
        self.src.execute("UPDATE stations SET active = 0 WHERE id = 'old'")
        db.insert_plays(self.src, "a", [Play("Adele", "Hello", NOW, "https://img/h.jpg"), Play("Roxette", "The Look", NOW - 900)])
        db.insert_plays(self.src, "old", [Play("Pearl Jam", "Black", NOW - 5000)])
        self.dst = self._db("dst")

    def _db(self, name):
        path = Path(self.tmp.name) / (name + ".db")
        db.init(path)
        return db.connect(path)

    def tearDown(self):
        self.src.close()
        self.dst.close()
        self.tmp.cleanup()

    def export(self):
        return b"".join(transfer.export_lines(self.src, NOW))

    def plays(self, conn):
        return [tuple(r) for r in conn.execute(
            "SELECT station_id, artist, title, played_at, image_url FROM plays ORDER BY played_at")]

    def test_round_trip_preserves_everything(self):
        result = transfer.import_stream(self.dst, io.BytesIO(self.export()))
        self.assertEqual((result["plays_added"], result["plays_skipped"], result["invalid_lines"]), (3, 0, 0))
        self.assertTrue(result["complete"])
        self.assertEqual(self.plays(self.dst), self.plays(self.src))
        self.assertEqual(result["stations_added"], 2)
        # a station the new server's config doesn't know is kept, as removed
        self.assertEqual(self.dst.execute("SELECT active FROM stations WHERE id = 'old'").fetchone()[0], 0)

    def test_importing_twice_adds_nothing(self):
        transfer.import_stream(self.dst, io.BytesIO(self.export()))
        again = transfer.import_stream(self.dst, io.BytesIO(self.export()))
        self.assertEqual((again["plays_added"], again["plays_skipped"], again["stations_added"]), (0, 3, 0))
        self.assertEqual(len(self.plays(self.dst)), 3)

    def test_import_merges_with_existing_history(self):
        add_stations(self.dst, "a")
        db.insert_plays(self.dst, "a", [Play("Adele", "Hello", NOW + 10), Play("Madonna", "Vogue", NOW - 100)])
        result = transfer.import_stream(self.dst, io.BytesIO(self.export()))
        self.assertEqual((result["plays_added"], result["plays_skipped"]), (2, 1))  # Hello is within the dedupe window
        self.assertEqual(len(self.plays(self.dst)), 4)
        self.assertEqual(self.dst.execute("SELECT image_url FROM plays WHERE title = 'Hello'").fetchone()[0], "https://img/h.jpg")

    def test_rejects_files_that_are_not_exports(self):
        for body in (b"", b"hello world\n", b'{"type":"play"}\n', json.dumps({"type": "header", "format": "other", "version": 1}).encode(),
                     json.dumps({"type": "header", "format": "radio-poller", "version": 99}).encode()):
            with self.assertRaises(transfer.FormatError):
                transfer.import_stream(self.dst, io.BytesIO(body))
        self.assertEqual(self.plays(self.dst), [])

    def test_bad_lines_are_counted_and_skipped(self):
        lines = self.export().splitlines()
        lines.insert(2, b"not json")
        lines.insert(3, json.dumps({"type": "play", "station_id": "a", "artist": "", "title": "x", "played_at": NOW}).encode())
        lines.insert(4, json.dumps({"type": "play", "station_id": "BAD ID", "artist": "x", "title": "y", "played_at": NOW}).encode())
        lines.insert(5, json.dumps({"type": "play", "station_id": "a", "artist": "x", "title": "y", "played_at": "soon"}).encode())
        lines.insert(6, json.dumps({"type": "mystery"}).encode())
        result = transfer.import_stream(self.dst, io.BytesIO(b"\n".join(lines)))
        self.assertEqual((result["plays_added"], result["invalid_lines"]), (3, 5))
        self.assertEqual(len(result["errors"]), 5)

    def test_truncated_file_is_reported_incomplete(self):
        cut = self.export().rsplit(b"\n", 2)[0] + b"\n"  # drop the end marker
        result = transfer.import_stream(self.dst, io.BytesIO(cut))
        self.assertFalse(result["complete"])
        self.assertEqual(result["plays_added"], 3)

    def test_single_station_export_has_only_that_station(self):
        records = [json.loads(x) for x in b"".join(transfer.export_lines(self.src, NOW, "old")).splitlines()]
        self.assertEqual([r["type"] for r in records], ["header", "station", "play", "end"])
        self.assertEqual((records[0]["plays"], records[1]["id"], records[2]["title"], records[3]["plays"]), (1, "old", "Black", 1))
        # it imports cleanly on its own
        result = transfer.import_stream(self.dst, io.BytesIO(b"".join(transfer.export_lines(self.src, NOW, "a"))))
        self.assertEqual((result["plays_added"], result["stations_added"], result["complete"]), (2, 2 - 1, True))
        self.assertEqual({r[0] for r in self.plays(self.dst)}, {"a"})

    def test_export_ends_with_marker_and_counts(self):
        lines = [json.loads(x) for x in self.export().splitlines()]
        self.assertEqual((lines[0]["type"], lines[0]["plays"]), ("header", 3))
        self.assertEqual(lines[-1], {"type": "end", "plays": 3})


class TransferHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        conn = db.connect(self.path)
        add_stations(conn, "a")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", NOW)])
        self.export = b"".join(transfer.export_lines(conn, NOW))
        conn.close()
        self.servers = []

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        self.tmp.cleanup()

    def start(self, **kwargs):
        path = Path(self.tmp.name) / ("s%d.db" % len(self.servers))
        db.init(path)
        httpd = server.make_server(0, path, "America/New_York", host="127.0.0.1", **kwargs)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        return "http://127.0.0.1:%d" % httpd.server_address[1], path

    def post(self, base, body, headers=None):
        from urllib.error import HTTPError
        from urllib.request import Request
        req = Request(base + "/api/import", data=body, method="POST",
                      headers={"Content-Type": "application/x-ndjson", **(headers or {})})
        try:
            resp = urlopen(req, timeout=10)
            return resp.status, json.loads(resp.read())
        except HTTPError as err:
            return err.code, json.loads(err.read())

    def test_import_over_http_and_export_download(self):
        base, path = self.start()
        status, result = self.post(base, self.export)
        self.assertEqual((status, result["plays_added"], result["complete"]), (200, 1, True))
        resp = urlopen(base + "/api/export", timeout=5)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertEqual(json.loads(resp.read().splitlines()[0])["plays"], 1)
        info = json.loads(urlopen(base + "/api/transfer-info", timeout=5).read())
        self.assertEqual((info["plays"], info["token_required"]), (1, False))

    def test_export_one_station_over_http(self):
        base, path = self.start()
        self.post(base, self.export)
        resp = urlopen(base + "/api/export?station=a", timeout=5)
        self.assertIn("radio-poller-a-", resp.headers["Content-Disposition"])
        self.assertEqual(json.loads(resp.read().splitlines()[0])["plays"], 1)
        info = json.loads(urlopen(base + "/api/transfer-info", timeout=5).read())
        self.assertEqual([(x["id"], x["plays"]) for x in info["station_list"]], [("a", 1)])
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as ctx:
            urlopen(base + "/api/export?station=nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_wrong_content_type_is_refused(self):
        base, _ = self.start()
        for ctype in ("text/plain", "multipart/form-data"):
            status, result = self.post(base, self.export, {"Content-Type": ctype})
            self.assertEqual(status, 415)

    def test_admin_token_is_enforced_when_set(self):
        base, _ = self.start(admin_token="s3cret")
        self.assertEqual(self.post(base, self.export)[0], 401)
        self.assertEqual(self.post(base, self.export, {"X-Admin-Token": "nope"})[0], 401)
        self.assertEqual(self.post(base, self.export, {"X-Admin-Token": "s3cret"})[0], 200)
        self.assertTrue(json.loads(urlopen(base + "/api/transfer-info", timeout=5).read())["token_required"])

    def test_size_limit_and_bad_file(self):
        base, _ = self.start(max_import_bytes=50)
        self.assertEqual(self.post(base, self.export)[0], 413)
        base, _ = self.start()
        status, result = self.post(base, b"this is not an export\n")
        self.assertEqual(status, 400)
        self.assertIn("radio-poller", result["error"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "t.db"
        db.init(path)
        conn = db.connect(path)
        add_stations(conn, "a")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", int(time.time()) - 600)])
        conn.close()
        cls.httpd = server.make_server(0, path, "America/New_York", host="127.0.0.1")
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        return urlopen(self.base + path, timeout=5).read()

    def test_endpoints(self):
        self.assertTrue(json.loads(self.get("/health"))["ok"])
        self.assertEqual(json.loads(self.get("/api/summary?range=24h"))["plays"], 1)
        np = json.loads(self.get("/api/now-playing?station=a"))
        self.assertEqual(np["stations"][0]["title"], "Hello")
        self.assertEqual(json.loads(self.get("/api/top-songs"))[0]["song_key"], "adele|hello")
        self.assertIn(b"Radio Poller", self.get("/"))
        self.assertIn(b"station", self.get("/app.js"))

    def test_unknown_paths(self):
        from urllib.error import HTTPError
        for path in ("/nope", "/api/nope", "/api/song?key=zzz", "/../app/db.py", "/api/plays.csv"):
            with self.assertRaises(HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
