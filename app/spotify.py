"""Finds the Spotify track or artist behind a song the radio played, so the dashboard can link straight to it.

This only *searches* Spotify, using the app's own client credentials (no Spotify login, nothing about the
listener). Matches are cached in the database: a song is looked up the first time someone clicks its Spotify
button, and never again once it's found. If Spotify isn't set up, is unreachable, or finds nothing trustworthy,
the button falls back to a Spotify search page for the song, so it always lands somewhere useful.
"""
import base64
import json
import logging
import re
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import db

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
TIMEOUT = 8
SEARCH_LIMIT = 10          # the most Spotify will return per search for an app in development mode
MISS_RETRY = 14 * 86400    # a song with no match is searched for again after this long
BLOCK_ON_ERROR = 60        # seconds to leave Spotify alone after it refuses us (bad credentials, an outage)
MAX_BLOCK = 2 * 86400     # ... and after it asks us to slow down: as long as it says (it has said about a day)
ID_RE = re.compile(r"[A-Za-z0-9]{22}")

# Words that make a search result a different recording of the song (unless the radio's own title has them too).
VERSION_WORDS = {"live", "karaoke", "instrumental", "tribute", "remix", "acoustic", "cover", "demo", "lullaby",
                 "slowed", "sped", "reverb", "nightcore", "8d", "midi"}
# A trailing " - Radio Edit" / " - 2011 Remaster" on a title is the same song.
DASH_SUFFIX = re.compile(r"\s+[-–]\s+[^-–]*\b(edit|remaster(ed)?|version|mix|mono|stereo|single|album|explicit|from|feat\.?)\b.*$", re.I)
TRAILING_BRACKETS = re.compile(r"(\s*[(\[][^()\[\]]*[)\]])+\s*$")
ANY_BRACKETS = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*")
FEATURING = re.compile(r"\s*(?:\bfeat\.?|\bft\.?|\bfeaturing)\s.*$", re.I)
ARTIST_SPLIT = re.compile(r"\s*(?:,|;|/|\bfeat\.?\s|\bft\.?\s|\bfeaturing\s|\bwith\s|\bx\s|&|\band\s|\+)\s*", re.I)


# Radio feeds write some artists without the punctuation Spotify's name has. Keys are folded, as fold() writes them.
ARTIST_SPELLINGS = {"pink": "P!nk"}


class SpotifyError(Exception):
    """Spotify couldn't be asked (or refused to answer). Nothing is cached when this happens."""


# ---- comparing names ---------------------------------------------------------------------------------

def fold(text):
    """A name reduced to what matters for comparing: no accents, case, punctuation or leading "the"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("&", " and ")
    text = re.sub(r"['\u2018\u2019`]", "", text)    # "Don't" and "Dont" are the same word
    text = re.sub(r"[^\w\s]", " ", text)             # "Oops!...I" and "Three-Days" are separate words
    text = re.sub(r"\s+", " ", text).strip()
    return text[4:] if text.startswith("the ") else text


def title_forms(title):
    """The ways a title may legitimately be written: as given, without a trailing "(Radio Edit)" or
    " - Remastered", and without any bracketed part at all."""
    forms = {fold(title)}
    stripped = DASH_SUFFIX.sub("", TRAILING_BRACKETS.sub("", title or ""))
    forms.add(fold(stripped))
    forms.add(fold(ANY_BRACKETS.sub(" ", stripped)))
    forms.discard("")
    return forms | {f.replace(" ", "") for f in forms}  # "3 A.m." and "3AM", "Jo Jo" and "JoJo"


def clean_title(title):
    """The title with trailing "(Radio Edit)" style noise removed, for use in a search."""
    cleaned = DASH_SUFFIX.sub("", TRAILING_BRACKETS.sub("", title or "")).strip()
    return cleaned or (title or "").strip()


def artist_forms(artist):
    """Names an artist string may refer to: the whole thing ("Simon & Garfunkel") and each part of it
    ("Dua Lipa feat. Elton John" -> Dua Lipa, Elton John)."""
    forms = {fold(artist)}
    forms.update(fold(part) for part in ARTIST_SPLIT.split(artist or ""))
    forms.discard("")
    forms.update(fold(ARTIST_SPELLINGS[f]) for f in list(forms) if f in ARTIST_SPELLINGS)
    return forms | {f.replace(" ", "") for f in forms}


def lead_artist(artist):
    """The artist string without a "feat. ..." tail, for use in a search."""
    return FEATURING.sub("", artist or "").strip() or (artist or "").strip()


def pick_track(items, artist, title):
    """The best of Spotify's track results for a radio play, or None. A result must have the same title and
    one of the same artists; among those, the one Spotify ranked highest wins, but recordings that are
    evidently another version (live, karaoke, a remix...) lose to ones that aren't."""
    wanted_titles, wanted_artists = title_forms(title), artist_forms(artist)
    asked_for = set(fold(title).split()) | set(fold(artist).split())
    best, best_rank = None, None
    for index, item in enumerate(items or []):
        if not isinstance(item, dict) or not ID_RE.fullmatch(str(item.get("id", ""))):
            continue
        if not title_forms(item.get("name") or "") & wanted_titles:
            continue
        names = {fold(a.get("name") or "") for a in item.get("artists") or [] if isinstance(a, dict)}
        if not (names | {n.replace(" ", "") for n in names}) & wanted_artists:
            continue
        album = fold((item.get("album") or {}).get("name") or "")
        words = (set(fold(item.get("name") or "").split()) | set(album.split())) & VERSION_WORDS
        rank = (1 if words - asked_for else 0, index)
        if best_rank is None or rank < best_rank:
            best, best_rank = item, rank
    return best


