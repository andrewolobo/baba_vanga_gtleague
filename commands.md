# Commands

All commands run from the repo root unless noted. Python commands use the
project venv (`.venv`). Data lives in `data/gtleague.db` (SQLite).

## One-time setup

```shell
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cd services/api && npm install && cd ../..
```

Copy `.env.example` to `.env`. The only value that may need refreshing is
`BETPAWA_FINGERPRINT` (re-capture from browser devtools into
`scraper/betpawa.curl` if the odds fetch starts returning 401/403).

## Run the application (API + web UI + pipeline)

```shell
cd services/api
npm run dev
```

Then open **http://localhost:8787**.

Starting the API also starts the pipeline: results merge every 10 min, odds
fetch every 5 min, a prediction cycle after each ingest, settlement every
15 min. Nothing else needs to be running.

- Different port: set `PORT=8888` before `npm run dev`.
- API only, no pipeline jobs: set `GTL_NO_JOBS=1`.
- `npm run dev` restarts on source changes; `npm run start` doesn't.

## Pipeline pieces, run by hand

Scrape today's results (idempotent merge, safe to re-run):

```shell
.venv/Scripts/python -m results_ingest.cli today
```

Scrape a date range (inclusive; ~1 year of history is available):

```shell
.venv/Scripts/python -m results_ingest.cli backfill --from 2026-05-01 --to 2026-06-07
```

Fetch the current betPawa odds slate:

```shell
.venv/Scripts/python -m odds_ingest.cli fetch
```

Predict the upcoming slate (appends to the predictions table):

```shell
.venv/Scripts/python -m predictor.cycle
```

Settle finished fixtures and show the rolling scorecard:

```shell
.venv/Scripts/python -m settlement.settle run
.venv/Scripts/python -m settlement.settle scorecard --days 7
```

## Model evaluation

```shell
.venv/Scripts/python -m model.evaluate gate          # walk-forward metrics vs gates
.venv/Scripts/python -m model.evaluate dispersion    # distribution-family check
.venv/Scripts/python -m model.evaluate sweep-blend   # blend-weight sweep
.venv/Scripts/python -m model.evaluate sweep-poisson # half-life x alpha sweep
```

## Tests

```shell
.venv/Scripts/python -m pytest -q
```

All tests are offline (saved fixtures in `scraper/fixtures/`); no network or
tokens needed.

## Operations

Backup now (also runs nightly while the app is up; keeps the last 7 under
`data/backups/`, prunes model artifacts >7d and raw odds >14d):

```shell
.venv/Scripts/python -m store.backup
```

To restore: stop the app, copy a `data/backups/gtleague_YYYYMMDD.db` over
`data/gtleague.db`, start the app.

**Downtime is self-healing.** On boot the scheduler checks for missing result
days and backfills the gap automatically, and the daily model artifact refits
itself when backfilled data changes what it was trained on. The status strip
shows "results gap" while catch-up runs. Nothing manual is needed.

Weekly review (5 minutes): open the Analysis tab — hit rate trending near the
backtest (~66% decisive)? regen agreement ~100% and leak flags 0 on the
Ledger scorecard? If tiers drift from their labels, re-run the sweeps
(`model.evaluate sweep-blend`, `sweep-poisson`) and re-check calibration.

## Exit codes worth knowing

- results CLI `2` — data-quality anomaly flagged (rows still landed; details
  in the per-day output lines)
- odds CLI `3` — feed auth failure: re-capture `scraper/betpawa.curl`,
  refresh `BETPAWA_*` values in `.env`
- settlement CLI `2` — a settlement was flagged `leak_risk` (investigate
  before trusting metrics)
