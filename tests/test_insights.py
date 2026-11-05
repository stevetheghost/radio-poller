import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from app import db, queries, server
from app.providers import Play

DAY = 86400
HOUR = 3600
NY = db.load_tz("America/New_York")
# 2026-11-04 12:00 UTC (a Wednesday; 07:00 in New York once the clocks went back on 2026-11-01)
NOW = 1_793_793_600


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        self.conn = db.connect(self.path, NY)
        for sid, tz in (("a", "America/New_York"), ("b", "America/Los_Angeles"), ("c", None)):
            db.add_station(self.conn, sid, sid.upper(), "iheart", "1", tz=tz)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def play(self, station, artist, title, when, times=1):
        for i in range(times):
            db.insert_plays(self.conn, station, [Play(artist, title, when + i * 4 * HOUR)])
        self.conn.commit()


class HeatmapTests(Base):
    def test_plays_land_on_weekday_and_hour_in_the_zone(self):
        # Wed 2026-11-04 07:00 New York is hour 7 of weekday 2 (Monday = 0); in Los Angeles it is 04:00
        self.play("a", "Adele", "Hello", NOW)
        self.play("a", "Adele", "Hello", NOW + 7 * DAY)
        self.play("a", "Roxette", "The Look", NOW + 3 * HOUR)  # 10:00
        ny = queries.heatmap(self.conn, {}, NY, NOW + 8 * DAY)
        self.assertEqual((ny["cells"][2][7], ny["cells"][2][10], ny["total"], ny["max"]), (2, 1, 3, 2))
        la = queries.heatmap(self.conn, {}, db.load_tz("America/Los_Angeles"), NOW + 8 * DAY)
        self.assertEqual((la["cells"][2][4], la["cells"][2][7]), (2, 1))

    def test_filters_apply(self):
        self.play("a", "Adele", "Hello", NOW)
        self.play("b", "Roxette", "The Look", NOW)
        self.assertEqual(queries.heatmap(self.conn, {"station": "b"}, NY, NOW + 60)["total"], 1)
        self.assertEqual(queries.heatmap(self.conn, {"q": "adele"}, NY, NOW + 60)["total"], 1)
        self.assertEqual(queries.heatmap(self.conn, {"range": "24h"}, NY, NOW + 2 * DAY)["total"], 0)

    def test_empty_database_is_all_zero(self):
        heat = queries.heatmap(self.conn, {}, NY, NOW)
        self.assertEqual((len(heat["cells"]), len(heat["cells"][0]), heat["max"], heat["total"]), (7, 24, 0, 0))

    def test_half_hour_zones_land_in_the_right_hour(self):
        india = db.load_tz("Asia/Kolkata")  # UTC+5:30
        self.play("a", "Adele", "Hello", NOW + 20 * 60)  # 12:20 UTC = 17:50 in India
        cells = queries.heatmap(self.conn, {}, india, NOW + DAY)["cells"]
        self.assertEqual(cells[2][17], 1)

    def test_a_dst_change_is_followed(self):
        # 03:30 UTC on 2026-03-08 is 22:30 EST the night before; 2026-03-09 03:30 UTC is 23:30 EDT
        self.play("a", "Adele", "Hello", 1_772_940_600)
        self.play("a", "Adele", "Hello", 1_773_027_000)
        cells = queries.heatmap(self.conn, {}, NY, 1_773_100_000)["cells"]
        self.assertEqual(cells[5][22], 1)  # Saturday 22:30 EST
        self.assertEqual(cells[6][23], 1)  # Sunday 23:30 EDT


