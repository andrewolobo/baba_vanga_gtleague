# Population split: settle schedule-only predictions, per-population recal

Status: Phases 0–3 DONE (0–2 on 2026-07-11; Phase 3 on 2026-07-12 — owner
chose to surface schedule picks in the web app). Phase 4 gated as planned.
Phase 2 one-week verification gate due ~2026-07-18.

## Why (measured 2026-07-11)

Served picks were underperforming suppressed rows (45.3% vs 52.8% on 86/214
post-07-10 graded rows). Root cause is not model rot — it is adverse
selection against the book, visible only because the two prediction
populations were graded separately for the first time:

| population                  | picks | hit    | stated conf | Platt slope a |
|-----------------------------|-------|--------|-------------|---------------|
| book-priced (settled today) | 86    | 45.3%  | 0.651       | 0.14          |
| schedule-only (`gtl:`)      | 676   | 64.1%  | 0.640       | 1.30          |

Schedule-only tiers are monotone and honest (lean 57.0 / solid 66.0 /
strong 71.2%), calibration deciles within ±8 pts. On priced rows, hit rate
is flat ~50% across every model−book edge bucket and *drops* to 45% in the
biggest-disagreement bucket: when the model fights an efficient price, the
model is usually the one wrong. The model is calibrated almost everywhere
except where it disagrees with a book price.

The populations do NOT share a calibration curve: interaction test
`y ~ logit(p) * is_priced` gives slope_x_priced −1.17, 95% CI
[−2.21, −0.08], P(different) ≈ 0.98. A unified map fit on both pools is
near-identity (a ≈ 1.01, the 3× larger schedule pool drowns the priced
signal) and, applied to priced rows, surfaces 53 "picks" that hit 50.9% —
it re-opens the exact hole recal exists to close. **Unified maps are ruled
out by measurement; fit per population, never pooled.**

Two structural findings drive the phases below:

1. `gtl:` events never settle — `settle.run` iterates `fixtures`, which
   schedule events are not in. ~1,314 gradeable rows (and ~440/day ongoing,
   ~3× the priced rate) carry results in `matches` but no grades.
2. `cycle.run_cycle` fits recal maps on settled (= priced-only) rows but
   applies them to schedule rows too (`_schedule_row(..., maps)`). As the
   priced maps saturate toward a ≈ 0.14 they will drag calibrated 0.64
   schedule confidences below the 0.60 pick gate — killing the model's
   *best* picks with a correction learned from its *worst* population.
   This is live now (recal shipped 2026-07-11) and grows as maps engage.

Population identity, precisely: at the *game* level priced games are a
subset of the league schedule (settlement grades via `matches`). At the
*prediction-row* level the populations are nearly disjoint by construction —
`_scheduled_games` only emits a `gtl:` row when no book fixture covers the
game at prediction time (the `taken` set). Of 694 graded schedule picks,
8 later matched a book fixture and 2 had a price on the same line/side, so
schedule wins are informational, not directly bettable at betPawa.

## Phase 0 — stop applying priced maps to schedule rows (urgent, tiny)

DONE 2026-07-11: `_schedule_row` receives `{}`; `-recal` is per-row via
`_row_version` (only rows a map touched); tests
`test_priced_recal_maps_never_touch_schedule_rows` and the updated tag
test. Rows written between the recal ship and Phase 0 carry the old
batch-level tag (see RECAL_SERVING.md).

`services/py/predictor/cycle.py`: pass `{}` instead of `maps` to
`_schedule_row` until schedule-population maps exist (Phase 2 replaces the
`{}` with `maps_sched`). Identity is measurably close to correct for this
population (a ≈ 1.30, own-map Brier 0.2384 vs raw 0.2388).

Version-tag integrity (the confidence-column lesson: semantics drift with
no tag change is how the last incident happened): the `-recal` suffix is
currently computed once per cycle from `maps` being non-empty and stamped on
every row. Once populations diverge, compute the suffix per population —
schedule rows must not carry `-recal` while passing through identity.
Cheapest shape: build `version` without the suffix, append per-row-family
in `_line_row` / `_schedule_row` based on whether that row's maps engaged.

Tests: schedule rows priced with empty maps are byte-identical to
pre-recal output; priced rows unchanged; `-recal` appears only on rows a
map actually touched.

