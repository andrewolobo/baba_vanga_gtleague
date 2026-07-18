Commands

All commands run from the repo root unless noted. Python commands use the
project venv (`.venv`). Data lives in `data/gtleague.db` (SQLite).

## One-time setup

```shell
#To pullt o linux server
python -m http.server 8080


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

## Model vs the book

Scores every settled prediction — including the o-vigged Over
price before kickoff. The feed ships margin-free probabilities, so this is the
book at its strongest, not the book plus its margin.

```shell
.venv/Scripts/python -m settlement.settle vs-book --days 7
.venv/Scripts/python -m settlement.settle vs-book --days 30 --boot 500
.venv/Scripts/python -m settlement.settle vs-book --days 30 --tag recal2
```

`--boot` sets the bootstrap resamples behind every confidence interval
(default 2000; lower it for a faster answer, raise it for stabler tails).
`--seed` makes a run reproducible.

`--tag` keeps only rows whose `model_version` contains the substring — the
clean-generation read for a window that mixes serving generations (e.g.
`--tag recal2` scores only rows priced by the post-2026-07-13 raw-basis
maps; rows priced through the closed-loop `-recal` maps and pre-recal rows
drop out). The header reports how many rows the tag excluded. Works on
`x12-vs-book` too. Substring matching, so mind prefixes: `recal` also
matches `-recal2` — use `recal2` when you mean the clean generation.

Rows are excluded when they are leak-flagged, when the result pushed on an
integer line, or when the model had no coverage of a player — those served the
book's own price back, and scoring them against the book scores the book
against itself.

**How to read it.** The book sets each line so the market sits near a coin
flip, which pins model and book Brier to nearly the same number no matter what
either one knows; the printed `constant 0.500` row is there to show you how
little Brier can separate. So do not read the Brier line first. Read these:

- **paired Brier CI** — if it straddles zero, the run has told you nothing
  about which is better, and the sample-size line says how much more data a
  verdict needs.
- **edge coef** — the real question: does the model's disagreement with the
  price predict the outcome once the price itself is accounted for? Positive
  with a CI clear of zero means genuine edge.
- **book coef** — a sanity rail. The closing line is a strong predictor given
  enough samples, so a book coefficient whose CI spans zero means the sample is
  too small to score _anything_, and every other number on the page is noise.
- **lambda slope** — 1.0 is a calibrated mean. Above 1.0 the model is shrinking
  λ toward the league average while the market commits further, which is a
  probability-map problem (see [docs/FEATURE_IDEAS.md](docs/FEATURE_IDEAS.md)
  §4), not a bad-λ problem.

The by-tier table is only meaningful once the tier labels and the served probs
come from the same code that is running now: tiers are written at prediction
time, and rows priced before the per-line Platt maps activate
(`model_version` without a `-recal` suffix) are not comparable to rows priced
after.

### 1x2 vs the book

Same question asked of the 1x2 head: the served home/draw/away triple vs the
last de-vigged 1x2 price before kickoff. Priced population only by
construction — schedule (`gtl:`) rows have no book side. Rows settled without
a stored 1x2 close are excluded (the count is printed).

```shell
.venv/Scripts/python -m settlement.settle x12-vs-book --days 7
.venv/Scripts/python -m settlement.settle x12-vs-book --days 30 --boot 500
```

`--boot` / `--seed` behave as in `vs-book`.

**How to read it.** Same discipline as the totals command — the headline
multiclass Brier is a weak lens (both series sit near the outcome base rates),
so read past it:

- **edge coef** — fit on the DECISIVE SHARE `s = p_home/(p_home+p_away)`,
  decisive rows only, because that is the only axis serving can move: the H2H
  stacker reshapes `s` and `p_draw` is served raw. Positive with a CI clear of
  zero means the model's disagreement with the 1x2 price carries information
  the price does not have.
- **book coef** — the same sanity rail as `vs-book`: CI spanning zero means
  the sample can't score anything yet.
- **draw head line** — the model's raw `p_draw` vs the book's. This head has
  no stacker and no recal; if its Brier drifts well past the book's, that is a
  λ/PMF problem, not a stacker problem.
- **by h2h regime** — rows the stacker touched (`-h2h` in `model_version`)
  vs rows it didn't. This is the live read on whether the stacker earns its
  keep against the book; judge it only once the `-h2h` bucket has a few
  hundred rows, and never compare hit rates across regimes at different
  pick-gate mixes.

`roi` is flat 1u on the model's argmax at the close for every row, picks or
not — a diagnostic, not a strategy.

## Model evaluation

```shell
.venv/Scripts/python -m model.evaluate gate          # walk-forward metrics vs gates
.venv/Scripts/python -m model.evaluate dispersion    # distribution-family check
.venv/Scripts/python -m model.evaluate sweep-blend   # blend-weight sweep
.venv/Scripts/python -m model.evaluate sweep-poisson # half-life x alpha sweep
.venv/Scripts/python -m model.evaluate conditional   # book-population eval:
                       # each match scored only at the half-lines straddling
                       # its own E[total] — the tail-line (4.5/5.5) skill check
.venv/Scripts/python -m model.evaluate h2h           # pairwise H2H gate,
                       # 1x2 + totals, with the skill/pace control arms and
                       # the half-life x shrinkage sweep (docs/H2H_FEATURE.md)
```

## H2H stacker engagement tracker

How close each population is to the `X12_H2H_MIN_N` (500) decisive graded
rows the stacker needs before `X12_H2H_ENABLED=true` does anything. Counts
through h2h's own fit queries, so it can never drift from the engagement
logic. Read-only, safe while the app runs.

```shell
.venv/Scripts/python scripts/h2h_accrual.py
```

Prints per-population `n/500`, the home/away outcome split (a one-outcome
window cannot fit), the current accrual rate, and an ETA. Exit code 0 once
every population is READY — usable as a check in a loop or a scheduled
task. Flipping the flag early is safe: a below-bar population serves
identity, untagged, and engages automatically when it clears.

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