class ChangesTests(Base):
    def setUp(self):
        super().setUp()
        week = 7 * DAY
        # history: old favourite plays steadily; "Faded" plays in the previous week only; "Fresh" appears this week
        self.play("a", "Old", "Favourite", NOW - 20 * DAY, times=3)
        self.play("a", "Old", "Favourite", NOW - 3 * DAY, times=2)
        self.play("a", "Gone", "Faded", NOW - 10 * DAY, times=3)
        self.play("a", "Once", "Seen", NOW - 10 * DAY)           # played once before: not "in rotation"
        self.play("a", "New", "Arrival", NOW - 2 * DAY, times=4)
        self.play("a", "New", "Single", NOW - 1 * DAY)
        self.play("b", "Other", "Station", NOW - 1 * DAY, times=2)  # new on b, but b has no history before the range
        self.week = week

    def test_new_and_dropped_songs_for_a_range(self):
        res = queries.changes(self.conn, {"range": "7d", "station": "a"}, NY, NOW)
        self.assertTrue(res["applicable"])
        new = {s["title"]: s for s in res["new"]["songs"]}
        self.assertEqual(set(new), {"Arrival", "Single"})
        self.assertEqual(res["new"]["songs"][0]["title"], "Arrival")  # most played first
        self.assertEqual((new["Arrival"]["plays"], new["Arrival"]["station_id"]), (4, "a"))
        self.assertEqual(res["new"]["total"], 2)
        dropped = res["dropped"]["songs"]
        self.assertEqual([s["title"] for s in dropped], ["Faded"])  # "Seen" had one play; "Favourite" is still played
        self.assertEqual(dropped[0]["plays"], 3)
        self.assertEqual(res["dropped"]["min_plays"], 2)

    def test_a_song_new_to_the_range_but_old_to_the_station_is_not_new(self):
        res = queries.changes(self.conn, {"range": "7d", "station": "a"}, NY, NOW)
        self.assertNotIn("Favourite", [s["title"] for s in res["new"]["songs"]])

    def test_needs_history_older_than_the_range(self):
        self.assertEqual(queries.changes(self.conn, {"range": "all"}, NY, NOW)["reason"], "range")
        res = queries.changes(self.conn, {"range": "7d", "station": "b"}, NY, NOW)  # station b only began yesterday
        self.assertEqual((res["applicable"], res["reason"]), (False, "history"))
        self.assertFalse(queries.changes(self.conn, {"range": "7d", "station": "c"}, NY, NOW)["applicable"])  # no plays

    def test_the_limit_caps_the_lists_but_not_the_totals(self):
        res = queries.changes(self.conn, {"range": "7d", "station": "a", "limit": "1"}, NY, NOW)
        self.assertEqual((len(res["new"]["songs"]), res["new"]["total"]), (1, 2))

    def test_all_stations_counts_a_song_new_when_no_station_had_played_it(self):
        self.play("b", "Old", "Favourite", NOW - 12 * HOUR)
        res = queries.changes(self.conn, {"range": "7d"}, NY, NOW)
        self.assertNotIn("Favourite", [s["title"] for s in res["new"]["songs"]])
        self.assertIn("Station", [s["title"] for s in res["new"]["songs"]])

    def test_search_narrows_both_lists(self):
        res = queries.changes(self.conn, {"range": "7d", "station": "a", "q": "faded"}, NY, NOW)
        self.assertEqual((res["new"]["total"], res["dropped"]["total"]), (0, 1))

    def test_baseline_reports_how_much_earlier_history_exists(self):
        res = queries.changes(self.conn, {"range": "7d", "station": "a"}, NY, NOW)
        self.assertGreater(res["baseline"], 10 * DAY)
        self.assertLess(res["baseline"], 14 * DAY)


