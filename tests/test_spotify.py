import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import db, server, spotify
from app.providers import Play
from app.spotify import Spotify, SpotifyError

NOW = 1_793_800_000


def tid(n):
    return "%022d" % n


def track(n, name, artists, album="Album"):
    return {"id": tid(n), "name": name, "artists": [{"name": a} for a in artists], "album": {"name": album}}


class FakeSpotify:
    """Stands in for Spotify's servers: `search(query, kind)` returns (status, items)."""

    def __init__(self, search=None):
        self.search = search or (lambda q, kind: (200, []))
        self.calls, self.token_status, self.headers = [], 200, {}

    def __call__(self, url, headers=None, data=None):
        self.calls.append(url)
        if url == spotify.TOKEN_URL:
            return self.token_status, ({"access_token": "tok", "expires_in": 3600} if self.token_status == 200 else {}), {}
        query = parse_qs(urlparse(url).query)
        status, items = self.search(query["q"][0], query["type"][0])
        return status, ({query["type"][0] + "s": {"items": items}} if status == 200 else None), self.headers

    def searches(self):
        return [c for c in self.calls if c != spotify.TOKEN_URL]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class NamesTests(unittest.TestCase):
    def test_fold_ignores_case_accents_punctuation_and_the(self):
        self.assertEqual(spotify.fold("Beyoncé"), "beyonce")
        self.assertEqual(spotify.fold("The Cure"), "cure")
        self.assertEqual(spotify.fold("Don’t Stop"), spotify.fold("Dont Stop"))
        self.assertEqual(spotify.fold("Oops!...I Did It Again"), spotify.fold("Oops! I Did It Again"))
        self.assertEqual(spotify.fold("Three-Days-Grace"), "three days grace")
        self.assertEqual(spotify.fold("Simon & Garfunkel"), "simon and garfunkel")

    def test_title_forms_drop_edit_noise_but_keep_the_title(self):
        self.assertTrue(spotify.title_forms("Waterloo (Radio Edit)") & spotify.title_forms("Waterloo"))
        self.assertTrue(spotify.title_forms("Hello - Remastered 2011") & spotify.title_forms("Hello"))
        self.assertTrue(spotify.title_forms("Dracula [Jennie Remix]") & spotify.title_forms("Dracula"))
        self.assertTrue(spotify.title_forms("(Sittin' On) The Dock of the Bay") & spotify.title_forms("The Dock of the Bay"))
        self.assertFalse(spotify.title_forms("Hello") & spotify.title_forms("Hello Goodbye"))
        self.assertEqual(spotify.clean_title("Waterloo (Radio Edit)"), "Waterloo")
        self.assertEqual(spotify.clean_title("(Radio Edit)"), "(Radio Edit)")  # never cleaned down to nothing

    def test_artist_forms(self):
        forms = spotify.artist_forms("Dua Lipa feat. Elton John")
        self.assertTrue({"dua lipa", "elton john"} <= forms)
        self.assertIn("simon and garfunkel", spotify.artist_forms("Simon & Garfunkel"))
        self.assertIn("p nk", spotify.artist_forms("Pink"))
        self.assertEqual(spotify.lead_artist("Dua Lipa feat. Elton John"), "Dua Lipa")


