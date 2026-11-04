import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import db, detect, poller, providers, queries, server, stations, transfer
from app.providers import Play

NOW = 1_793_800_000
SEED = [
    {"id": "washfm", "name": "97.1 WASH-FM", "provider": "iheart", "station_id": 2501},
    {"id": "2wd", "name": "101.3 2WD", "provider": "audacy", "station_id": "101-988", "poll_seconds": 120},
]


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def rows(self, sql="SELECT * FROM stations ORDER BY sort"):
        return [dict(r) for r in self.conn.execute(sql)]


class SeedAndMigrationTests(TempDb):
    def test_first_run_seeds_from_the_file_once(self):
        self.assertTrue(db.seed_stations(self.conn, SEED))
        self.assertEqual([(r["id"], r["provider"], r["provider_id"], r["poll_seconds"], r["active"]) for r in self.rows()],
                         [("washfm", "iheart", "2501", None, 1), ("2wd", "audacy", "101-988", 120, 1)])
        self.assertFalse(db.seed_stations(self.conn, [{"id": "new", "name": "New", "provider": "iheart", "station_id": 1}]))
        self.assertEqual(len(self.rows()), 2)

    def test_deleting_every_station_does_not_bring_the_file_back(self):
        db.seed_stations(self.conn, SEED)
        self.conn.execute("DELETE FROM stations")
        self.conn.commit()
        self.assertFalse(db.seed_stations(self.conn, SEED))
        self.assertEqual(self.rows(), [])

    def test_seeding_attaches_feeds_to_stations_that_already_have_history(self):
        db.add_station(self.conn, "washfm", "old name")  # from a database made before stations lived in it
        db.insert_plays(self.conn, "washfm", [Play("Adele", "Hello", NOW)])
        db.seed_stations(self.conn, SEED)
        row = self.rows("SELECT * FROM stations WHERE id = 'washfm'")[0]
        self.assertEqual((row["name"], row["provider"], row["active"]), ("97.1 WASH-FM", "iheart", 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 1)

    def test_old_database_is_upgraded_and_keeps_its_colours(self):
        old = Path(self.tmp.name) / "old.db"
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE stations (id TEXT PRIMARY KEY, name TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0, "
                    "active INTEGER NOT NULL DEFAULT 1, last_poll_at INTEGER, last_ok_at INTEGER, last_error TEXT)")
        raw.execute("INSERT INTO stations (id, name, sort) VALUES ('a', 'A', 0), ('b', 'B', 1)")
        raw.commit()
        raw.close()
        db.init(old)
        conn = db.connect(old)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, color, provider FROM stations ORDER BY sort")],
                         [("a", 0, None), ("b", 1, None)])
        self.assertIn("enabled_at", {r["name"] for r in conn.execute("PRAGMA table_info(stations)")})
        conn.close()

    def test_poller_only_polls_switched_on_stations_with_a_feed(self):
        db.add_station(self.conn, "on", "On", "iheart", "1")
        db.add_station(self.conn, "paused", "Paused", "iheart", "2", active=False)
        db.add_station(self.conn, "nofeed", "No feed")
        db.add_station(self.conn, "also-on", "Also on", "audacy", "101-5", 90)
        self.conn.commit()
        got = poller.Poller.active_stations(self.conn)
        self.assertEqual([(s["id"], s["provider"], s["station_id"], s["poll_seconds"]) for s in got],
                         [("on", "iheart", "1", None), ("also-on", "audacy", "101-5", 90)])


