# baba.vanga.gtleague

Over/Under prediction engine for **GT Leagues** (FC25 esoccer). Results from
`api.gtleagues.com`, odds from betPawa. Architecture and phase plan:
[docs/SPINOFF_PLAN.md](docs/SPINOFF_PLAN.md) · measured source behavior:
[docs/PHASE0_PROBES.md](docs/PHASE0_PROBES.md) · feed wire format:
[docs/BETPAWA_FEED.md](docs/BETPAWA_FEED.md).

## Layout

```
libs/py/core/               schemas, market math, typed .env config
libs/py/store/              SQLite (WAL) + migrations + repositories
services/py/results_ingest/ GT Leagues results API -> matches table
services/py/odds_ingest/    betPawa protobuf feed -> fixtures + odds_snapshots
scraper/fixtures/           saved raw responses = offline test fixtures
data/                       gtleague.db + raw archives (gitignored)
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp .env.example .env   # then fill BETPAWA_FINGERPRINT (see scraper/betpawa.curl)
```

## Commands

```bash
.venv/Scripts/python -m pytest -q                                        # offline tests
.venv/Scripts/python -m results_ingest.cli backfill --from 2026-06-08 --to 2026-07-08
.venv/Scripts/python -m results_ingest.cli today                         # incremental merge
.venv/Scripts/python -m odds_ingest.cli fetch                            # live slate -> DB
.venv/Scripts/python -m odds_ingest.cli fetch --from-file scraper/fixtures/betpawa_gtleagues_2026-07-08.bin
.venv/Scripts/python -m odds_ingest.cli unmatched                        # alias worklist
.venv/Scripts/python -m predictor.cycle                                  # predict slate -> predictions
.venv/Scripts/python -m model.evaluate gate                              # walk-forward scorecard
```

All ingests are idempotent merges; `odds_snapshots` and `predictions` are
append-only. Exit codes: results CLI `2` = data-quality anomaly; odds CLI `3` =
feed auth failure (re-capture `scraper/betpawa.curl`, refresh `.env`).

## Status

- [x] Phase 0 — feasibility probes (all gates passed)
- [x] Phase 1 — scaffold, core, storage
- [x] Phase 2 — results ingestion
- [x] Phase 3 — odds ingestion
- [x] Phase 4 — model port + re-validation (all gates passed; see
      [docs/PHASE4_RESULTS.md](docs/PHASE4_RESULTS.md) — blend AUC ~0.72, served
      constants hl=7d, α=0.01, w=0.7)
- [x] Phase 5 — predictor service (`python -m predictor.cycle`; cold 2.7s /
      warm 0.5s; scheduler deferred — the Phase-7 Node API spawns ingest +
      cycle on its own timer in dev)
- [x] Phase 6 — settlement & scorecard (`python -m settlement.settle run|scorecard`;
      exact-kickoff join, leak_risk + served-vs-regen guards)
- [x] Phase 7 (first iteration) — Node API + web UI: `cd services/api && npm run dev`
      → http://localhost:8787 (read-only API over SQLite via node:sqlite, SSE push,
      embedded dev scheduler spawning the Python CLIs; static broadsheet UI in
      apps/web/public). `GTL_NO_JOBS=1` starts the API without the scheduler.
- [x] Phase 8 — hardening & ops: boot-time results-gap catch-up, self-invalidating
      model artifacts (training-data fingerprint), nightly checkpointed DB backup +
      retention (`python -m store.backup`), data-gap health indicator. Ops notes in
      [commands.md](commands.md).
