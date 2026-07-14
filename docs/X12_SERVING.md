# Serving head: 1x2 match-winner (built 2026-07-11, ships dark)

**What this is:** the predictor now optionally prices home/draw/away for every
model-covered fixture, off the **same served side-λs** as the totals rows — no
new model, no new parameters, one pmf convolution
(`core.markets.x12_probs`). Gate that justified it:
[CLUB_FEATURE.md](CLUB_FEATURE.md) §1x2 measurement gate (walk-forward PASSES;
vs book not significantly worse on λs that were two-thirds club-blind).

**Status: OFF by default.** `X12_ENABLED=true` turns it on; the API's timer
makes any default-True flag an instant ship, which this repo has done by
accident once already.

## Rollback

`X12_ENABLED=false` (or simply never set it). The totals path is untouched by
this feature in either state — x12 writes to its own tables.

## Mechanics

- `predictions_x12` / `settlements_x12` (migration `003_x12.sql`). Separate
  tables, not a `market` column: totals columns (line, p_over/p_push,
  result_total) mean nothing for a 3-outcome market, and overloading them is
  the semantics drift the confidence-column incident documented. Separate
  NOT EXISTS loops also make the transition safe — events already settled for
  totals still get x12 grading once x12 rows exist.
- One row per covered fixture per cycle (book fixtures *and* `gtl:` scheduled
  games). No book fallback: an uncovered fixture gets no row.
- **Pick gate `X12_PICK_PROB_THRESHOLD = 0.50`, measured — NOT the totals
  0.60.** Max-of-three lives on a different scale: its median is 0.434 on this
  league, so 0.60 would surface ~1% of matches. At 0.50 the walk-forward frame
  shows 11% of matches picked at 59.1% hit (fair odds 1.69; book home/away
  averages ~2.3). The draw is never the argmax at these λs — picks are
  home/away in practice.
- No tiers yet, deliberately: tier bands must be quantiled on *served* pick
  confidence (the totals tier incident), and there is none until this runs.
  `confidence` is stored; bands can be derived later the `settle tiers` way.
- `value_flag`: model prob − book de-vigged implied ≥ `MIN_EDGE`, using the
  latest stored 1x2 snapshot. Scheduled (`gtl:`) rows never flag — no price.
- Settlement grades `result` home/draw/away from the matched match and extends
  the **regen canary** — via TWO loops since 2026-07-12: fixtures
  (`_run_x12`) and schedule/gtl (`_run_x12_schedule`, added after the
  fixtures-only original left gtl x12 rows permanently unsettled): `_Regen.x12_pick` re-derives the pick from honest λs
  through the same `store.aliases` resolution, version-aware (`-club`/`-tod`
  read off the served row). Known tolerance: the gate threshold is read from
  current settings at regen time, so changing it mid-flight flips agreement on
  near-gate picks — config drift, not leakage.
- `model_version` is shared with the totals rows (same λ generator), so club-
  and tod-attribution carries over for free.

## Verified

- 115 tests pass; `tests/test_predictor_x12.py` covers: off-by-default with
  totals untouched, probs sum to 1, gate contract (pick iff max ≥ threshold),
  value needs a stored price, grading of wins/losses/draw-no-pick, regen
  agreement, idempotent settlement.
- Dry run on a production snapshot (`X12_ENABLED=true`): 88 rows over 13 book
  fixtures + 75 scheduled, 6 picks (2 home / 4 away), all rows sum to 1,
  versions `blend-w0.7-hl7-a0.01-club-tod`.

## Not done (deliberate)

- **API/UI exposure.** `services/api/src/routes.ts` reads only `predictions`;
  `predictions_x12` is invisible to the web app until an endpoint is added.
  The head can accrue a settled track record dark before anything is shown.
- **Tier bands** — see above; needs served data.
- **A 1x2 vs-book scorecard** (the analogue of `settle vs-book`). Worth
  building once `settlements_x12` has volume; the edge-coefficient regression
  is the test that matters, and it needs a few thousand graded rows.

## Turning it on

Set `X12_ENABLED=true` in `.env`. Expect ~35–40 picks/day at current league
volume (11% of ~340 matches). Watch `x12_settled` / `regen_agrees` in
`settlement.settle run` output for the first day; hit rate should trend toward
the walk-forward 59% on gate-clearing picks, judged only after a few hundred
graded picks (at 60%/40% base odds symmetry, 100 picks resolves nothing).
