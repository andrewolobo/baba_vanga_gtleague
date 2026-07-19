/* The Totals Ledger — dumb renderer over the read-only API.
   All picks/tiers/values are server-derived and persisted; this file only
   formats them. SSE drives refresh; no client-side polling of data routes. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pct = (x, dp = 1) => x == null ? '—' : (x * 100).toFixed(dp) + '%';

/* ---------- time: render every timestamp in the viewer's own zone ----------
   Source values are UTC ISO strings (…+00:00 from the store), so new Date()
   parses the correct instant and the toLocale* calls project it into the
   local zone. TZ_LABEL (e.g. "GMT+3") is shown once in the status strip so
   the bare wall-clock times on cards and rows stay unambiguous. Day buckets
   for the daily charts use localDay too, so a row's date always agrees with
   the bucket it lands in. */
const TZ_LABEL = (() => {
  try {
    const p = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
      .formatToParts(new Date()).find((x) => x.type === 'timeZoneName');
    return p ? p.value : 'local time';
  } catch { return 'local time'; }
})();
const localDay = (iso) => {            // YYYY-MM-DD, local, lexically sortable
  const d = iso ? new Date(iso) : NaN;  // guard null -> epoch, not ''
  return isNaN(d) ? '' : d.toLocaleDateString('en-CA');
};
const localHM = (iso) => {             // HH:MM, 24h, local
  const d = iso ? new Date(iso) : NaN;
  return isNaN(d) ? '' : d.toLocaleTimeString('en-GB',
    { hour: '2-digit', minute: '2-digit' });
};
const localDHM = (iso) => {            // "MM-DD HH:MM", local
  const day = localDay(iso);
  return day ? `${day.slice(5)} ${localHM(iso)}` : '';
};

