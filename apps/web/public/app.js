/* The Totals Ledger — dumb renderer over the read-only API.
   All picks/tiers/values are server-derived and persisted; this file only
   formats them. SSE drives refresh; no client-side polling of data routes. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pct = (x, dp = 1) => x == null ? '—' : (x * 100).toFixed(dp) + '%';

const state = { slate: [], metrics: null, players: [], settled: [],
                health: null, filter: 'all' };

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

/* ---------- status strip ---------- */

function renderStatus() {
  const h = state.health;
  if (!h) return;
  const f = h.freshness_min ?? {};
  const ageTxt = (v) => v == null ? '—' : v < 1 ? 'just now' : `${v}m ago`;
  const next = state.slate[0];
  const cd = next ? Math.max(0, Math.round((Date.parse(next.kickoff) - Date.now()) / 60000)) : null;
  const gap = h.results_gap_days ?? 0;
  const bad = !h.ok || (f.odds ?? 99) > 20;
  $('status-strip').innerHTML = `
    <span class="live">● LIVE</span>
    <span class="item">results <b>${ageTxt(f.results)}</b></span>
    <span class="item">odds <b>${ageTxt(f.odds)}</b></span>
    <span class="item">predictions <b>${ageTxt(f.predictions)}</b></span>
    ${cd != null ? `<span class="item">next kickoff in <b>${cd}m</b></span>` : ''}
    ${gap > 1 ? `<span class="item warn">⚠ results gap ${gap}d — catch-up backfill in progress</span>` : ''}
    ${bad ? '<span class="item warn">⚠ pipeline attention needed</span>'
          : '<span class="item" style="color:var(--good);font-weight:600;">✓ pipeline healthy</span>'}`;
}

/* ---------- slate ---------- */

const FILTERS = ['all', 'strong', 'solid', 'lean', 'value'];

function headlineLine(fx) {
  const picked = fx.lines.filter((l) => l.pick);
  if (!picked.length) return fx.lines[0] ?? null;
  return picked.sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))[0];
}

function renderChips() {
  $('tier-chips').innerHTML = FILTERS.map((f) =>
    `<button class="pill ${state.filter === f ? 'active' : ''}" data-f="${f}">
       ${f.toUpperCase()}</button>`).join('');
  for (const b of $('tier-chips').querySelectorAll('button')) {
    b.onclick = () => { state.filter = b.dataset.f; renderSlate(); };
  }
}

function slateVisible() {
  return state.slate.filter((fx) => {
    const h = headlineLine(fx);
    if (!h) return false;
    if (state.filter === 'all') return true;
    if (state.filter === 'value') return fx.lines.some((l) => l.value);
    return h.tier === state.filter;
  });
}

function fixtureCard(fx) {
  const h = headlineLine(fx);
  const kick = new Date(fx.kickoff);
  const mins = Math.max(0, Math.round((kick - Date.now()) / 60000));
  const hh = kick.toISOString().slice(11, 16);
  const pickTxt = h?.pick
    ? `${h.pick.toUpperCase()} ${h.line}`
    : `O/U ${h?.line ?? '—'}`;
  const over = (h?.p_over ?? 0) * 100, push = (h?.p_push ?? 0) * 100,
        under = (h?.p_under ?? 0) * 100;
  const covWarn = fx.coverage !== 'full';
  return `<article class="card">
    <div class="top"><span><b>${hh}Z</b> · in ${mins}m</span>
      <span>${esc(fx.competition)}</span></div>
    <div class="pair">${esc(fx.home.club)} <b>${esc(fx.home.player ?? '?')}</b></div>
    <div class="pair" style="margin-top:2px;">${esc(fx.away.club)}
      <b>${esc(fx.away.player ?? '?')}</b></div>
    <div class="headline">
      <span class="pick serif ${h?.pick ? '' : 'nopick'}">${pickTxt}</span>
      ${h?.tier ? `<span class="tier ${h.tier}">${h.tier}</span>` : ''}
    </div>
    <div class="bar" title="over / push / under">
      <div class="over" style="width:${over}%"></div>
      ${push > 0.5 ? `<div class="push" style="width:${push}%"></div>` : ''}
      <div class="under" style="width:${under}%"></div>
    </div>
    <div class="kv"><span>Model O/U</span>
      <span class="num">${pct(h?.p_over)} / ${pct(h?.p_under)}</span></div>
    ${fx.priced ? `
    <div class="kv"><span>Book O/U</span>
      <span class="num">${pct(h?.book_over)} / ${pct(h?.book_under)}</span></div>
    <div class="kv"><span>Odds</span>
      <span class="num">${h?.over_odds ?? '—'} / ${h?.under_odds ?? '—'}</span></div>
    ${h?.edge != null ? `<div class="kv"><span>Edge (model side)</span>
      <span class="num" style="color:${h.edge >= 0 ? 'var(--good)' : 'var(--bad)'}">
      ${h.edge >= 0 ? '+' : ''}${(h.edge * 100).toFixed(1)} pts</span></div>` : ''}`
    : `<div class="kv"><span>Book</span>
      <span class="num" style="color:var(--ink3);">awaiting odds</span></div>`}
    <div class="badges">
      ${h?.value ? '<span class="badge-value">▲ VALUE</span>' : ''}
      ${!fx.priced ? '<span class="badge-cov">◌ model-only · book not yet priced</span>'
        : `<span class="badge-cov ${covWarn ? 'warn' : ''}">
        ${covWarn ? '◌ coverage ' + esc(fx.coverage) : '● full coverage'}</span>`}
      <span style="margin-left:auto;color:var(--ink3);">λ ${fx.lambda_home ?? '—'}
        / ${fx.lambda_away ?? '—'}</span>
    </div>
  </article>`;
}