@mock.patch.object(providers, "fetch", return_value=[Play("Adele", "Hello", NOW)])
class StationAdminTests(TempDb):
    def test_create_adds_an_enabled_station_with_the_next_colour(self, fetch):
        db.seed_stations(self.conn, SEED)
        sid, newest = stations.create(self.conn, {"name": "106.7 KROQ", "provider": "audacy", "provider_id": "101-299"})
        self.assertEqual((sid, newest.title), ("106-7-kroq", "Hello"))
        row = self.rows("SELECT * FROM stations WHERE id = '106-7-kroq'")[0]
        self.assertEqual((row["provider"], row["provider_id"], row["active"], row["color"], row["sort"]), ("audacy", "101-299", 1, 2, 2))

    def test_create_refuses_a_feed_that_fails(self, fetch):
        fetch.side_effect = OSError("no route")
        with self.assertRaises(stations.StationError) as ctx:
            stations.create(self.conn, {"name": "X", "provider": "iheart", "provider_id": "5"})
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(self.rows(), [])

    def test_create_validates_input(self, fetch):
        for data in ({"name": "", "provider": "iheart", "provider_id": "5"},
                     {"name": "X", "provider": "spotify", "provider_id": "5"},
                     {"name": "X", "provider": "iheart", "provider_id": "abc"},
                     {"name": "X", "provider": "audacy", "provider_id": "2501"},
                     {"name": "X", "provider": "iheart", "provider_id": "5", "poll_seconds": 3},
                     {"name": "X", "provider": "iheart", "provider_id": "5", "id": "Bad Id"}):
            with self.assertRaises(stations.StationError, msg=data):
                stations.create(self.conn, data)
        self.assertEqual(self.rows(), [])
        fetch.assert_not_called()

    def test_the_same_feed_can_be_added_again_under_another_name_and_starts_paused(self, fetch):
        db.seed_stations(self.conn, SEED)
        sid, _ = stations.create(self.conn, {"name": "97.1 WASH-FM Christmas", "provider": "iheart", "provider_id": "2501"})
        self.assertEqual(sid, "97-1-wash-fm-christmas")
        copy = self.rows("SELECT * FROM stations WHERE id = '%s'" % sid)[0]
        self.assertEqual((copy["provider"], copy["provider_id"], copy["active"], copy["color"]), ("iheart", "2501", 0, 2))
        self.assertIsNotNone(copy["enabled_at"])
        original = self.rows("SELECT * FROM stations WHERE id = 'washfm'")[0]
        self.assertEqual((original["active"], original["enabled_at"]), (1, None))
        self.assertEqual([s["id"] for s in poller.Poller.active_stations(self.conn)], ["washfm", "2wd"])  # the copy isn't polled

    def test_a_copy_can_be_added_switched_on_and_even_under_the_same_name(self, fetch):
        db.seed_stations(self.conn, SEED)
        sid, _ = stations.create(self.conn, {"name": "97.1 WASH-FM", "provider": "iheart", "provider_id": "2501", "enabled": True})
        self.assertEqual(sid, "97-1-wash-fm")
        self.assertEqual(self.rows("SELECT active FROM stations WHERE id = '97-1-wash-fm'"), [{"active": 1}])
        sid2, _ = stations.create(self.conn, {"name": "97.1 WASH-FM", "provider": "iheart", "provider_id": "2501"})
        self.assertEqual(sid2, "97-1-wash-fm-2")  # ids stay unique even when names don't

    def test_a_first_copy_of_a_feed_is_switched_on_as_before(self, fetch):
        sid, _ = stations.create(self.conn, {"name": "Brand new", "provider": "iheart", "provider_id": "77"})
        row = self.rows("SELECT * FROM stations WHERE id = '%s'" % sid)[0]
        self.assertEqual((row["active"], row["enabled_at"]), (1, None))  # keeps the feed's recent history

    def test_switching_a_station_on_stamps_when_and_only_then(self, fetch):
        db.seed_stations(self.conn, SEED)
        with mock.patch.object(stations.time, "time", return_value=1000):
            stations.update(self.conn, "washfm", {"enabled": False})
            stations.update(self.conn, "washfm", {"enabled": True})
        self.assertEqual(self.rows("SELECT enabled_at FROM stations WHERE id = 'washfm'"), [{"enabled_at": 1000}])
        with mock.patch.object(stations.time, "time", return_value=2000):
            stations.update(self.conn, "washfm", {"enabled": True, "name": "again"})  # already on: not a fresh start
        self.assertEqual(self.rows("SELECT enabled_at FROM stations WHERE id = 'washfm'"), [{"enabled_at": 1000}])

    def test_a_freshly_switched_on_station_ignores_songs_from_before(self, fetch):
        db.seed_stations(self.conn, SEED)
        fetch.return_value = [Play("Old", "Before", NOW - 600), Play("Old", "Edge", NOW - 1),
                              Play("New", "At", NOW), Play("New", "After", NOW + 200)]
        p = poller.Poller(self.path, 60, 10, threading.Event())
        station = {"id": "washfm", "name": "x", "provider": "iheart", "station_id": "2501", "enabled_at": NOW}
        p.poll(self.conn, station)
        self.assertEqual([r["title"] for r in self.conn.execute("SELECT title FROM plays ORDER BY played_at")], ["At", "After"])
        # with no stamp (a station that has been on all along) everything the feed offers is kept
        p.poll(self.conn, {**station, "id": "2wd", "enabled_at": None})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plays WHERE station_id = '2wd'").fetchone()[0], 4)

    def test_ids_are_made_unique_and_a_feedless_station_is_reconnected(self, fetch):
        db.add_station(self.conn, "kroq", "KROQ")  # imported without a feed, has history
        db.add_station(self.conn, "dc101", "dc101", "iheart", "2525")
        self.conn.commit()
        sid, _ = stations.create(self.conn, {"name": "KROQ", "provider": "audacy", "provider_id": "101-299"})
        self.assertEqual(sid, "kroq")  # attached to the existing station rather than a duplicate
        self.assertEqual(self.rows("SELECT provider, active FROM stations WHERE id = 'kroq'"), [{"provider": "audacy", "active": 1}])
        sid, _ = stations.create(self.conn, {"name": "DC101", "provider": "iheart", "provider_id": "9999"})
        self.assertEqual(sid, "dc101-2")

    def test_update_renames_and_pauses(self, fetch):
        db.seed_stations(self.conn, SEED)
        stations.update(self.conn, "washfm", {"name": "  WASH  ", "enabled": False, "poll_seconds": 30})
        row = self.rows("SELECT * FROM stations WHERE id = 'washfm'")[0]
        self.assertEqual((row["name"], row["active"], row["poll_seconds"]), ("WASH", 0, 30))
        stations.update(self.conn, "washfm", {"enabled": True, "poll_seconds": None})
        row = self.rows("SELECT * FROM stations WHERE id = 'washfm'")[0]
        self.assertEqual((row["active"], row["poll_seconds"]), (1, None))

    def test_update_errors(self, fetch):
        db.add_station(self.conn, "nofeed", "No feed", active=False)
        self.conn.commit()
        for sid, data, status in (("nofeed", {"enabled": True}, 400), ("missing", {"name": "x"}, 404),
                                  ("nofeed", {"name": " "}, 400), ("nofeed", {"enabled": "yes"}, 400)):
            with self.assertRaises(stations.StationError) as ctx:
                stations.update(self.conn, sid, data)
            self.assertEqual(ctx.exception.status, status, (sid, data))

    def test_reorder_changes_the_order_but_not_the_colours(self, fetch):
        db.seed_stations(self.conn, SEED)
        before = {r["id"]: r["color"] for r in self.rows()}
        stations.reorder(self.conn, ["2wd", "washfm"])
        self.assertEqual([r["id"] for r in self.rows()], ["2wd", "washfm"])
        self.assertEqual({r["id"]: r["color"] for r in self.rows()}, before)
        stations.reorder(self.conn, ["washfm"])  # unlisted stations follow
        self.assertEqual([r["id"] for r in self.rows()], ["washfm", "2wd"])
        with self.assertRaises(stations.StationError):
            stations.reorder(self.conn, ["washfm", "nope"])

    def test_delete_removes_the_station_and_its_plays_only_when_confirmed(self, fetch):
        db.seed_stations(self.conn, SEED)
        db.insert_plays(self.conn, "washfm", [Play("A", "B", NOW), Play("C", "D", NOW + 500)])
        db.insert_plays(self.conn, "2wd", [Play("E", "F", NOW)])
        with self.assertRaises(stations.StationError):
            stations.delete(self.conn, "washfm", "")
        with self.assertRaises(stations.StationError):
            stations.delete(self.conn, "washfm", "2wd")
        self.assertEqual(stations.delete(self.conn, "washfm", "washfm"), 2)
        self.assertEqual([r["id"] for r in self.rows()], ["2wd"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 1)
        with self.assertRaises(stations.StationError) as ctx:
            stations.delete(self.conn, "washfm", "washfm")
        self.assertEqual(ctx.exception.status, 404)

    def test_paused_stations_still_show_up_when_picked_in_now_playing(self, fetch):
        db.seed_stations(self.conn, SEED)
        db.insert_plays(self.conn, "washfm", [Play("A", "B", NOW)])
        stations.update(self.conn, "washfm", {"enabled": False})
        self.assertEqual([r["station_id"] for r in queries.now_playing(self.conn, {})], ["2wd"])
        self.assertEqual(queries.now_playing(self.conn, {"station": "washfm"})[0]["title"], "B")


class DetectTests(unittest.TestCase):
    AUDACY_PAGE = ('<title>The World Famous KROQ - KROQ-FM | 106.7 KROQ</title>'
                   '<script>window.STATION_ID = 2286704775; STATION_ROC_ID = \'101-299\'</script>')

    def test_audacy_page(self):
        with mock.patch.object(detect, "_fetch_page", return_value=(self.AUDACY_PAGE, "u")):
            self.assertEqual(detect.detect("audacy.com/kroq"),
                             {"provider": "audacy", "provider_id": "101-299", "name": "106.7 KROQ", "timezone": None})

    def test_audacy_page_without_a_station(self):
        with mock.patch.object(detect, "_fetch_page", return_value=("<title>Audacy</title>", "u")):
            with self.assertRaises(detect.DetectError):
                detect.detect("https://www.audacy.com/")

    def test_iheart_page_url_carries_its_id(self):
        with mock.patch.object(detect, "_fetch_page") as page, \
             mock.patch.object(detect, "_iheart_station", return_value={"id": 2501, "name": "97.1 WASH-FM", "website": "washfm.iheart.com"}):
            for url in ("https://www.iheart.com/live/97-1-wash-fm-2501/", "https://www.iheart.com/live/2501/?autoplay=true"):
                self.assertEqual(detect.detect(url), {"provider": "iheart", "provider_id": "2501", "name": "97.1 WASH-FM", "timezone": None})
            page.assert_not_called()

    def test_iheart_subdomain_picks_the_id_whose_website_matches(self):
        records = {"282167": {"id": 282167, "name": "Other", "website": "other.iheart.com"},
                   "2525": {"id": 2525, "name": "DC101", "website": "dc101.iheart.com"}}
        page = '{"id":282167,"x":1} {"id":2525,"y":2} {"id":2525}'
        with mock.patch.object(detect, "_fetch_page", return_value=(page, "u")), \
             mock.patch.object(detect, "_iheart_station", side_effect=lambda i: records.get(i)):
            self.assertEqual(detect.detect("dc101.iheart.com")["provider_id"], "2525")

    def test_other_hosts_are_refused_without_any_request(self):
        with mock.patch.object(detect, "_fetch_page") as page:
            for url in ("https://www.96x.fm/", "http://169.254.169.254/latest/", "https://iheart.com.evil.example/x",
                        "https://evil.example/?u=audacy.com", "ftp://www.audacy.com/kroq", "", "nonsense"):
                with self.assertRaises(detect.DetectError, msg=url):
                    detect.detect(url)
            page.assert_not_called()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        conn = db.connect(self.path)
        db.seed_stations(conn, SEED)
        db.insert_plays(conn, "washfm", [Play("Adele", "Hello", NOW)])
        conn.close()
        self.servers = []

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        self.tmp.cleanup()

    def start(self, **kwargs):
        httpd = server.make_server(0, self.path, "America/New_York", host="127.0.0.1", **kwargs)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append(httpd)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def call(self, base, method, path, body=None, headers=None, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode()
        hdrs = dict(headers or {})
        if data is not None:
            hdrs["Content-Type"] = ctype
        try:
            resp = urlopen(Request(base + path, data=data, method=method, headers=hdrs), timeout=10)
            return resp.status, json.loads(resp.read())
        except HTTPError as err:
            return err.code, json.loads(err.read())

    def test_full_lifecycle(self):
        base = self.start()
        with mock.patch.object(providers, "fetch", return_value=[Play("Adele", "Hello", NOW)]):
            status, body = self.call(base, "POST", "/api/stations", {"name": "KROQ", "provider": "audacy", "provider_id": "101-299"})
            self.assertEqual((status, body["created"], body["sample"]["title"]), (201, "kroq", "Hello"))
            self.assertEqual([s["id"] for s in body["stations"]], ["washfm", "2wd", "kroq"])
        status, body = self.call(base, "PATCH", "/api/stations/kroq", {"name": "106.7 KROQ", "enabled": False})
        self.assertEqual(status, 200)
        kroq = [s for s in body["stations"] if s["id"] == "kroq"][0]
        self.assertEqual((kroq["name"], kroq["active"], kroq["provider"], kroq["provider_id"]), ("106.7 KROQ", 0, "audacy", "101-299"))
        status, body = self.call(base, "POST", "/api/stations/order", {"order": ["kroq", "2wd", "washfm"]})
        self.assertEqual([s["id"] for s in body["stations"]], ["kroq", "2wd", "washfm"])
        self.assertEqual(self.call(base, "DELETE", "/api/stations/washfm")[0], 400)  # needs confirm
        status, body = self.call(base, "DELETE", "/api/stations/washfm?confirm=washfm")
        self.assertEqual((status, body["deleted_plays"]), (200, 1))
        self.assertEqual(self.call(base, "PATCH", "/api/stations/washfm", {"name": "x"})[0], 404)

    def test_adding_a_copy_over_http_comes_back_paused(self):
        base = self.start()
        with mock.patch.object(providers, "fetch", return_value=[Play("Adele", "Hello", NOW)]):
            status, body = self.call(base, "POST", "/api/stations", {"name": "WASH Christmas", "provider": "iheart", "provider_id": "2501"})
        self.assertEqual(status, 201)
        by_id = {s["id"]: s for s in body["stations"]}
        self.assertEqual((by_id[body["created"]]["active"], by_id["washfm"]["active"]), (0, 1))
        self.assertEqual(by_id[body["created"]]["provider_id"], by_id["washfm"]["provider_id"])

    def test_stations_only_export_over_http(self):
        base = self.start()
        resp = urlopen(base + "/api/export?plays=0", timeout=5)
        self.assertIn("radio-poller-stations-", resp.headers["Content-Disposition"])
        lines = [json.loads(x) for x in resp.read().splitlines()]
        self.assertEqual([r["type"] for r in lines], ["header", "station", "station", "end"])
        one = urlopen(base + "/api/export?plays=0&station=2wd", timeout=5)
        self.assertIn("radio-poller-stations-2wd-", one.headers["Content-Disposition"])
        self.assertEqual(len(one.read().splitlines()), 3)
        full = urlopen(base + "/api/export", timeout=5).read().splitlines()
        self.assertEqual(len(full), 5)  # header, 2 stations, 1 play, end

    def test_errors_come_back_as_json(self):
        base = self.start()
        with mock.patch.object(providers, "fetch", side_effect=OSError("down")):
            status, body = self.call(base, "POST", "/api/stations", {"name": "X", "provider": "iheart", "provider_id": "7"})
        self.assertEqual(status, 422)
        self.assertIn("didn't work", body["error"])
        self.assertEqual(self.call(base, "POST", "/api/stations", {"name": ""})[0], 400)
        self.assertEqual(self.call(base, "POST", "/api/stations/detect", {"url": "https://www.96x.fm/"})[0], 422)

    def test_detect_reports_the_feed_and_whether_it_is_already_added(self):
        base = self.start()
        found = {"provider": "iheart", "provider_id": "2501", "name": "97.1 WASH-FM"}
        with mock.patch.object(detect, "detect", return_value=dict(found)), \
             mock.patch.object(providers, "fetch", return_value=[Play("Adele", "Hello", NOW)]):
            status, body = self.call(base, "POST", "/api/stations/detect", {"url": "washfm.iheart.com"})
        self.assertEqual(status, 200)
        self.assertEqual((body["feed_ok"], body["sample"], body["existing"]),
                         (True, {"artist": "Adele", "title": "Hello"}, "97.1 WASH-FM"))

    def test_changes_need_a_json_content_type(self):
        base = self.start()
        for method, path in (("POST", "/api/stations"), ("PATCH", "/api/stations/washfm"), ("POST", "/api/stations/order")):
            status, _ = self.call(base, method, path, {"name": "x"}, ctype="text/plain")
            self.assertEqual(status, 415, path)
        self.assertEqual(len(queries.stations(db.connect(self.path))), 2)

    def test_admin_token_protects_every_change(self):
        base = self.start(admin_token="s3cret", trust_forwarded=True)  # each request pretends to be a new client,
        good = {"X-Admin-Token": "s3cret"}                              # so the wrong-token lockout doesn't get in the way
        counter = iter(range(1, 1000))
        ip = lambda extra=None: {"X-Forwarded-For": "10.0.0.%d" % next(counter), **(extra or {})}
        calls = [("POST", "/api/stations", {"name": "x"}), ("PATCH", "/api/stations/washfm", {"name": "x"}),
                 ("POST", "/api/stations/order", {"order": []}), ("POST", "/api/stations/detect", {"url": "x"}),
                 ("DELETE", "/api/stations/washfm?confirm=washfm", None)]
        for method, path, body in calls:
            self.assertEqual(self.call(base, method, path, body, ip())[0], 401, path)
            self.assertEqual(self.call(base, method, path, body, ip({"X-Admin-Token": "wrong"}))[0], 401, path)
        self.assertEqual(self.call(base, "PATCH", "/api/stations/washfm", {"name": "Renamed"}, ip(good))[0], 200)
        self.assertEqual(self.call(base, "GET", "/api/stations")[0], 200)  # reading stays open


class TransferFeedSettingsTests(TempDb):
    def export(self, **kw):
        return b"".join(transfer.export_lines(self.conn, NOW, **kw))

    def fresh(self):
        path = Path(self.tmp.name) / "new.db"
        db.init(path)
        return db.connect(path)

    def test_export_includes_feed_settings_and_import_recreates_the_station(self):
        db.seed_stations(self.conn, SEED)
        db.insert_plays(self.conn, "2wd", [Play("Adele", "Hello", NOW)])
        new = self.fresh()
        result = transfer.import_stream(new, io.BytesIO(self.export()))
        self.assertEqual((result["stations_added"], result["plays_added"]), (2, 1))
        rows = {r["id"]: dict(r) for r in new.execute("SELECT * FROM stations")}
        self.assertEqual((rows["2wd"]["provider"], rows["2wd"]["provider_id"], rows["2wd"]["poll_seconds"], rows["2wd"]["active"]),
                         ("audacy", "101-988", 120, 1))
        self.assertEqual(poller.Poller.active_stations(new)[1]["id"], "2wd")  # and it gets polled
        new.close()

    def test_stations_only_export_has_settings_and_no_plays(self):
        db.seed_stations(self.conn, SEED)
        db.insert_plays(self.conn, "2wd", [Play("Adele", "Hello", NOW)])
        data = self.export(plays=False)
        records = [json.loads(x) for x in data.splitlines()]
        self.assertEqual([r["type"] for r in records], ["header", "station", "station", "end"])
        self.assertEqual((records[0]["plays"], records[-1]["plays"]), (0, 0))
        self.assertEqual([(r["id"], r["provider"], r["provider_id"]) for r in records[1:3]],
                         [("washfm", "iheart", "2501"), ("2wd", "audacy", "101-988")])
        # it sets up the same stations on a fresh server, without any history
        new = self.fresh()
        result = transfer.import_stream(new, io.BytesIO(data))
        self.assertEqual((result["stations_added"], result["plays_added"], result["complete"]), (2, 0, True))
        self.assertEqual([s["id"] for s in poller.Poller.active_stations(new)], ["washfm", "2wd"])
        self.assertEqual(new.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 0)
        # and one station on its own
        only = [json.loads(x)["id"] for x in self.export(station="2wd", plays=False).splitlines() if b'"station"' in x[:20]]
        self.assertEqual(only, ["2wd"])
        new.close()

    def test_paused_stations_stay_paused_and_existing_ones_are_untouched(self):
        db.seed_stations(self.conn, SEED)
        self.conn.execute("UPDATE stations SET active = 0 WHERE id = 'washfm'")
        self.conn.commit()
        new = self.fresh()
        db.add_station(new, "2wd", "My own name", "audacy", "101-988", 45)
        new.commit()
        transfer.import_stream(new, io.BytesIO(self.export()))
        rows = {r["id"]: dict(r) for r in new.execute("SELECT * FROM stations")}
        self.assertEqual(rows["washfm"]["active"], 0)
        self.assertEqual((rows["2wd"]["name"], rows["2wd"]["poll_seconds"]), ("My own name", 45))
        new.close()

    def test_version_1_files_still_import_but_without_feeds(self):
        v1 = "\n".join(json.dumps(o) for o in [
            {"type": "header", "format": "radio-poller", "version": 1},
            {"type": "station", "id": "old", "name": "Old", "sort": 0, "active": 1},
            {"type": "play", "station_id": "old", "artist": "A", "title": "B", "played_at": NOW},
            {"type": "end", "plays": 1}]).encode()
        new = self.fresh()
        result = transfer.import_stream(new, io.BytesIO(v1))
        self.assertEqual((result["plays_added"], result["complete"]), (1, True))
        row = dict(new.execute("SELECT * FROM stations").fetchone())
        self.assertEqual((row["provider"], row["active"]), (None, 0))
        new.close()

    def test_a_hostile_station_record_cannot_set_an_unsafe_feed(self):
        lines = "\n".join(json.dumps(o) for o in [
            {"type": "header", "format": "radio-poller", "version": 2},
            {"type": "station", "id": "x", "name": "X", "active": 1, "provider": "iheart", "provider_id": "../../etc"},
            {"type": "station", "id": "y", "name": "Y", "active": 1, "provider": "curl", "provider_id": "1"}]).encode()
        new = self.fresh()
        transfer.import_stream(new, io.BytesIO(lines))
        self.assertEqual([(r["provider"], r["active"]) for r in new.execute("SELECT * FROM stations")], [(None, 0), (None, 0)])
        new.close()


if __name__ == "__main__":
    unittest.main()
