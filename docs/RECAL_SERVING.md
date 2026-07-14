# Serving change: per-line Platt recalibration (2026-07-09)

**What changed:** every model-covered O/U probability served by the predictor
is now passed through a per-line monotone map `p' = sigmoid(a·logit(p) + b)`
fit on the last 14 days of settled predictions. Validated walk-forward before
shipping ([FEATURE_IDEAS.md](FEATURE_IDEAS.md) §combiner option 4,
`model_runs` kind='recal'): tail calibration deciles −7.1/+4.9 → −1.6/+0.8
at line 3.5, Brier better at every line, AUC unchanged (map is monotone).

## Rollback

Set in the environment (or `.env`) and restart the predictor timer:

```
RECAL_ENABLED=false
```

That is the whole rollback — no code or data changes. With the flag off (or
whenever no line has enough settled data) the cycle prices exactly as before
this change, byte-identical.

**Identifying affected rows:** rows priced through an engaged map carry a
`-recal` suffix in `model_version` (e.g. `blend-w0.7-hl7-a0.01-recal`).
Rows without the suffix were priced through the raw Poisson map, including
every row from before this change and any cold-start/rolled-back period.
Since the population split (2026-07-11, [POPULATION_SPLIT.md](POPULATION_SPLIT.md)
Phase 0) the suffix tags the *row*, not the batch: `book` fallback rows,
unmapped lines, and schedule (`gtl:`) rows — which deliberately pass
through identity until schedule-population maps exist — stay untagged even
when the cycle's priced maps are engaged. Rows written between 07-11
(recal ship) and Phase 0 carry the old batch-level tag, so a `-recal` on a
schedule row in that window means "same batch as engaged maps", not
"touched by a map".

## Mechanics

- Fit: per half-line, on the served batch (last pre-kickoff prediction
  batch, same definition settlement grades) of settled, leak-clean,
  model-covered predictions from the last `RECAL_DAYS` (14) days. A line
  only gets a map with ≥ `RECAL_MIN_N` (300) graded samples; integer lines
  (push risk) never get maps. Everything unmapped passes through untouched.
- Cold start: as of shipping there were 124 settled events (~25–35 per
  line), so serving starts in identity mode and maps engage automatically
  as settlements accrue past the threshold — expected within days at
  current volume. `recal_lines=[...]` in the cycle stdout / report shows
  which lines are live each cycle.
- Settlement regen: `_Regen` applies the current maps so `regen_agrees`
  remains a λ-honesty canary. Known tolerance: maps are refit at settle
  time on slightly more data than the serving batch saw, so rare
  disagreements on near-coin-flip picks are map drift, not leakage —
  investigate regen dips only if they exceed a few events.

## Files touched

- [libs/py/model/recal.py](../libs/py/model/recal.py) — new: Platt
  fit/apply, `fit_line_maps`, `apply_to_line`
- [libs/py/core/config.py](../libs/py/core/config.py) — `RECAL_ENABLED`,
  `RECAL_DAYS`, `RECAL_MIN_N`
- [services/py/predictor/cycle.py](../services/py/predictor/cycle.py) —
  maps fit per cycle, applied after `totals_probs`, `-recal` version tag
- [services/py/settlement/settle.py](../services/py/settlement/settle.py)
  — regen applies the same maps
- [libs/py/model/evaluate.py](../libs/py/model/evaluate.py) — imports the
  shared Platt math from `recal`
- Tests: `tests/test_predictor.py` (fit gating, engage/sharpen/tag,
  cold-start identity, rollback flag), `tests/test_recal.py` (map math)

## Update 2026-07-11: hierarchical engagement for thin lines

**Why:** through 07-11 recal had never actually engaged — no line reached
`RECAL_MIN_N` (300) inside the 14-day window (best line ~133), so every
batch was priced through the raw map. Meanwhile the live tail miss the maps
were built for was visible at the lines that accrue slowest: settled line
5.5 realized 66.7% overs vs 43.7% mean served p_over (n=69, ~4 se), pick
hit 26%. The λ dynamic-range compression is shared across lines (fitted
per-line slopes a ≈ 1.21–1.32 everywhere) while only the intercept is
line-specific — so thin lines don't need their own 2-parameter fit.

**What changed:** `fit_line_maps` gained a second tier. Lines with
`RECAL_MIN_N_LINE` (75) ≤ n < `RECAL_MIN_N` (300) graded samples borrow the
slope from a joint shared-a/per-line-b fit pooled over all lines with ≥ 75
samples (the pool itself must clear 300). Lines at ≥ 300 keep their own
2-parameter fit, byte-identical to the original behavior; lines under 75
stay unmapped. Walk-forward validated 2026-07-11 (`model_runs`
kind='recal', 45 eval days, now including line 5.5): the shared-slope arm
matches full per-line Platt at every line (Brier within ±0.0001, AUC
unchanged), fitted shared a = 1.26, per-line b −0.25/−0.09/+0.09/+0.27 at
2.5/3.5/4.5/5.5.

**Rollback:** `RECAL_MIN_N_LINE=0` disables just the hierarchical tier
(per-line-only engagement, the original contract). `RECAL_ENABLED=false`
still disables everything.

**Known tolerance — mixed-regime windows:** the fit query does not filter
`model_version`, so after a λ-regime switch (club 07-10, tod 07-11) the
window briefly mixes residuals from both regimes. The maps are two
parameters refit every cycle on a rolling window; the drift washes out
within days. If maps look pathological right after a switch, wait for the
window to roll before suspecting the λs.

