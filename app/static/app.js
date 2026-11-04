(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nf = new Intl.NumberFormat();
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const TOP_PAGE = 10;     // top songs shown at first, and added by each "Show more"
  const RECENT_PAGE = 25;

  const state = { station: '', range: '7d', q: '', songLimit: TOP_PAGE, recentLimit: RECENT_PAGE, stations: [], tz: '' };
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
      </tr>`).join('') : empty(4, 'No plays match.');
    $('more-songs').classList.toggle('hidden', rows.length < state.songLimit);
  }

  function renderArtists(rows) {
    $('artists').innerHTML = rows.length ? rows.map((r, i) => `
      <tr class="hover:bg-base-200 cursor-pointer" data-artist="${esc(r.artist)}">
        <td class="opacity-60">${i + 1}</td>
        <td>${esc(r.artist)}<div class="text-xs opacity-60">${r.songs} song${r.songs === 1 ? '' : 's'}</div></td>
        <td class="text-right tabular-nums">${nf.format(r.plays)}</td></tr>`).join('') : empty(3, 'No plays match.');
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
          <div class="text-sm opacity-60 mt-1">${fmtTime(s.played_at, s.station_id)}</div>        </div></div>`;
      return;
    }

    el.innerHTML = `<div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${data.stations.map((s) => s.title ? `
      <div class="rounded-box border border-base-300 p-3 cursor-pointer hover:bg-base-200" data-key="${esc(s.song_key)}">
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="inline-flex items-center gap-1.5 text-sm font-medium">${stationDot(s.station_id)}${esc(s.station_name)}</span>${status(s)}
        </div>
        <div class="font-semibold break-words">${esc(s.title)}</div>
        <div class="text-sm opacity-70 break-words">${esc(s.artist)}</div>      </div>` : `
      <div class="rounded-box border border-base-300 p-3">
        <span class="inline-flex items-center gap-1.5 text-sm font-medium">${stationDot(s.station_id)}${esc(s.station_name)}</span>
        <div class="text-sm opacity-60 mt-1">Nothing logged yet.</div>
      </div>`).join('')}</div>`;
  }

  // ---- refresh --------------------------------------------------------------

  async function refresh() {
    const token = ++refreshToken;
    const f = filters();
    try {
      const [info, now, sum, ts, songs, artists, recent] = await Promise.all([
        api('/api/stations'), api('/api/now-playing', { station: state.station }),
        api('/api/summary', f), api('/api/timeseries', f),
        api('/api/top-songs', { ...f, limit: state.songLimit }), api('/api/top-artists', f),
        api('/api/recent', { ...f, limit: state.recentLimit }),
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
    } catch (err) {
      console.error(err);
      $('health').textContent = 'Could not reach the server';
    }
  }

  let lastSeries = null;

  // ---- song detail ----------------------------------------------------------

  let modalSeries = null;

  async function openSong(key) {
    const modal = $('song-modal');
    modal.showModal();
    $('song-title').textContent = 'Loading…';
    try {
      const [info, ts, plays] = await Promise.all([
        api('/api/song', { key }),
        api('/api/timeseries', { song_key: key, range: 'all' }),
        api('/api/recent', { song_key: key, limit: 100 }),
      ]);
      $('song-title').textContent = info.title;
      $('song-artist').textContent = info.artist;
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

  function resetPaging() { state.songLimit = TOP_PAGE; state.recentLimit = RECENT_PAGE; }

  $('ranges').addEventListener('click', (e) => {
    const b = e.target.closest('[data-range]');
    if (!b) return;
    state.range = b.dataset.range; resetPaging(); setActiveRange(); refresh();
  });
  $('station').addEventListener('change', (e) => { state.station = e.target.value; resetPaging(); refresh(); });
  let qTimer;
  $('q').addEventListener('input', (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = e.target.value.trim(); resetPaging(); refresh(); }, 300);
  });
  $('now-playing').addEventListener('click', (e) => { const el = e.target.closest('[data-key]'); if (el) openSong(el.dataset.key); });
  $('now-playing').addEventListener('error', (e) => {  // broken or blocked artwork falls back to the placeholder tile
    if (e.target.tagName === 'IMG') e.target.outerHTML = ART_PLACEHOLDER;
  }, true);
  $('songs').addEventListener('click', (e) => { const tr = e.target.closest('[data-key]'); if (tr) openSong(tr.dataset.key); });
  $('artists').addEventListener('click', (e) => {
    const tr = e.target.closest('[data-artist]');
    if (!tr) return;
    $('q').value = state.q = tr.dataset.artist; resetPaging(); refresh();
  });
  $('more-songs').addEventListener('click', () => { state.songLimit += TOP_PAGE; refresh(); });
  $('more-recent').addEventListener('click', () => { state.recentLimit += RECENT_PAGE; refresh(); });

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastSeries) drawChart($('chart'), lastSeries);
      if (modalSeries && $('song-modal').open) drawChart($('song-chart'), modalSeries);
    }, 150);
  });

  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  setActiveRange();
  refresh();
})();
