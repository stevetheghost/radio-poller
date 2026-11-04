import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import db, detect, providers, queries, server, stations, transfer
from app.providers import Play

# 2025-11-05 07:30 UTC is 02:30 on Nov 5 in New York but 23:30 on Nov 4 in Los Angeles (a date safely in the past)
LATE_PT = int(datetime(2025, 11, 5, 7, 30, tzinfo=dt_timezone.utc).timestamp())
NOW = LATE_PT + 3600


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def tz(self, sid):
        return self.conn.execute("SELECT timezone FROM stations WHERE id = ?", (sid,)).fetchone()[0]


class ValidationTests(unittest.TestCase):
    def test_only_real_zone_names_are_accepted(self):
        for good in ("America/Los_Angeles", "America/New_York", "UTC", "Pacific/Honolulu", "America/Argentina/Buenos_Aires"):
            self.assertEqual(db.valid_timezone(good), good)
        for bad in ("", None, 5, "Not/AZone", "../../etc/passwd", "America/../x", "America/New York", "a" * 80, "/etc/passwd"):
            self.assertIsNone(db.valid_timezone(bad), bad)


class SeedTests(TempDb):
    ENTRIES = [{"id": "a", "name": "A", "provider": "iheart", "station_id": 1, "timezone": "America/Los_Angeles"},
               {"id": "b", "name": "B", "provider": "iheart", "station_id": 2, "timezone": "Not/AZone"},
               {"id": "c", "name": "C", "provider": "iheart", "station_id": 3}]

    def test_first_run_seeds_timezones_and_ignores_bad_ones(self):
        db.seed_stations(self.conn, self.ENTRIES)
        self.assertEqual([self.tz(i) for i in "abc"], ["America/Los_Angeles", None, None])
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_databases_seeded_before_timezones_get_them_once_without_overwriting_choices(self):
        for sid in "ab":
            db.add_station(self.conn, sid, sid.upper(), "iheart", "1")
        self.conn.execute("UPDATE stations SET timezone = 'America/Chicago' WHERE id = 'b'")  # chosen on the Stations page
        self.conn.commit()
        self.conn.execute("PRAGMA user_version = 1")
        entries = [dict(e, timezone="America/Los_Angeles") for e in self.ENTRIES[:1]] + \
                  [{"id": "b", "name": "B", "provider": "iheart", "station_id": 2, "timezone": "America/Denver"}]
        self.assertTrue(db.fill_timezones(self.conn, entries))
        self.assertEqual((self.tz("a"), self.tz("b")), ("America/Los_Angeles", "America/Chicago"))
        self.assertFalse(db.fill_timezones(self.conn, entries))  # only ever once