Rollback: none needed — this restores pre-recal behavior for one
population. Note the live-timer hazard: the API spawns cycles on a timer,
so this takes effect on the next cycle after merge, no deploy step.

## Phase 1 — settle `gtl:` events

DONE 2026-07-11: `_run_schedule` shipped as planned; backfill settled 705
events, 0 pending, 0 leak flags. Verification gate PASSED — line-row picks
704 @ 63.1% stated 0.640 (probe: 676 @ 64.1% stated 0.640; the settled set
includes post-probe accrual), tiers monotone 55.4/65.6/70.7 (probe
57.0/66.0/71.2). Regen agreement 100.0% on the 1,020 rows served outside
the priced-map window; the only 19 disagreements are rows batch-tagged
`-recal` pre-Phase-0 (priced maps briefly applied to schedule serving) at
near-gate confidences 0.601–0.671 — the documented map-intercept side
flip, not λ dishonesty. One deviation from the plan text: `_Regen.pick`
gained a `maps` override and the schedule loop passes `{}`, mirroring
Phase 0 serving — without it, regen would apply priced maps that serving
no longer applies and fake disagreement on near-coin-flip rows.

New loop `_run_schedule` in `services/py/settlement/settle.py`, following
the `_run_x12` pattern (own candidate query, own NOT EXISTS), writing to the
existing `settlements` table. **No migration needed**:

- `event_id` PK cannot collide (book ids never start with `gtl:`).
- Every column keeps its meaning (same totals market). The x12 rationale
  for a separate table — columns meaning different things — does not apply.
- Safety-by-default: every existing consumer (`scorecard`, `vs_book`,
  `tier_bands`, `recal._FIT_QUERY`) joins `settlements` through `fixtures`,
  so schedule settlements are invisible to all of them until a query is
  extended deliberately. Population is derivable as
  `event_id LIKE 'gtl:%'`; no flag column.

Candidate query joins `matches` directly — an exact-ID join, cleaner than
the priced path's fuzzy player+kickoff-window join:

```sql
SELECT m.* FROM matches m
WHERE m.status = 3 AND m.home_ft IS NOT NULL
  AND EXISTS (SELECT 1 FROM predictions p
              WHERE p.event_id = 'gtl:' || m.source_match_id)
  AND NOT EXISTS (SELECT 1 FROM settlements s
                  WHERE s.event_id = 'gtl:' || m.source_match_id)
  AND m.kickoff_ts <= :ready
```

(String concatenation, not CAST — `source_match_id` is TEXT and the cycle
builds the id as `f"gtl:{source_match_id}"`.)

Per event: served row = last batch before `m.kickoff_ts` (same
`_served_prediction` ordering); grade against `home_ft + away_ft`;
`matched_match_id = m.id`; `offset_min_used = 0` by construction.

Regen canary: works unchanged — build the fixture-shaped dict the same way
`_scheduled_games` does (home_raw from `*_club (*_player)`), call
`_Regen.pick`. Model-version flags are read off the served row as today.

Leak guard: weaker than the priced path and the plan should say so.
`matches.scraped_at` is overwritten on re-scrape, and a finished game has
necessarily been scraped after kickoff, so `scraped_at <= predicted_at`
(definite leak: the score was in the DB before prediction) will almost never
fire even when it should. Use it anyway (it is the conservative direction;
probe measured 0 leak-shaped rows), and note that the real protections are
the cycle's as-of cutoff and the regen check, which are population-blind.
If a hard guard is ever wanted: migration adding
`matches.finished_seen_at`, set by ingest when status flips to 3 — deferred,
not needed to ship.

Backfill: free. The NOT EXISTS back-settles all ~1,300 historical rows on
the first run. Regen cost is bounded by the per-day model cache in `_Regen`
(~3 distinct days currently).

Config: `schedule_settle_enabled: bool = True` in `core/config.py` as the
one-flag rollback (additive rows, invisible to existing queries, so
default-on is safe). Same live-switch caveat as `club_enabled`.

