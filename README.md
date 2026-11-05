# Radio Poller

Polls radio stations' public now-playing feeds, stores every play in SQLite, and serves a
small dashboard (top songs, plays over time, history) with import and export of the data.

Python standard library only (plus `tzdata`). In the container it idles around 20-30 MB RAM
and near-zero CPU; it makes one small request per station per minute.

## Run it

On the machine that will host it (needs Docker with the Compose plugin):

1. **Make the two folders** for the data and the backups, `./data` and `./backups` next to
   `docker-compose.yml`, and tell compose which user should own what it writes there:

   ```bash
   mkdir -p data backups
   printf 'APP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" > .env
   ```

   Create the folders yourself: if Docker has to create them, it makes them owned by root and the app can't
   write to them. The `.env` file makes the app run as you, so it can.
2. **Optional: put them elsewhere.** To use another location (say a second disk or a NAS mount for the backups),
   set `DATA_PATH` and/or `BACKUP_PATH` in `.env` to absolute paths; see `.env.example`. On Docker Desktop (Mac
   or Windows) the folders must be somewhere Docker is allowed to share, which normally includes your home folder.
3. **Start it:**

   ```bash
   docker compose up -d --build
   ```

Open `http://<that-machine>:5555`. To use a different port, put `PORT=<number>` in the same `.env` file.
Logs: `docker compose logs -f`.