def pick_artist(items, artist):
    """The first of Spotify's artist results whose name is the radio's artist name."""
    wanted = artist_forms(artist)
    for item in items or []:
        name = fold(item.get("name") or "") if isinstance(item, dict) else ""
        if name and ID_RE.fullmatch(str(item.get("id", ""))) and (name in wanted or name.replace(" ", "") in wanted):
            return item
    return None


# ---- the Spotify web API -----------------------------------------------------------------------------

def default_http(url, headers=None, data=None):
    """(status, parsed JSON or None, response headers). Network problems raise SpotifyError."""
    req = Request(url, data=data, headers=headers or {})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            status, headers_out, body = resp.status, resp.headers, resp.read(1_000_000)
    except HTTPError as err:
        status, headers_out, body = err.code, err.headers, err.read(100_000)
    except (URLError, OSError, ValueError) as err:
        raise SpotifyError("could not reach Spotify: %s" % err)
    try:
        return status, json.loads(body), headers_out
    except ValueError:
        return status, None, headers_out


class Spotify:
    """A minimal Spotify client: an app token (client credentials) and search. Thread-safe."""

    def __init__(self, client_id, client_secret, http=default_http, clock=time.time):
        self._auth = base64.b64encode(("%s:%s" % (client_id, client_secret)).encode()).decode()
        self._http, self._clock = http, clock
        self._lock = threading.Lock()
        self._token, self._token_until, self._blocked_until = None, 0, 0

    def _block(self, seconds):
        self._blocked_until = self._clock() + min(MAX_BLOCK, max(1, seconds))

    def _get_token(self, force=False):
        with self._lock:
            now = self._clock()
            if now < self._blocked_until:
                raise SpotifyError("backing off")
            if self._token and not force and now < self._token_until - 60:
                return self._token
            try:
                status, body, _ = self._http(TOKEN_URL, {"Authorization": "Basic " + self._auth,
                                                         "Content-Type": "application/x-www-form-urlencoded"},
                                             b"grant_type=client_credentials")
            except SpotifyError:
                self._block(BLOCK_ON_ERROR)
                raise
            if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
                self._block(BLOCK_ON_ERROR)
                raise SpotifyError("Spotify wouldn't issue a token (HTTP %s): check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET" % status)
            self._token = body["access_token"]
            self._token_until = now + int(body.get("expires_in") or 3600)
            return self._token

    def search(self, query, kind):
        """The `kind` ("track" or "artist") results for a query: a list of Spotify's own result objects."""
        for attempt in (0, 1):
            token = self._get_token(force=attempt == 1)
            url = SEARCH_URL + "?" + urlencode({"q": query, "type": kind, "limit": SEARCH_LIMIT})
            try:
                status, body, headers = self._http(url, {"Authorization": "Bearer " + token})
            except SpotifyError:
                self._block(BLOCK_ON_ERROR)
                raise
            if status == 401 and attempt == 0:  # the token expired early: get a fresh one and retry once
                continue
            if status == 429:
                try:
                    wait = int(headers.get("Retry-After", 30))
                except (TypeError, ValueError, AttributeError):
                    wait = 30
                with self._lock:
                    self._block(wait)
                raise SpotifyError("Spotify asked us to slow down (%ds)" % wait)
            if status != 200 or not isinstance(body, dict):
                raise SpotifyError("Spotify search failed (HTTP %s)" % status)
            items = (body.get(kind + "s") or {}).get("items") or []
            return [i for i in items if i]
        raise SpotifyError("Spotify kept refusing our token")

    def find_track(self, artist, title):
        """(track id, "Artist - Title" as Spotify names it) or None."""
        who, what = lead_artist(artist), clean_title(title).replace('"', " ")
        who = ARTIST_SPELLINGS.get(fold(who), who).replace('"', " ")
        for query in ('track:"%s" artist:"%s"' % (what, who), "%s %s" % (what, who)):
            hit = pick_track(self.search(query, "track"), artist, title)
            if hit:
                names = ", ".join(a.get("name", "") for a in hit.get("artists") or [])
                return hit["id"], "%s - %s" % (names, hit.get("name", ""))
        return None

    def find_artist(self, artist):
        """(artist id, name as Spotify has it) or None."""
        names = [lead_artist(artist)] + [p for p in ARTIST_SPLIT.split(lead_artist(artist)) if p.strip()][:1]
        seen = set()
        for name in names:
            if not name or name in seen:
                continue
            seen.add(name)
            hit = pick_artist(self.search(name.replace('"', " "), "artist"), artist)
            if hit:
                return hit["id"], hit.get("name", "")
        return None


