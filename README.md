# Radio Poller

Polls radio stations' public now-playing feeds, stores every play in SQLite, and serves a
small dashboard (top songs, plays over time, history) with import and export of the data.

Python standard library only (plus `tzdata`). In the container it idles around 20-30 MB RAM
and near-zero CPU; it makes one small request per station per minute.

## Run it

On the machine that will host it (needs Docker with the Compose plugin):

```bash
docker compose up -d --build
```

Open `http://<that-machine>:5555`. To use a different port, put `PORT=<number>` in a `.env` file beside
`docker-compose.yml`. Logs: `docker compose logs -f`.

- **Data** lives in the `radio-data` Docker volume (`/data/radio.db`), so it survives
  rebuilds and restarts. Back it up with a copy of that file, e.g.
  `docker compose cp radio-poller:/data/radio.db ./radio-backup.db`.
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

The pages bundle their own copies of daisyUI 5.7.50 and Tailwind 4.3.4 (in `app/static/vendor/`), so
nothing is loaded from a third party and the dashboard works without internet access, other than album art.

## API

`/api/stations`, `/api/summary`, `/api/top-songs`, `/api/top-artists`, `/api/recent`,
`/api/timeseries`, `/api/now-playing`, `/api/transfer-info`, `/api/song?key=`, `/api/export`,
`POST /api/import`, `/health`. Most accept `station`,
`range` (`24h`, `7d`, `30d`, `90d`, `365d`, `all`), `q`, and `limit`.

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
- Back up the database. It lives in the `radio-data` Docker volume, which `docker compose down -v` deletes.

## Development

```bash
python3 -m app.main                 # http://localhost:5555, data in ./data (PORT=... to change it)
python3 -m unittest discover -s tests
```

## License

Released under the [MIT License](LICENSE). The bundled daisyUI and Tailwind CSS files, and the `tzdata`
package the Docker build installs, keep their own licenses: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