// x12: one shared display toggle for the whole app (both views), off unless
// the user opted in — persisted so Ledger and Performance always agree
// (docs/X12_UI.md). It only reveals fields the API already serves.
const state = { slate: [], metrics: null, players: [], settled: [],
                health: null, filter: 'all', line: 'all',
                x12: localStorage.getItem('x12ui') === '1' };

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
    <span class="item">times in <b>${TZ_LABEL}</b></span>
    ${gap > 1 ? `<span class="item warn">⚠ results gap ${gap}d — catch-up backfill in progress</span>` : ''}
    ${bad ? '<span class="item warn">⚠ pipeline attention needed</span>'
          : '<span class="item" style="color:var(--good);font-weight:600;">✓ pipeline healthy</span>'}`;
}

/* ---------- slate ---------- */

// 'value' = model beats the book's price by >= MIN_EDGE on some line.
// 'book'  = the book has priced this fixture at all (the rest are model-only
//           cards for league-scheduled games with no odds yet).
// 1x2 mode has no tiers (none exist until bands are quantiled on served
// picks) and no lines, so its chip set is its own.
const FILTERS_OU = ['all', 'strong', 'solid', 'lean', 'value', 'agree', 'book'];
const FILTERS_X12 = ['all', 'picks', 'value', 'agree', 'book'];
const FILTER_LABEL = { book: 'BOOK PRICED', agree: 'AGREE >50%' };
const filterSet = () => state.x12 ? FILTERS_X12 : FILTERS_OU;

/* 'agree' = model and book are confident about the SAME outcome: the model's
   top 1x2 outcome clears 50% and the book prices that outcome above 50% too.
   Two outcomes can't both exceed 50%, so both clearing it IS agreement — no
   separate same-side check needed. Spans every book-priced fixture; pick
   gate and value flag don't matter here. */
function x12Agree(fx) {
  const x = fx.x12;
  if (!fx.priced || !x?.book) return false;
  const side = ['home', 'draw', 'away'].reduce((a, b) =>
    (x[`p_${a}`] ?? 0) >= (x[`p_${b}`] ?? 0) ? a : b);
  return (x[`p_${side}`] ?? 0) > 0.5 && (x.book[side] ?? 0) > 0.5;
}

/* O/U flavour of the same idea, per line: model's heavier side of THIS line
   clears 50% and the book prices that same side above 50%. (The book's two
   sides can both sit above 50% with the vig, so the check stays on the
   model's side only.) */
function ouAgree(l) {
  if (!l || l.book_over == null || l.book_under == null) return false;
  const over = (l.p_over ?? 0) >= (l.p_under ?? 0);
  return (over ? l.p_over : l.p_under) > 0.5
    && (over ? l.book_over : l.book_under) > 0.5;
}

/* With a line selected the card must speak about THAT line, not the model's
   favourite — otherwise filtering to 2.5 would still show you the 4.5 pick. */
function headlineLine(fx) {
  if (state.line !== 'all') {
    return fx.lines.find((l) => String(l.line) === state.line) ?? null;
  }
  const picked = fx.lines.filter((l) => l.pick);
  if (!picked.length) return fx.lines[0] ?? null;
  return picked.sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))[0];
}

function renderChips() {
  const fs = filterSet();
  if (!fs.includes(state.filter)) state.filter = 'all'; // mode switch resets
  $('tier-chips').innerHTML = fs.map((f) =>
    `<button class="pill ${state.filter === f ? 'active' : ''}" data-f="${f}">
       ${FILTER_LABEL[f] ?? f.toUpperCase()}</button>`).join('');
  for (const b of $('tier-chips').querySelectorAll('button')) {
    b.onclick = () => { state.filter = b.dataset.f; renderSlate(); };
  }
}

/* Lines come and go with the slate, so rebuild each render and drop a
   selection the current slate no longer offers. */
function slateLines() {
  return [...new Set(state.slate.flatMap((fx) => fx.lines.map((l) => l.line)))]
    .sort((a, b) => a - b);
}

function renderLineChips() {
  if (state.x12) { // lines are a totals concept
    $('line-chips').innerHTML = '';
    return;
  }
  const lines = slateLines();
  if (state.line !== 'all' && !lines.some((l) => String(l) === state.line)) {
    state.line = 'all';
  }
  $('line-chips').innerHTML = [['all', 'ALL LINES'],
    ...lines.map((l) => [String(l), `O/U ${l}`])]
    .map(([v, label]) => `<button class="pill ${state.line === v ? 'active' : ''}"
       data-l="${v}">${label}</button>`).join('');
  for (const b of $('line-chips').querySelectorAll('button')) {
    b.onclick = () => { state.line = b.dataset.l; renderSlate(); };
  }
}

function slateVisible() {
  if (state.x12) {
    return state.slate.filter((fx) => {
      if (state.filter === 'picks') return !!fx.x12?.pick;
      if (state.filter === 'value') return !!fx.x12?.value;
      if (state.filter === 'agree') return x12Agree(fx);
      if (state.filter === 'book') return fx.priced;
      return true;
    });
  }
  return state.slate.filter((fx) => {
    const h = headlineLine(fx);
    if (!h) return false; // fixture doesn't offer the selected line
    if (state.filter === 'all') return true;
    if (state.filter === 'book') return fx.priced;
    // scope value to the selected line, so the chip and the card agree
    if (state.filter === 'value') {
      return state.line === 'all' ? fx.lines.some((l) => l.value) : h.value;
    }
    // same line-scoping as value: ALL LINES means any line agreeing qualifies
    if (state.filter === 'agree') {
      return state.line === 'all' ? fx.lines.some(ouAgree) : ouAgree(h);
    }
    return h.tier === state.filter;
  });
}

/* 1x2-mode fixture card: same frame as the O/U card, entirely 1x2 content —
   the toggle swaps markets, it never blends them. Confidence is shown raw as
   the headline chip; no tier chips, deliberately: bands don't exist until
   quantiled on served picks (docs/X12_SERVING.md). */
function x12FixtureCard(fx) {
  const x = fx.x12;
  const kick = new Date(fx.kickoff);
  const mins = Math.max(0, Math.round((kick - Date.now()) / 60000));
  const hh = localHM(fx.kickoff);
  const covWarn = fx.coverage !== 'full';
  return `<article class="card">
    <div class="top"><span><b>${hh}</b> · in ${mins}m</span>
      <span>${esc(fx.competition)}</span></div>
    <div class="pair">${esc(fx.home.club)} <b>${esc(fx.home.player ?? '?')}</b></div>
    <div class="pair" style="margin-top:2px;">${esc(fx.away.club)}
      <b>${esc(fx.away.player ?? '?')}</b></div>
    <div class="headline">
      <span class="pick serif ${x?.pick ? '' : 'nopick'}">
        ${x?.pick ? esc(x.pick.toUpperCase()) : '1X2'}</span>
      ${x?.pick ? `<span class="tier solid">${pct(x.confidence)}</span>` : ''}
    </div>
    ${x ? `
    <div class="bar x12-bar" title="home / draw / away">
      <div class="h" style="width:${x.p_home * 100}%"></div>
      <div class="d" style="width:${x.p_draw * 100}%"></div>
      <div class="a" style="width:${x.p_away * 100}%"></div>
    </div>
    <div class="kv"><span>Model H/D/A</span>
      <span class="num">${pct(x.p_home)} / ${pct(x.p_draw)} / ${pct(x.p_away)}</span></div>
    ${fx.priced ? `
    <div class="kv"><span>Book H/D/A</span>
      <span class="num">${pct(x.book.home)} / ${pct(x.book.draw)} / ${pct(x.book.away)}</span></div>
    <div class="kv"><span>Odds</span>
      <span class="num">${x.odds.home ?? '—'} / ${x.odds.draw ?? '—'} / ${x.odds.away ?? '—'}</span></div>
    ${x.edge != null ? `<div class="kv"><span>Edge (model side)</span>
      <span class="num" style="color:${x.edge >= 0 ? 'var(--good)' : 'var(--bad)'}">
      ${x.edge >= 0 ? '+' : ''}${(x.edge * 100).toFixed(1)} pts</span></div>` : ''}`
    : `<div class="kv"><span>Book</span>
      <span class="num" style="color:var(--ink3);">awaiting odds</span></div>`}`
    : `<div class="kv" style="margin-top:10px;">
      <span style="color:var(--ink3);">no 1x2 data yet</span></div>`}
    <div class="badges">
      ${x?.value ? '<span class="badge-value">▲ VALUE</span>' : ''}
      ${!fx.priced ? '<span class="badge-cov">◌ model-only · book not yet priced</span>'
        : `<span class="badge-cov ${covWarn ? 'warn' : ''}">
        ${covWarn ? '◌ coverage ' + esc(fx.coverage) : '● full coverage'}</span>`}
      <span style="margin-left:auto;color:var(--ink3);">λ ${fx.lambda_home ?? '—'}
        / ${fx.lambda_away ?? '—'}</span>
    </div>
    ${oddsButtons(fx)}
  </article>`;
}

function fixtureCard(fx) {
  const h = headlineLine(fx);
  const kick = new Date(fx.kickoff);
  const mins = Math.max(0, Math.round((kick - Date.now()) / 60000));
  const hh = localHM(fx.kickoff);
  const pickTxt = h?.pick
    ? `${h.pick.toUpperCase()} ${h.line}`
    : `O/U ${h?.line ?? '—'}`;
  const over = (h?.p_over ?? 0) * 100, push = (h?.p_push ?? 0) * 100,
        under = (h?.p_under ?? 0) * 100;
  const covWarn = fx.coverage !== 'full';
  return `<article class="card">
    <div class="top"><span><b>${hh}</b> · in ${mins}m</span>
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
    ${oddsButtons(fx)}
  </article>`;
}

function renderSlate() {
  renderChips();
  renderLineChips();
  const vis = slateVisible();
  const nPriced = state.slate.filter((fx) => fx.priced).length;
  const nPicks = state.x12
    ? state.slate.filter((fx) => fx.x12?.pick).length
    : state.slate.filter((fx) => headlineLine(fx)?.pick).length;
  const scope = state.x12 || state.line === 'all' ? state.filter
    : `${state.filter} · O/U ${state.line}`;
  $('slate-meta').textContent =
    `${state.slate.length} upcoming · ${nPriced} priced by book`
    + ` · ${nPicks} ${state.x12 ? '1x2 ' : ''}picks`
    + ` · showing ${vis.length} · filter: ${scope}`;
  $('slate').innerHTML = vis.length
    ? vis.map(state.x12 ? x12FixtureCard : fixtureCard).join('')
    : `<div class="empty" style="grid-column:1/-1;">no fixtures match —
       the book prices ~1h ahead, so an empty slate usually just means
       between-rounds. It refreshes automatically.</div>`;
}

/* ---------- betslip (paper trading) ----------

   The one write path in the UI. Odds buttons render on PRICED cards only and
   respect the 1X2 swap rule (O/U buttons xor 1x2 buttons, never both). Clicks
   reach the slip via delegation on #slate (which persists across renders); the
   selected state is rebuilt from `slip` on every renderSlate, so an SSE refresh
   that rebuilds the cards can never desync the buttons. The floating panel is
   its own root in index.html, outside every re-rendered container. */

const slip = (() => {
  try {
    const s = JSON.parse(localStorage.getItem('wagerSlip') || '{}');
    return { legs: Array.isArray(s.legs) ? s.legs : [], stake: s.stake ?? 1 };
  } catch { return { legs: [], stake: 1 }; }
})();
const saveSlip = () =>
  localStorage.setItem('wagerSlip', JSON.stringify(slip));

const legKey = (ev, market, line, selection) =>
  `${ev}|${market}|${line ?? ''}|${selection}`;
const inSlip = (ev, market, line, selection) =>
  slip.legs.some((l) => legKey(l.event_id, l.market, l.line, l.selection)
    === legKey(ev, market, line, selection));
const slateHas = (ev) => state.slate.some((f) => f.event_id === ev);

function oddsBtn(fx, market, line, selection, odds, label) {
  const active = inSlip(fx.event_id, market, line, selection);
  return `<button class="odds-btn${active ? ' active' : ''}"
    data-ev="${esc(fx.event_id)}" data-mkt="${market}" data-line="${line ?? ''}"
    data-sel="${selection}" data-odds="${odds}"
    >${esc(label)} <b>${odds}</b></button>`;
}

// Priced cards only; 1x2 view shows H/D/A, O/U view shows every priced line's
// two sides. The swap rule holds because state.x12 picks exactly one branch.
function oddsButtons(fx) {
  if (!fx.priced) return '';
  if (state.x12) {
    const o = fx.x12?.odds;
    if (!o || o.home == null) return '';
    return `<div class="oddsrow">${['home', 'draw', 'away'].map((s) =>
      o[s] != null
        ? oddsBtn(fx, '1x2', null, s, o[s], s[0].toUpperCase() + s.slice(1))
        : '').join('')}</div>`;
  }
  const rows = fx.lines
    .filter((l) => l.over_odds != null || l.under_odds != null)
    .map((l) => `<div class="oddsrow">
      ${l.over_odds != null ? oddsBtn(fx, 'ou', l.line, 'over', l.over_odds, `O ${l.line}`) : ''}
      ${l.under_odds != null ? oddsBtn(fx, 'ou', l.line, 'under', l.under_odds, `U ${l.line}`) : ''}
    </div>`).join('');
  return rows;
}

// One leg per event: an exact re-click toggles the leg off; a different
// selection on the same event replaces it (client mirror of the server rule).
function toggleLeg(ds) {
  const market = ds.mkt;
  const line = market === 'ou' ? Number(ds.line) : null;
  const selection = ds.sel;
  const ev = ds.ev;
  const key = legKey(ev, market, line, selection);
  const exact = slip.legs.findIndex((l) =>
    legKey(l.event_id, l.market, l.line, l.selection) === key);
  if (exact >= 0) {
    slip.legs.splice(exact, 1);
  } else {
    const fx = state.slate.find((f) => f.event_id === ev);
    slip.legs = slip.legs.filter((l) => l.event_id !== ev);
    slip.legs.push({
      event_id: ev, market, line, selection, odds: Number(ds.odds),
      home: fx?.home?.club ?? '', away: fx?.away?.club ?? '',
      home_player: fx?.home?.player ?? null, away_player: fx?.away?.player ?? null,
      kickoff: fx?.kickoff ?? null,
    });
  }
  saveSlip();
  renderSlate();  // rebuilds the active state on the fresh buttons
  renderSlip();
}

const selLabel = (l) => l.market === 'ou'
  ? `${l.selection === 'over' ? 'Over' : 'Under'} ${l.line}`
  : `${l.selection[0].toUpperCase()}${l.selection.slice(1)}`;

function showToast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { t.hidden = true; }, 2600);
}

function renderSlip() {
  const panel = $('slip');
  if (!slip.legs.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const combined = slip.legs.reduce((a, l) => a * l.odds, 1);
  const anyStale = slip.legs.some((l) => !slateHas(l.event_id));
  $('slip-legs').innerHTML = slip.legs.map((l, i) => {
    const stale = !slateHas(l.event_id);
    const match = l.home_player || l.away_player
      ? `${esc(l.home_player ?? l.home)} v ${esc(l.away_player ?? l.away)}`
      : `${esc(l.home)} v ${esc(l.away)}`;
    return `<div class="slip-leg${stale ? ' stale' : ''}">
      <div><div class="lg-sel">${esc(selLabel(l))}</div>
        <div class="lg-match">${match}</div>
        ${stale ? '<div class="lg-stale">kicked off — remove to place</div>' : ''}</div>
      <div class="lg-r"><span class="lg-odds">${l.odds}</span>
        <button class="lg-x" data-rm="${i}" aria-label="Remove leg">×</button></div>
    </div>`;
  }).join('');
  $('slip-odds').textContent = combined.toFixed(2);
  const stake = Number($('slip-stake').value) || 0;
  $('slip-return').textContent = (stake * combined).toFixed(2);
  $('slip-place').disabled = anyStale || stake <= 0;
}

async function placeSlip() {
  const msg = $('slip-msg');
  msg.hidden = true;
  if (slip.legs.some((l) => !slateHas(l.event_id))) {
    msg.textContent = 'Remove kicked-off legs before placing.';
    msg.hidden = false;
    return;
  }
  const stake = Number($('slip-stake').value);
  try {
    const r = await fetch('/api/wagers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        stake,
        legs: slip.legs.map((l) => ({
          event_id: l.event_id, market: l.market,
          line: l.line, selection: l.selection,
        })),
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg.textContent = data.error || `could not place bet (${r.status})`;
      msg.hidden = false;
      return;
    }
    slip.legs = [];
    saveSlip();
    renderSlate();
    renderSlip();
    showToast('Bet placed ✓');
    if (!$('view-wagers').hidden) void loadWagers();
  } catch {
    msg.textContent = 'network error — bet not placed';
    msg.hidden = false;
  }
}

function wireSlip() {
  // delegation: #slate persists, its cards are rebuilt each render
  $('slate').addEventListener('click', (ev) => {
    const b = ev.target.closest('.odds-btn');
    if (b) toggleLeg(b.dataset);
  });
  $('slip-legs').addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-rm]');
    if (!b) return;
    slip.legs.splice(Number(b.dataset.rm), 1);
    saveSlip();
    renderSlate();
    renderSlip();
  });
  $('slip-clear').onclick = () => {
    slip.legs = [];
    saveSlip();
    renderSlate();
    renderSlip();
  };
  $('slip-stake').value = slip.stake;
  $('slip-stake').addEventListener('input', () => {
    slip.stake = Number($('slip-stake').value) || 0;
    saveSlip();
    renderSlip();  // updates potential return + place enablement, no rebuild
  });
  $('slip-place').onclick = () => void placeSlip();
}

/* ---------- scorecard ---------- */

/* Two prediction populations, reported separately and never pooled
   (docs/POPULATION_SPLIT.md): model-only (schedule) picks are the product's
   real signal; book-priced picks are few by construction — the per-population
   map passes only calibrated-honest ones (~10% of rows, gate review
   2026-07-18), and they are not bettable signal until the Phase 4 edge
   gate clears. */
const tilesHtml = (tiles) => tiles.map(([k, v, cls]) =>
  `<div class="tile ${cls}"><div class="v num">${v}</div>
   <div class="k">${k}</div></div>`).join('');

function popTiles(m) {
  return tilesHtml([
    ['SETTLED', m.settled ?? 0, ''],
    ['GRADED PICKS', m.graded ?? 0, ''],
    ['HIT RATE', m.hit_rate != null ? pct(m.hit_rate) : '—', 'accent'],
    ['MODEL BRIER', m.brier_model?.toFixed(4) ?? '—', ''],
    ['REGEN AGREE', m.regen_agreement != null ? pct(m.regen_agreement, 0) : '—',
      m.regen_agreement != null && m.regen_agreement < 1 ? 'accent' : ''],
    ['LEAK FLAGS', m.leak_risk_count ?? 0, m.leak_risk_count ? 'accent' : ''],
  ]);
}

/* 1x2 scorecard rows: same population split as totals, never pooled with the
   O/U tiles above. Value tiles replace the tier table the totals side has —
   x12 has no tiers yet by design. */
function x12Tiles(m) {
  // regen's denominator is the graded picks, not the settled rows — name the
  // count so a 1-pick sample can't masquerade as a trend
  const n = m.graded ?? 0;
  return tilesHtml([
    ['SETTLED', m.settled ?? 0, ''],
    ['GRADED PICKS', m.graded ?? 0, ''],
    ['HIT RATE', m.hit_rate != null ? pct(m.hit_rate) : '—', 'accent'],
    ['VALUE PICKS', m.value?.n ?? 0, ''],
    ['VALUE HIT', m.value?.hit_rate != null ? pct(m.value.hit_rate) : '—', ''],
    [`REGEN AGREE · ${n} PICK${n === 1 ? '' : 'S'}`,
      m.regen_agreement != null ? pct(m.regen_agreement, 0) : '—',
      m.regen_agreement != null && m.regen_agreement < 1 ? 'accent' : ''],
  ]);
}

function popTierTable(m, withValue) {
  const rows = ['strong', 'solid', 'lean'].map((t) => {
    const x = m.tiers?.[t];
    return `<tr><td style="text-transform:capitalize;">${t}</td>
      <td class="num r">${x?.n ?? 0}</td>
      <td class="num r">${x?.hit_rate != null ? pct(x.hit_rate) : '—'}</td>
      <td style="width:40%;"><div class="minibar">
        <span style="width:${(x?.hit_rate ?? 0) * 100}%"></span></div></td></tr>`;
  }).join('');
  const v = m.value;
  // value needs a price to beat, so the row only exists for the priced side
  const valueRow = withValue
    ? `<tr><td>value flag</td><td class="num r">${v?.n ?? 0}</td>
        <td class="num r">${v?.hit_rate != null ? pct(v.hit_rate) : '—'}</td>
        <td><div class="minibar"><span style="width:${(v?.hit_rate ?? 0) * 100}%;
          background:var(--good);"></span></div></td></tr>`
    : '';
  return `<table class="ledger">
    <thead><tr><th>Tier</th><th class="r">Picks</th><th class="r">Hit</th><th></th></tr></thead>
    <tbody>${rows}${valueRow}</tbody></table>`;
}