class PickTests(unittest.TestCase):
    def test_needs_the_same_title_and_a_matching_artist(self):
        items = [track(1, "Hello", ["Lionel Richie"]), track(2, "Hello Goodbye", ["Adele"]), track(3, "Hello", ["Adele"])]
        self.assertEqual(spotify.pick_track(items, "Adele", "Hello")["id"], tid(3))
        self.assertIsNone(spotify.pick_track(items[:2], "Adele", "Hello"))
        self.assertIsNone(spotify.pick_track([track(1, "Hello - Live", ["Adele"])], "Adele", "Hello"))  # not obviously the same song
        self.assertIsNone(spotify.pick_track([], "Adele", "Hello"))
        self.assertIsNone(spotify.pick_track(None, "Adele", "Hello"))

    def test_prefers_spotifys_order_among_equals(self):
        items = [track(1, "Creep", ["Radiohead"]), track(2, "Creep", ["Radiohead"])]
        self.assertEqual(spotify.pick_track(items, "Radiohead", "Creep")["id"], tid(1))

    def test_another_version_loses_to_the_studio_recording(self):
        items = [track(1, "Hello (Live)", ["Adele"]), track(2, "Hello", ["Adele"], album="Karaoke Hits"),
                 track(3, "Hello", ["Adele"], album="25")]
        self.assertEqual(spotify.pick_track(items, "Adele", "Hello")["id"], tid(3))
        # ...but it still beats nothing, and counts as a match when the radio asked for that version
        self.assertEqual(spotify.pick_track(items[:1], "Adele", "Hello")["id"], tid(1))
        live = [track(1, "Hello", ["Adele"]), track(2, "Hello (Live)", ["Adele"])]
        self.assertEqual(spotify.pick_track(live, "Adele", "Hello (Live)")["id"], tid(1))

    def test_featured_and_joined_artist_names(self):
        self.assertEqual(spotify.pick_track([track(1, "Dark Horse", ["Katy Perry", "Juicy J"])], "Katy Perry / Juicy J", "Dark Horse")["id"], tid(1))
        self.assertEqual(spotify.pick_track([track(1, "Dark Horse", ["Juicy J"])], "Katy Perry feat. Juicy J", "Dark Horse")["id"], tid(1))
        self.assertEqual(spotify.pick_track([track(1, "All My Life", ["K-Ci & JoJo"])], "K-Ci & Jo Jo", "All My Life")["id"], tid(1))
        self.assertEqual(spotify.pick_track([track(1, "Chop Suey!", ["System Of A Down"])], "System Of A Down", "Chop Suey")["id"], tid(1))

    def test_ignores_malformed_results(self):
        items = [None, "x", {"id": "short", "name": "Hello", "artists": [{"name": "Adele"}]}, {"name": "Hello"}, track(1, "Hello", ["Adele"])]
        self.assertEqual(spotify.pick_track(items, "Adele", "Hello")["id"], tid(1))

    def test_pick_artist_wants_the_exact_name(self):
        items = [{"id": tid(1), "name": "Dua Lipa Tribute"}, {"id": tid(2), "name": "Dua Lipa"}]
        self.assertEqual(spotify.pick_artist(items, "Dua Lipa")["id"], tid(2))
        self.assertIsNone(spotify.pick_artist(items[:1], "Dua Lipa"))