- **Data** lives in `./data` (`radio.db` inside it) and the automatic backups in `./backups`, so you can see
  them, back them up with ordinary tools, and they survive rebuilds, restarts and `docker compose down -v`. Keep
  the project folder where you run it: if you replace it with a fresh copy, copy `data` and `backups` across
  first. The database has to be on a local disk, not a network share: SQLite and network file systems don't mix.
  See [Backups](#backups).
- **Timezone** for grouping plays into days: `TZ` in `docker-compose.yml`.
- **Poll rate**: `POLL_SECONDS` (default 60, minimum 15). Each poll also asks the provider
  for its last 10 plays, so a restart or short outage is backfilled and nothing is missed
  as long as it's under ~30 minutes.
- **No login.** Out of the box, anyone who can reach port 5555 can view the data, change stations
  and import data. That's fine on a trusted home network. If strangers can reach it, read
  [Running on a public server](#running-on-a-public-server) first.

## Stations

Manage them on the **Stations** page (`/stations`, linked from the dashboard's top bar):

- **Switch a station on or off.** Off pauses polling and keeps its history, which stays on the
  dashboard. Changes apply within a few seconds, no restart.
- **Add a station** by pasting its web page: an iHeart page (`washfm.iheart.com`, or
  `iheart.com/live/<name>-<number>/`) or an Audacy page (`audacy.com/kroq`). The app finds the
  station's feed and tests it before adding. If detection can't work, pick the provider and type the
  station ID by hand: an iHeart ID is a number (`2501`), an Audacy code looks like `101-988`.
- **Rename and reorder** stations. A station keeps its chart colour when you reorder.
- **Set a station's timezone.** Pick the zone its local time is in (adding by URL fills it in when the page or
  state makes it clear). See [Times and timezones](#times-and-timezones).
- **Delete a station** and all of its plays, after a confirmation. Export it first if you want a copy.
- **Add the same station twice** under different names, for example a station's Christmas-music
  version, to keep its plays separate. The copy starts switched off. Switch one off and the other on to
  change over. A station you switch on only records songs from that moment, so it doesn't copy in the
  feed's recent history, which the other copy has already logged. If both are on, each song is logged
  twice, and the page marks both rows "Both on".

Stations are stored in the database, so exports carry them and a migration brings your
station list along. `config/stations.json` is only a **starting list for the very first run**: it's
read once, when the database is new (or was made by an older version), and ignored after that.
Editing it later does nothing; use the Stations page.

Starting stations: 97.1 WASH-FM (iHeart `2501`), DC101 (iHeart `2525`), 101.3 2WD (Audacy
`101-988`) and 106.7 KROQ (Audacy `101-299`).

Stations can only be added from **iHeart and Audacy** pages. For another kind of station, add a
function in `app/providers.py` that returns a list of `Play(artist, title, played_at)` and register it
in `PROVIDERS`.

Like importing, changing stations writes to the server, so it also requires `ADMIN_TOKEN` when one is set
(see below), and it's disabled entirely in `READ_ONLY` mode.

**96X (WROX, 96.1)** is not included. Its website puts the playlist behind a bot check and
its metadata proxy refuses outside requests, and there's no public feed I could use.
Its only audio stream is ad-stitched HLS (`waterloo.streamguys1.com/wrox`) with no
song titles in it, and polling it would also register as a listener session each time.
The only remaining now-playing source is StreamGuys' metadata service, which needs the
key embedded in the station's own player; that isn't offered for outside use, so it's not
used here. If the station (Sinclair) or another source gives you a real feed, it can be
added as a provider in `app/providers.py`.

## Times and timezones

Every play is stored as an absolute UTC timestamp, so daylight saving changes can never corrupt the history.
What changes is how times are shown and how days are grouped, and that follows each **station's own timezone**
(set on the Stations page; a station without one uses the server's `TZ`):

- A play's time is shown in its station's local time: Now playing, Recent plays, a song's history and the
  "Last played" column. When your stations aren't all in one zone, the zone is added (`PST`, `EST`) so no
  time is ambiguous. If they are all in one zone, times are shown plain.
- Picking one station groups its days and hours by *that station's* local calendar. With **All stations**
  they're grouped in the server's `TZ`. The line under the chart says which zone it's using.
- Exports carry each station's timezone along with its feed settings.

Audacy's pages state the station's timezone, so adding by URL sets it. iHeart's feed only gives the state, so
the zone is filled in for states that sit in a single zone and left for you to choose in split states (such as
Texas or Florida). If you choose nothing, the server's `TZ` is used.

## How plays are counted

Each play is recorded once per station with the time the station reported. The same
song on the same station within 90 seconds is treated as one play, so the poller seeing the
same entry twice (or with a slightly shifted timestamp) never double-counts. Songs are
matched ignoring case and extra spaces, so "Madonna - Like a Virgin" from different
stations counts together. Some feeds omit talk and ads; DC101 in particular logs few songs
during its morning show.

## Moving to another server (import / export)

Open **Import / Export** in the dashboard's top bar (`/transfer`).

1. On the old server, click **Download export**. You get one `.jsonl` file with every play and
   station (about 250 bytes per play; a year of three stations is roughly 100 MB). To export just
   one station, choose it in the dropdown first; the file then holds only that station.
2. Start the new server, open its `/transfer` page, choose the file and click **Import**.

To move **just your station list** (names, feeds, order, on/off) without any history, untick
**Include play history** before downloading. The file is tiny, and importing it on the new server
sets up the same stations, which start polling right away. You can combine that with the station
dropdown to export a single station's settings.

Import merges: plays already on the server are skipped, so importing the same file twice is
harmless, and you can import into a server that has already been collecting. Stations the new
server doesn't have yet are added with their feed settings (and keep polling if they were on);
stations it already has keep their own settings. A year of data imports in about 10 seconds. If the file looks cut off (it ends with an
end marker, and the page tells you if that's missing), download it again and re-import.

Import, and changing stations, write to the database, so for a server others can reach, set a token and the
pages will ask for it:

```yaml
    environment:
      ADMIN_TOKEN: "pick-something-long"   # optional; required to export, import or change stations when set
      MAX_IMPORT_MB: "1024"                # largest file accepted
```

Straight from the command line, with `-H "X-Admin-Token: ..."` added if you set a token:

```bash
curl -o plays.jsonl http://old-server:5555/api/export             # add ?station=washfm for one station, ?plays=0 for stations only
curl -H "Content-Type: application/x-ndjson" --data-binary @plays.jsonl http://new-server:5555/api/import
```

## Dashboard

A **Now playing** card shows the latest song on each station (with album art when a single
station is selected). Below are the top 10 songs (**Show more** for more) and top artists, then totals and a plays-over-time chart.
Filter by station, range (24h / 7 / 30 / 90 days / all) and search text. Click a song for its
history, a per-day chart and a per-station breakdown; click an artist to filter to them.
For a copy of the data, use Import / Export.

**Station songs** lists every distinct song a station played in a range, with its play count and when it was last
heard, in a scrolling card with its own station, sort (most played / most recent / A–Z) and range. Pick a preset
(24h, 1 week, 1 month, 3 months, 6 months, 1 year, all time) or type a From / To date; both dates are whole days,
inclusive, in the station's own timezone. It starts with the first 200 songs and **Show more** adds 200 at a time.

**Open in Spotify** buttons (the green Spotify icon) sit on Now playing, Top songs, Top artists, Station songs and
the song details. Out of the box they open a Spotify *search* for that song or artist in a new tab, so you tap the
result once in Spotify. Give the app a Spotify developer app (see [Spotify](#spotify)) and they go straight to the
song or artist instead.

Further down, three cards follow the same filters:

- **When it plays**: a weekday-by-hour heatmap of plays, in the station's own timezone (the server's `TZ` when
  all stations are showing). Search for an artist to see when *they* get played.
- **New and dropped songs**: *New* are songs first heard (on the chosen station, or any) inside the selected range;
  *dropped* are songs played at least twice in the equally long period before it and not once since. It compares the
  range with the period before it, so it needs history older than the range: it says so when it doesn't have
  enough, and isn't available for "All time". Because the app only knows what it has logged, "new" really means
  "new since this app started watching", so give it a few weeks before trusting it.
- **Compare stations**: pick any two stations to see their overlap, plays per song (a rough measure of how tight
  the rotation is), the songs both play, and which station played each one first. "First" is when the app first
  logged the song on each station, so it's only claimed when both stations were already being logged; a song that was
  already playing when the later station was added shows as "Already in rotation". Songs only one of them plays
  are listed too.

The pages bundle their own copies of daisyUI 5.7.50 and Tailwind 4.3.4 (in `app/static/vendor/`), so
nothing is loaded from a third party and the dashboard works without internet access, other than album art.

## API

`/api/stations`, `/api/summary`, `/api/top-songs`, `/api/top-artists`, `/api/recent`,
`/api/station-songs` (every distinct song in the range; also `sort=plays|recent|az`), `/api/timeseries`, `/api/heatmap`, `/api/changes`, `/api/compare?a=&b=`, `/api/now-playing`, `/api/transfer-info`,
`/api/song?key=`, `/api/export`, `POST /api/import`, `/health` (also reports live thread counts:
`threads`, `requests_active` / `requests_max`, and on Linux `container_pids` / `container_pids_max`). Most accept `station`,
`range` (`24h`, `7d`, `30d`, `90d`, `180d`, `365d`, `all`), `q`, and `limit`. Instead of a preset they also take
`from_date` / `to_date` (`YYYY-MM-DD`, inclusive, in the station's timezone).

## Spotify

The Spotify buttons work with no setup (they open a Spotify search), but they can go straight to the exact song or
artist. For that the app searches Spotify using **your own Spotify developer app**; it never logs in as you and
sends Spotify nothing but the artist and title it is looking up.

1. At [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) (a Premium account is needed to own
   an app) choose **Create app**, tick **Web API**, and give it any name and a redirect URI such as
   `http://127.0.0.1:5555/callback` (the form requires one; matching doesn't use it).
2. Copy the app's **Client ID** and **Client secret** into `.env`, next to `docker-compose.yml`
   (see `.env.example`), then `docker compose up -d` to apply them:

   ```
   SPOTIFY_CLIENT_ID=...
   SPOTIFY_CLIENT_SECRET=...
   ```

How it works: the buttons link to `/spotify/song?key=...` and `/spotify/artist?name=...` on this server, which look
the song up on Spotify the *first time* someone clicks it, remember the answer in the database (the
`spotify_tracks` and `spotify_artists` tables, which hold no secrets), and redirect. After that a click is instant
and makes no request to Spotify. Only songs and artists already in the history are ever looked up.

- **Matching** needs the same title and one of the same artists, ignoring case, accents and punctuation, and
  "(Radio Edit)", "[Remix]" or "- Remastered" style tags. Live, karaoke, tribute and similar recordings lose to the
  studio one. On a test of about 240 real songs from this app it found 98%.
- **When there's no match** (the radio's title isn't Spotify's, say), or Spotify is unreachable, rate-limiting
  the app or refusing the credentials, the button opens the Spotify search instead, so it always lands somewhere
  useful. Songs with no match are searched for again after 14 days; failures aren't remembered at all.
- **Rate limits.** An app in development mode has a request quota that Spotify doesn't publish. It is plenty for
  clicking buttons, but looking up hundreds of songs in a burst can use it up, after which Spotify refuses the app
  for up to about a day (the buttons fall back to search in the meantime). The app honours the wait Spotify asks for.
- If the credentials are wrong, the log says so (`docker compose logs`).

## Backups

**The app backs itself up.** By default it saves a snapshot of the database every 24 hours and keeps the
newest 7, in a `backups/` folder beside the database (in Docker, `./backups`, or wherever `BACKUP_PATH` points). Each is a complete SQLite
file made with SQLite's online backup, so it's consistent even while the poller is writing, then gzipped
(`radio-YYYYMMDD-HHMMSS.db.gz`), which typically makes it a third of the size or less. (Don't copy
`radio.db` by hand while the app is running: the copy can be corrupt.) The first one is made about 20 seconds
after start, and the Import / Export page shows when the last one ran. Each backup is checked before it's kept,
and the compressed file is read back to make sure it unpacks to the same database.

| Setting | Default | |
|---|---|---|
| `BACKUP_EVERY_HOURS` | `24` | `0` turns automatic backups off |
| `BACKUP_KEEP` | `7` | how many of the newest to keep |
| `BACKUP_COMPRESS` | `1` | `0` saves plain `.db` files instead of `.db.gz` |
| `BACKUP_DIR` | `<data folder>/backups` | where they go |

**Copy them off the machine.** With `./backups` beside `./data`, snapshots protect against mistakes and
corruption but not against losing that disk. Point `BACKUP_PATH` at a different disk or a NAS mount (a network
share is fine for backups, just not for the database), or copy the folder elsewhere. Docker Compose has no
scheduler of its own (there's no cron in a compose file), so for that pick one of these:

- **A cron job on the host** (the simplest). `crontab -e`, then either of these (`%` must be written `\%` in a
  crontab; add `-H "X-Admin-Token: ..."` to the curl if you set a token):

  ```
  # a full export (every play and station), restorable through the Import page
  0 4 * * * curl -sf -o /path/to/radio-poller/backups/radio-$(date +\%F).jsonl http://localhost:5555/api/export

  # or sync the backups folder to another machine
  0 5 * * * rsync -a /path/to/radio-poller/backups/ otherhost:/backups/radio-poller/
  ```
- **A small extra service in `docker-compose.yml`** that does the same on a timer (not tested here, since
  Docker isn't installed on the machine this was built on). It saves a dated export into your backups folder
  once a day and deletes exports older than 30 days (the app's own snapshots in that folder are left alone):

  ```yaml
    backup-copy:
      image: alpine:3.20
      restart: unless-stopped
      depends_on:
        - radio-poller
      environment:
        ADMIN_TOKEN: "${ADMIN_TOKEN:-}"
      volumes:
        - "${BACKUP_PATH:-./backups}:/backups"
      command:
        - sh
        - -c
        - |
          while true; do
            f=/backups/radio-$$(date +%F).jsonl
            if wget -q -O "$$f.tmp" --header "X-Admin-Token: $$ADMIN_TOKEN" http://radio-poller:5555/api/export; then
              mv "$$f.tmp" "$$f"
            else
              rm -f "$$f.tmp"
            fi
            find /backups -name 'radio-*.jsonl' -mtime +30 -delete
            sleep 86400
          done
  ```

  (If you changed `PORT`, change the 5555 here too.)
- Any file-sync or backup tool pointed at the backups folder.

**Restoring**
- *From an export file* (`.jsonl`): start the app, open Import / Export and import it. It merges, so it works into
  an empty server or one that has some data. This is the easiest way and works the same everywhere.
- *From a snapshot (`radio-….db.gz`):* stop the app, delete `radio.db-wal` and `radio.db-shm` beside `radio.db`,
  unpack the snapshot over `radio.db`, and start the app again. In Docker, run this from the project folder:

  ```bash
  docker compose stop
  rm -f data/radio.db-wal data/radio.db-shm
  gunzip -c backups/radio-YYYYMMDD-HHMMSS.db.gz > data/radio.db
  docker compose start
  ```

  (A snapshot ending in plain `.db`, like those made before compression existed or with `BACKUP_COMPRESS=0`,
  is copied instead: `cp backups/radio-YYYYMMDD-HHMMSS.db data/radio.db`.)

  The restored file has to be writable by the user in `APP_UID` (if you copied it with `sudo`, `chown` it to
  that user). The same steps worked in my test without Docker; the compose version itself is untested.

## Running on a public server

The app is built for a network you trust. If strangers can reach it, here is what to set up. Start with
the first three.

1. **HTTPS, through a reverse proxy.** The app only speaks plain HTTP, so the admin token would travel
   in the clear. Put Caddy, nginx or Traefik in front. With Caddy, a complete config is:

   ```
   radio.example.com {
       reverse_proxy 127.0.0.1:5555
   }
   ```

   Caddy gets the certificate itself. Then keep the app off the open network: put `BIND_ADDRESS=127.0.0.1`
   in a `.env` file beside `docker-compose.yml`, and firewall everything except ports 80 and 443.
2. **Choose how much to open up.**
   - **Private**: keep the whole site behind a login at the proxy (Caddy `basic_auth`, Cloudflare Access, or
     a VPN such as Tailscale). Safest.
   - **Public dashboard, you administer it**: set `ADMIN_TOKEN` to 16 or more random characters
     (for example `openssl rand -hex 24`). Visitors can browse the dashboard, but exporting, importing and
     changing stations need the token. Five wrong tokens lock a client out for 10 minutes.
   - **Public dashboard, nobody administers it from the web**: set `READ_ONLY=1`. Every change endpoint is
     refused, with or without the token, and the Stations and Import pages become view-only. You can still
     change things by running without it for a moment, or by editing the database directly.
3. **Tell the app about your proxy.** Behind one reverse proxy, set `TRUST_FORWARDED_FOR=1` so the lockout
   tracks each visitor instead of treating everyone as the proxy. Only set it when a proxy really is in front
   (otherwise visitors could fake their address), and only for a single proxy.

What's built in: a limit on simultaneous requests (`MAX_CONNECTIONS`, extra ones get a 503), idle
connections dropped after `REQUEST_TIMEOUT` seconds, search terms capped, security headers on every response
(including a content security policy that only allows the app's own scripts), no third-party code on the
pages, and a container with a read-only filesystem, no extra privileges and a process limit. For a busy site
add rate limiting at the proxy too.

Things to know:
- The dashboard's data is public to anyone who can load the page, unless the proxy asks for a login.
- The song data comes from iHeart's and Audacy's unofficial endpoints, and the album art loads from their
  servers. That suits personal use. Check their terms before republishing it publicly.
- Back up the database: point `BACKUP_PATH` at a different disk from the data, and copy it off the machine too.

## Development

```bash
python3 -m app.main                 # http://localhost:5555, data in ./data (PORT=... to change it)
python3 -m unittest discover -s tests
```

## License

Released under the [MIT License](LICENSE). The bundled daisyUI and Tailwind CSS files, and the `tzdata`
package the Docker build installs, keep their own licenses: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