**Expected map shape — supersedes follow-up #3 below:** follow-up #3's
"suspect an upstream λ regression if |a−1.25| is large" was calibrated on
the *unconditional* walk-forward (every match scored at fixed lines), where
sharpening is right. Serving maps are fit on settled predictions — the
book-conditional population, where within-line discrimination is much
weaker (`model.evaluate conditional`: AUC ~0.60–0.64 vs ~0.72
unconditional) — so the honest fitted map there FLATTENS toward the line's
base rate (measured a ≈ 0.4 on the first engaging window, 07-11). That is
the correct correction for the population being priced, validated by the
conditional report's recal arm (Brier improves at every line/segment, AUC
unchanged). Judge engaged maps against the conditional report, not against
a = 1.25.

## Update 2026-07-11: per-population maps (population split Phase 2)

Maps are now fit per prediction population and never pooled
([POPULATION_SPLIT.md](POPULATION_SPLIT.md)): the priced fit is unchanged;
schedule (`gtl:`) rows are priced through their own maps fit on schedule
settlements (`fit_line_maps(..., population="schedule")`). Measured basis:
priced a ≈ 0.14 vs schedule a ≈ 1.30 — no shared calibration curve. The
cycle report shows `recal_lines` (priced) and `recal_lines_sched`
separately. Settlement regen mirrors serving per population and is
version-aware for recal: rows served without the per-row `-recal` tag
regen through identity. `RECAL_ENABLED=false` still disables everything
for both populations.

## Update 2026-07-13: raw-probability fit basis (closed-loop fix)

**Why:** the original fit consumed `predictions.p_over` — which, since maps
engaged on 2026-07-11, is the POST-map served value — while serving applies
maps to the raw `totals_probs` output. Fit and application disagreed about
what the map's input is, so once recal-tagged rows filled the fit window
(measured 2026-07-13: 33–67% of the pool per line, rising daily) each fit
measured the *residual* after the previous map and the fitted slopes decayed
toward identity. Observable damage at the point of diagnosis:

- priced and schedule fitted slopes "converging" — a loop artifact, not
  population convergence (the interaction test on raw probs recomputed from
  stored λs was still −0.68, 95% CI [−1.03, −0.38]: the populations remain
  distinct, per-population fitting stands);
- the priced hierarchical shared slope had gone NEGATIVE (a = −0.17 on
  2.5/4.5/5.5), inverting the model — line 4.5 mapped a stated 0.60-over to
  0.413, i.e. a 0.587 under, and could mint inverted under-picks above
  stated ~0.70. The honest raw-basis fit is a ≈ +0.30 (flatten, as the
  population split predicts).

**What changed:**

- `recal.fit_line_maps` recomputes each fit row's raw p_over from its stored
  `lambda_home/lambda_away` (`totals_probs` is deterministic given λ and
  line; reconstruction verified exact to 2.5e-5, the 4dp λ rounding). The
  stored p_over is no longer read anywhere in the fit. Fit and serve now
  share one target: raw → truth. The loop is structurally gone.
- Rows a map touches are tagged `-recal2` (fit generation 2). Plain
  `-recal` = the 07-11..07-13 served-prob fit generation. Settlement regen
  matches on the `-recal` substring, so both generations regen through the
  current maps; analytics can split generations on the exact tag (the
  semantics-drift lesson: the meaning changed, so the tag changed).
- Priced pick guard: while the priced population's maps are engaged, a line
  WITHOUT its own map surfaces no pick — unmapped lines are where the
  residual (sub-coin-flip, 47.6% measured) priced picks leaked through,
  6.5 above all (n=51 < RECAL_MIN_N_LINE, and the worst line in vs-book,
  skill −12.8%). With maps empty (cold start, rollback) every line picks
  exactly as before. Schedule rows are untouched by the guard.

**Expected and intended consequences — do not "fix" these:**

- A short regen-disagreement burst as rows served through the inverted 4.5
  map settle: regen refits with the raw-basis code, and those rows flip
  sides. Same artifact class as the 19 pre-Phase-0 rows POPULATION_SPLIT.md
  documents. Concentrated on line 4.5, gone once the pre-fix serving window
  has fully settled (~1–2 days of kickoffs). NOT a leak signal.
- Priced picks stay rare (the honest flatten maps stated 0.65 to ~0.54); a
  handful of extreme-confidence priced rows (stated ≳ 0.80) may clear the
  gate that the inverted maps were suppressing wholesale.
- The Phase 2 verification gate (~2026-07-18) must judge `-recal2` rows
  only — rows served under the contaminated maps are not evidence about the
  fixed ones.

**Rollback:** `RECAL_ENABLED=false` unchanged (disables fitting for both
populations; the pick guard is inert with empty maps, so rollback behavior
is byte-identical to pre-recal serving).

The same closed loop existed latently in `h2h.fit_stacker` (stored
`predictions_x12.p_home/p_away` are post-stack on `-h2h` rows and the fit
read them back); fixed the same way — the raw decisive share is recomputed
from the row's stored λs — BEFORE `X12_H2H_ENABLED` ever goes live, so the
x12 fit pool was never contaminated.

## Post-ship follow-ups

1. **Watch the first mapped week's scorecard** (`settlement.settle
   scorecard`): hit rate by tier should hold or improve while pick counts
   rise (underconfident tails were suppressing true strong picks — the
   top decile's mean served prob rises from ~0.79 to ~0.83).
2. **Re-quantile tier thresholds** (`TIER_LEAN/SOLID/STRONG`) after the
   first settled week with maps active, per plan §2.1.8 — the confidence
   distribution shifts right once tails decompress.
3. If maps ever look pathological (|a−1.25| large or |b| > 0.5 on a full
   window), suspect an upstream λ regression before blaming the maps —
   they are two parameters fit on hundreds of samples and were stable in
   validation.