class ClientTests(unittest.TestCase):
    def client(self, search=None, clock=None):
        self.fake = FakeSpotify(search)
        return Spotify("id", "secret", http=self.fake, clock=clock or Clock())

    def test_find_track_tries_the_strict_query_then_a_plain_one(self):
        queries = []

        def search(q, kind):
            queries.append(q)
            return 200, [track(1, "Hello", ["Adele"])] if "track:" not in q else []
        found = self.client(search).find_track("Adele", "Hello (Radio Edit)")
        self.assertEqual(found, (tid(1), "Adele - Hello"))
        self.assertEqual(queries, ['track:"Hello" artist:"Adele"', "Hello Adele"])

    def test_find_track_gives_up_after_two_searches(self):
        client = self.client()
        self.assertIsNone(client.find_track("Nobody", "Nothing"))
        self.assertEqual(len(self.fake.searches()), 2)

    def test_the_token_is_reused_until_it_nearly_expires(self):
        clock = Clock()
        client = self.client(lambda q, k: (200, []), clock)
        client.search("a", "track"); client.search("b", "track")
        self.assertEqual(self.fake.calls.count(spotify.TOKEN_URL), 1)
        clock.now += 3600
        client.search("c", "track")
        self.assertEqual(self.fake.calls.count(spotify.TOKEN_URL), 2)

    def test_an_expired_token_is_replaced_and_the_search_retried(self):
        answers = iter([401, 200])
        client = self.client(lambda q, k: (next(answers), [track(1, "Hello", ["Adele"])]))
        self.assertEqual(len(client.search("hello", "track")), 1)
        self.assertEqual(self.fake.calls.count(spotify.TOKEN_URL), 2)

    def test_slow_down_pauses_all_requests_for_as_long_as_spotify_says(self):
        clock = Clock()
        client = self.client(lambda q, k: (429, []), clock)
        self.fake.headers = {"Retry-After": "120"}
        with self.assertRaises(SpotifyError):
            client.search("a", "track")
        calls = len(self.fake.calls)
        with self.assertRaises(SpotifyError):
            client.search("b", "track")
        self.assertEqual(len(self.fake.calls), calls)  # not asked again while paused
        clock.now += 121
        self.fake.search = lambda q, k: (200, [])
        self.assertEqual(client.search("c", "track"), [])

    def test_a_missing_or_silly_retry_after_still_pauses(self):
        client = self.client(lambda q, k: (429, []))
        self.fake.headers = {"Retry-After": "soon"}
        with self.assertRaises(SpotifyError):
            client.search("a", "track")
        with self.assertRaises(SpotifyError):
            client.search("b", "track")
        self.assertEqual(len(self.fake.searches()), 1)

    def test_bad_credentials_are_reported_and_not_retried_at_once(self):
        client = self.client()
        self.fake.token_status = 400
        with self.assertRaises(SpotifyError) as ctx:
            client.search("a", "track")
        self.assertIn("SPOTIFY_CLIENT_ID", str(ctx.exception))
        with self.assertRaises(SpotifyError):
            client.search("b", "track")
        self.assertEqual(self.fake.calls.count(spotify.TOKEN_URL), 1)

    def test_server_errors_are_errors(self):
        with self.assertRaises(SpotifyError):
            self.client(lambda q, k: (500, [])).search("a", "track")

    def test_find_artist_matches_the_name_or_its_lead_part(self):
        def search(q, kind):
            return 200, [{"id": tid(7), "name": "Dua Lipa"}] if q == "Dua Lipa" else []
        client = self.client(search)
        self.assertEqual(client.find_artist("Dua Lipa feat. Elton John"), (tid(7), "Dua Lipa"))
        self.assertIsNone(self.client().find_artist("Nobody"))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        db.init(self.path)
        conn = db.connect(self.path)
        db.add_station(conn, "a", "A", "iheart", "1")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", NOW), Play("Roxette", "The Look", NOW + 1000)])
        conn.commit()
        conn.close()
        self.key = db.song_key("Adele", "Hello")
        self.found = [track(1, "Hello", ["Adele"])]

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, items=None):
        self.fake = FakeSpotify(lambda q, k: (200, self.found if items is None else items) if k == "track" else (200, [{"id": tid(9), "name": "Adele"}]))
        return Spotify("id", "secret", http=self.fake)

    def cached(self, table="spotify_tracks"):
        conn = db.connect(self.path)
        try:
            return [tuple(r) for r in conn.execute("SELECT * FROM %s" % table)]
        finally:
            conn.close()

    def test_unknown_songs_get_no_link_and_cause_no_lookup(self):
        client = self.client()
        self.assertIsNone(spotify.song_url(self.path, client, "nobody|nothing", NOW))
        self.assertIsNone(spotify.artist_url(self.path, client, "Nobody", NOW))
        self.assertEqual(self.fake.calls, [])

    def test_without_credentials_the_link_is_a_spotify_search(self):
        self.assertEqual(spotify.song_url(self.path, None, self.key, NOW), "https://open.spotify.com/search/Hello%20Adele/tracks")
        self.assertEqual(spotify.artist_url(self.path, None, "Adele", NOW), "https://open.spotify.com/search/Adele/artists")
        self.assertEqual(self.cached(), [])

    def test_a_match_links_to_the_track_and_is_remembered(self):
        client = self.client()
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW), "https://open.spotify.com/track/" + tid(1))
        searches = len(self.fake.searches())
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW + 5), "https://open.spotify.com/track/" + tid(1))
        self.assertEqual(len(self.fake.searches()), searches)  # the second click didn't ask Spotify
        self.assertEqual(self.cached(), [(self.key, tid(1), "Adele - Hello", NOW)])

    def test_a_miss_falls_back_to_search_and_is_retried_much_later(self):
        client = self.client(items=[])
        search = "https://open.spotify.com/search/Hello%20Adele/tracks"
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW), search)
        searches = len(self.fake.searches())
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW + 86400), search)
        self.assertEqual(len(self.fake.searches()), searches)
        self.found = [track(1, "Hello", ["Adele"])]
        self.client_items = None
        client = self.client()  # later, Spotify has it
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW + spotify.MISS_RETRY + 1), "https://open.spotify.com/track/" + tid(1))

    def test_a_spotify_failure_is_not_remembered(self):
        client = self.client()
        self.fake.search = lambda q, k: (500, [])
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW), "https://open.spotify.com/search/Hello%20Adele/tracks")
        self.assertEqual(self.cached(), [])
        self.fake.search = lambda q, k: (200, self.found)
        self.assertEqual(spotify.song_url(self.path, client, self.key, NOW + 1), "https://open.spotify.com/track/" + tid(1))

    def test_artist_links(self):
        client = self.client()
        self.assertEqual(spotify.artist_url(self.path, client, "Adele", NOW), "https://open.spotify.com/artist/" + tid(9))
        self.assertEqual(spotify.artist_url(self.path, client, "adele", NOW), "https://open.spotify.com/artist/" + tid(9))  # cached under the folded name
        self.assertEqual(len(self.fake.searches()), 1)
        self.assertEqual(self.cached("spotify_artists"), [("adele", tid(9), "Adele", NOW)])

    def test_special_characters_in_a_search_link_are_encoded(self):
        conn = db.connect(self.path)
        db.insert_plays(conn, "a", [Play("AC/DC", "Hells Bells & More?", NOW + 5000)])
        conn.close()
        url = spotify.song_url(self.path, None, db.song_key("AC/DC", "Hells Bells & More?"), NOW)
        self.assertEqual(url, "https://open.spotify.com/search/Hells%20Bells%20%26%20More%3F%20AC%20DC/tracks")


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "t.db"
        db.init(path)
        conn = db.connect(path)
        db.add_station(conn, "a", "A", "iheart", "1")
        db.insert_plays(conn, "a", [Play("Adele", "Hello", NOW)])
        conn.commit()
        conn.close()
        self.key = db.song_key("Adele", "Hello")
        self.fake = FakeSpotify(lambda q, k: (200, [track(1, "Hello", ["Adele"])]))
        self.httpd = server.make_server(0, path, "America/New_York", host="127.0.0.1", spotify_client=Spotify("id", "secret", http=self.fake))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.getheader("Location"), resp.getheader("Cache-Control")
        finally:
            conn.close()

    def test_a_song_redirects_to_its_spotify_page(self):
        status, location, cache = self.get("/spotify/song?key=" + self.key.replace("|", "%7C"))
        self.assertEqual((status, location, cache), (302, "https://open.spotify.com/track/" + tid(1), "no-store"))

    def test_an_artist_redirects(self):
        status, location, _ = self.get("/spotify/artist?name=Adele")
        self.assertEqual((status, location.startswith("https://open.spotify.com/")), (302, True))

    def test_unknown_songs_and_artists_are_404_and_never_looked_up(self):
        self.assertEqual(self.get("/spotify/song?key=nope%7Cnope")[0], 404)
        self.assertEqual(self.get("/spotify/song")[0], 404)
        self.assertEqual(self.get("/spotify/artist?name=Nobody")[0], 404)
        self.assertEqual(self.get("/spotify/artist")[0], 404)
        self.assertEqual(self.fake.calls, [])

    def test_a_server_without_credentials_still_redirects_to_search(self):
        self.httpd.spotify = None
        status, location, _ = self.get("/spotify/song?key=" + self.key.replace("|", "%7C"))
        self.assertEqual((status, location), (302, "https://open.spotify.com/search/Hello%20Adele/tracks"))


if __name__ == "__main__":
    unittest.main()
