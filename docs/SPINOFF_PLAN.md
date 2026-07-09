# Spin-off Implementation Plan: Over/Under Prediction Engine for a New League

> **Purpose.** This document is self-contained: it is meant to be dropped into a brand-new
> project window with no access to the original `baba.vanga.esports` repo. It carries over
> every hard-won learning from the eAdriatic/FC26 project, specifies a performance-first
> re-architecture (Python + Node.js, one submodule per cross-functional task), and lays out
> a phased build with numeric acceptance gates.

---

## 1. Mission & scope

Build a prediction engine for a **new league** (results scraped from a new source; odds
still from **betPawa**) whose **primary product is the Over/Under (goal totals) market**.
1X2 is kept only as supporting detail, never the headline pick.

| Decision | Choice |
|---|---|
| Model structure | **Replicated as-is** from the parent project (Poisson attack/defense GLM + recent-form heuristic + λ-blend), re-validated on the new league's data before serving |
| Results source | **New scraper** (league TBD — if it is *GT Leagues*, note the parent's betPawa feed already carries its fixtures; they were only excluded for lack of a results source) |
| Odds source | betPawa (same protobuf feed; new category/market IDs) |
| Primary market | Over/Under totals; the full total-goals distribution is the core object so *any* listed line can be priced |
| Languages | **Python** = ingestion + modeling + settlement. **Node.js** = serving layer (API/BFF, scheduler/orchestrator, web UI) |
| Storage | **SQLite (WAL mode)** as the single shared store — replaces the parent's pile of overwritten JSON files |

---

## 2. Learnings ledger — what the parent project proved (carry these over verbatim)

Each item below cost real time to discover. Treat them as priors to *re-verify cheaply*,
not re-derive.

### 2.1 Modeling

1. **Poisson attack/defense GLM is the right base.** Time-weighted (exponential recency,
   half-life **14 days**), ridge-shrunk (`alpha ≈ 0.02`), per-entity attack/defense rates →
   `λ_total = λ_home + λ_away` → all market probabilities read off one distribution, so
   Under/push/Over always sum to 1. It was the best-calibrated model at every decile and
   beat every challenger on honest data. Baselines on ~5.2k out-of-sample matches:
   **Over AUC ≈ 0.641, decisive accuracy ≈ 63.5%**.
2. **The entity is the *player*, not the club.** betPawa names events `Club (Player)`; the
   club carries **no marginal signal** beyond the player (club explained 0.75% of
   player-adjusted residual; cross-club scoring sd indistinguishable from noise). Train and
   join on the player. Expect the new league to behave the same, but re-check once.
3. **Freshness beats depth — the intra-day form blend is the biggest validated win.**
   A 0.5/0.5 λ-blend of day-frozen Poisson with a cheap short-memory recent-form heuristic
   beat pure Poisson across the whole 0–90 min scrape-lag band (**0.657 decisive / 0.663
   Over AUC at ≤30 min lag**, still +1.9 pts at 60 min). Preconditions: the league site must
   publish same-day results intra-day (probe this — Phase 0), and the pipeline must merge
   them on a ~20–30 min cadence. Weight 0.5 was the sweep max, robust 0.3–0.7.
4. **Goal totals were UNDER-dispersed** (per-side var/mean ≈ 0.80, Cameron–Trivedi
   t ≈ −30). Therefore **do not reach for Negative Binomial or Dixon-Coles** — they add
   dispersion in the *wrong direction* and collapse to Poisson. If a dispersion correction
   is ever needed, it's Efron's **double-Poisson** / Conway–Maxwell–Poisson (variance <
   mean). First step on the new league: **measure the dispersion sign** before choosing any
   family.
5. **Tree models (GBM per-side rate-grid) had no honest edge** — their apparent +6 AUC pts
   was 100% a duplicate-match leak (see 2.2). On deduped data pure rate-grid was *worse*
   than Poisson (0.625 vs 0.641). Don't build it first; keep it as an optional challenger
   to re-race once data accrues.
6. **Rejected levers — do not spend time on:** decision-threshold retuning (measured effect:
   exactly 0.0 pts — ridge shrinkage already does the job); temperature scaling (monotone →
   cannot change the argmax pick; also unstable on thin history); club-as-feature; the
   match's own half-time score pre-match (post-kickoff leakage; historical HT redundant
   with FT rates — HT only pays in a future *live/in-play* product).