Tests (`tests/test_settlement.py` or a new `test_settlement_schedule.py`):
exact-join settles the right match and never a decoy replay (the ±2.5 h
replay hazard does not exist here — assert the join is by id, not window);
NOT EXISTS idempotency; pick_correct/regen_agrees NULL semantics for
no-pick rows; scorecard/vs-book/tiers outputs unchanged by the presence of
schedule settlements.

Verification gate: after backfill, recompute the schedule-population hit
rates from the `settlements` table and confirm they reproduce the probe
(64.1% picks, monotone tiers). Disagreement means the settlement join and
the probe graded different batches — stop and reconcile.

## Phase 2 — per-population recal maps

DONE 2026-07-11: `fit_line_maps` gained a `population` parameter
(`_FIT_QUERY_SCHEDULE` routed through `settlements.matched_match_id`);
the cycle fits `maps` + `maps_sched` and reports `recal_lines_sched`.
One extension beyond the plan text: `_Regen.pick` is now **recal
version-aware** the way `_lambdas` is for club/tod — a row whose served
`model_version` lacks the (per-row since Phase 0) `-recal` tag was served
through identity and is regenerated through identity, whatever maps are
engaged at settle time. Without this, the moment `maps_sched` engaged,
every schedule row served in the Phase 0/1 identity window would fake
regen disagreement via the map-intercept side flip (the measured
pre-Phase-0 artifact, 19 rows at 84.3%).

First live fit (read-only preview at ship): priced maps a ≈ 0.014 (the
full flattening, as intended), schedule maps a ≈ 1.17–1.84 with per-line
intercepts. Applied retrospectively to all 1,418 graded schedule
line-rows: gate survivors 717 @ 64.6% under the maps vs 659 @ 64.3% raw —
schedule picks survive and sharpen slightly, exactly the probe's
prediction.

`libs/py/model/recal.py`: second fit query for the schedule population.
Route it through `settlements` (indexed PK join), not a substr join:

```sql
SELECT p.line, p.p_over, s.result_total
FROM settlements s
JOIN matches m ON m.id = s.matched_match_id
JOIN predictions p ON p.event_id = s.event_id
 AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                       WHERE event_id = s.event_id
                       AND predicted_at < m.kickoff_ts)
WHERE s.event_id LIKE 'gtl:%' AND s.result_total IS NOT NULL
  AND s.leak_risk = 0 AND s.settled_at >= ?
```

`fit_line_maps` gains a `population` parameter selecting the query (or a
thin `fit_schedule_line_maps` wrapper — implementer's choice; the fitting
machinery including the hierarchical shared-slope tier is reused as-is).
The hierarchical tier remains valid *within* a population; the one thing
the measurement forbids is sharing slope *across* populations (0.14 vs
1.30 is precisely the pooled parameter).