function renderSlate() {
  renderChips();
  const vis = slateVisible();
  const nPicks = state.slate.filter((fx) => headlineLine(fx)?.pick).length;
  const nPriced = state.slate.filter((fx) => fx.priced).length;
  $('slate-meta').textContent =
    `${state.slate.length} upcoming · ${nPriced} priced by book · ${nPicks} picks · filter: ${state.filter}`;
  $('slate').innerHTML = vis.length
    ? vis.map(fixtureCard).join('')
    : `<div class="empty" style="grid-column:1/-1;">no fixtures match —
       the book prices ~1h ahead, so an empty slate usually just means
       between-rounds. It refreshes automatically.</div>`;
}

/* ---------- scorecard ---------- */

function renderScorecard() {
  const m = state.metrics;
  if (!m) return;
  $('acc-badge').textContent = m.hit_rate != null ? pct(m.hit_rate, 0) : '—';
  const tiles = [
    ['SETTLED', m.settled, ''],
    ['GRADED PICKS', m.graded, ''],
    ['HIT RATE', m.hit_rate != null ? pct(m.hit_rate) : '—', 'accent'],
    ['MODEL BRIER', m.brier_model?.toFixed(4) ?? '—', ''],
    ['REGEN AGREE', m.regen_agreement != null ? pct(m.regen_agreement, 0) : '—',
      m.regen_agreement != null && m.regen_agreement < 1 ? 'accent' : ''],
    ['LEAK FLAGS', m.leak_risk_count, m.leak_risk_count ? 'accent' : ''],
  ];
  $('tiles').innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="tile ${cls}"><div class="v num">${v}</div>
     <div class="k">${k}</div></div>`).join('');

  const rows = ['strong', 'solid', 'lean'].map((t) => {
    const x = m.tiers?.[t];
    return `<tr><td style="text-transform:capitalize;">${t}</td>
      <td class="num r">${x?.n ?? 0}</td>
      <td class="num r">${x?.hit_rate != null ? pct(x.hit_rate) : '—'}</td>
      <td style="width:40%;"><div class="minibar">
        <span style="width:${(x?.hit_rate ?? 0) * 100}%"></span></div></td></tr>`;
  }).join('');
  const v = m.value;
  $('tier-table').innerHTML = `<table class="ledger">
    <thead><tr><th>Tier</th><th class="r">Picks</th><th class="r">Hit</th><th></th></tr></thead>
    <tbody>${rows}
      <tr><td>value flag</td><td class="num r">${v?.n ?? 0}</td>
        <td class="num r">${v?.hit_rate != null ? pct(v.hit_rate) : '—'}</td>
        <td><div class="minibar"><span style="width:${(v?.hit_rate ?? 0) * 100}%;
          background:var(--good);"></span></div></td></tr>
    </tbody></table>`;
}

/* ---------- players ---------- */

function renderPlayers() {
  const ps = state.players.slice(0, 16);
  const half = Math.ceil(ps.length / 2);
  const col = (xs, off) => `<table class="ledger"><thead><tr>
      <th>#</th><th>Player</th><th class="r">M</th><th class="r">GF</th>
      <th class="r">GA</th><th class="r">O3.5</th></tr></thead><tbody>
    ${xs.map((p, i) => `<tr>
      <td class="num" style="color:var(--ink3);">${off + i + 1}</td>
      <td style="font-weight:600;">${esc(p.player)}</td>
      <td class="num r">${p.matches}</td>
      <td class="num r">${p.gf_per_match}</td>
      <td class="num r">${p.ga_per_match}</td>
      <td class="num r">${pct(p.over35_rate, 0)}</td></tr>`).join('')}
    </tbody></table>`;
  $('players').innerHTML = col(ps.slice(0, half), 0) + col(ps.slice(half), half);
}

/* ---------- settled ---------- */

function settledCard(s) {
  const kick = s.start_time_utc?.slice(11, 16) ?? '';
  const tag = { correct: '✓ pick correct', wrong: '✕ pick wrong',
                push: '— push', 'no-pick': '· no pick' }[s.outcome];
  return `<article class="settled ${s.outcome}">
    <div class="top meta" style="display:flex;justify-content:space-between;
      margin-bottom:8px;"><span>${kick}Z</span>
      ${s.tier ? `<span class="tier ${s.tier}">${s.tier}</span>` : ''}</div>
    <div class="row"><span class="pair">${esc(s.home_club)}
      <b>${esc((s.home_raw.match(/\(([^)]*)\)/) || [])[1] ?? '')}</b></span>
      <span class="score num">${s.home_ft ?? '–'}</span></div>
    <div class="row" style="margin-top:2px;"><span class="pair">${esc(s.away_club)}
      <b>${esc((s.away_raw.match(/\(([^)]*)\)/) || [])[1] ?? '')}</b></span>
      <span class="score num">${s.away_ft ?? '–'}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:10px;
      padding-top:9px;border-top:1px solid var(--line);" class="meta">
      <span class="tag ${s.outcome}">${tag}</span>
      <span>${s.pick ? esc(s.pick) + ' ' + s.line : 'O/U ' + s.line}
        · total ${s.result_total}</span></div>
  </article>`;
}

function renderSettled() {
  const xs = state.settled;
  $('settled-meta').textContent = xs.length
    ? `${xs.length} most recent · graded against scraped results` : '';
  $('settled').innerHTML = xs.length
    ? xs.map(settledCard).join('')
    : `<div class="empty" style="grid-column:1/-1;">nothing settled yet —
       settlements appear ~45 minutes after each predicted kickoff.</div>`;
}

/* ---------- analysis tab ---------- */

const analysis = { days: 30, data: null };
const RANGES = [[7, '7D'], [30, '30D'], [90, '90D'], [365, 'ALL']];

function renderRangeChips() {
  $('analysis-range').innerHTML = RANGES.map(([d, label]) =>
    `<button class="pill ${analysis.days === d ? 'active' : ''}" data-d="${d}">
       ${label}</button>`).join('');
  for (const b of $('analysis-range').querySelectorAll('button')) {
    b.onclick = () => { analysis.days = Number(b.dataset.d); void loadAnalysis(); };
  }
}

/* Two stacked panels, one shared x — never a dual axis: rate line on its own
   0–100% scale, graded-pick volume as columns on its own count scale. */
function renderHitrateChart() {
  const daily = analysis.data?.daily ?? [];
  const host = $('hitrate-chart');
  if (daily.length < 2) {
    host.innerHTML = `<div class="empty">not enough graded days yet —
      the chart appears once picks settle on two or more days.</div>`;
    return;
  }
  const W = 1000, H = 340, L = 46, R = 16;
  const rateTop = 26, rateBot = 200, volTop = 240, volBot = 306, xLabY = 330;
  const n = daily.length;
  const x = (i) => L + (i + 0.5) * ((W - L - R) / n);
  const yRate = (v) => rateBot - v * (rateBot - rateTop);
  const maxVol = Math.max(...daily.map((d) => d.graded));
  const yVol = (v) => volBot - (v / maxVol) * (volBot - volTop);

  let s = '';
  // recessive solid hairline grid + clean ticks (0/25/50/75/100)
  for (const g of [0, 0.25, 0.5, 0.75, 1]) {
    s += `<line x1="${L}" x2="${W - R}" y1="${yRate(g)}" y2="${yRate(g)}"
           stroke="var(--line)" stroke-width="1"/>
          <text x="${L - 8}" y="${yRate(g) + 4}" text-anchor="end" font-size="11"
           fill="var(--ink3)" style="font-variant-numeric:tabular-nums;">${g * 100}</text>`;
  }
  s += `<text x="${L}" y="14" class="panel-label" font-size="10.5"
         fill="var(--ink2)" letter-spacing="1.5">DAILY HIT RATE %</text>
        <text x="${L}" y="${volTop - 8}" font-size="10.5" fill="var(--ink2)"
         letter-spacing="1.5">GRADED PICKS · max ${maxVol}</text>`;

  // volume columns: thin, rounded data-end, square baseline, surface gaps by slot
  const slot = (W - L - R) / n;
  const bw = Math.min(24, Math.max(3, slot * 0.55));
  for (let i = 0; i < n; i++) {
    const h = volBot - yVol(daily[i].graded);
    s += `<path d="M ${x(i) - bw / 2} ${volBot}
           v ${-Math.max(0, h - 4)} q 0 -4 4 -4 h ${bw - 8} q 4 0 4 4
           v ${Math.max(0, h - 4)} z" fill="var(--ink2)"/>`;
  }
  // rate line: 2px round, markers with 2px surface ring
  const pts = daily.map((d, i) => `${x(i)},${yRate(d.hit_rate)}`);
  s += `<polyline points="${pts.join(' ')}" fill="none" stroke="var(--accent)"
         stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  for (let i = 0; i < n; i++) {
    s += `<circle cx="${x(i)}" cy="${yRate(daily[i].hit_rate)}" r="4.5"
           fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/>`;
  }
  // selective direct label: endpoint only (text token, not series color)
  const last = daily[n - 1];
  s += `<text x="${x(n - 1) + 9}" y="${yRate(last.hit_rate) + 4}" font-size="12.5"
         font-weight="700" fill="var(--ink)"
         style="font-variant-numeric:tabular-nums;">${Math.round(last.hit_rate * 100)}%</text>`;
  // x labels: every ~8th
  const step = Math.max(1, Math.ceil(n / 8));
  for (let i = 0; i < n; i += step) {
    s += `<text x="${x(i)}" y="${xLabY}" text-anchor="middle" font-size="11"
           fill="var(--ink3)">${daily[i].date.slice(5)}</text>`;
  }
  s += `<line id="xhair" x1="0" x2="0" y1="${rateTop}" y2="${volBot}"
         stroke="var(--ink3)" stroke-width="1" visibility="hidden"/>`;

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Daily hit rate and graded pick volume">${s}</svg>`;

  // crosshair + one tooltip for both panels; whole-slot hit target
  const svg = host.querySelector('svg');
  const xhair = svg.querySelector('#xhair');
  const tt = $('chart-tt');
  const show = (i, clientX, clientY) => {
    xhair.setAttribute('x1', x(i)); xhair.setAttribute('x2', x(i));
    xhair.setAttribute('visibility', 'visible');
    const d = daily[i];
    tt.replaceChildren();
    const date = document.createElement('div');
    date.className = 'tt-date'; date.textContent = d.date;
    tt.appendChild(date);
    const row = (key, val, lbl) => {
      const r = document.createElement('div'); r.className = 'tt-row';
      const k = document.createElement('span'); k.className = `tt-key ${key}`;
      const v = document.createElement('span'); v.className = 'tt-val';
      v.textContent = val;
      const l = document.createElement('span'); l.className = 'tt-lbl';
      l.textContent = lbl;
      r.append(k, v, l); tt.appendChild(r);
    };
    row('', `${Math.round(d.hit_rate * 100)}%`, `hit rate (${d.hits}/${d.graded})`);
    row('vol', String(d.graded), 'graded picks');
    tt.style.display = 'block';
    const card = $('chart-card').getBoundingClientRect();
    const ttW = tt.offsetWidth;
    let px = clientX - card.left + 14;
    if (px + ttW > card.width - 8) px = clientX - card.left - ttW - 14;
    tt.style.left = `${px}px`;
    tt.style.top = `${Math.max(8, clientY - card.top - 20)}px`;
  };
  svg.addEventListener('pointermove', (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ((ev.clientX - box.left) / box.width) * W;
    const i = Math.min(n - 1, Math.max(0,
      Math.round((mx - L) / ((W - L - R) / n) - 0.5)));
    show(i, ev.clientX, ev.clientY);
  });
  svg.addEventListener('pointerleave', () => {
    xhair.setAttribute('visibility', 'hidden');
    tt.style.display = 'none';
  });
}