7. **The push band is noise.** For an Over 6.5 / Under 5.5 pair, `total == 6` loses both and
   no model beat the base rate on the 3-class problem. **Judge the market on binary
   Over/Under AUC + Brier, never on 3-class log-loss.** Only flag a pick when market prob is
   high AND push probability is low (parent: `PICK_PROB_THRESHOLD 0.60`, `MAX_PUSH_PROB 0.20`).
8. **Pick = most-likely O/U selection, ranked by a 0.5/0.5 blend of model probability and
   the book's margin-free implied probability** — *not* the biggest-edge selection (that
   selects longshots). `value` is a separate flag (edge ≥ 0.05). Confidence tiers on the
   blended confidence rank cleanly: all picks ~65.6% decisive → top 25% ~79.6%
   (parent edges: lean ≥ 0.50, solid ≥ 0.58, strong ≥ 0.66 — **re-calibrate to the new
   league's quantiles**).

### 2.2 Data integrity (the expensive lessons)

9. **Source sites double-list matches.** The parent site listed every match twice (aggregate
   season table + per-round table, same kickoff/players/score, different match_id). 46% of
   rows were dupes, and the twin at the same kickoff **leaked each match's own result into
   rolling-form features**, inflating every form-based model. The fix: dedup on
   `(date, kickoff, home_player, away_player, score)` at load, and **re-run every backtest
   after any dedup change**. Assume the new source has an equivalent pathology until proven
   otherwise.
10. **The parent feed was a ~60-min delayed rebroadcast.** The physical match played ~60 min
    before the feed's advertised start; the results site clock was UTC+3, so the
    feed↔results join offset was **+120 min**, and — critically — **the league site could
    publish a result *before* the book closed betting on it**. Consequences to build in
    from day one on the new league:
    - Never nearest-join feed events to results. Establish the modal offset empirically
      across ≥2 days (candidate deltas repeat every ~2.5 h because the same ordered player
      pair replays), anchor on it, refine within a small tolerance (±15 min).
    - Enforce an **as-of training cutoff** when predicting: truncate form/Elo training to
      results strictly before `start_time + offset − tolerance` so a coupled scrape can
      never leak a fixture's own result into its prediction.
    - In settlement, score served probabilities as-is but flag `leak_risk`, and *also*
      re-predict each settled fixture with training truncated before physical kickoff
      ("regen") — comparing served vs regen picks is the real contamination guard.
11. **`predictions.json`-style overwritten artifacts are not a record.** The parent needed an
    **append-only prediction log** (one line per fixture per refresh) to settle anything.
    In the new architecture this is a first-class database table, not a bolted-on JSONL.
12. **Feature hygiene that made the blend legal:** training frame sorted by
    `(date, kickoff, match_id)`, all rolling features built with `shift(1)` so a match sees
    only strictly-earlier kickoffs. Keep this invariant test-covered.

### 2.3 betPawa feed (port this knowledge directly)

13. The feed is **protobuf with no public `.proto`**. The parent walks the raw wire format
    directly (~100 lines, no protoc) and maps reverse-engineered field numbers to fixtures.
    Decimal odds and margin-free implied probabilities are **little-endian IEEE-754
    doubles**. Port `protobuf_decode.py` + `normalize.py` nearly as-is.
    - Endpoint: `POST {base}/api/sportsbook/v4/events/lists/by-queries` with a JSON `q`
      (eventType `UPCOMING`, `categories`, `view.marketTypes`, skip/take paging).
    - Parent IDs: category `101` (eFootball), markets `3743` (1X2 FT) and `5000`
      (Total Score Over/Under FT). **The new league needs its own category ID and possibly
      market IDs — capture from devtools, Phase 0.**
14. **Auth expires:** `x-pawa-token` (session), `__cf_bm` Cloudflare cookie (~30 min TTL),
    `x-device-fingerprint`. Read from env vars; on 401/403 re-capture the browser curl. The
    new build should make token refresh an *operational alert*, not a silent failure.
15. **Name joins need an alias table.** Book names vs scraped names never match exactly;
    maintain `player_aliases` and tag fixtures `coverage: full|partial|none` — no pick is
    emitted without model coverage (fall back to book implied only).

### 2.4 Architecture pain in the parent (what the re-architecture must fix)

- Model fitting lived **inside the API process** (`_get_models` with mtime checks) —
  refresh latency coupled to fitting, and state invisible outside the process.
- **JSON files as a database** (`results.json`, `upcoming.json`, `predictions.json`,
  `prediction_log.jsonl`, `vs_book.json`) — every read reparses everything; every write
  rewrites everything; concurrency by luck.
- The scraper launches a **headless Chromium per run** (source 403s plain HTTP) — fine,
  but it was coupled to the serving path in one deployment option; keep it a separate job.
- The UI **re-derives O/U picks client-side** because the data file is overwritten with 1X2
  picks by a live-refresh job — a known wart. In the new build, picks are computed once,
  server-side, persisted, and the UI is a dumb renderer.
- Config = Python module constants imported across package boundaries (`model.config`
  importing `scraper.config`) — replace with a single typed config layer per service +
  `.env`.

---

## 3. Target architecture

### 3.1 Language split (cross-functional submodules)

```
newleague/
├── pyproject.toml                  # single Python workspace (uv or pip-tools)
├── package.json                    # npm workspaces: services/api, apps/web
├── .env.example                    # all secrets/tunables documented here
├── Makefile (or justfile)          # one-command dev/test/run entrypoints
│
├── libs/py/
│   ├── core/                       # domain layer — NO I/O
│   │   ├── schema.py               #   pydantic models: Match, Fixture, OddsSnapshot,
│   │   │                           #   Prediction, Settlement (the shared contract)
│   │   ├── markets.py              #   line math: any O/U line off a total-goals dist,
│   │   │                           #   push-band logic, margin-free implied probs
│   │   └── config.py               #   typed settings (pydantic-settings, .env-driven)
│   ├── store/                      # persistence layer
│   │   ├── db.py                   #   SQLite WAL, migrations, connection mgmt
│   │   └── repo.py                 #   typed repositories per table
│   └── model/                      # the replicated model family
│       ├── data.py                 #   load + DEDUP + kickoff-sort invariants
│       ├── poisson.py              #   time-weighted ridge Poisson GLM (base)
│       ├── form.py / heuristic.py  #   short-memory recent-form totals model
│       ├── blend.py                #   λ-blend (the served totals source)
│       ├── elo.py                  #   1X2 supporting detail only
│       ├── evaluate.py             #   walk-forward + BATCH-MODE (scrape-lag) backtests
│       └── registry.py             #   fitted-artifact cache: Poisson day-frozen,
│                                   #   form leg refit on new results (cheap)
│
├── services/py/
│   ├── results_ingest/             # NEW scraper for the new league's scorelines
│   │   ├── fetch.py                #   network only (Playwright iff plain HTTP 403s)
│   │   ├── parse.py                #   pure HTML→rows, offline-testable vs saved pages
│   │   └── cli.py                  #   full backfill / --today incremental merge
│   ├── odds_ingest/                # betPawa client (ported)
│   │   ├── protobuf_decode.py      #   raw wire-format walker (port as-is)
│   │   ├── normalize.py            #   field mapping (re-verify IDs for new league)
│   │   └── cli.py                  #   fetch → odds_snapshots table (+ raw .bin archive)
│   ├── predictor/                  # the prediction cycle (was predict_slate + api._get_models)
│   │   └── cycle.py                #   join odds↔model via aliases, as-of cutoff,
│   │                               #   pick/tier/value derivation, WRITE predictions table
│   └── settlement/                 # was vsbook.py
│       └── settle.py               #   offset-anchored join, served-vs-regen leak guard,
│                                   #   model-vs-book scorecard, tier hit-rates
│
├── services/api/                   # Node.js (Fastify + TypeScript) — read-only, fast
│   ├── src/routes/                 #   GET /api/slate /api/metrics /api/settlements /api/health
│   ├── src/db.ts                   #   better-sqlite3 reads (sync, in-process, very fast)
│   └── src/sse.ts                  #   Server-Sent Events push when predictions update
│
├── services/scheduler/             # Node.js — the orchestrator (replaces cron-by-hand)
│   └── src/jobs.ts                 #   spawns Python CLIs on cadences; retries; alerting
│
└── apps/web/                       # Svelte 5 + Vite (carry the parent UI patterns)
    └── src/                        #   tier badges/filter, prob bars, O/U-first cards
```

**Why this split.** Python keeps everything numerical and scraping-related (pandas/sklearn
and Playwright live there). Node gets what Node is good at: a low-latency read-only API,
SSE push, process orchestration, and the web toolchain. The two sides **never call each
other in-process** — they meet only at the SQLite file (WAL allows one writer + many
readers concurrently) and at the process boundary (scheduler spawns Python CLIs).

### 3.2 Storage schema (SQLite, WAL)

```sql
matches         (id, source_match_id, date, kickoff_ts, competition,
                 home_player, away_player, home_club, away_club,
                 home_ft, away_ft, raw_hash, scraped_at,
                 UNIQUE(date, kickoff_ts, home_player, away_player, home_ft, away_ft))
                 -- the UNIQUE constraint IS the dedup lesson (2.2 §9), enforced by the DB
fixtures        (event_id PK, start_time_utc, competition, home_raw, away_raw,
                 home_player, away_player, coverage, first_seen_at, last_seen_at)
odds_snapshots  (id, event_id, fetched_at, market, line, selection, odds, implied_prob)
                 -- append-only: full odds history ≈ free CLV analysis later
predictions     (id, event_id, predicted_at, totals_source, line,
                 p_over, p_push, p_under, lambda_home, lambda_away,
                 pick, confidence, tier, value_flag, model_version, as_of_cutoff_ts)
                 -- append-only: replaces prediction_log.jsonl AND predictions.json
settlements     (event_id, matched_match_id, offset_min_used, result_total,
                 pick_correct, leak_risk, regen_pick, regen_agrees, settled_at)
model_runs      (id, kind, fitted_at, train_rows, params_json, metrics_json)
```

Views (or a small materializer) produce the exact JSON the API serves — the "current
slate" is `SELECT ... latest prediction per upcoming event`, not a file overwrite.

### 3.3 Dataflow & cadences

```
 every 20–30 min  results_ingest --today ──▶ matches ──┐
 every ~10 min    odds_ingest             ──▶ fixtures/odds_snapshots ──┐
                                                                        ▼
 on either update predictor.cycle:  registry (Poisson day-frozen; form leg
                  refit iff matches changed) → as-of cutoff → predictions (append)
                                                                        │
 continuous       Node API reads latest predictions ── SSE ──▶ web     ◀┘
 hourly           settlement.settle → settlements + rolling tier hit-rates
 daily (once)     full Poisson refit + walk-forward metrics refresh
```

Performance targets (all easily attainable with this shape):
- Odds fetch → new predictions visible: **< 5 s** (no model fitting in the path; form
  refit is one groupby; Poisson is a cached artifact).
- API p99 read latency: **< 20 ms** (better-sqlite3, indexed reads, no JSON reparse).
- Scraper isolated: a Chromium hang can never block serving.

---

## 4. Phased implementation plan

### Phase 0 — Feasibility probes (½–1 day; **gates everything**)

The parent project's biggest wins and biggest bugs were all *source-behavior* facts.
Establish them for the new league before writing product code.

1. **Results source probe.** Scrape the new league's results page manually.
   Record: does plain HTTP work or is a real browser needed (parent needed Playwright
   behind an AWS LB 403)? How many days of history are reachable (parent: ~24-day
   carousel — thin history shaped every modeling choice)? Are matches **double-listed**
   anywhere (assume yes until proven)? What is the site's clock/timezone?
2. **Intra-day publish probe** (gates the blend, the top modeling win). Mid-afternoon on a
   match day, scrape today twice, 2–3 h apart. Success = completed-match count grows
   through the day. Record the **publish lag** (kickoff → visible), parent saw 65–85 min.
3. **betPawa feed probe.** From browser devtools on the new league's betPawa page, capture:
   category ID, market type IDs for 1X2 and O/U, the **listed O/U line(s)** (parent: only
   6.5 — the new league may list several lines, which the full-distribution model prices
   for free), and a raw `.bin` response saved as the offline test fixture. Verify the
   parent's wire-format decoder parses it (expect yes; same API).
4. **Join geometry probe.** For one day, collect feed `start_time`s and results kickoffs;
   compute candidate deltas across ≥2 days. Determine: the modal offset, whether the feed
   is a delayed rebroadcast again, and whether results can publish before betting closes
   → sets `FEED_OFFSET_MIN`, `OFFSET_TOL_MIN`, and the as-of cutoff.
5. **Name-join sample.** Eyeball 20 book event names vs scraped player names; estimate
   alias-table effort and coverage ceiling.

**Gate:** proceed only when (a) results are scrapeable with ≥ ~2 weeks of history reachable
or accumulable, and (b) the feed carries a priced O/U market for the league. The intra-day
probe result doesn't gate the project — it only decides whether `TOTALS_SOURCE` can ever be
`blend` (fallback: pure Poisson, which is fully servable).

### Phase 1 — Scaffold, core, storage (1–2 days)

- Monorepo layout as in §3.1; Python workspace + npm workspaces; `Makefile` targets
  (`make dev`, `make test`, `make backfill`, `make cycle`).
- `libs/py/core`: pydantic schemas (the cross-language contract — generate TS types for
  the API from them via JSON Schema, so Python and Node can never drift), `markets.py`
  (generic O/U line pricing off a total-goals pmf; push logic; margin-free implied probs),
  typed `.env` config.
- `libs/py/store`: SQLite with WAL, migration runner, repositories; the `matches` UNIQUE
  dedup constraint from day one.
- CI-able test skeleton: `pytest` + `vitest`; every parser/decoder test runs **offline**
  against saved fixtures (a parent pattern that paid off constantly).

**Gate:** `make test` green; schema round-trips Python↔TS.

### Phase 2 — Results ingestion: the new scraper (2–4 days, depends on Phase 0.1)

- `fetch.py`: network-only. Prefer plain `httpx` with browser headers; fall back to
  Playwright only if the probe demanded it. Polite: single-threaded, delay between
  requests, realistic UA.
- `parse.py`: **pure** HTML→rows, developed against saved raw pages committed as test
  fixtures. Normalize the entity to the *player* on both sides regardless of raw layout
  (the parent's `Club (Player)` vs `Player (Org)` trap — check for the new source's
  equivalent).
- `cli.py`: `--backfill` (all reachable history) and `--today --merge` (incremental,
  idempotent upsert; the DB constraint makes re-runs safe).
- Data-quality checks as code, run on every ingest: duplicate-rate report, rows/day
  trend, score distribution sanity, timezone spot-check.

**Gate:** backfill lands ≥ 2 weeks of matches; duplicate audit clean (or dedup constraint
demonstrably absorbing the source's double-listing); parser tests offline-green.

### Phase 3 — Odds ingestion: betPawa port (1–2 days)

- Port `protobuf_decode.py` unchanged; update `normalize.py` field mapping against the new
  raw capture; parametrize category/market IDs + brand/country in config.
- Write to `fixtures` + append-only `odds_snapshots` (every fetch keeps history — this
  gives closing-line-value analysis later for free, something the parent couldn't do).
- Auth via env (`BETPAWA_TOKEN`, `BETPAWA_CF_BM`, `BETPAWA_FINGERPRINT`); on 401/403 the
  job records a `token_expired` status the scheduler surfaces loudly (parent lesson: this
  fails ~every 30 min of cookie TTL when unattended).
- Build the `player_aliases` table + a small CLI to list unmatched names each cycle.

**Gate:** a live fetch produces ≥ 1 slate with priced O/U lines; offline decode test green
against the saved `.bin`.

### Phase 4 — Model port + honest re-validation (3–5 days; the heart)

Port the model family **structurally as-is**, then re-earn every default on new-league data:

1. `data.py`: load from SQLite, kickoff-sort `(date, kickoff, match_id)`, dedup invariant
   test, `shift(1)`-only rolling features. **Add a leakage canary test**: a synthetic
   dataset where the only signal is the match's own outcome must score AUC ≈ 0.5.
2. `poisson.py`: identical structure (PoissonRegressor, exponential half-life, ridge
   shrinkage, goal cap). Re-tune only `HALF_LIFE_DAYS` (parent: 14) and `alpha` (0.02) by
   walk-forward sweep on the new data — these are league-tempo constants, not universal.
3. **Dispersion check first** (learning 2.1 §4): compute per-side var/mean and
   Cameron–Trivedi. Under-dispersed → Poisson tails fine, move on. Over-dispersed (new
   league might differ!) → *now* NB/Dixon-Coles become candidates. This single number
   decides the distribution family.
4. `heuristic.py` + `blend.py`: the recent-form leg and λ-blend, gated on the Phase 0
   intra-day probe. Re-sweep the blend weight **under the batch-mode backtest only**.
5. `evaluate.py`: port both harnesses — the plain walk-forward AND the **batch-mode
   intra-day walk-forward** (form frozen at `kickoff − scrape_lag`, as-of join, simulated
   refresh batches). The parent's core epistemic lesson: *the only backtest that counts is
   the one that mirrors serving*. Every served-config claim comes from this harness with
   the measured scrape lag.
6. Line generalization: if the new league lists multiple O/U lines, pick/edge logic runs
   per line off the one distribution; the headline pick is the best-tier line.

**Gates (all on batch-mode, out-of-sample, deduped data):**
- Poisson O/U AUC **> 0.58** and Brier beats the base-rate baseline (parent range
  0.58–0.64; below 0.58 the league may be too random to trade — stop and reassess).
- Calibration: reliability table within ~±3 pts per decile.
- Leakage canary at AUC ≈ 0.5.
- `blend` enabled **only if** it beats Poisson at the measured lag (parent margin: +2 pts
  decisive); otherwise serve `poisson` and revisit.

### Phase 5 — Predictor service + scheduler (2–3 days)

- `predictor/cycle.py`: one idempotent cycle = read latest fixtures/odds → alias-join →
  registry models (Poisson day-frozen artifact; form leg refit iff `matches` changed —
  the parent's cost asymmetry, now explicit) → **as-of cutoff** from Phase 0.4 → derive
  pick/confidence/tier/value → **append** to `predictions`.
- Tier edges initialized from parent quantile logic, re-calibrated to the new league's
  confidence distribution after the first settled week.
- `services/scheduler` (Node): job table = odds ingest ~10 min, results merge ~20–30 min,
  predictor on either's completion (event, not timer), settlement hourly, full refit +
  metrics daily. Per-job: timeout, retry w/ backoff, last-status persisted, failure
  surfaced in `/api/health` (token expiry especially).

**Gate:** 24 h unattended soak — cycles complete, no fitting in any request path,
odds-update → new-prediction latency < 5 s, prediction rows append (never overwrite).

### Phase 6 — Settlement & scorecard (2 days)

Port `vsbook` logic onto the DB:
- Offset-anchored feed↔results join (modal-delta estimation within tolerance; **never**
  nearest-join), `leak_risk` flag, and the **served-vs-regen** re-prediction guard.
- Rolling scorecard: decisive accuracy, all-match accuracy, per-tier realized hit rates,
  model-vs-book Brier, and (new, enabled by `odds_snapshots` history) **closing-line
  value** per pick.
- Settlement is the *product feedback loop* for an O/U-focused product — it feeds the tier
  re-calibration in Phase 5 and the weekly "is the edge real" review.

**Gate:** one full day settles automatically end-to-end; regen agreement ≈ 100% (any gap =
a leak in the serving path — stop and fix before trusting metrics).

### Phase 7 — API + Web UI (2–3 days)

- Node/Fastify API, read-only: `GET /api/slate` (latest prediction per upcoming fixture,
  O/U-first shape), `/api/metrics`, `/api/settlements`, `/api/health` (job statuses, data
  freshness ages, token state); SSE endpoint pushing "slate updated".
- Svelte 5 UI carrying the parent's proven surface: O/U pick leads the card, tier badge +
  tier filter (default **all**, per parent decision), to-scale probability bar, labelled
  Outcome/Odds/Model/Book/Edge table, coverage indicator, value badge — plus a settlement
  tab (realized tier hit-rates) the parent never had in-UI.
- **Picks are server-derived and persisted; the UI computes nothing** (kills the parent's
  client-side-pick wart permanently).

**Gate:** UI renders live slate from a cold start with only `make dev`; refresh pushes via
SSE without polling.

### Phase 8 — Hardening & ops (ongoing)

- `.env.example` complete; secrets never committed (the parent shipped captured fallback
  tokens in source — don't repeat that).
- Nightly SQLite backup (simple file copy under WAL checkpoint); raw scrape HTML + raw
  odds `.bin` archived per day for reprocessing.
- Weekly review ritual: batch-mode metrics vs served config, blend-edge vs measured
  scrape lag (the edge *tracks freshness* — if the cadence slips, re-measure), tier
  calibration drift, alias-table gaps.
- Deferred backlog (parent-validated ordering): double-Poisson dispersion head (only
  after the blend sharpens λ̂), 1X2 draw inflation (ρ diagonal), rate-grid re-race,
  live/in-play model using HT (a different product).

---

## 5. Porting map (parent file → new home)

| Parent | Disposition | New home |
|---|---|---|
| `oddsfeed/protobuf_decode.py` | **Port as-is** | `services/py/odds_ingest/protobuf_decode.py` |
| `oddsfeed/normalize.py` | Port, re-verify field/market IDs | `odds_ingest/normalize.py` |
| `oddsfeed/config.py` | Rewrite as typed .env config (strip baked-in secrets) | `libs/py/core/config.py` |
| `model/poisson.py` | Port structurally, re-tune half-life/alpha | `libs/py/model/poisson.py` |
| `model/form.py`, `heuristic.py` | Port | `libs/py/model/` |
| `model/elo.py` | Port (supporting detail only) | `libs/py/model/elo.py` |
| `model/evaluate.py` | Port both harnesses; batch-mode is canonical | `libs/py/model/evaluate.py` |
| `model/data.py` | Rewrite for SQLite; keep dedup + sort + shift(1) invariants as tests | `libs/py/model/data.py` |
| `model/predict_slate.py` | Rewrite as the predictor cycle | `services/py/predictor/cycle.py` |
| `model/vsbook.py` | Rewrite onto DB; keep offset-join + regen logic | `services/py/settlement/settle.py` |
| `model/rategrid.py`, `gbm.py` | **Drop initially** (no honest edge); backlog challenger | — |
| `scraper/*` | Rewrite for the new source; keep fetch/parse/cli split + offline-parse testing pattern | `services/py/results_ingest/` |
| `api/main.py` | Rewrite in Node (read-only; fitting moves to predictor) | `services/api/` |
| `web/src/*` (Svelte) | Port UI patterns; remove client-side pick derivation | `apps/web/` |
| JSON files in `data/` | Retire → SQLite tables (§3.2) | `store/` |

## 6. Seed constants (parent values = starting points, all re-validated in Phase 4)

```
HALF_LIFE_DAYS=14        POISSON_ALPHA=0.02      MAX_GOALS_FIT=12
TOTALS_SOURCE=poisson    # flip to blend only after Phase-4 gate passes
TOTALS_BLEND_WEIGHT=0.5  SCRAPE_LAG_MIN=<measured in Phase 0>
PICK_PROB_THRESHOLD=0.60 MAX_PUSH_PROB=0.20      PICK_MODEL_WEIGHT=0.5  MIN_EDGE=0.05
CONFIDENCE_TIERS lean=0.50 solid=0.58 strong=0.66   # re-quantile on new league
FEED_OFFSET_MIN / OFFSET_TOL_MIN / AS_OF_CUTOFF_MIN = <measured in Phase 0.4>
ODDS_CADENCE=10min  RESULTS_MERGE_CADENCE=20-30min  SETTLE_CADENCE=1h  FULL_REFIT=daily
```

## 7. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| New source has no intra-day results | medium | Blend is optional; Poisson alone is servable (parent: 63.5% decisive) |
| New source anti-bot harder than parent | medium | Playwright pattern proven; keep scraping fully decoupled from serving |
| betPawa auth churn | high (known) | Env-var secrets + loud health alerts; consider automating capture later |
| Thin history at launch | high | Warmup gate (parent skipped first 5 days); shrinkage handles small-n; widen tiers early |
| League is intrinsically less predictable | medium | Phase-4 AUC floor (0.58) is an explicit stop/reassess gate |
| Different dispersion regime | medium | Dispersion sign measured *before* family choice (Phase 4.3) |
| Silent duplicate/leak pathology in new source | medium | DB UNIQUE constraint + duplicate audit + leakage canary test + regen guard |

## 8. Day-one checklist for the new project window

1. Create repo, paste this document as `PLAN.md`.
2. Run all five Phase-0 probes; write results into `PLAN.md` §Phase 0 (they parametrize
   everything downstream).
3. Copy from the parent repo: `oddsfeed/protobuf_decode.py`, `oddsfeed/normalize.py`,
   `model/poisson.py`, `model/form.py`, `model/heuristic.py`, `model/elo.py`,
   `model/evaluate.py`, the Svelte components, and a saved `.bin` + raw HTML as test
   fixtures.
4. Scaffold Phase 1; commit the storage schema with the dedup constraint before any
   ingestion code exists.
5. Build in the phase order — each gate is cheap to check and expensive to skip.
