(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nf = new Intl.NumberFormat();
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const TOP_PAGE = 10;     // top songs shown at first, and added by each "Show more"
  const RECENT_PAGE = 25;
  const SS_PAGE = 200;     // station songs fetched at first, and added by each "Show more"
const CHANGES_PAGE = 10;
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAYS_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

  const state = { station: '', range: '7d', q: '', songLimit: TOP_PAGE, recentLimit: RECENT_PAGE, changeLimit: CHANGES_PAGE,
                  compareLimit: CHANGES_PAGE, compareA: '', compareB: '', stations: [], tz: '',
                  ss: { station: null, range: '30d', from: '', to: '', sort: 'plays', limit: SS_PAGE } };
  let refreshToken = 0;

  const seriesColor = (slot) => `var(--series-${(slot % 8) + 1})`;  // a station's slot never changes when you reorder

  async function api(path, params = {}) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) qs.set(k, v);
    const res = await fetch(`${path}?${qs}`);
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  const filters = (extra = {}) => ({ station: state.station, range: state.range, q: state.q, ...extra });

  // ---- formatting ----------------------------------------------------------

  // A play's time is shown in its station's own timezone (stored times are UTC, so this is only about display).
  // The zone label (PST, EST...) is added when your stations aren't all in one zone, so no time is ambiguous.
  const stationZone = (id) => ((state.stations.find((s) => s.id === id) || {}).timezone) || state.tz || undefined;
  const mixedZones = () => new Set(state.stations.map((s) => s.timezone || state.tz)).size > 1;
  const zoneAbbr = (zone, date) => {
    try { return new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'short' }).formatToParts(date).find((p) => p.type === 'timeZoneName').value; }
    catch (_) { return ''; }
  };
  const zoneName = (zone) => {
    try { return new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'longGeneric' }).formatToParts(new Date()).find((p) => p.type === 'timeZoneName').value; }
    catch (_) { return zone; }
  };
  function fmtTime(t, stationId) {
    const zone = stationZone(stationId), date = new Date(t * 1000);
    let text;
    try { text = date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: zone }); }
    catch (_) { text = date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }
    return mixedZones() ? `${text} ${zoneAbbr(zone, date)}`.trim() : text;
  }

  function ago(t, now) {
    if (!t) return 'never';
    const s = Math.max(0, now - t);
    if (s < 90) return 'just now';
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 129600) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  function fmtLabel(label, bucket, long = false) {
    const [date, hour] = label.split('T');
    const [, m, d] = date.split('-').map(Number);
    if (bucket === 'day') return `${MONTHS[m - 1]} ${d}`;
    const h = Number(hour);
    const clock = `${h % 12 || 12}${h < 12 ? 'a' : 'p'}`;
    return long ? `${MONTHS[m - 1]} ${d}, ${clock}` : (h === 0 ? `${MONTHS[m - 1]} ${d}` : clock);
  }

  const compact = (n) => (n >= 1000 ? `${+(n / 1000).toFixed(1)}K` : String(n));

  // Whole-number gridlines on a 1-2-5 step, at most 5 intervals.
  function niceScale(max) {
    if (max <= 4) return { top: 4, step: 1 };
    const pow = 10 ** Math.floor(Math.log10(max / 5));
    const step = [1, 2, 5, 10].map((m) => m * pow).find((s) => max / s <= 5);
    return { top: Math.ceil(max / step) * step, step };
  }

  // ---- chart: stacked columns, one series per station -----------------------

  function drawChart(el, data) {
    const { labels, series, bucket } = data;
    el.innerHTML = '';
    if (!labels.length || !series.length) {
      el.innerHTML = '<div class="py-12 text-center opacity-60 text-sm">No plays in this range yet.</div>';
      return;
    }
    const W = Math.max(280, el.clientWidth), H = 240;
    const m = { l: 34, r: 6, t: 8, b: 22 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = labels.length, band = pw / n;
    const barW = Math.max(1.5, Math.min(24, band * 0.7));
    const totals = labels.map((_, i) => series.reduce((s, x) => s + x.counts[i], 0));
    const { top, step: tickStep } = niceScale(Math.max(...totals));
    const y = (v) => m.t + ph - (v / top) * ph;
    const GAP = 2, R = 4;

    let g = '';
    for (let v = 0; v <= top; v += tickStep) {
      const yy = y(v);
      g += `<line x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}" stroke="${v === 0 ? 'var(--axis)' : 'var(--grid)'}" stroke-width="1"/>`;
      g += `<text x="${m.l - 6}" y="${yy + 4}" text-anchor="end">${compact(v)}</text>`;
    }
    const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 56))));
    for (let i = 0; i < n; i += step) {
      g += `<text x="${m.l + band * i + band / 2}" y="${H - 6}" text-anchor="middle">${esc(fmtLabel(labels[i], bucket))}</text>`;
    }
    for (let i = 0; i < n; i++) {
      const x = m.l + band * i + (band - barW) / 2;
      let cursor = y(0), topIdx = -1;
      series.forEach((s, k) => { if (s.counts[i]) topIdx = k; });
      series.forEach((s, k) => {
        const v = s.counts[i];
        if (!v) return;
        const h = (v / top) * ph;
        const bottom = cursor === y(0) ? cursor : cursor - GAP;
        const segH = Math.max(1, bottom - (cursor - h));
        const segTop = bottom - segH;
        const fill = seriesColor(s.color);
        if (k === topIdx) {
          const r = Math.min(R, barW / 2, segH);
          g += `<path d="M${x},${bottom}V${segTop + r}Q${x},${segTop} ${x + r},${segTop}H${x + barW - r}Q${x + barW},${segTop} ${x + barW},${segTop + r}V${bottom}Z" fill="${fill}"/>`;
        } else {
          g += `<rect x="${x}" y="${segTop}" width="${barW}" height="${segH}" fill="${fill}"/>`;
        }
        cursor -= h;
      });
    }
    g += `<rect class="hover-band" y="${m.t}" width="${band}" height="${ph}" fill="currentColor" opacity="0" pointer-events="none"/>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Plays per ${bucket}">${g}</svg><div class="chart-tip" hidden></div>`;

    const svg = el.querySelector('svg'), tip = el.querySelector('.chart-tip'), hb = el.querySelector(".hover-band");
    svg.addEventListener('mousemove', (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.floor((px - m.l) / band);
      if (i < 0 || i >= n) return hideTip();
      hb.setAttribute('x', m.l + band * i);
      hb.setAttribute('opacity', '0.06');
      const rows = series.filter((s) => s.counts[i]).map((s) =>
        `<div class="flex items-center gap-2"><span class="swatch" style="background:${seriesColor(s.color)}"></span><span class="flex-1">${esc(s.name)}</span><b>${nf.format(s.counts[i])}</b></div>`).join('');
      tip.innerHTML = `<div class="font-semibold">${esc(fmtLabel(labels[i], bucket, true))}</div>${rows || '<div class="opacity-60">No plays</div>'}` +
        (series.length > 1 && totals[i] ? `<div class="flex border-t border-base-300 mt-1 pt-1"><span class="flex-1">Total</span><b>${nf.format(totals[i])}</b></div>` : '');
      tip.hidden = false;
      const cx = ((m.l + band * i + band / 2) / W) * r.width;
      tip.style.left = `${Math.min(Math.max(0, cx + 12), r.width - tip.offsetWidth)}px`;
      tip.style.top = '8px';
    });
    const hideTip = () => { tip.hidden = true; hb.setAttribute('opacity', '0'); };
    svg.addEventListener('mouseleave', hideTip);
  }

  function drawLegend(el, series) {
    el.innerHTML = series.length > 1
      ? series.map((s) => `<span class="inline-flex items-center gap-1.5"><span class="swatch" style="background:${seriesColor(s.color)}"></span>${esc(s.name)}</span>`).join('')
      : '';
  }

  function drawTable(el, data) {
    if (!data.labels.length) { el.innerHTML = ''; return; }
    const head = data.series.map((s) => `<th class="text-right">${esc(s.name)}</th>`).join('');
    const rows = data.labels.map((l, i) =>
      `<tr><td>${esc(fmtLabel(l, data.bucket, true))}</td>${data.series.map((s) => `<td class="text-right">${s.counts[i]}</td>`).join('')}</tr>`).reverse().join('');
    el.innerHTML = `<table class="table table-xs table-pin-rows"><thead><tr><th>${data.bucket === 'hour' ? 'Hour' : 'Day'}</th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  // ---- Spotify links (a search on open.spotify.com: no account or API key involved) ----------

  const SPOTIFY_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="12" fill="#1db954"/><path d="M5.5 9.2c4.2-1.2 9-.9 13 1.3M6.3 12.6c3.5-1 7.3-.7 10.4 1M7.2 15.7c2.8-.8 5.6-.5 8 .8" fill="none" stroke="#000" stroke-width="1.6" stroke-linecap="round"/></svg>';

  // A song when `title` is given, otherwise just the artist. With `text`, the button shows that label next to the icon.
  function spotifyLink({ artist, title }, text = '') {
    const song = Boolean(title);
    const query = (song ? `${title} ${artist}` : artist).replace(/[\/\\]/g, ' ');
    const url = `https://open.spotify.com/search/${encodeURIComponent(query)}/${song ? 'tracks' : 'artists'}`;
    const label = song ? `Search Spotify for ${title} by ${artist}` : `Search Spotify for ${artist}`;
    return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="btn btn-ghost btn-xs ${text ? 'gap-1.5' : 'btn-square'}" title="${esc(label)}" aria-label="${esc(label)}">${SPOTIFY_ICON}${text ? `<span>${esc(text)}</span>` : ''}</a>`;
  }

  // ---- sections -------------------------------------------------------------

  const empty = (cols, msg) => `<tr><td colspan="${cols}" class="text-center opacity-60 py-6">${msg}</td></tr>`;

  function renderSongs(rows) {
    const max = rows.length ? rows[0].plays : 1;
    $('songs').innerHTML = rows.length ? rows.map((r, i) => `
      <tr class="hover:bg-base-200 cursor-pointer" data-key="${esc(r.song_key)}">
        <td class="opacity-60">${i + 1}</td>
        <td><div class="font-medium">${esc(r.title)}</div><div class="text-xs opacity-60">${esc(r.artist)}</div></td>
        <td><div class="flex items-center gap-2"><span class="w-8 text-right tabular-nums">${nf.format(r.plays)}</span>
          <span class="hidden sm:inline-block h-1.5 rounded-sm" style="width:${Math.max(2, (r.plays / max) * 100)}%;max-width:7rem;background:var(--series-1)"></span></div></td>
        <td class="hidden sm:table-cell text-right text-sm opacity-70 whitespace-nowrap">${fmtTime(r.last_played, r.last_station_id)}</td>
        <td class="text-right">${spotifyLink(r)}</td>
      </tr>`).join('') : empty(5, 'No plays match.');
    $('more-songs').classList.toggle('hidden', rows.length < state.songLimit);
  }

  function renderArtists(rows) {
    $('artists').innerHTML = rows.length ? rows.map((r, i) => `
      <tr class="hover:bg-base-200 cursor-pointer" data-artist="${esc(r.artist)}">
        <td class="opacity-60">${i + 1}</td>
        <td>${esc(r.artist)}<div class="text-xs opacity-60">${r.songs} song${r.songs === 1 ? '' : 's'}</div></td>
        <td class="text-right tabular-nums">${nf.format(r.plays)}</td>
        <td class="text-right">${spotifyLink({ artist: r.artist })}</td></tr>`).join('') : empty(4, 'No plays match.');
  }

  const stationDot = (id) => {
    const s = state.stations.find((x) => x.id === id);
    return `<span class="swatch" style="background:${seriesColor(s ? s.color : 0)}"></span>`;
  };

  const recentRow = (r) => `<tr><td class="whitespace-nowrap">${fmtTime(r.played_at, r.station_id)}</td>
    <td class="whitespace-nowrap"><span class="inline-flex items-center gap-1.5">${stationDot(r.station_id)}${esc(r.station_name)}</span></td>
    <td><span class="font-medium">${esc(r.title)}</span> <span class="opacity-60">· ${esc(r.artist)}</span></td></tr>`;

  function renderRecent(rows) {
    $('recent').innerHTML = rows.length ? rows.map(recentRow).join('') : empty(3, 'Nothing logged yet. The poller checks each station every minute.');
    $('more-recent').classList.toggle('hidden', rows.length < state.recentLimit);
  }

  function renderStations(info) {
    state.stations = info.stations;
    state.tz = info.tz;
    const sel = $('station');
    const current = state.station;
    sel.innerHTML = '<option value="">All stations</option>' +
      info.stations.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}${s.active ? '' : ' (paused)'}</option>`).join('');
    sel.value = current;

    $('station-status').innerHTML = info.stations.map((s) => {
      const failing = s.active && s.last_error;
      const stale = s.active && !failing && s.last_ok_at && info.now - s.last_ok_at > 600;
      const badge = !s.active ? '<span class="badge badge-sm badge-ghost">Paused</span>'
        : failing ? `<span class="badge badge-sm badge-error badge-soft" title="${esc(s.last_error)}">Failing</span>`
        : stale ? '<span class="badge badge-sm badge-warning badge-soft">Stale</span>'
        : s.last_ok_at ? '<span class="badge badge-sm badge-success badge-soft">OK</span>'
        : '<span class="badge badge-sm badge-ghost">Waiting</span>';
      return `<tr><td><span class="inline-flex items-center gap-1.5">${stationDot(s.id)}${esc(s.name)}</span></td>
        <td class="text-right tabular-nums">${nf.format(s.plays)}</td>
        <td>${ago(s.last_play, info.now)}</td><td>${ago(s.last_poll_at, info.now)}</td>
        <td>${badge}${failing ? `<div class="text-xs opacity-60 mt-1 max-w-xs truncate" title="${esc(s.last_error)}">${esc(s.last_error)}</div>` : ''}</td></tr>`;
    }).join('');

    const active = info.stations.filter((s) => s.active);
    const bad = active.filter((s) => s.last_error).length;
    $('health').textContent = !active.length ? 'No stations switched on'
      : bad ? `${bad} of ${active.length} stations failing` : `${active.length} stations polling`;
  }

  // ---- now playing ----------------------------------------------------------

  const NOW_WINDOW = 6 * 60; // a song that started within this many seconds is "now playing"
  const ART_PLACEHOLDER = '<div class="art-placeholder w-28 h-28 shrink-0 rounded-box bg-base-200 flex items-center justify-center text-3xl opacity-60" aria-hidden="true">♪</div>';

  function renderNowPlaying(data) {
    const el = $('now-playing');
    const single = Boolean(state.station);
    const status = (s) => (data.now - s.played_at <= NOW_WINDOW
      ? '<span class="badge badge-sm badge-success badge-soft">Now playing</span>'
      : `<span class="text-sm opacity-70">Last played ${ago(s.played_at, data.now)}</span>`);

    if (!data.stations.length) { el.innerHTML = '<div class="opacity-60 text-sm">No stations configured.</div>'; return; }

    if (single) {
      const s = data.stations[0];
      if (!s.title) { el.innerHTML = '<div class="opacity-60 text-sm">Nothing logged yet for this station.</div>'; return; }
      const art = /^https?:\/\//.test(s.image_url || '')
        ? `<img src="${esc(s.image_url)}" alt="Album art for ${esc(s.title)}" class="w-28 h-28 shrink-0 rounded-box object-cover bg-base-200" loading="lazy">`
        : ART_PLACEHOLDER;
      el.innerHTML = `<div class="flex items-center gap-4 cursor-pointer" data-key="${esc(s.song_key)}">${art}
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">${status(s)}<span class="inline-flex items-center gap-1.5 text-sm opacity-70">${stationDot(s.station_id)}${esc(s.station_name)}</span></div>
          <div class="text-xl font-semibold mt-1 break-words">${esc(s.title)}</div>
          <div class="opacity-70 break-words">${esc(s.artist)}</div>
          <div class="text-sm opacity-60 mt-1">${fmtTime(s.played_at, s.station_id)}</div>
          <div class="mt-2 flex flex-wrap gap-1">${spotifyLink(s, 'Open in Spotify')}${spotifyLink({ artist: s.artist }, 'Open artist')}</div></div></div>`;
      return;
    }

    el.innerHTML = `<div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${data.stations.map((s) => s.title ? `
      <div class="rounded-box border border-base-300 p-3 cursor-pointer hover:bg-base-200" data-key="${esc(s.song_key)}">
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="inline-flex items-center gap-1.5 text-sm font-medium">${stationDot(s.station_id)}${esc(s.station_name)}</span>${status(s)}
        </div>
        <div class="flex items-end justify-between gap-2">
          <div class="min-w-0"><div class="font-semibold break-words">${esc(s.title)}</div>
            <div class="text-sm opacity-70 break-words">${esc(s.artist)}</div></div>
          ${spotifyLink(s)}</div></div>` : `
      <div class="rounded-box border border-base-300 p-3">
        <span class="inline-flex items-center gap-1.5 text-sm font-medium">${stationDot(s.station_id)}${esc(s.station_name)}</span>
        <div class="text-sm opacity-60 mt-1">Nothing logged yet.</div>
      </div>`).join('')}</div>`;
  }

  // ---- station songs ----------------------------------------------------------

  let ssToken = 0, ssSignature = '';

  function syncSongStations() {
    const ss = state.ss, ids = state.stations.map((x) => x.id);
    if (ss.station === null) ss.station = state.station || (state.stations.find((x) => x.active) || state.stations[0] || {}).id || '';
    if (ss.station && !ids.includes(ss.station)) ss.station = '';
    const signature = state.stations.map((x) => `${x.id}|${x.name}|${x.active}`).join(',');
    if (signature !== ssSignature) {  // rebuilding the list would close one the user has open
      ssSignature = signature;
      $('ss-station').innerHTML = '<option value="">All stations</option>' +
        state.stations.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}${x.active ? '' : ' (paused)'}</option>`).join('');
    }
    $('ss-station').value = ss.station;
  }

  function syncSongControls() {
    const ss = state.ss, custom = Boolean(ss.from || ss.to);
    document.querySelectorAll('#ss-ranges .tab').forEach((b) => {
      const on = !custom && b.dataset.range === ss.range;
      b.classList.toggle('tab-active', on);
      b.setAttribute('aria-selected', on);
    });
    $('ss-from').value = ss.from; $('ss-to').value = ss.to;
    $('ss-from').max = ss.to; $('ss-to').min = ss.from;  // the browser stops "from" landing after "to"
    $('ss-sort').value = ss.sort;
  }

  async function loadStationSongs() {
    const token = ++ssToken, ss = state.ss;
    syncSongStations(); syncSongControls();
    const custom = Boolean(ss.from || ss.to);
    try {
      const data = await api('/api/station-songs', {
        station: ss.station, sort: ss.sort, limit: ss.limit,
        ...(custom ? { from_date: ss.from, to_date: ss.to } : { range: ss.range }),
      });
      if (token === ssToken) renderStationSongs(data, custom);
    } catch (err) {
      console.error(err);
      if (token === ssToken) $('ss-songs').innerHTML = empty(5, 'Could not load the songs.');
    }
  }

  function renderStationSongs(data, custom) {
    const ss = state.ss;
    $('ss-songs').innerHTML = data.songs.length ? data.songs.map((r, i) => `
      <tr class="hover:bg-base-200 cursor-pointer" data-key="${esc(r.song_key)}">
        <td class="opacity-60">${i + 1}</td>
        <td><div class="font-medium">${esc(r.title)}</div><div class="text-xs opacity-60">${esc(r.artist)}</div></td>
        <td class="text-right tabular-nums">${nf.format(r.plays)}</td>
        <td class="hidden sm:table-cell text-right text-sm opacity-70 whitespace-nowrap">${fmtTime(r.last_played, r.last_station_id)}</td>
        <td class="text-right">${spotifyLink(r)}</td>
      </tr>`).join('') : empty(5, 'No songs in this range.');
    const shown = data.songs.length;
    $('ss-caption').textContent = data.total
      ? `${nf.format(data.total)} song${data.total === 1 ? '' : 's'} · ${nf.format(data.plays)} play${data.plays === 1 ? '' : 's'}` +
        (shown < data.total ? ` · showing the first ${nf.format(shown)}` : '') +
        (custom ? ` · dates in ${zoneName(stationZone(ss.station))}` : '')
      : '';
    $('ss-more').classList.toggle('hidden', shown >= data.total);
  }

  // ---- when it plays: weekday x hour heatmap ---------------------------------

  const hourShort = (h) => `${h % 12 || 12}${h < 12 ? 'a' : 'p'}`;
  const hourLong = (h) => `${h % 12 || 12} ${h < 12 ? 'AM' : 'PM'}`;
  // One hue, light to dark, mixed into the card's own surface so it reads in both themes.
  const heatFill = (v, max) => `color-mix(in srgb, var(--series-1) ${Math.round(14 + 86 * (v / max))}%, var(--color-base-100))`;

  function drawHeat(el, heat) {
    el.innerHTML = '';
    if (!heat.total) {
      el.innerHTML = '<div class="py-12 text-center opacity-60 text-sm">No plays in this range yet.</div>';
      return;
    }
    const W = Math.max(280, el.clientWidth), m = { l: 34, r: 2, t: 16, b: 2 };
    const cw = (W - m.l - m.r) / 24, ch = Math.max(14, Math.min(26, cw * 0.9)), H = m.t + 7 * ch + m.b, GAP = 2;
    let g = '';
    for (let h = 0; h < 24; h += 3) g += `<text x="${m.l + cw * h + cw / 2}" y="10" text-anchor="middle">${hourShort(h)}</text>`;
    DAYS.forEach((d, i) => { g += `<text x="${m.l - 6}" y="${m.t + ch * i + ch / 2 + 4}" text-anchor="end">${d}</text>`; });
    heat.cells.forEach((row, d) => row.forEach((v, h) => {
      g += `<rect x="${m.l + cw * h + GAP / 2}" y="${m.t + ch * d + GAP / 2}" width="${cw - GAP}" height="${ch - GAP}" rx="2" style="fill:${v ? heatFill(v, heat.max) : 'var(--grid)'}"/>`;
    }));
    g += `<rect class="hover-cell" width="${cw - GAP}" height="${ch - GAP}" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0" pointer-events="none"/>`;
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Plays by weekday and hour of day">${g}</svg><div class="chart-tip" hidden></div>`;

    const svg = el.querySelector('svg'), tip = el.querySelector('.chart-tip'), mark = el.querySelector('.hover-cell');
    const hide = () => { tip.hidden = true; mark.setAttribute('opacity', '0'); };
    svg.addEventListener('mousemove', (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W, py = ((e.clientY - r.top) / r.height) * H;
      const h = Math.floor((px - m.l) / cw), d = Math.floor((py - m.t) / ch);
      if (h < 0 || h > 23 || d < 0 || d > 6) return hide();
      const v = heat.cells[d][h];
      mark.setAttribute('x', m.l + cw * h + GAP / 2); mark.setAttribute('y', m.t + ch * d + GAP / 2); mark.setAttribute('opacity', '0.5');
      tip.innerHTML = `<div class="font-semibold">${DAYS_LONG[d]} ${hourLong(h)}–${hourLong((h + 1) % 24)}</div>` +
        `<div><b>${nf.format(v)}</b> play${v === 1 ? '' : 's'}${heat.total ? ` · ${(100 * v / heat.total).toFixed(1)}% of all` : ''}</div>`;
      tip.hidden = false;
      const cx = ((m.l + cw * h + cw / 2) / W) * r.width, cy = ((m.t + ch * d) / H) * r.height;
      tip.style.left = `${Math.min(Math.max(0, cx + 12), r.width - tip.offsetWidth)}px`;
      tip.style.top = `${Math.max(0, cy - tip.offsetHeight - 6)}px`;
    });
    svg.addEventListener('mouseleave', hide);
  }

  function renderHeat(heat) {
    $('heat-caption').textContent = heat.total
      ? `Total plays by weekday and hour of day, in ${zoneName(heat.tz)}, for the selected range${state.q ? ` and search “${state.q}”` : ''}.` : '';
    $('heat-legend').innerHTML = heat.total
      ? `<span>Fewer</span><span class="inline-block h-2.5 w-24 rounded-sm" style="background:linear-gradient(90deg,${heatFill(1, heat.max * 7.2)},${heatFill(1, 1)})"></span><span>More (peak ${nf.format(heat.max)})</span>` : '';
    drawHeat($('heat'), heat);
    $('heat-table').innerHTML = heat.total
      ? `<table class="table table-xs"><thead><tr><th></th>${Array.from({ length: 24 }, (_, h) => `<th class="text-right">${hourShort(h)}</th>`).join('')}</tr></thead><tbody>` +
        heat.cells.map((row, d) => `<tr><th>${DAYS[d]}</th>${row.map((v) => `<td class="text-right tabular-nums">${v || ''}</td>`).join('')}</tr>`).join('') + '</tbody></table>'
      : '';
  }

  // ---- new and dropped songs --------------------------------------------------

  function duration(s) {
    if (s < 5400) return `${Math.max(1, Math.round(s / 60))} min`;
    if (s < 129600) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} d`;
  }

  function spanText(s) {  // "7 days", "36 hours"
    const days = s / 86400, n = days >= 1.5 ? Math.round(days) : Math.max(1, Math.round(s / 3600));
    return `${n} ${days >= 1.5 ? 'day' : 'hour'}${n === 1 ? '' : 's'}`;
  }

  function renderChanges(res) {
    const note = $('changes-note'), box = $('changes'), more = $('more-changes');
    box.innerHTML = ''; note.innerHTML = ''; more.classList.add('hidden');
    $('changes-caption').textContent = '';
    if (!res.applicable) {
      note.innerHTML = `<div class="text-sm opacity-70 py-6 text-center">${res.reason === 'range'
        ? 'Pick a range shorter than “All time” (it compares the range with the period just before it).'
        : 'Not enough history yet: this compares the selected range with the period before it, so it needs plays from before the range started.'}</div>`;
      return;
    }
    const span = res.since - res.previous_start, where = state.station ? ` on ${(state.stations.find((s) => s.id === state.station) || {}).name || 'this station'}` : '';
    $('changes-caption').textContent = `The selected range${where}, compared with the ${spanText(span)} before it.` +
      (res.baseline < span ? ` Only ${spanText(res.baseline)} of earlier history exists, so some songs here may just not have come up before.` : '');

    const item = (s, verb) => `
      <li class="flex items-start gap-3 py-2 px-1 border-t border-base-300 first:border-t-0 cursor-pointer hover:bg-base-200" data-key="${esc(s.song_key)}">
        <div class="min-w-0 flex-1"><div class="font-medium truncate">${esc(s.title)}</div><div class="text-xs opacity-60 truncate">${esc(s.artist)}</div></div>
        <div class="text-right text-sm whitespace-nowrap">
          <div class="tabular-nums">${nf.format(s.plays)} play${s.plays === 1 ? '' : 's'}</div>
          <div class="text-xs flex items-center justify-end gap-1.5">${state.station ? '' : stationDot(s.station_id)}<span class="opacity-60">${verb} ${fmtTime(s.at, s.station_id)}</span></div></div>
      </li>`;
    const column = (title, sub, group, verb, none) => `
      <div>
        <div class="flex items-center gap-2"><h3 class="font-semibold">${title}</h3><span class="badge badge-sm badge-ghost">${nf.format(group.total)}</span></div>
        <p class="text-xs opacity-60 mb-1">${sub}</p>
        ${group.songs.length ? `<ul>${group.songs.map((s) => item(s, verb)).join('')}</ul>` : `<div class="text-sm opacity-60 py-4">${none}</div>`}
      </div>`;
    box.innerHTML =
      column('New in rotation', 'First heard in this range', res.new, 'first heard', 'Nothing new in this range.') +
      column('Dropped out', `Played ${res.dropped.min_plays}+ times in the period before, not once since`, res.dropped, 'last heard', 'Nothing has dropped out.');
    more.classList.toggle('hidden', res.new.total <= res.new.songs.length && res.dropped.total <= res.dropped.songs.length);
  }

  // ---- comparing two stations -------------------------------------------------

  let compareSignature = '', compareToken = 0;

  function syncCompareSelects() {
    const ids = state.stations.map((s) => s.id);
    if (!ids.includes(state.compareA) || state.compareA === state.compareB) state.compareA = ids.find((id) => id !== state.compareB) || ids[0] || '';
    if (!ids.includes(state.compareB) || state.compareB === state.compareA) state.compareB = ids.find((id) => id !== state.compareA) || '';
    const signature = state.stations.map((s) => `${s.id}|${s.name}|${s.active}`).join(',');
    if (signature !== compareSignature) {  // rebuilding the lists would close one the user has open, so only when they changed
      compareSignature = signature;
      const options = state.stations.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}${s.active ? '' : ' (paused)'}</option>`).join('');
      $('cmp-a').innerHTML = options; $('cmp-b').innerHTML = options;
    }
    $('cmp-a').value = state.compareA; $('cmp-b').value = state.compareB;
  }

  async function loadCompare() {
    const token = ++compareToken;
    $('compare-card').classList.toggle('hidden', state.stations.length < 2);
    if (state.stations.length < 2) return;
    syncCompareSelects();
    try {
      const data = await api('/api/compare', { a: state.compareA, b: state.compareB, range: state.range, q: state.q, limit: state.compareLimit });
      if (token === compareToken) renderCompare(data);
    } catch (err) {
      console.error(err);
      if (token === compareToken) $('compare').innerHTML = '<div class="text-sm opacity-60 py-4">Could not load the comparison.</div>';
    }
  }

  function renderCompare(c) {
    const label = (side) => `<span class="inline-flex items-center gap-1.5">${stationDot(side.id)}${esc(side.name)}</span>`;
    const card = (side, other) => `
      <div class="rounded-box border border-base-300 p-3">
        <div class="font-medium">${label(side)}</div>
        <div class="mt-1 text-sm flex flex-wrap gap-x-4 gap-y-0.5">
          <span><b class="tabular-nums">${nf.format(side.plays)}</b> plays</span><span><b class="tabular-nums">${nf.format(side.songs)}</b> songs</span>
          <span><b>${side.avg_plays}</b> plays per song</span></div>
        <div class="text-xs opacity-70 mt-1">${side.songs ? `${side.shared_pct}% of its songs also play on ${esc(other.name)}` : 'No plays in this range'}</div>
      </div>`;
    let html = `<div class="grid sm:grid-cols-2 gap-3">${card(c.a, c.b)}${card(c.b, c.a)}</div>`;

    if (!c.shared.songs) {
      html += '<div class="text-sm opacity-60 py-4 text-center">These stations have no songs in common in this range.</div>';
    } else {
      const f = c.first, undecided = f.unknown + f.tie;
      html += `<p class="text-sm"><b>${nf.format(c.shared.songs)}</b> shared song${c.shared.songs === 1 ? '' : 's'}. Of those, ` +
        `${label(c.a)} played <b>${nf.format(f.a)}</b> first and ${label(c.b)} played <b>${nf.format(f.b)}</b> first` +
        (undecided ? `; <b>${nf.format(undecided)}</b> can't be called (already in rotation when the later station was added${f.tie ? ', or heard at the same moment' : ''})` : '') + '.</p>';
      html += `<div class="overflow-x-auto"><table class="table table-sm"><thead><tr><th>Song</th>
        <th class="text-right">${label(c.a)}</th><th class="text-right">${label(c.b)}</th><th>Heard first on</th></tr></thead><tbody>` +
        c.songs.map((s) => {
          const winner = s.first === 'a' ? c.a : s.first === 'b' ? c.b : null;
          const first = winner ? `${label(winner)} <span class="text-xs opacity-60">${s.lead ? duration(s.lead) + ' earlier' : ''}</span>`
            : s.first === 'tie' ? '<span class="text-xs opacity-60">Same time</span>' : '<span class="text-xs opacity-60">Already in rotation</span>';
          return `<tr class="hover:bg-base-200 cursor-pointer" data-key="${esc(s.song_key)}"><td><div class="font-medium">${esc(s.title)}</div><div class="text-xs opacity-60">${esc(s.artist)}</div></td>
            <td class="text-right tabular-nums">${nf.format(s.plays_a)}</td><td class="text-right tabular-nums">${nf.format(s.plays_b)}</td><td class="whitespace-nowrap">${first}</td></tr>`;
        }).join('') + '</tbody></table></div>';
      if (c.shared.songs > c.songs.length) html += '<button id="more-compare" class="btn btn-sm self-start">Show more</button>';
    }

    const only = (side, group) => group.total ? `
      <div><h3 class="font-semibold text-sm">Only on ${label(side)} <span class="badge badge-sm badge-ghost">${nf.format(group.total)}</span></h3>
        <ul>${group.songs.map((s) => `
          <li class="flex items-center gap-3 py-1.5 px-1 border-t border-base-300 first:border-t-0 cursor-pointer hover:bg-base-200" data-key="${esc(s.song_key)}">
            <div class="min-w-0 flex-1"><div class="text-sm font-medium truncate">${esc(s.title)}</div><div class="text-xs opacity-60 truncate">${esc(s.artist)}</div></div>
            <span class="text-sm tabular-nums">${nf.format(s.plays)}</span></li>`).join('')}</ul></div>` : '';
    const left = only(c.a, c.only_a), right = only(c.b, c.only_b);
    if (left || right) html += `<div class="grid md:grid-cols-2 gap-x-6 gap-y-4">${left}${right}</div>`;
    $('compare').innerHTML = `<div class="flex flex-col gap-3">${html}</div>`;
  }

  // ---- refresh --------------------------------------------------------------

  async function refresh() {
    const token = ++refreshToken;
    const f = filters();
    try {
      const [info, now, sum, ts, songs, artists, recent, heat, changes] = await Promise.all([
        api('/api/stations'), api('/api/now-playing', { station: state.station }),
        api('/api/summary', f), api('/api/timeseries', f),
        api('/api/top-songs', { ...f, limit: state.songLimit }), api('/api/top-artists', f),
        api('/api/recent', { ...f, limit: state.recentLimit }),
        api('/api/heatmap', f), api('/api/changes', { ...f, limit: state.changeLimit }),
      ]);
      if (token !== refreshToken) return;
      renderStations(info);
      renderNowPlaying(now);
      $('stat-plays').textContent = nf.format(sum.plays);
      $('stat-songs').textContent = nf.format(sum.songs);
      $('stat-artists').textContent = nf.format(sum.artists);
      $('chart-title').textContent = `Plays per ${ts.bucket}`;
      $('chart-tz').textContent = ts.tz ? `${ts.bucket === 'hour' ? 'Hours' : 'Days'} in ${zoneName(ts.tz)}` : '';
      drawChart($('chart'), ts);
      drawLegend($('legend'), ts.series);
      drawTable($('chart-table'), ts);
      lastSeries = ts;
      renderSongs(songs);
      renderArtists(artists);
      renderRecent(recent);
      lastHeat = heat;
      renderHeat(heat);
      renderChanges(changes);
      loadCompare();
      loadStationSongs();
    } catch (err) {
      console.error(err);
      $('health').textContent = 'Could not reach the server';
    }
  }

  let lastSeries = null, lastHeat = null;

  // ---- song detail ----------------------------------------------------------

  let modalSeries = null;

  async function openSong(key) {
    const modal = $('song-modal');
    modal.showModal();
    $('song-title').textContent = 'Loading…';
    $('song-spotify').innerHTML = '';
    try {
      const [info, ts, plays] = await Promise.all([
        api('/api/song', { key }),
        api('/api/timeseries', { song_key: key, range: 'all' }),
        api('/api/recent', { song_key: key, limit: 100 }),
      ]);
      $('song-title').textContent = info.title;
      $('song-artist').textContent = info.artist;
      $('song-spotify').innerHTML = spotifyLink(info, 'Open in Spotify') + ' ' + spotifyLink({ artist: info.artist }, 'Open artist');
      $('song-stats').innerHTML =
        `<span><b>${nf.format(info.plays)}</b> plays</span><span>First: ${fmtTime(info.first_played, info.first_station_id)}</span><span>Last: ${fmtTime(info.last_played, info.last_station_id)}</span>` +
        info.stations.map((s) => `<span class="inline-flex items-center gap-1.5">${stationDot(s.station_id)}${esc(s.station_name)}: <b>${s.plays}</b></span>`).join('');
      modalSeries = ts;
      drawChart($('song-chart'), ts);
      drawLegend($('song-legend'), ts.series);
      $('song-plays').innerHTML = plays.map((r) =>
        `<tr><td class="whitespace-nowrap">${fmtTime(r.played_at, r.station_id)}</td><td><span class="inline-flex items-center gap-1.5">${stationDot(r.station_id)}${esc(r.station_name)}</span></td></tr>`).join('');
    } catch (err) {
      console.error(err);
      $('song-title').textContent = 'Could not load this song';
    }
  }

  // ---- wiring ---------------------------------------------------------------

  function setActiveRange() {
    document.querySelectorAll('#ranges .tab').forEach((b) => {
      const on = b.dataset.range === state.range;
      b.classList.toggle('tab-active', on);
      b.setAttribute('aria-selected', on);
    });
  }

  function resetPaging() { state.songLimit = TOP_PAGE; state.recentLimit = RECENT_PAGE; state.changeLimit = CHANGES_PAGE; state.compareLimit = CHANGES_PAGE; }

  $('ranges').addEventListener('click', (e) => {
    const b = e.target.closest('[data-range]');
    if (!b) return;
    state.range = b.dataset.range; resetPaging(); setActiveRange(); refresh();
  });
  $('station').addEventListener('change', (e) => {
    state.station = e.target.value;
    if (state.station) { state.ss.station = state.station; state.ss.limit = SS_PAGE; }  // the song list follows the page's station
    resetPaging(); refresh();
  });
  let qTimer;
  $('q').addEventListener('input', (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = e.target.value.trim(); resetPaging(); refresh(); }, 300);
  });
  $('now-playing').addEventListener('click', (e) => { if (e.target.closest('a')) return; const el = e.target.closest('[data-key]'); if (el) openSong(el.dataset.key); });
  $('now-playing').addEventListener('error', (e) => {  // broken or blocked artwork falls back to the placeholder tile
    if (e.target.tagName === 'IMG') e.target.outerHTML = ART_PLACEHOLDER;
  }, true);
  $('songs').addEventListener('click', (e) => { if (e.target.closest('a')) return; const tr = e.target.closest('[data-key]'); if (tr) openSong(tr.dataset.key); });
  $('artists').addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const tr = e.target.closest('[data-artist]');
    if (!tr) return;
    $('q').value = state.q = tr.dataset.artist; resetPaging(); refresh();
  });
  // The station songs card has its own station, range and sort, so it can be looked at apart from the page's filters.
  $('ss-station').addEventListener('change', (e) => { state.ss.station = e.target.value; state.ss.limit = SS_PAGE; loadStationSongs(); });
  $('ss-sort').addEventListener('change', (e) => { state.ss.sort = e.target.value; state.ss.limit = SS_PAGE; loadStationSongs(); });
  $('ss-ranges').addEventListener('click', (e) => {
    const b = e.target.closest('[data-range]');
    if (!b) return;
    Object.assign(state.ss, { range: b.dataset.range, from: '', to: '', limit: SS_PAGE }); loadStationSongs();
  });
  for (const id of ['ss-from', 'ss-to']) {
    $(id).addEventListener('change', () => {
      Object.assign(state.ss, { from: $('ss-from').value, to: $('ss-to').value, limit: SS_PAGE });
      if (!state.ss.from && !state.ss.to) state.ss.range = 'all';  // clearing both dates leaves no range chosen, so show everything
      loadStationSongs();
    });
  }
  $('ss-more').addEventListener('click', () => { state.ss.limit += SS_PAGE; loadStationSongs(); });
  $('ss-songs').addEventListener('click', (e) => { if (e.target.closest('a')) return; const tr = e.target.closest('[data-key]'); if (tr) openSong(tr.dataset.key); });

  $('more-songs').addEventListener('click', () => { state.songLimit += TOP_PAGE; refresh(); });
  $('more-recent').addEventListener('click', () => { state.recentLimit += RECENT_PAGE; refresh(); });
  $('more-changes').addEventListener('click', () => { state.changeLimit += CHANGES_PAGE; refresh(); });
  $('changes').addEventListener('click', (e) => { const li = e.target.closest('[data-key]'); if (li) openSong(li.dataset.key); });
  $('compare').addEventListener('click', (e) => {
    if (e.target.closest('#more-compare')) { state.compareLimit += CHANGES_PAGE; loadCompare(); return; }
    const row = e.target.closest('[data-key]');
    if (row) openSong(row.dataset.key);
  });
  function pickCompare(side) {  // the two sides can't be the same station: moving one onto the other moves the other away
    const mine = side === 'A' ? 'compareA' : 'compareB', theirs = side === 'A' ? 'compareB' : 'compareA';
    state[mine] = $(side === 'A' ? 'cmp-a' : 'cmp-b').value;
    if (state[theirs] === state[mine]) state[theirs] = '';
    state.compareLimit = CHANGES_PAGE;
    loadCompare();
  }
  $('cmp-a').addEventListener('change', () => pickCompare('A'));
  $('cmp-b').addEventListener('change', () => pickCompare('B'));

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastSeries) drawChart($('chart'), lastSeries);
      if (lastHeat) drawHeat($('heat'), lastHeat);
      if (modalSeries && $('song-modal').open) drawChart($('song-chart'), modalSeries);
    }, 150);
  });

  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  setActiveRange();
  refresh();
})();