class CompareTests(Base):
    def setUp(self):
        super().setUp()
        # both stations are logged from 30 days ago
        self.play("a", "Seed", "A start", NOW - 30 * DAY)
        self.play("b", "Seed", "B start", NOW - 30 * DAY + HOUR)
        self.play("a", "Adele", "Hello", NOW - 10 * DAY, times=3)
        self.play("b", "Adele", "Hello", NOW - 8 * DAY, times=2)        # a first, 2 days ahead
        self.play("b", "Roxette", "The Look", NOW - 6 * DAY)
        self.play("a", "Roxette", "The Look", NOW - 5 * DAY, times=2)    # b first
        self.play("a", "Only", "On A", NOW - 4 * DAY, times=4)
        self.play("b", "Only", "On B", NOW - 4 * DAY, times=1)

    def test_overlap_and_who_was_first(self):
        res = queries.compare(self.conn, {"a": "a", "b": "b"}, NY, NOW)
        self.assertEqual(res["shared"]["songs"], 2)
        self.assertEqual((res["a"]["songs"], res["b"]["songs"]), (4, 4))
        self.assertEqual(res["a"]["shared_pct"], 50)
        self.assertEqual((res["a"]["plays"], res["b"]["plays"]), (3 + 2 + 4 + 1, 2 + 1 + 1 + 1))
        self.assertEqual(res["a"]["avg_plays"], round(10 / 4, 1))
        rows = {s["title"]: s for s in res["songs"]}
        self.assertEqual(list(rows), ["Hello", "The Look"])  # most played together first
        self.assertEqual((rows["Hello"]["plays_a"], rows["Hello"]["plays_b"], rows["Hello"]["first"]), (3, 2, "a"))
        self.assertEqual((rows["Hello"]["lead"], rows["The Look"]["first"]), (2 * DAY, "b"))
        self.assertEqual((res["first"]["a"], res["first"]["b"], res["first"]["unknown"]), (1, 1, 0))
        self.assertEqual(res["only_a"]["songs"][0]["title"], "On A")
        self.assertEqual((res["only_a"]["total"], res["only_b"]["total"]), (2, 2))

    def test_a_song_already_playing_when_the_later_station_began_is_not_called_first(self):
        # station c is added later; a played this long before c existed
        self.play("a", "Early", "Bird", NOW - 20 * DAY)
        self.play("c", "Seed", "C start", NOW - 3 * DAY)
        self.play("c", "Early", "Bird", NOW - 2 * DAY)
        res = queries.compare(self.conn, {"a": "a", "b": "c"}, NY, NOW)
        self.assertEqual(res["shared"]["songs"], 1)
        self.assertEqual((res["first"]["unknown"], res["first"]["a"], res["first"]["b"]), (1, 0, 0))
        self.assertIsNone(res["songs"][0]["first"])
        self.assertEqual(res["common_start"], NOW - 3 * DAY)

    def test_range_and_search_apply(self):
        res = queries.compare(self.conn, {"a": "a", "b": "b", "range": "7d"}, NY, NOW)
        self.assertEqual(res["shared"]["songs"], 1)  # Hello was last played here 8 and 10 days ago
        self.assertEqual(queries.compare(self.conn, {"a": "a", "b": "b", "q": "adele"}, NY, NOW)["shared"]["songs"], 1)
        # but who was first is judged on all history, not just the range
        self.assertEqual(res["songs"][0]["first"], "b")

    def test_stations_that_share_nothing(self):
        self.play("c", "Nobody", "Else", NOW - DAY)
        res = queries.compare(self.conn, {"a": "a", "b": "c"}, NY, NOW)
        self.assertEqual((res["shared"]["songs"], res["songs"], res["first"]["a"]), (0, [], 0))
        self.assertIsNone(res["common_start"])

    def test_bad_picks(self):
        self.assertIsNone(queries.compare(self.conn, {"a": "a", "b": "a"}, NY, NOW))
        self.assertIsNone(queries.compare(self.conn, {"a": "a", "b": "nope"}, NY, NOW))
        self.assertIsNone(queries.compare(self.conn, {}, NY, NOW))

    def test_limit_caps_the_shared_list(self):
        res = queries.compare(self.conn, {"a": "a", "b": "b", "limit": "1"}, NY, NOW)
        self.assertEqual((len(res["songs"]), res["shared"]["songs"]), (1, 2))


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.play("a", "Adele", "Hello", NOW - 20 * DAY)
        self.play("b", "Adele", "Hello", NOW - 19 * DAY)
        self.httpd = server.make_server(0, self.path, "America/New_York", host="127.0.0.1")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        return json.loads(urlopen(self.base + path, timeout=5).read())

    def test_endpoints(self):
        heat = self.get("/api/heatmap")
        self.assertEqual((len(heat["cells"]), heat["total"], heat["tz"]), (7, 2, "America/New_York"))
        self.assertEqual(self.get("/api/heatmap?station=b")["tz"], "America/Los_Angeles")  # the station's own zone
        self.assertEqual(self.get("/api/changes?range=all")["applicable"], False)
        self.assertEqual(self.get("/api/compare?a=a&b=b")["shared"]["songs"], 1)

    def test_bad_comparison_is_a_400(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/api/compare?a=a&b=a", timeout=5)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