function renderAnalysisTable() {
  const xs = analysis.data?.settlements ?? [];
  $('analysis-table-meta').textContent =
    `${xs.length} settled events in window · probabilities are for the Over side`;
  if (!xs.length) {
    $('analysis-table').innerHTML =
      '<div class="empty">no settled results in this window yet.</div>';
    return;
  }
  const sym = { correct: '✓', wrong: '✕', push: '—', 'no-pick': '·' };
  const col = { correct: 'var(--good)', wrong: 'var(--bad)',
                push: 'var(--gold)', 'no-pick': 'var(--ink3)' };
  const rows = xs.map((r) => `<tr>
    <td class="num" style="white-space:nowrap;color:var(--ink2);">
      ${r.kickoff.slice(5, 10)} ${r.kickoff.slice(11, 16)}Z</td>
    <td style="font-weight:600;">${esc(r.home_player ?? r.home_club)}
      <span style="color:var(--ink3);">v</span>
      ${esc(r.away_player ?? r.away_club)}</td>
    <td class="num r">${r.line}</td>
    <td>${r.pick ? esc(r.pick) : '<span style="color:var(--ink3);">—</span>'}
      ${r.tier ? `<span class="tier ${r.tier}">${r.tier}</span>` : ''}</td>
    <td class="num r">${pct(r.model_p_over)}</td>
    <td class="num r">${pct(r.book_p_over)}</td>
    <td class="num r" style="color:${r.book_p_over != null
      ? (r.model_p_over - r.book_p_over >= 0 ? 'var(--good)' : 'var(--bad)') : 'var(--ink3)'};">
      ${r.book_p_over != null
        ? ((r.model_p_over - r.book_p_over) * 100).toFixed(1) : '—'}</td>
    <td class="num r" style="font-weight:700;">${r.result_total}</td>
    <td style="color:${col[r.outcome]};font-weight:600;white-space:nowrap;">
      ${sym[r.outcome]} ${r.outcome}</td>
  </tr>`).join('');
  $('analysis-table').innerHTML = `<table class="ledger">
    <thead><tr><th>Kickoff</th><th>Match</th><th class="r">Line</th><th>Pick</th>
      <th class="r">Model p(O)</th><th class="r">Book p(O)</th>
      <th class="r">Δ pts</th><th class="r">Total</th><th>Outcome</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function loadAnalysis() {
  renderRangeChips();
  $('chart-card').classList.add('loading'); // hold frame, no skeleton flash
  try {
    analysis.data = await getJSON(`/api/analysis?days=${analysis.days}`);
    renderHitrateChart();
    renderAnalysisTable();
  } finally {
    $('chart-card').classList.remove('loading');
  }
}

/* ---------- tabs ---------- */

function showTab() {
  const isAnalysis = location.hash === '#analysis';
  $('view-ledger').hidden = isAnalysis;
  $('view-analysis').hidden = !isAnalysis;
  $('tab-ledger').classList.toggle('active', !isAnalysis);
  $('tab-analysis').classList.toggle('active', isAnalysis);
  if (isAnalysis) void loadAnalysis();
}
window.addEventListener('hashchange', showTab);

/* ---------- orchestration ---------- */

async function refresh() {
  try {
    const [slate, metrics, players, settled, health] = await Promise.all([
      getJSON('/api/slate'), getJSON('/api/metrics?days=7'),
      getJSON('/api/players?days=7'), getJSON('/api/settlements?limit=16'),
      getJSON('/api/health'),
    ]);
    Object.assign(state, { slate, metrics, players, settled, health });
    renderStatus(); renderSlate(); renderScorecard(); renderPlayers();
    renderSettled();
    const mv = slate.find((f) => f.model_version)?.model_version;
    if (mv) $('foot-model').textContent = `model ${mv}`;
  } catch (e) {
    $('status-strip').innerHTML =
      `<span class="live">●</span><span class="item warn">API unreachable: ${esc(e.message)}</span>`;
  }
}

function connectSse() {
  const es = new EventSource('/api/events');
  es.addEventListener('update', () => void refresh());
  es.onerror = () => { es.close(); setTimeout(connectSse, 5000); };
}

$('theme-btn').onclick = () => {
  const el = document.documentElement;
  const dark = el.dataset.theme === 'dark';
  el.dataset.theme = dark ? 'light' : 'dark';
  $('theme-btn').textContent = dark ? '◐ DARK' : '◑ LIGHT';
};

showTab();
void refresh();
connectSse();
setInterval(() => { renderStatus(); renderSlate(); }, 30_000); // countdowns