class StationAdminTests(TempDb):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(providers, "fetch", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_with_a_timezone_and_change_or_clear_it(self):
        sid, _ = stations.create(self.conn, {"name": "KROQ", "provider": "audacy", "provider_id": "101-299",
                                             "timezone": "America/Los_Angeles"})
        self.assertEqual(self.tz(sid), "America/Los_Angeles")
        stations.update(self.conn, sid, {"timezone": "America/Denver"})
        self.assertEqual(self.tz(sid), "America/Denver")
        stations.update(self.conn, sid, {"timezone": None})
        self.assertIsNone(self.tz(sid))
        stations.update(self.conn, sid, {"timezone": "America/Chicago"})
        stations.update(self.conn, sid, {"timezone": ""})  # the page's "server default" option
        self.assertIsNone(self.tz(sid))

    def test_unknown_timezones_are_refused_before_anything_happens(self):
        with self.assertRaises(stations.StationError):
            stations.create(self.conn, {"name": "X", "provider": "iheart", "provider_id": "5", "timezone": "Mars/Olympus"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0], 0)
        db.add_station(self.conn, "a", "A", "iheart", "1")
        self.conn.commit()
        with self.assertRaises(stations.StationError):
            stations.update(self.conn, "a", {"timezone": "../../etc"})
        self.assertIsNone(self.tz("a"))


class DetectTests(unittest.TestCase):
    def iheart(self, markets):
        record = {"id": 4321, "name": "Some FM", "website": "some.iheart.com", "markets": markets}
        with mock.patch.object(detect, "_fetch_page", return_value=('{"id":4321}', "u")), \
             mock.patch.object(detect, "_iheart_station", return_value=record):
            return detect.detect("some.iheart.com")["timezone"]

    def test_iheart_zone_comes_from_the_primary_markets_state(self):
        self.assertEqual(self.iheart([{"stateAbbreviation": "CA", "primary": True}]), "America/Los_Angeles")
        self.assertEqual(self.iheart([{"stateAbbreviation": "DC"}]), "America/New_York")
        self.assertEqual(self.iheart([{"stateAbbreviation": "TX", "primary": False}, {"stateAbbreviation": "CO", "primary": True}]),
                         "America/Denver")
        self.assertEqual(self.iheart([{"stateAbbreviation": "AZ"}]), "America/Phoenix")

    def test_iheart_zone_is_left_open_when_it_cannot_be_known(self):
        for state in ("TX", "FL", "IN", "TN", "OR", "NV", "ZZ", None):
            self.assertIsNone(self.iheart([{"stateAbbreviation": state}]), state)
        self.assertIsNone(self.iheart([]))

    def audacy(self, line):
        page = '<title>X | 106.7 KROQ</title> STATION_ROC_ID = \'101-299\'; %s' % line
        with mock.patch.object(detect, "_fetch_page", return_value=(page, "u")):
            return detect.detect("audacy.com/kroq")["timezone"]

    def test_audacy_zone_comes_from_the_page_and_old_names_are_modernised(self):
        self.assertEqual(self.audacy('const timezone = "US/Pacific" || "US/Eastern";'), "America/Los_Angeles")
        self.assertEqual(self.audacy('const timezone = "US/Eastern" || "US/Eastern";'), "America/New_York")
        self.assertEqual(self.audacy('const timezone = "America/Chicago";'), "America/Chicago")
        self.assertIsNone(self.audacy(""))
        self.assertIsNone(self.audacy('const timezone = "Mars/Olympus";'))


class GroupingTests(unittest.TestCase):
    """A station's plays are grouped into days in its own timezone when it is picked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        conn = db.connect(self.path)
        db.add_station(conn, "la", "KROQ", "audacy", "101-299", tz="America/Los_Angeles")
        db.add_station(conn, "dc", "WASH", "iheart", "2501", tz="America/New_York")
        db.add_station(conn, "plain", "No zone", "iheart", "7")  # falls back to the server's
        conn.commit()
        for sid in ("la", "dc", "plain"):
            db.insert_plays(conn, sid, [Play("Adele", "Hello", LATE_PT)])
        db.insert_plays(conn, "la", [Play("Roxette", "The Look", LATE_PT - 5 * 3600)])
        conn.close()
        self.httpd = server.make_server(0, self.path, "America/New_York", host="127.0.0.1")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        return json.loads(urlopen(self.base + path, timeout=5).read())

    def days(self, data):
        # the chart fills the days in between with zeros; only the days that have plays matter here
        return {s["station_id"]: {d: n for d, n in zip(data["labels"], s["counts"]) if n} for s in data["series"]}

    def test_a_picked_station_is_grouped_by_its_own_local_day(self):
        la = self.get("/api/timeseries?station=la&range=all")
        self.assertEqual(la["tz"], "America/Los_Angeles")
        self.assertEqual(self.days(la)["la"], {"2025-11-04": 2})  # 23:30 and 18:30 Pacific, both Nov 4
        dc = self.get("/api/timeseries?station=dc&range=all")
        self.assertEqual((dc["tz"], self.days(dc)["dc"]), ("America/New_York", {"2025-11-05": 1}))  # 02:30 Eastern

    def test_across_all_stations_the_servers_timezone_is_used(self):
        every = self.get("/api/timeseries?range=all")
        self.assertEqual(every["tz"], "America/New_York")
        self.assertEqual(self.days(every)["la"]["2025-11-05"], 1)  # the 23:30 Pacific play is already Nov 5 in New York
        self.assertEqual(self.days(every)["la"]["2025-11-04"], 1)  # and the 18:30 Pacific one is 21:30 on Nov 4 there
        self.assertEqual(self.get("/api/timeseries?station=plain&range=all")["tz"], "America/New_York")

    def test_stations_list_shows_each_timezone_and_the_default(self):
        data = self.get("/api/stations")
        self.assertEqual(data["tz"], "America/New_York")
        self.assertEqual({s["id"]: s["timezone"] for s in data["stations"]},
                         {"la": "America/Los_Angeles", "dc": "America/New_York", "plain": None})

    def test_changing_a_timezone_over_http_and_refusing_a_bad_one(self):
        def patch_station(body):
            req = Request(self.base + "/api/stations/plain", data=json.dumps(body).encode(), method="PATCH",
                          headers={"Content-Type": "application/json"})
            try:
                resp = urlopen(req, timeout=5)
                return resp.status, json.loads(resp.read())
            except HTTPError as err:
                return err.code, json.loads(err.read())
        status, body = patch_station({"timezone": "America/Chicago"})
        self.assertEqual(status, 200)
        self.assertEqual(body["tz"], "America/New_York")  # edits reply with the default too, for the page's labels
        self.assertEqual({s["id"]: s["timezone"] for s in body["stations"]}["plain"], "America/Chicago")
        self.assertEqual(patch_station({"timezone": "Mars/Olympus"})[0], 400)


class SongStationTests(TempDb):
    def test_the_station_behind_a_songs_first_and_last_play_is_reported(self):
        for sid in ("la", "dc"):
            db.add_station(self.conn, sid, sid, "iheart", sid)
        self.conn.commit()
        db.insert_plays(self.conn, "la", [Play("Adele", "Hello", 1000)])
        db.insert_plays(self.conn, "dc", [Play("Adele", "Hello", 5000)])
        info = queries.song(self.conn, {"key": "adele|hello"})
        self.assertEqual((info["first_station_id"], info["last_station_id"]), ("la", "dc"))
        top = queries.top_songs(self.conn, {}, db.load_tz("UTC"), 9000)
        self.assertEqual((top[0]["last_played"], top[0]["last_station_id"]), (5000, "dc"))


class TransferTests(TempDb):
    def test_timezones_travel_with_an_export_and_bad_ones_are_dropped_on_import(self):
        db.add_station(self.conn, "a", "A", "iheart", "1", tz="America/Los_Angeles")
        self.conn.commit()
        data = b"".join(transfer.export_lines(self.conn, NOW, plays=False))
        self.assertEqual(json.loads(data.splitlines()[1])["timezone"], "America/Los_Angeles")
        other = Path(self.tmp.name) / "o.db"
        db.init(other)
        new = db.connect(other)
        transfer.import_stream(new, io.BytesIO(data))
        self.assertEqual(new.execute("SELECT timezone FROM stations").fetchone()[0], "America/Los_Angeles")
        new.close()

        hostile = "\n".join(json.dumps(o) for o in [
            {"type": "header", "format": "radio-poller", "version": 2},
            {"type": "station", "id": "x", "name": "X", "timezone": "../../etc/passwd"},
            {"type": "station", "id": "y", "name": "Y", "timezone": "Mars/Olympus"}]).encode()
        third = Path(self.tmp.name) / "h.db"
        db.init(third)
        h = db.connect(third)
        transfer.import_stream(h, io.BytesIO(hostile))
        self.assertEqual([r[0] for r in h.execute("SELECT timezone FROM stations")], [None, None])
        h.close()

    def test_version_1_exports_without_timezones_still_import(self):
        old = "\n".join(json.dumps(o) for o in [
            {"type": "header", "format": "radio-poller", "version": 1},
            {"type": "station", "id": "a", "name": "A", "sort": 0, "active": 1},
            {"type": "end", "plays": 0}]).encode()
        result = transfer.import_stream(self.conn, io.BytesIO(old))
        self.assertEqual((result["stations_added"], result["complete"]), (1, True))
        self.assertIsNone(self.tz("a"))


if __name__ == "__main__":
    unittest.main()