function renderScorecard() {
  const m = state.metrics;
  if (!m?.schedule || !m?.priced) return;
  // masthead badge follows the active market
  const badgeRate = state.x12 ? m.x12?.schedule?.hit_rate : m.schedule.hit_rate;
  $('acc-badge').textContent = badgeRate != null ? pct(badgeRate, 0) : '—';
  $('acc-badge-k').textContent =
    state.x12 ? '7D 1X2 HIT · MODEL-ONLY' : '7D HIT · MODEL-ONLY';
  $('ou-scorecard').hidden = state.x12;
  $('x12-scorecard').hidden = !state.x12;
  if (state.x12) {
    if (m.x12) {
      $('tiles-x12-schedule').innerHTML = x12Tiles(m.x12.schedule);
      $('tiles-x12-priced').innerHTML = x12Tiles(m.x12.priced);
    }
    return;
  }
  $('tiles-schedule').innerHTML = popTiles(m.schedule);
  $('tiles-priced').innerHTML = popTiles(m.priced);
  $('tier-table-schedule').innerHTML = popTierTable(m.schedule, false);
  $('tier-table-priced').innerHTML = popTierTable(m.priced, true);
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
  const kick = s.start_time_utc ? localHM(s.start_time_utc) : '';
  // 1x2 mode grades the card on the x12 settlement; events settled before the
  // head went live have no x12 row and read as ungraded, not as wrong
  const x12Cls = s.x12_outcome === 'correct' || s.x12_outcome === 'wrong'
    ? s.x12_outcome : 'no-pick';
  const cls = state.x12 ? x12Cls : s.outcome;
  const tag = state.x12
    ? (s.x12_outcome == null ? '· not graded for 1x2'
       : { correct: '✓ 1x2 pick correct', wrong: '✕ 1x2 pick wrong',
           'no-pick': '· no 1x2 pick' }[s.x12_outcome])
    : { correct: '✓ pick correct', wrong: '✕ pick wrong',
        push: '— push', 'no-pick': '· no pick' }[s.outcome];
  const detail = state.x12
    ? (s.x12_outcome == null ? '—'
       : `${s.x12_pick ? esc(s.x12_pick) + ' · ' : ''}winner ${esc(s.x12_result)}`)
    : `${s.pick ? esc(s.pick) + ' ' + s.line : 'O/U ' + s.line}
        · total ${s.result_total}`;
  return `<article class="settled ${cls}">
    <div class="top meta" style="display:flex;justify-content:space-between;
      align-items:center;gap:6px;margin-bottom:8px;"><span>${kick}</span>
      ${s.population === 'schedule'
        ? '<span class="badge-cov" title="league-schedule game the book never priced — informational, not bettable">◌ model-only</span>'
        : ''}
      ${state.x12
        ? (s.x12_confidence != null
           ? `<span class="tier solid">${pct(s.x12_confidence)}</span>` : '')
        : (s.tier ? `<span class="tier ${s.tier}">${s.tier}</span>` : '')}</div>
    <div class="row"><span class="pair">${esc(s.home_club)}
      <b>${esc((s.home_raw.match(/\(([^)]*)\)/) || [])[1] ?? '')}</b></span>
      <span class="score num">${s.home_ft ?? '–'}</span></div>
    <div class="row" style="margin-top:2px;"><span class="pair">${esc(s.away_club)}
      <b>${esc((s.away_raw.match(/\(([^)]*)\)/) || [])[1] ?? '')}</b></span>
      <span class="score num">${s.away_ft ?? '–'}</span></div>
    <div style="display:flex;justify-content:space-between;margin-top:10px;
      padding-top:9px;border-top:1px solid var(--line);" class="meta">
      <span class="tag ${cls}">${tag}</span>
      <span>${detail}</span></div>
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

/* ---------- hit/miss chart (shared) ---------- */

const RANGES = [[7, '7D'], [30, '30D'], [90, '90D'], [365, 'ALL']];

/* One frame, two granularities: a selected kickoff date charts that day hour
   by hour (local); no date selection charts the current local week, Monday
   through Sunday. Both domains are FIXED — a quiet hour or day stays an
   honest gap on the axis instead of collapsing out of it. `correctOf` picks
   the grade column; null (a push) never enters the rates. */
function hitMissFrame(xs, dateSel, correctOf) {
  const mk = (label) => ({ label, graded: 0, hits: 0, pushes: 0 });
  let mode, title, buckets;
  if (dateSel) {
    mode = 'day';
    title = `${dateSel} · BY HOUR · ${TZ_LABEL}`;
    buckets = Array.from({ length: 24 },
      (_, h) => mk(`${String(h).padStart(2, '0')}:00`));
    // xs is already scoped to dateSel by the date filter upstream
    for (const r of xs) {
      const b = buckets[new Date(r.kickoff).getHours()];
      const c = correctOf(r);
      if (c == null) b.pushes += 1;
      else { b.graded += 1; b.hits += c; }
    }
  } else {
    mode = 'week';
    const mon = new Date(); mon.setHours(0, 0, 0, 0);
    mon.setDate(mon.getDate() - (mon.getDay() + 6) % 7); // back to Monday
    buckets = [];
    const byKey = new Map();
    for (let i = 0; i < 7; i++) {
      const d = new Date(mon); d.setDate(mon.getDate() + i);
      const key = d.toLocaleDateString('en-CA');
      const b = mk(`${d.toLocaleDateString('en-GB', { weekday: 'short' })
        } ${key.slice(5)}`);
      buckets.push(b); byKey.set(key, b);
    }
    const keys = [...byKey.keys()];
    title = `THIS WEEK · ${keys[0]} → ${keys[6]} · DAILY`;
    for (const r of xs) {
      const b = byKey.get(localDay(r.kickoff)); // rows outside the week drop
      if (!b) continue;
      const c = correctOf(r);
      if (c == null) b.pushes += 1;
      else { b.graded += 1; b.hits += c; }
    }
  }
  for (const b of buckets) {
    b.misses = b.graded - b.hits;
    b.hit_rate = b.graded ? b.hits / b.graded : null;
    b.miss_rate = b.graded ? b.misses / b.graded : null;
  }
  return { mode, title, buckets };
}

/* Two stacked panels, one shared x — never a dual axis: hit and miss rate
   lines on one 0–100% scale, volume as columns on its own count scale.
   `noun` names what `graded` counts, since suppressed rows chart would-be
   results rather than picks. */
function renderHitMissChart({ hostId, ttId, cardId, frame, noun }) {
  const host = $(hostId);
  const { buckets } = frame;
  const n = buckets.length;
  if (!buckets.some((b) => b.graded)) {
    host.innerHTML = `<div class="empty">${frame.mode === 'day'
      ? `no gradable ${noun} on this date — pick another date or loosen the
         filters above.`
      : `nothing gradable this week yet — the week view fills in as events
         settle.`}</div>`;
    return;
  }
  const W = 1000, H = 340, L = 46, R = 56;
  const rateTop = 26, rateBot = 200, volTop = 240, volBot = 306, xLabY = 330;
  const x = (i) => L + (i + 0.5) * ((W - L - R) / n);
  const yRate = (v) => rateBot - v * (rateBot - rateTop);
  const maxVol = Math.max(...buckets.map((b) => b.graded));
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
         fill="var(--ink2)" letter-spacing="1.5">${frame.title} · %</text>
        <text x="${L}" y="${volTop - 8}" font-size="10.5" fill="var(--ink2)"
         letter-spacing="1.5">${noun.toUpperCase()} · max ${maxVol}</text>`;
  // legend: swatches carry identity, text stays in ink tokens
  s += `<line x1="${W - R - 120}" x2="${W - R - 106}" y1="10" y2="10"
         stroke="var(--chartHit)" stroke-width="3" stroke-linecap="round"/>
        <text x="${W - R - 100}" y="14" font-size="10.5" fill="var(--ink2)"
         letter-spacing="1.5">HIT</text>
        <line x1="${W - R - 62}" x2="${W - R - 48}" y1="10" y2="10"
         stroke="var(--chartMiss)" stroke-width="3" stroke-linecap="round"/>
        <text x="${W - R - 42}" y="14" font-size="10.5" fill="var(--ink2)"
         letter-spacing="1.5">MISS</text>`;

  // volume columns: thin, rounded data-end, square baseline, surface gaps by slot
  const slot = (W - L - R) / n;
  const bw = Math.min(24, Math.max(3, slot * 0.55));
  for (let i = 0; i < n; i++) {
    if (!buckets[i].graded) continue;
    const h = volBot - yVol(buckets[i].graded);
    s += `<path d="M ${x(i) - bw / 2} ${volBot}
           v ${-Math.max(0, h - 4)} q 0 -4 4 -4 h ${bw - 8} q 4 0 4 4
           v ${Math.max(0, h - 4)} z" fill="var(--ink2)"/>`;
  }

  // contiguous stretches with data, so lines break honestly across empty
  // hours/days instead of bridging them
  const runs = [];
  let cur = [];
  buckets.forEach((b, i) => {
    if (b.graded) cur.push(i);
    else if (cur.length) { runs.push(cur); cur = []; }
  });
  if (cur.length) runs.push(cur);

  // rate lines: 2px round, markers with 2px surface ring; hit drawn last so
  // it sits on top where the two cross
  for (const [rateKey, color] of [['miss_rate', 'var(--chartMiss)'],
                                  ['hit_rate', 'var(--chartHit)']]) {
    for (const run of runs) {
      s += `<polyline points="${run.map((i) =>
             `${x(i)},${yRate(buckets[i][rateKey])}`).join(' ')}" fill="none"
             stroke="${color}" stroke-width="2" stroke-linejoin="round"
             stroke-linecap="round"/>`;
      for (const i of run) {
        s += `<circle cx="${x(i)}" cy="${yRate(buckets[i][rateKey])}" r="4.5"
               fill="${color}" stroke="var(--surface)" stroke-width="2"/>`;
      }
    }
  }
  // selective direct labels: last bucket with data only (text token, not
  // series color); complementary rates can meet at 50/50, so dodge overlap
  const li = runs[runs.length - 1][runs[runs.length - 1].length - 1];
  const lb = buckets[li];
  let yH = yRate(lb.hit_rate) + 4, yM = yRate(lb.miss_rate) + 4;
  if (Math.abs(yH - yM) < 15) {
    const mid = (yH + yM) / 2, sign = yH <= yM ? 1 : -1;
    yH = mid - sign * 7.5; yM = mid + sign * 7.5;
  }
  s += `<text x="${x(li) + 9}" y="${yH}" font-size="12.5" font-weight="700"
         fill="var(--ink)" style="font-variant-numeric:tabular-nums;"
        >${Math.round(lb.hit_rate * 100)}%</text>
        <text x="${x(li) + 9}" y="${yM}" font-size="12.5" font-weight="700"
         fill="var(--ink)" style="font-variant-numeric:tabular-nums;"
        >${Math.round(lb.miss_rate * 100)}%</text>`;
  // x labels: every 3rd hour in day mode, every day in week mode
  const step = frame.mode === 'day' ? 3 : 1;
  for (let i = 0; i < n; i += step) {
    s += `<text x="${x(i)}" y="${xLabY}" text-anchor="middle" font-size="11"
           fill="var(--ink3)">${frame.mode === 'day'
             ? buckets[i].label.slice(0, 2) : buckets[i].label}</text>`;
  }
  s += `<line id="xhair" x1="0" x2="0" y1="${rateTop}" y2="${volBot}"
         stroke="var(--ink3)" stroke-width="1" visibility="hidden"/>`;

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Hit and miss rates ${frame.mode === 'day'
      ? 'by hour of the selected day' : 'by day of the current week'}">${s}</svg>`;

  // crosshair + one tooltip for both panels; whole-slot hit target
  const svg = host.querySelector('svg');
  const xhair = svg.querySelector('#xhair');
  const tt = $(ttId);
  const show = (i, clientX, clientY) => {
    xhair.setAttribute('x1', x(i)); xhair.setAttribute('x2', x(i));
    xhair.setAttribute('visibility', 'visible');
    const b = buckets[i];
    tt.replaceChildren();
    const date = document.createElement('div');
    date.className = 'tt-date'; date.textContent = b.label;
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
    if (b.graded) {
      row('hit', `${Math.round(b.hit_rate * 100)}%`,
          `hit (${b.hits}/${b.graded})`);
      row('miss', `${Math.round(b.miss_rate * 100)}%`,
          `miss (${b.misses}/${b.graded})`);
      row('vol', String(b.graded), noun);
    } else {
      row('vol', '0', `${noun} — nothing gradable`);
    }
    if (b.pushes) row('vol', String(b.pushes), 'ungradable (push)');
    tt.style.display = 'block';
    const card = $(cardId).getBoundingClientRect();
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

/* ---------- model performance tab ----------

   Every settled event, graded on the side the model leaned — whether or not
   that lean cleared the pick gate. Both populations are graded the same way,
   off /api/analysis's model_side_correct, which is derived from the result and
   never feeds the Ledger scorecard. That symmetry is the point: it is what
   makes picked and suppressed rows comparable at all.

   Rows the model had no coverage of (model_side null) served the book's own
   price back and are excluded — they carry no model opinion to grade. */

/* ---- pagination (both settled-event tables) ----
   The size choice persists per table; the index is transient and clamped in
   pageSlice on every render, so a filter change that shrinks the result set
   can never strand the view past the last page. */
const PAGE_SIZES = ['10', '25', '50', '100', 'all'];
const pagerSize = (key) => {
  const v = localStorage.getItem(key);
  return PAGE_SIZES.includes(v) ? v : '25';
};

function pageSlice(xs, page) {
  if (page.size === 'all') {
    page.index = 0;
    return { rows: xs, pages: 1, start: 0 };
  }
  const size = Number(page.size);
  const pages = Math.max(1, Math.ceil(xs.length / size));
  page.index = Math.min(Math.max(page.index, 0), pages - 1);
  const start = page.index * size;
  return { rows: xs.slice(start, start + size), pages, start };
}

function pagerHtml(page, pages, total, start, shown) {
  const opts = PAGE_SIZES.map((s) =>
    `<option value="${s}"${page.size === s ? ' selected' : ''}>${
      s === 'all' ? 'All' : s} rows</option>`).join('');
  return `<div class="pager">
    <span class="meta num">${start + 1}–${start + shown} of ${total}</span>
    <select data-pg="size" aria-label="Rows per page">${opts}</select>
    <span style="margin-left:auto;"></span>
    <button class="pill" data-pg="prev" ${page.index === 0 ? 'disabled' : ''}
      >&#8249; PREV</button>
    <span class="meta num">page ${page.index + 1} of ${pages}</span>
    <button class="pill" data-pg="next"
      ${page.index >= pages - 1 ? 'disabled' : ''}>NEXT &#8250;</button>
  </div>`;
}

// delegation, like wireSort: the pager is rebuilt with the table on every
// render, its host container is not
function wirePager(hostId, page, storeKey, rerender) {
  const host = $(hostId);
  host.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-pg]');
    if (!b || b.disabled) return;
    page.index += b.dataset.pg === 'next' ? 1 : -1;
    rerender();
  });
  host.addEventListener('input', (ev) => {
    if (ev.target.dataset?.pg !== 'size') return;
    page.size = ev.target.value;
    page.index = 0;
    localStorage.setItem(storeKey, page.size);
    rerender();
  });
}