`cycle.run_cycle`: fit both `maps` (priced, unchanged) and `maps_sched`;
pass `maps_sched` to `_schedule_row` (replacing Phase 0's `{}`). Reuse the
existing `recal_days` / `recal_min_n` / `recal_min_n_line` knobs for both
populations — schedule accrues ~3× faster so it engages sooner with the
same thresholds; add per-population knobs only if a measured reason
appears.

Expected and intended consequences — write them down so nobody "fixes"
them later:

- Priced picks will nearly vanish once the priced map saturates (a ≈ 0.14
  maps stated 0.65 to ~0.52, below the 0.60 gate). That is the honest
  output: on book-priced lines the model currently has no pick-grade
  information. Picks against the book should return only if the vs-book
  edge coefficient ever turns positive (Phase 4 gate).
- Schedule picks survive with a near-identity map (probe: 376 picks at
  64.1% under own map). Tier volume shifts almost entirely to the
  schedule population; `tier_bands` quantiles must be computed per
  population or they will be schedule-dominated (see Phase 3).

Tests (`tests/test_recal.py` additions): the two fit queries select
disjoint row sets; schedule maps never fed by priced settlements and vice
versa; identity fallback per population when its own pool is thin;
per-population `-recal` tagging from Phase 0 now driven by the correct map
set.

Verification gate: one week after ship, rerun the calibration-decile probe
per population on recal-ON rows only. Pass = schedule deciles stay within
±8 pts with picks surviving; priced picks rare-to-zero. Judge the priced
map by `model.evaluate conditional`, not by the fitted `a` (live maps
flatten by design — see docs/RECAL_SERVING.md and the recal memory note).

Gate amendment (2026-07-13): the fit basis changed mid-window — maps are
now fit on raw probs recomputed from stored λs, tagged `-recal2`
(docs/RECAL_SERVING.md 2026-07-13 update: the original served-prob fit was
consuming its own output, and the priced shared slope had gone negative).
Judge the gate on `-recal2` rows only; plain `-recal` rows (07-11..07-13)
were served under the contaminated fit generation and are not evidence
about the fixed maps. If `-recal2` accrual is thin by 07-18, slide the
gate to ~07-20 rather than pooling generations.

## Phase 3 — analytics and product surfacing

DONE 2026-07-12. Product decision resolved: the product surfaces
*predictions* — schedule picks are in the web app, population always
labeled, bettability never implied.

- CLI: `settle scorecard` and `settle tiers` emit two sections
  (`== book-priced ==` / `== schedule (model-only) ==`, ASCII headers —
  cp1252 console). First split run confirmed the thesis in production:
  priced 163 graded @ 47.2% (Brier 0.2708) vs schedule 631 @ 63.9%,
  tiers monotone 57.5/64.6/70.7 (Brier 0.2284). `vs-book` stays
  priced-only by definition.
- `tier_bands` quantiles per population; the schedule section now drives
  proposals. First proposal (TIER_SOLID=0.6404, TIER_STRONG=0.689–0.692,
  stable across 7d/14d windows; graded 59.5/64.6/77.8 under it) was
  APPLIED 2026-07-12: `TIER_STRONG` 0.68 → 0.69, `TIER_SOLID` unchanged
  at 0.64. Bands apply to both populations. Rows keep the tier they were
  served with, so tier analytics spanning 2026-07-12 mix band regimes —
  split on that date if it matters.
- API (`services/api/src/routes.ts`): `/api/metrics` returns
  `{ priced, schedule }` blocks (shape unchanged inside); `/api/settlements`
  and `/api/analysis` merge schedule rows tagged `population:
  'priced'|'schedule'`; the analysis `daily` series stays priced-only.
- Web (`apps/web/public`): scorecard renders both populations (model-only
  first — it is the product's real signal; the book-priced label says
  "picks are rare by design"); header badge is the model-only 7d hit;
  settled cards carry a "◌ model-only" badge; the Performance tab gets a
  population filter + BY POPULATION breakdown and its caveat notes
  model-only rows are informational, not bettable; the Analysis tab stays
  the model-vs-book view (priced-only, stated in its meta line).

Original scoping notes (kept for context):
- The pooled single-population view systematically reported the model's
  worst subpopulation and is what made the model look broken.
- Schedule picks are the strongest output (strong tier 71.2%) but are not
  bettable at betPawa (2/694 ever priced).

## Phase 4 — gates for anything fancier (meta-model deferred)

In order, each gating the next:

1. Re-measure picked-vs-suppressed on priced recal-ON rows after maps
   saturate (~1 week). The 45%-hit pick population should be gone by
   construction. If picks still surface and still lose, something beyond
   calibration is wrong — stop and diagnose before any meta-model.
2. Edge-coefficient go/no-go at ≥ ~1k settled priced rows:
   `settle vs-book` edge coef > 0 with CI clear of zero is the ONLY
   condition under which picks that fight the book can ever be positive
   signal. Until then, no confidence model can rescue them, including a
   meta-model.
3. Meta-labeling model (P(argmax side correct | features), walk-forward
   logistic first, GBM only at ≥ ~5k rows) becomes worth building only if
   (1) or (2) leaves value unexplained. With schedule settlements flowing
   (~600 graded rows/day), the data floor arrives in days, not weeks —
   but the exploration already suggests its dominant feature is just
   `has_book_price` × distance-from-book, which Phases 0–2 encode as
   architecture instead of learned parameters.

## Probe provenance

Read-only scripts (scratchpad, not in repo): picked-vs-suppressed and
edge-bucket tables; counterfactual grading of 1,314 `gtl:` rows;
later-priced overlap (8/694 fixtures, 2 same-line prices); pooling
interaction test (n=348 priced / 926 schedule). Rerun against
`settlements` after Phase 1 to promote these numbers from probe to
tracked metrics.