# ---- links, with the cache ---------------------------------------------------------------------------

def search_url(kind, text):
    """Spotify's own search page: where a button lands when there's no exact match to send it to."""
    return "https://open.spotify.com/search/%s/%s" % (quote(re.sub(r"[/\\]", " ", text), safe=""), kind)


def _fresh(row, now):
    return row is not None and (row["found_id"] is not None or row["matched_at"] > now - MISS_RETRY)


def song_url(db_path, client, key, now=None):
    """Where a song's Spotify button should go, or None if the app has never heard of that song."""
    now = int(time.time()) if now is None else now
    conn = db.connect(db_path)
    try:
        play = conn.execute("SELECT artist, title FROM plays WHERE song_key = ? LIMIT 1", (key,)).fetchone()
        if play is None:
            return None
        fallback = search_url("tracks", "%s %s" % (play["title"], play["artist"]))
        if client is None:
            return fallback
        row = conn.execute("SELECT track_id AS found_id, matched_at FROM spotify_tracks WHERE song_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is not None and row["found_id"]:
        return "https://open.spotify.com/track/" + row["found_id"]
    if _fresh(row, now):
        return fallback
    try:
        found = client.find_track(play["artist"], play["title"])
    except SpotifyError as err:
        log.warning("spotify: %s", err)
        return fallback
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO spotify_tracks (song_key, track_id, name, matched_at) VALUES (?, ?, ?, ?)",
                     (key, found[0] if found else None, found[1] if found else None, now))
        conn.commit()
    finally:
        conn.close()
    if not found:
        log.info("spotify: no match for %s - %s", play["artist"], play["title"])
        return fallback
    return "https://open.spotify.com/track/" + found[0]


def artist_url(db_path, client, name, now=None):
    """Where an artist's Spotify button should go, or None if no song in the history has that artist."""
    now = int(time.time()) if now is None else now
    key = db.norm(name).casefold()
    if not key:
        return None
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT artist_id AS found_id, matched_at FROM spotify_artists WHERE artist_key = ?", (key,)).fetchone()
        if row is None and not conn.execute("SELECT 1 FROM plays WHERE artist = ? LIMIT 1", (db.norm(name),)).fetchone():
            return None
    finally:
        conn.close()
    fallback = search_url("artists", db.norm(name))
    if client is None:
        return fallback
    if row is not None and row["found_id"]:
        return "https://open.spotify.com/artist/" + row["found_id"]
    if _fresh(row, now):
        return fallback
    try:
        found = client.find_artist(db.norm(name))
    except SpotifyError as err:
        log.warning("spotify: %s", err)
        return fallback
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO spotify_artists (artist_key, artist_id, name, matched_at) VALUES (?, ?, ?, ?)",
                     (key, found[0] if found else None, found[1] if found else None, now))
        conn.commit()
    finally:
        conn.close()
    return ("https://open.spotify.com/artist/" + found[0]) if found else fallback