const perf = { days: 30, data: null,
               sort: { key: 'kickoff', dir: 'desc' },
               page: { size: pagerSize('perfRows'), index: 0 },
               filters: { match: '', date: '', population: 'all', gate: 'all',
                          line: 'all', side: 'all', model: 'all', book: 'all',
                          outcome: 'all' } };
const P_FILTER_IDS = { match: 'p-match', date: 'p-date', population: 'p-pop',
                       gate: 'p-gate', line: 'p-line', side: 'p-side',
                       model: 'p-model', book: 'p-book',
                       outcome: 'p-outcome' };
// filters whose empty state is '' (the rest use the 'all' sentinel)
const TEXT_FILTERS = new Set(['match', 'date']);

const outcomeOf = (r) => r.model_side_correct == null ? 'push'
  : r.model_side_correct ? 'hit' : 'miss';

/* Probability filter values: 'all', 'ge50'/'lt50' (the headline split), or a
   percent band 'lo-hi' — half-open [lo, hi) so the bands tile without overlap,
   except the top band which closes at 100. A row with no probability (e.g. no
   book price on a model-only game) never matches an active filter — use the
   population filter to see those rows. */
function probInBand(p, v) {
  if (v === 'all') return true;
  if (p == null) return false;
  if (v === 'ge50') return p >= 0.5;
  if (v === 'lt50') return p < 0.5;
  const [lo, hi] = v.split('-').map(Number);
  return p >= lo / 100 && (hi === 100 ? p <= 1 : p < hi / 100);
}
const isPick = (r) => r.pick != null;

function perfRows() {
  return (perf.data?.settlements ?? []).filter((r) => r.model_side != null);
}

function perfFiltersActive() {
  const f = perf.filters;
  return !!f.match.trim() || !!f.date || f.gate !== 'all' || f.line !== 'all'
    || f.side !== 'all' || f.model !== 'all' || f.book !== 'all'
    || f.outcome !== 'all';
}

function perfMatches(r) {
  const f = perf.filters;
  // same local-day basis as the chart, so the filtered table and the daily
  // point for that date always agree
  if (f.date && localDay(r.kickoff) !== f.date) return false;
  if (f.population !== 'all' && r.population !== f.population) return false;
  if (f.gate === 'picks' && !isPick(r)) return false;
  if (f.gate === 'nopicks' && isPick(r)) return false;
  if (f.line !== 'all' && String(r.line) !== f.line) return false;
  if (f.side !== 'all' && r.model_side !== f.side) return false;
  if (!probInBand(r.model_p_over, f.model)) return false;
  if (!probInBand(r.book_p_over, f.book)) return false;
  if (f.outcome !== 'all' && outcomeOf(r) !== f.outcome) return false;
  const q = f.match.trim().toLowerCase();
  if (q) {
    const hay = [r.home_player, r.away_player, r.home_club, r.away_club]
      .filter(Boolean).join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

/* Pushes are ungradable, so every rate divides by the graded subset only —
   never by row count, which would silently deflate each rate. */
const graded = (xs) => xs.filter((r) => r.model_side_correct != null);
const hitRate = (xs) => xs.length
  ? xs.reduce((a, r) => a + r.model_side_correct, 0) / xs.length : null;
const brierOf = (xs) => xs.length
  ? xs.reduce((a, r) =>
      a + (r.model_p_over - (r.result_total > r.line ? 1 : 0)) ** 2, 0) / xs.length
  : null;

/* The caveat is conditional: it is only a counterfactual when suppressed rows
   are in scope. Showing it over a picks-only view would be a lie. */
function renderPerfCaveat(xs) {
  const sup = xs.filter((r) => !isPick(r)).length;
  const sched = xs.filter((r) => r.population === 'schedule').length;
  const el = $('perf-caveat');
  if (!sup && !sched) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const parts = [];
  if (sup) {
    parts.push(sup === xs.length
      ? `Every row in scope is a <b>no-pick</b> — suppressed by the gate and
         never bet. These rates are counterfactual: what would have happened
         had the gate been open.`
      : `${sup} of ${xs.length} rows in scope are <b>no-picks</b> that were
         never bet. Their contribution to these rates is counterfactual — use
         the <b>Picked vs suppressed</b> breakdown to keep the two apart.`);
  }
  if (sched) {
    parts.push(`${sched === xs.length
      ? 'Every row in scope is'
      : `${sched} of ${xs.length} rows in scope are`} <b>model-only</b> —
       league-schedule games the book never priced. Their wins are
       informational, not bettable, and the two populations are calibrated
       separately (never pool their rates).`);
  }
  el.innerHTML = parts.join(' ');
}

function renderPerfTiles(xs) {
  const g = graded(xs);
  const rate = hitRate(g);
  const conf = xs.length
    ? xs.reduce((a, r) => a + (r.confidence ?? 0), 0) / xs.length : null;
  const over = xs.length
    ? xs.filter((r) => r.model_side === 'over').length / xs.length : null;
  const tiles = [
    ['EVENTS', xs.length, ''],
    ['GRADABLE', g.length, ''],
    ['MODEL SIDE HIT', rate != null ? pct(rate) : '—', 'accent'],
    ['MODEL BRIER', brierOf(g)?.toFixed(4) ?? '—', ''],
    ['AVG CONFIDENCE', conf != null ? pct(conf) : '—', ''],
    ['LEANED OVER', over != null ? pct(over, 0) : '—', ''],
  ];
  $('perf-tiles').innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="tile ${cls}"><div class="v num">${v}</div>
     <div class="k">${k}</div></div>`).join('');
}

/* One renderer for all the breakdowns: bucket the rows, show gradable count
   and hit rate, bar coloured against the 50% coin-flip line. `correctOf`
   picks the grade column — totals use model_side_correct, 1x2 side_correct. */
function renderBreakdown(hostId, head, buckets, empty,
                         correctOf = (r) => r.model_side_correct) {
  const rows = buckets.filter(([, xs]) => xs.length).map(([label, xs]) => {
    const g = xs.filter((r) => correctOf(r) != null);
    const rate = g.length
      ? g.reduce((a, r) => a + correctOf(r), 0) / g.length : null;
    return `<tr><td style="text-transform:capitalize;">${esc(label)}</td>
      <td class="num r">${g.length}</td>
      <td class="num r">${rate != null ? pct(rate) : '—'}</td>
      <td style="width:34%;"><div class="minibar">
        <span style="width:${(rate ?? 0) * 100}%;
          background:${(rate ?? 0) >= 0.5 ? 'var(--good)' : 'var(--bad)'};">
        </span></div></td></tr>`;
  }).join('');
  $(hostId).innerHTML = rows
    ? `<table class="ledger"><thead><tr><th>${head}</th>
         <th class="r">N</th><th class="r">Hit</th><th></th></tr></thead>
       <tbody>${rows}</tbody></table>`
    : `<div class="empty">${empty}</div>`;
}

function renderPerfBreakdowns(xs) {
  renderBreakdown('perf-pop-table', 'Population', [
    ['model-only', xs.filter((r) => r.population === 'schedule')],
    ['book-priced', xs.filter((r) => r.population === 'priced')],
  ], 'nothing in scope.');

  renderBreakdown('perf-gate-table', 'Gate', [
    ['picked', xs.filter(isPick)],
    ['suppressed', xs.filter((r) => !isPick(r))],
  ], 'nothing in scope.');

  const lines = [...new Set(xs.map((r) => r.line))].sort((a, b) => a - b);
  renderBreakdown('perf-line-table', 'Line',
    lines.map((l) => [String(l), xs.filter((r) => r.line === l)]),
    'nothing in scope.');

  renderBreakdown('perf-lean-table', 'Lean', [
    ['over', xs.filter((r) => r.model_side === 'over')],
    ['under', xs.filter((r) => r.model_side === 'under')],
  ], 'nothing in scope.');
}

/* ---- sortable "Every settled event" table ----
   Accessors return the sortable value; null means "no value" and sorts last
   in BOTH directions (a missing book price is not a small edge). */
const PERF_SORT_KEYS = {
  kickoff: (r) => r.kickoff,
  match: (r) => (r.home_player ?? r.home_club ?? '').toLowerCase(),
  line: (r) => r.line,
  lean: (r) => r.model_side,
  conf: (r) => r.confidence,
  model: (r) => r.model_p_over,
  book: (r) => r.book_p_over,
  edge: (r) => r.book_p_over == null ? null : r.model_p_over - r.book_p_over,
  total: (r) => r.result_total,
  outcome: (r) => outcomeOf(r),
};

function sortRows(xs, { key, dir }, keys) {
  const acc = keys[key] ?? keys.kickoff;
  const sign = dir === 'asc' ? 1 : -1;
  return [...xs].sort((a, b) => {
    const va = acc(a), vb = acc(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return va < vb ? -sign : va > vb ? sign : 0;
  });
}

const perfSorted = (xs) => sortRows(xs, perf.sort, PERF_SORT_KEYS);

// delegation: tables are rebuilt on every render, their containers are not
function wireSort(hostId, sort, rerender) {
  $(hostId).addEventListener('click', (ev) => {
    const th = ev.target.closest('th[data-key]');
    if (!th) return;
    const key = th.dataset.key;
    if (sort.key === key) {
      sort.dir = sort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      // fresh column: newest-first for time, ascending for everything else
      sort.key = key;
      sort.dir = key === 'kickoff' ? 'desc' : 'asc';
    }
    rerender();
  });
}

// player leads (it's the sort key); the club tags along muted so newly
// aliased teams are visible without widening the column for club-only rows
const teamCell = (player, club) => player == null ? esc(club ?? '—')
  : club == null ? esc(player)
  : `${esc(player)} <span style="color:var(--ink3);font-weight:400;"
       >· ${esc(club)}</span>`;

const thBuilder = (sort) => (key, label, right = false) => {
  const active = sort.key === key;
  const arrow = active ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
  return `<th class="${right ? 'r ' : ''}sortable${active ? ' sorted' : ''}"
    data-key="${key}" title="sort by ${label}">${label}${arrow}</th>`;
};

function renderPerfTable(xs) {
  xs = perfSorted(xs);
  const pg = pageSlice(xs, perf.page);
  const cls = { hit: 'wb-hit', miss: 'wb-miss', push: 'wb-push' };
  const sym = { hit: '✓', miss: '✕', push: '—' };
  // a suppressed row's outcome is hypothetical; a pick's is what happened
  const label = (r, o) => isPick(r)
    ? { hit: 'hit', miss: 'miss', push: 'push' }[o]
    : { hit: 'would hit', miss: 'would miss', push: 'would push' }[o];
  // the settlement join is LEFT, so a score can be absent even with a total
  const score = (r) => r.home_ft == null || r.away_ft == null
    ? '<span style="color:var(--ink3);">—</span>'
    : `${r.home_ft}–${r.away_ft}`;
  const rows = pg.rows.map((r) => {
    const o = outcomeOf(r);
    const edge = r.book_p_over != null ? r.model_p_over - r.book_p_over : null;
    return `<tr>
      <td class="num" style="white-space:nowrap;color:var(--ink2);">
        ${localDHM(r.kickoff)}</td>
      <td style="font-weight:600;">${r.population === 'schedule'
        ? '<span title="model-only · league schedule, no book price" style="color:var(--ink3);">◌ </span>'
        : ''}${teamCell(r.home_player, r.home_club)}
        <span style="color:var(--ink3);">v</span>
        ${teamCell(r.away_player, r.away_club)}</td>
      <td class="num r">${r.line}</td>
      <td>${isPick(r)
        ? `${esc(r.model_side)}${r.tier
            ? ` <span class="tier ${r.tier}">${r.tier}</span>` : ''}`
        : `<span class="lean" title="below the pick gate — never bet"
             >${esc(r.model_side)}</span>`}</td>
      <td class="num r">${pct(r.confidence)}</td>
      <td class="num r">${pct(r.model_p_over)}</td>
      <td class="num r">${pct(r.book_p_over)}</td>
      <td class="num r" style="color:${edge == null ? 'var(--ink3)'
        : edge >= 0 ? 'var(--good)' : 'var(--bad)'};">
        ${edge == null ? '—' : (edge * 100).toFixed(1)}</td>
      <td class="num r" style="white-space:nowrap;">${score(r)}</td>
      <td class="num r" style="font-weight:700;">${r.result_total}</td>
      <td class="${cls[o]}" style="white-space:nowrap;">${sym[o]} ${label(r, o)}</td>
    </tr>`;
  }).join('');
  const th = thBuilder(perf.sort);
  $('perf-table').innerHTML = `<table class="ledger">
    <thead><tr>${th('kickoff', 'Kickoff')}${th('match', 'Match')}
      ${th('line', 'Line', true)}${th('lean', 'Leaned')}
      ${th('conf', 'Conf', true)}${th('model', 'Model p(O)', true)}
      ${th('book', 'Book p(O)', true)}${th('edge', 'Δ pts', true)}
      <th class="r">Score</th>${th('total', 'Total', true)}
      ${th('outcome', 'Outcome')}</tr></thead>
    <tbody>${rows}</tbody></table>`
    + pagerHtml(perf.page, pg.pages, xs.length, pg.start, pg.rows.length);
}

function renderPerf() {
  const all = perfRows();
  const xs = all.filter(perfMatches);
  const pushes = xs.length - graded(xs).length;
  const picks = xs.filter(isPick).length;
  $('perf-table-meta').textContent =
    (perfFiltersActive()
      ? `${xs.length} of ${all.length} settled events shown`
      : `${all.length} settled events in window`)
    + ` · ${picks} picked, ${xs.length - picks} suppressed`
    + (pushes ? ` · ${pushes} ungradable (push)` : '')
    + ' · click a column header to sort';

  renderPerfCaveat(xs);
  renderPerfTiles(xs);
  renderPerfBreakdowns(xs);
  // the chart still follows the filters — the date filter is what flips it
  // from the current-week view to the single-day hourly view
  renderHitMissChart({
    hostId: 'perf-chart', ttId: 'perf-chart-tt', cardId: 'perf-chart-card',
    frame: hitMissFrame(xs, perf.filters.date, (r) => r.model_side_correct),
    noun: 'gradable events',
  });

  if (xs.length) {
    renderPerfTable(xs);
  } else {
    $('perf-table').innerHTML = all.length
      ? '<div class="empty">no settled events match these filters.</div>'
      : '<div class="empty">no settled events in this window yet.</div>';
  }
  renderX12Perf();
}

function syncPerfChrome() {
  for (const [key, id] of Object.entries(P_FILTER_IDS)) {
    const el = $(id);
    const on = TEXT_FILTERS.has(key) ? !!perf.filters[key].trim()
      : perf.filters[key] !== 'all';
    el.classList.toggle('on', on);
  }
  $('p-reset').hidden = !perfFiltersActive();
}

function syncPerfLineOptions() {
  const lines = [...new Set(perfRows().map((r) => r.line))].sort((a, b) => a - b);
  if (perf.filters.line !== 'all'
      && !lines.some((l) => String(l) === perf.filters.line)) {
    perf.filters.line = 'all';
  }
  const sel = $('p-line');
  sel.replaceChildren();
  sel.add(new Option('All lines', 'all'));
  for (const l of lines) sel.add(new Option(`Line ${l}`, String(l)));
  sel.value = perf.filters.line;
}

function renderPerfRangeChips() {
  // both markets share one window; the chips render into whichever block is
  // visible (and both, so the state survives a mode switch)
  const html = RANGES.map(([d, label]) =>
    `<button class="pill ${perf.days === d ? 'active' : ''}" data-d="${d}">
       ${label}</button>`).join('');
  for (const id of ['perf-range', 'x12-range']) {
    $(id).innerHTML = html;
    for (const b of $(id).querySelectorAll('button')) {
      b.onclick = () => { perf.days = Number(b.dataset.d); void loadPerf(); };
    }
  }
}

function wirePerfFilters() {
  for (const [key, id] of Object.entries(P_FILTER_IDS)) {
    $(id).addEventListener('input', (ev) => {
      perf.filters[key] = ev.target.value;
      perf.page.index = 0; // a new frame starts on its first page
      syncPerfChrome();
      renderPerf();
    });
  }
  $('p-reset').onclick = () => {
    perf.filters = { match: '', date: '', population: 'all', gate: 'all',
                     line: 'all', side: 'all', model: 'all', book: 'all',
                     outcome: 'all' };
    perf.page.index = 0;
    for (const [key, id] of Object.entries(P_FILTER_IDS)) {
      $(id).value = TEXT_FILTERS.has(key) ? '' : 'all';
    }
    syncPerfChrome();
    renderPerf();
  };
}

async function loadPerf() {
  renderPerfRangeChips();
  $('perf-chart-card').classList.add('loading');
  try {
    perf.data = await getJSON(`/api/analysis?days=${perf.days}`);
    syncPerfLineOptions();
    syncX12ClubOptions();
    syncPerfChrome();
    syncX12Chrome();
    renderPerf();
  } finally {
    $('perf-chart-card').classList.remove('loading');
  }
}

/* ---------- 1x2 on the performance tab (docs/X12_UI.md Phase 3) ----------

   Same symmetry as the totals view above: every x12-settled event, graded on
   the model's top outcome whether or not it cleared the 0.50 pick gate —
   side_correct is the shadow grade and never feeds the Ledger scorecard.
   A 3-way market has no pushes, so every settled row grades. Data rides in
   on /api/analysis (perf.data.x12) and follows the shared range chips. */

const X12_OUTCOMES = ['home', 'draw', 'away'];
const x12perf = { sort: { key: 'kickoff', dir: 'desc' },
                  page: { size: pagerSize('x12Rows'), index: 0 },
                  filters: { match: '', date: '', club: 'all',
                             population: 'all', gate: 'all', side: 'all',
                             model: 'all', book: 'all', outcome: 'all' } };
const X_FILTER_IDS = { match: 'x-match', date: 'x-date', club: 'x-club',
                       population: 'x-pop', gate: 'x-gate', side: 'x-side',
                       model: 'x-model', book: 'x-book',
                       outcome: 'x-outcome' };

const x12PerfRows = () => perf.data?.x12 ?? [];
const x12TopP = (r) => Math.max(r.p_home, r.p_draw, r.p_away);
const x12BookP = (r) => r[`book_p_${r.side}`];

/* Filters scope the whole section — tiles, breakdowns, chart and table all
   read the same filtered frame, like the totals view above. `side` is the
   model's read (its top outcome), picked or not. */
function x12Matches(r) {
  const f = x12perf.filters;
  if (f.date && localDay(r.kickoff) !== f.date) return false;
  if (f.population !== 'all' && r.population !== f.population) return false;
  if (f.gate === 'picks' && r.pick == null) return false;
  if (f.gate === 'nopicks' && r.pick != null) return false;
  if (f.side !== 'all' && r.side !== f.side) return false;
  // same bands as the totals view: model = confidence in its read (top
  // outcome), book = the book's price for that same read. Model-only rows
  // have no book price, so an active book filter excludes them.
  if (!probInBand(x12TopP(r), f.model)) return false;
  if (!probInBand(x12BookP(r), f.book)) return false;
  if (f.outcome !== 'all') {
    // same four-way split the table's outcome column shows: picked rows are
    // hit/miss, gate-suppressed rows are the hypothetical would-hit/would-miss
    const o = (r.pick != null ? '' : 'would-')
      + (r.side_correct ? 'hit' : 'miss');
    if (o !== f.outcome) return false;
  }
  if (f.club !== 'all' && r.home_club !== f.club && r.away_club !== f.club) {
    return false;
  }
  const q = f.match.trim().toLowerCase();
  if (q) {
    const hay = [r.home_player, r.away_player, r.home_club, r.away_club]
      .filter(Boolean).join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function x12FiltersActive() {
  const f = x12perf.filters;
  return !!f.match.trim() || !!f.date || f.club !== 'all'
    || f.population !== 'all' || f.gate !== 'all' || f.side !== 'all'
    || f.model !== 'all' || f.book !== 'all' || f.outcome !== 'all';
}

function syncX12Chrome() {
  for (const [key, id] of Object.entries(X_FILTER_IDS)) {
    const on = TEXT_FILTERS.has(key) ? !!x12perf.filters[key].trim()
      : x12perf.filters[key] !== 'all';
    $(id).classList.toggle('on', on);
  }
  $('x-reset').hidden = !x12FiltersActive();
}

/* The team dropdown lists whatever clubs appear in the loaded window, so a
   newly aliased club shows up as soon as it has a settled 1x2 row. Same
   clamp-then-rebuild shape as syncPerfLineOptions. */
function syncX12ClubOptions() {
  const clubs = [...new Set(x12PerfRows()
    .flatMap((r) => [r.home_club, r.away_club]).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
  if (x12perf.filters.club !== 'all'
      && !clubs.includes(x12perf.filters.club)) {
    x12perf.filters.club = 'all';
  }
  const sel = $('x-club');
  sel.replaceChildren();
  sel.add(new Option('All teams', 'all'));
  for (const c of clubs) sel.add(new Option(c, c));
  sel.value = x12perf.filters.club;
}

function wireX12Filters() {
  for (const [key, id] of Object.entries(X_FILTER_IDS)) {
    $(id).addEventListener('input', (ev) => {
      x12perf.filters[key] = ev.target.value;
      x12perf.page.index = 0; // a new frame starts on its first page
      syncX12Chrome();
      renderX12Perf();
    });
  }
  $('x-reset').onclick = () => {
    x12perf.filters = { match: '', date: '', club: 'all', population: 'all',
                        gate: 'all', side: 'all', model: 'all', book: 'all',
                        outcome: 'all' };
    x12perf.page.index = 0;
    for (const [key, id] of Object.entries(X_FILTER_IDS)) {
      $(id).value = TEXT_FILTERS.has(key) ? '' : 'all';
    }
    syncX12Chrome();
    renderX12Perf();
  };
}

function renderX12Caveat(xs) {
  const el = $('x12-caveat');
  const sup = xs.filter((r) => r.pick == null).length;
  const sched = xs.filter((r) => r.population === 'schedule').length;
  if (!sup && !sched) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const parts = [];
  if (sup) {
    parts.push(sup === xs.length
      ? `Every row in scope is a <b>no-pick</b> — suppressed by the 0.50 gate
         and never bet. These rates are counterfactual: what would have
         happened had the gate been open.`
      : `${sup} of ${xs.length} rows in scope are <b>no-picks</b> that were
         never bet — their contribution to these rates is counterfactual. Use
         the <b>Picked vs suppressed</b> breakdown to keep the two apart.`);
  }
  if (sched) {
    parts.push(`${sched === xs.length
      ? 'Every row in scope is'
      : `${sched} of ${xs.length} rows in scope are`} <b>model-only</b> —
       league-schedule games the book never priced. Their wins are
       informational, not bettable, and the populations are never pooled.`);
  }
  el.innerHTML = parts.join(' ');
}

function renderX12Tiles(xs) {
  const picks = xs.filter((r) => r.pick != null);
  const pickHits = picks.filter((r) => r.outcome === 'correct');
  const rate = xs.length
    ? xs.reduce((a, r) => a + r.side_correct, 0) / xs.length : null;
  const conf = xs.length
    ? xs.reduce((a, r) => a + x12TopP(r), 0) / xs.length : null;
  const regen = xs.filter((r) => r.regen_agrees != null);
  $('x12-tiles').innerHTML = tilesHtml([
    ['EVENTS', xs.length, ''],
    ['PICKED', picks.length, ''],
    ['PICK HIT', picks.length ? pct(pickHits.length / picks.length) : '—',
      'accent'],
    ['TOP-OUTCOME HIT', rate != null ? pct(rate) : '—', ''],
    ['AVG TOP PROB', conf != null ? pct(conf) : '—', ''],
    // denominator is picked rows only — tiny early samples must say so
    [`REGEN AGREE · ${regen.length} PICK${regen.length === 1 ? '' : 'S'}`,
      regen.length
        ? pct(regen.reduce((a, r) => a + r.regen_agrees, 0) / regen.length, 0)
        : '—', ''],
  ]);
}

function renderX12Breakdowns(xs) {
  const c = (r) => r.side_correct;
  renderBreakdown('x12-pop-table', 'Population', [
    ['model-only', xs.filter((r) => r.population === 'schedule')],
    ['book-priced', xs.filter((r) => r.population === 'priced')],
  ], 'nothing settled yet.', c);

  renderBreakdown('x12-gate-table', 'Gate', [
    ['picked', xs.filter((r) => r.pick != null)],
    ['suppressed', xs.filter((r) => r.pick == null)],
  ], 'nothing settled yet.', c);

  renderBreakdown('x12-side-table', 'Top outcome',
    X12_OUTCOMES.map((s) => [s, xs.filter((r) => r.side === s)]),
    'nothing settled yet.', c);

  renderBreakdown('x12-result-table', 'Result',
    X12_OUTCOMES.map((s) => [s, xs.filter((r) => r.result === s)]),
    'nothing settled yet.', c);
}

const X12_SORT_KEYS = {
  kickoff: (r) => r.kickoff,
  match: (r) => (r.home_player ?? r.home_club ?? '').toLowerCase(),
  side: (r) => r.side,
  conf: (r) => x12TopP(r),
  model: (r) => x12TopP(r),
  book: (r) => x12BookP(r),
  edge: (r) => x12BookP(r) == null ? null : x12TopP(r) - x12BookP(r),
  result: (r) => r.result,
  outcome: (r) => r.side_correct,
};

function renderX12Table(xs) {
  xs = sortRows(xs, x12perf.sort, X12_SORT_KEYS);
  const pg = pageSlice(xs, x12perf.page);
  // a suppressed row's outcome is hypothetical; a pick's is what happened
  const label = (r) => r.pick != null
    ? (r.side_correct ? 'hit' : 'miss')
    : (r.side_correct ? 'would hit' : 'would miss');
  const score = (r) => r.home_ft == null || r.away_ft == null
    ? '<span style="color:var(--ink3);">—</span>'
    : `${r.home_ft}–${r.away_ft}`;
  const hda = (r) => [r.p_home, r.p_draw, r.p_away]
    .map((p) => Math.round(p * 100)).join(' / ');
  const rows = pg.rows.map((r) => {
    const edge = x12BookP(r) != null ? x12TopP(r) - x12BookP(r) : null;
    return `<tr>
      <td class="num" style="white-space:nowrap;color:var(--ink2);">
        ${localDHM(r.kickoff)}</td>
      <td style="font-weight:600;">${r.population === 'schedule'
        ? '<span title="model-only · league schedule, no book price" style="color:var(--ink3);">◌ </span>'
        : ''}${teamCell(r.home_player, r.home_club)}
        <span style="color:var(--ink3);">v</span>
        ${teamCell(r.away_player, r.away_club)}</td>
      <td>${r.pick != null
        ? esc(r.side)
        : `<span class="lean" title="below the 0.50 pick gate — never bet"
             >${esc(r.side)}</span>`}</td>
      <td class="num r">${pct(x12TopP(r))}</td>
      <td class="num r" title="model home / draw / away">${hda(r)}</td>
      <td class="num r">${pct(x12BookP(r))}</td>
      <td class="num r" style="color:${edge == null ? 'var(--ink3)'
        : edge >= 0 ? 'var(--good)' : 'var(--bad)'};">
        ${edge == null ? '—' : (edge * 100).toFixed(1)}</td>
      <td class="num r" style="white-space:nowrap;">${score(r)}</td>
      <td class="num r" style="font-weight:700;">${esc(r.result)}</td>
      <td class="${r.side_correct ? 'wb-hit' : 'wb-miss'}"
        style="white-space:nowrap;">${r.side_correct ? '✓' : '✕'} ${label(r)}</td>
    </tr>`;
  }).join('');
  const th = thBuilder(x12perf.sort);
  $('x12-table').innerHTML = `<table class="ledger">
    <thead><tr>${th('kickoff', 'Kickoff')}${th('match', 'Match')}
      ${th('side', 'Read')}${th('conf', 'Model conf', true)}
      ${th('model', 'Model H/D/A', true)}${th('book', 'Book p(read)', true)}
      ${th('edge', 'Δ pts', true)}<th class="r">Score</th>
      ${th('result', 'Result', true)}${th('outcome', 'Outcome')}</tr></thead>
    <tbody>${rows}</tbody></table>`
    + pagerHtml(x12perf.page, pg.pages, xs.length, pg.start, pg.rows.length);
}

function renderX12Perf() {
  // the toggle SWITCHES the market view: O/U block xor 1x2 block
  $('ou-perf').hidden = state.x12;
  $('x12-perf').hidden = !state.x12;
  if (!state.x12 || !perf.data) return;
  const all = x12PerfRows();
  const xs = all.filter(x12Matches);
  const picks = xs.filter((r) => r.pick != null).length;
  $('x12-table-meta').textContent =
    (x12FiltersActive()
      ? `${xs.length} of ${all.length} settled events shown`
      : `${all.length} settled events in window`)
    + ` · ${picks} picked, ${xs.length - picks} suppressed`
    + ' · click a column header to sort';

  renderX12Caveat(xs);
  renderX12Tiles(xs);
  renderX12Breakdowns(xs);
  // no pushes in a 3-way market, so side_correct always grades
  renderHitMissChart({
    hostId: 'x12-chart', ttId: 'x12-chart-tt', cardId: 'x12-chart-card',
    frame: hitMissFrame(xs, x12perf.filters.date, (r) => r.side_correct),
    noun: 'settled events',
  });
  if (!xs.length) {
    $('x12-table').innerHTML = all.length
      ? '<div class="empty">no settled 1x2 events match these filters.</div>'
      : '<div class="empty">nothing settled for 1x2 in this window yet.</div>';
    return;
  }
  renderX12Table(xs);
}

/* ---------- trajectory tab (docs: vs-book "lambda dynamic range") ----------

   Daily series of the λ slope + upcoming checkpoints. The slope is
   market-agnostic — O/U and 1x2 read the same served λs — so this page
   ignores the 1X2 toggle. It's the quick read (analytic ±95% band, no
   bootstrap); `settlement.settle vs-book` stays the careful number. */

const traj = { data: null };

const fmtSlope = (v) =>
  v ? `${v.slope.toFixed(3)} ±${(1.96 * v.se).toFixed(2)}` : '—';

async function loadTrajectory() {
  $('traj-chart-card').classList.add('loading');
  try {
    traj.data = await getJSON('/api/trajectory?days=30');
    renderTrajectory();
  } finally {
    $('traj-chart-card').classList.remove('loading');
  }
}

function renderTrajectory() {
  if (!traj.data) return;
  const series = traj.data.series;
  const latest = [...series].reverse().find((d) => d.priced || d.schedule);
  $('traj-meta').textContent = latest
    ? `latest (${latest.date}) · book-priced ${fmtSlope(latest.priced)}`
      + ` · model-only ${fmtSlope(latest.schedule)}`
    : '';
  renderSlopeChart({ hostId: 'traj-chart', ttId: 'traj-chart-tt',
                     cardId: 'traj-chart-card', series });
  renderMilestones();
}

// contiguous non-null stretches of one population, so lines break honestly
// across thin days instead of bridging them
function slopeRuns(series, key) {
  const out = []; let cur = [];
  series.forEach((d, i) => {
    if (d[key]) cur.push({ d, i });
    else if (cur.length) { out.push(cur); cur = []; }
  });
  if (cur.length) out.push(cur);
  return out;
}

function slopeTicks(lo, hi, m = 5) {
  const span = hi - lo, raw = span / (m - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag)
    .find((st) => span / st <= m) ?? 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step)
    out.push(Math.round(v * 1000) / 1000);
  return out;
}

function renderSlopeChart({ hostId, ttId, cardId, series }) {
  const host = $(hostId);
  if (series.filter((d) => d.priced || d.schedule).length < 2) {
    host.innerHTML = `<div class="empty">not enough settled days yet — a point
      appears once a population's 7-day window holds
      ${traj.data?.min_n ?? 25}+ settled events.</div>`;
    return;
  }
  // R leaves room for the endpoint value labels
  const W = 1000, H = 340, L = 46, R = 56, T = 26, B = 300, xLabY = 330;
  const n = series.length;
  const x = (i) => L + (i + 0.5) * ((W - L - R) / n);
  // y domain spans both series' bands and always includes calibrated 1.0
  let lo = 1, hi = 1;
  for (const d of series) for (const k of ['priced', 'schedule']) {
    const v = d[k];
    if (v) {
      lo = Math.min(lo, v.slope - 1.96 * v.se);
      hi = Math.max(hi, v.slope + 1.96 * v.se);
    }
  }
  const pad = (hi - lo) * 0.08 || 0.1;
  lo -= pad; hi += pad;
  const y = (v) => B - ((v - lo) / (hi - lo)) * (B - T);

  let s = '';
  for (const g of slopeTicks(lo, hi)) {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}"
           stroke="var(--line)" stroke-width="1"/>
          <text x="${L - 8}" y="${y(g) + 4}" text-anchor="end" font-size="11"
           fill="var(--ink3)" style="font-variant-numeric:tabular-nums;">${g}</text>`;
  }
  s += `<line x1="${L}" x2="${W - R}" y1="${y(1)}" y2="${y(1)}"
         stroke="var(--ink)" stroke-width="1" stroke-dasharray="5 4"/>
        <text x="${W - R}" y="${y(1) - 6}" text-anchor="end" font-size="10.5"
         fill="var(--ink2)" letter-spacing="1.5">1.0 · CALIBRATED</text>
        <text x="${L}" y="14" font-size="10.5" fill="var(--ink2)"
         letter-spacing="1.5">SLOPE · REALIZED TOTAL ~ λ-TOTAL · ROLLING 7D ±95%</text>`;

  for (const [key, color] of [['schedule', 'var(--ink2)'],
                              ['priced', 'var(--accent)']]) {
    for (const run of slopeRuns(series, key)) {
      const band = [
        ...run.map(({ d, i }) =>
          `${x(i)},${y(d[key].slope + 1.96 * d[key].se)}`),
        ...[...run].reverse().map(({ d, i }) =>
          `${x(i)},${y(d[key].slope - 1.96 * d[key].se)}`),
      ];
      s += `<polygon points="${band.join(' ')}" fill="${color}" opacity="0.1"/>`;
      s += `<polyline points="${run.map(({ d, i }) =>
             `${x(i)},${y(d[key].slope)}`).join(' ')}" fill="none"
             stroke="${color}" stroke-width="2" stroke-linejoin="round"
             stroke-linecap="round"/>`;
      for (const { d, i } of run) {
        s += `<circle cx="${x(i)}" cy="${y(d[key].slope)}" r="3.5"
               fill="${color}" stroke="var(--surface)" stroke-width="2"/>`;
      }
      const last = run[run.length - 1];
      if (last.i === n - 1) {
        s += `<text x="${x(last.i) + 9}" y="${y(last.d[key].slope) + 4}"
               font-size="12.5" font-weight="700" fill="var(--ink)"
               style="font-variant-numeric:tabular-nums;"
              >${last.d[key].slope.toFixed(2)}</text>`;
      }
    }
  }
  const step = Math.max(1, Math.ceil(n / 8));
  for (let i = 0; i < n; i += step) {
    s += `<text x="${x(i)}" y="${xLabY}" text-anchor="middle" font-size="11"
           fill="var(--ink3)">${series[i].date.slice(5)}</text>`;
  }
  s += `<line id="xhair" x1="0" x2="0" y1="${T}" y2="${B}"
         stroke="var(--ink3)" stroke-width="1" visibility="hidden"/>`;

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Rolling 7-day lambda slope by population">${s}</svg>`;

  const svg = host.querySelector('svg');
  const xhair = svg.querySelector('#xhair');
  const tt = $(ttId);
  const show = (i, clientX, clientY) => {
    xhair.setAttribute('x1', x(i)); xhair.setAttribute('x2', x(i));
    xhair.setAttribute('visibility', 'visible');
    const d = series[i];
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
    row('', fmtSlope(d.priced),
        d.priced ? `book-priced · n=${d.priced.n}` : 'book-priced · thin');
    row('vol', fmtSlope(d.schedule),
        d.schedule ? `model-only · n=${d.schedule.n}` : 'model-only · thin');
    tt.style.display = 'block';
    const card = $(cardId).getBoundingClientRect();
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

/* Date-gated milestones count down to a calendar date; data-gated ones fill
   on graded model-only 1x2 picks (the population that matches the
   walk-forward frame) and pace their ETA off recent accrual. */
const MILESTONES = [
  { title: 'Slope baseline read', due: '2026-07-18',
    note: `The vs-book 7-day window is fully post-club/TOD from this day.
      Record where the slope settles as the new baseline — judge residual
      shrinkage above 1.0 only after this, not during the mixed window.` },
  { title: 'Population-split verification gate', due: '2026-07-18',
    note: `One full week of re-banded tiers (TIER_STRONG 0.69) and gtl
      settlement. Re-check per-population hit rates before trusting tier
      labels again (docs/POPULATION_SPLIT.md).` },
  { title: '1x2 · first honest read vs the 59% gate', target: 370,
    pop: 'schedule',
    note: `Graded model-only 1x2 picks for a ±5-point read against the
      walk-forward 59.1%. Also watch the live pick rate — it is running
      above the walk-forward's 11%.` },
  { title: '1x2 · Phase 4 tier bands', target: 500, pop: 'schedule',
    note: `Quantile 1x2 tier bands on served pick confidence (the settle
      tiers pattern — never eval-frame quantiles), then tier chips return
      to the 1x2 surfaces.` },
  { title: '1x2 · tight read (±3 pts)', target: 1000, pop: 'schedule',
    note: `Enough graded picks to tell whether the elevated live pick rate
      costs accuracy near the 0.50 gate.` },
  { title: 'Refresh the 1x2 walk-forward benchmark', due: '2026-08-12',
    note: `python -m model.evaluate x12 on a month more of history —
      re-measure the 59% target itself (recorded to model_runs).` },
];

function msView(m) {
  if (m.due) {
    const ms = Date.parse(`${m.due}T00:00:00Z`) - Date.now();
    const d = Math.floor(ms / 86_400_000);
    const h = Math.floor((ms % 86_400_000) / 3_600_000);
    return { eta: ms / 86_400_000, due: ms <= 0, prog: null, sub: m.due,
             when: ms <= 0 ? 'DUE' : d >= 1 ? `${d}d ${h}h` : `${h}h` };
  }
  const g = traj.data?.x12_graded?.[m.pop];
  const done = g?.total ?? 0;
  const rate = g ? Math.max(g.d3 / 3, g.d7 / 7) : 0;
  // all graded history younger than 3 days = cold start; the pace estimate
  // is settlement backlog, not accrual — don't project an ETA off it
  const cold = !g || (g.d3 === g.total && g.total < 50);
  const left = Math.max(0, m.target - done);
  const eta = left === 0 ? 0 : !cold && rate > 0 ? left / rate : Infinity;
  return { eta, due: left === 0, prog: Math.min(1, done / m.target),
           sub: `${done} / ${m.target} picks`
             + (left && eta === Infinity ? ' · pace forming' : ''),
           when: left === 0 ? 'READY' : eta === Infinity ? '—'
             : `~${Math.ceil(eta)}d` };
}

function renderMilestones() {
  if (!traj.data) return;
  const items = MILESTONES.map((m) => ({ m, v: msView(m) }))
    .sort((a, b) => a.v.eta - b.v.eta);
  $('milestones').innerHTML = items.map(({ m, v }) => `
    <div class="ms">
      <div class="when${v.due ? ' due' : ''}">${v.when}
        <span class="sub">${esc(v.sub)}</span></div>
      <div>
        <h4>${esc(m.title)}</h4>
        <p>${esc(m.note.replace(/\s+/g, ' '))}</p>
        ${v.prog == null ? ''
          : `<div class="bar"><i style="width:${(v.prog * 100).toFixed(1)}%"></i></div>`}
      </div>
    </div>`).join('');
}

// a real timer: date-gated countdowns tick while the tab is open
setInterval(() => {
  if (!$('view-trajectory').hidden) renderMilestones();
}, 60_000);

/* ---------- wagers tab (paper trading) ----------

   Reads /api/wagers + /api/wagers/summary and renders on tab activation, and
   again right after a bet is placed. Grading is server-derived; this is a dumb
   renderer like the rest of the app. A won slip whose only winning legs pushed
   or voided is shown as a PUSH (it returns exactly the stake). */

const wagersState = { list: [], summary: null };

// display status folds the server's 3-way status into the 4-way ledger view:
// a 'won' slip with no actually-won leg (all push/void) reads as a push
const wagerDisplayStatus = (w) => w.status !== 'won' ? w.status
  : w.legs.some((l) => l.outcome === 'won') ? 'won' : 'push';

const LEG_OUTCOME_SYM = { pending: '·', won: '✓', lost: '✕', push: '—', void: '∅' };

function wagerLegLine(l) {
  const match = l.home_player || l.away_player
    ? `${esc(l.home_player ?? l.home_club ?? '')} v ${esc(l.away_player ?? l.away_club ?? '')}`
    : `${esc(l.home_club ?? '')} v ${esc(l.away_club ?? '')}`;
  const sel = l.market === 'ou'
    ? `${l.selection === 'over' ? 'Over' : 'Under'} ${l.line}`
    : `${l.selection[0].toUpperCase()}${l.selection.slice(1)}`;
  return `<div class="wr-leg">
    <span>${match} <span style="color:var(--ink3);">·</span> ${esc(sel)}</span>
    <span class="wl-o"><span class="wl-${l.outcome}">${LEG_OUTCOME_SYM[l.outcome]}
      ${l.outcome}</span> · ${l.odds}</span></div>`;
}

function renderWagerTiles() {
  const s = wagersState.summary;
  if (!s) return;
  const r = s.record;
  const settled = r.won + r.lost + r.push;
  const decided = r.won + r.lost;
  const pnlCls = s.pnl > 0 ? 'accent' : '';
  $('wager-tiles').innerHTML = tilesHtml([
    ['RECORD W-L-P', `${r.won}-${r.lost}-${r.push}`, ''],
    ['HIT RATE', decided ? pct(r.won / decided) : '—', 'accent'],
    ['STAKED', settled ? s.staked.toFixed(2) : '—', ''],
    ['RETURNED', settled ? s.returned.toFixed(2) : '—', ''],
    ['P / L', settled ? (s.pnl >= 0 ? '+' : '') + s.pnl.toFixed(2) : '—', pnlCls],
    ['ROI', s.roi != null ? pct(s.roi) : '—', pnlCls],
  ]);
}

function renderOpenWagers() {
  const open = wagersState.list.filter((w) => w.status === 'open');
  const stake = wagersState.summary?.open_stake ?? 0;
  $('wager-open-meta').textContent = open.length
    ? `${open.length} open · ${stake.toFixed(2)} units at stake` : '';
  $('wager-open').innerHTML = open.length ? open.map((w) => {
    const ret = (w.stake * w.total_odds);
    const single = w.legs.length === 1;
    return `<div class="wager-row open">
      <div class="wr-head">
        <span class="meta">${localDHM(w.placed_at)} · ${single ? 'single'
          : w.legs.length + '-leg parlay'}</span>
        <span class="wr-status">open</span></div>
      ${w.legs.map(wagerLegLine).join('')}
      <div class="wr-foot">
        <span>stake <b>${w.stake}</b> · odds <b>${w.total_odds.toFixed(2)}</b>
          · to return <b>${ret.toFixed(2)}</b></span>
        <button class="wr-cancel" data-cancel="${w.id}">CANCEL</button></div>
    </div>`;
  }).join('') : `<div class="empty">no open wagers — place one from the Ledger
    tab's slate to start a paper trade.</div>`;
}

function renderSettledWagers() {
  const settled = wagersState.list.filter((w) => w.status !== 'open');
  $('wager-settled-meta').textContent = settled.length
    ? `${settled.length} settled · won/push return stake × odds, losses return 0`
    : '';
  if (!settled.length) {
    $('wager-settled').innerHTML = `<div class="empty">nothing settled yet —
      wagers grade as their legs' events settle (~45 min after kickoff).</div>`;
    return;
  }
  const rows = settled.map((w) => {
    const ds = wagerDisplayStatus(w);
    const pnl = (w.payout ?? 0) - w.stake;
    const plCls = pnl > 0 ? 'pl-pos' : pnl < 0 ? 'pl-neg' : '';
    const legs = w.legs.map((l) => {
      const sel = l.market === 'ou'
        ? `${l.selection === 'over' ? 'O' : 'U'} ${l.line}`
        : l.selection[0].toUpperCase() + l.selection.slice(1);
      const m = l.home_player ?? l.home_club ?? '';
      const a = l.away_player ?? l.away_club ?? '';
      return `<div class="wr-leg"><span>${esc(m)} v ${esc(a)}
        <span style="color:var(--ink3);">·</span> ${esc(sel)}</span>
        <span class="wl-o"><span class="wl-${l.outcome}">${LEG_OUTCOME_SYM[l.outcome]}</span>
          ${l.odds}</span></div>`;
    }).join('');
    return `<tr class="wager-row-tr ${ds}">
      <td class="num" style="white-space:nowrap;color:var(--ink2);vertical-align:top;">
        ${localDHM(w.placed_at)}</td>
      <td style="min-width:220px;">${legs}</td>
      <td class="num r" style="vertical-align:top;">${w.stake}</td>
      <td class="num r" style="vertical-align:top;">${w.total_odds.toFixed(2)}</td>
      <td class="num r" style="vertical-align:top;">${(w.payout ?? 0).toFixed(2)}</td>
      <td class="num r ${plCls}" style="vertical-align:top;">
        ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</td>
      <td style="vertical-align:top;"><span class="wl-${ds === 'push' ? 'push'
        : ds === 'won' ? 'won' : 'lost'}" style="white-space:nowrap;">
        ${ds.toUpperCase()}</span></td>
    </tr>`;
  }).join('');
  $('wager-settled').innerHTML = `<table class="ledger">
    <thead><tr><th>Placed</th><th>Legs</th><th class="r">Stake</th>
      <th class="r">Odds</th><th class="r">Return</th><th class="r">P/L</th>
      <th>Result</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderWagers() {
  renderWagerTiles();
  renderOpenWagers();
  renderSettledWagers();
}

async function loadWagers() {
  try {
    const [list, summary] = await Promise.all([
      getJSON('/api/wagers'), getJSON('/api/wagers/summary'),
    ]);
    wagersState.list = list;
    wagersState.summary = summary;
    renderWagers();
  } catch (e) {
    $('wager-open').innerHTML =
      `<div class="empty">could not load wagers: ${esc(e.message)}</div>`;
  }
}

// cancel is delegated on the persistent open-list container
$('wager-open').addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-cancel]');
  if (!b) return;
  const r = await fetch(`/api/wagers/${b.dataset.cancel}`, { method: 'DELETE' });
  if (r.ok) { showToast('Wager cancelled'); void loadWagers(); }
  else {
    const d = await r.json().catch(() => ({}));
    showToast(d.error || 'could not cancel');
  }
});

/* ---------- tabs ---------- */

const TABS = ['ledger', 'performance', 'trajectory', 'wagers'];

function showTab() {
  const hash = location.hash.slice(1);
  const active = TABS.includes(hash) ? hash : 'ledger';
  for (const t of TABS) {
    $(`view-${t}`).hidden = t !== active;
    $(`tab-${t}`).classList.toggle('active', t === active);
  }
  if (active === 'performance') void loadPerf();
  if (active === 'trajectory') void loadTrajectory();
  if (active === 'wagers') void loadWagers();
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
    renderSettled(); renderSlip();  // slate changed → refresh slip stale marks
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

function syncX12Btn() {
  $('x12-btn').textContent = `1X2 · ${state.x12 ? 'ON' : 'OFF'}`;
  $('x12-btn').classList.toggle('active', state.x12);
}

$('x12-btn').onclick = () => {
  state.x12 = !state.x12;
  localStorage.setItem('x12ui', state.x12 ? '1' : '0');
  syncX12Btn();
  renderSlate(); renderScorecard(); renderSettled(); renderX12Perf();
};

$('theme-btn').onclick = () => {
  const el = document.documentElement;
  const dark = el.dataset.theme === 'dark';
  el.dataset.theme = dark ? 'light' : 'dark';
  $('theme-btn').textContent = dark ? '◐ DARK' : '◑ LIGHT';
};

wirePerfFilters();
wireX12Filters();
// re-sorting reorders the whole frame, so the view snaps back to page one
wireSort('perf-table', perf.sort,
  () => { perf.page.index = 0; renderPerf(); });
wireSort('x12-table', x12perf.sort,
  () => { x12perf.page.index = 0; renderX12Perf(); });
wirePager('perf-table', perf.page, 'perfRows', renderPerf);
wirePager('x12-table', x12perf.page, 'x12Rows', renderX12Perf);
syncX12Btn();
wireSlip();
renderSlip();  // restore any persisted slip before the first slate load
showTab();
void refresh();
connectSse();
// countdowns; re-render the slip too so stale (kicked-off) marks stay current
setInterval(() => { renderStatus(); renderSlate(); renderSlip(); }, 30_000);
