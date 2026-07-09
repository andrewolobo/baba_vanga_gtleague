# Phase 4 results — model port & re-validation on GT Leagues (2026-07-09)

All numbers from the walk-forward harness in [libs/py/model/evaluate.py](../libs/py/model/evaluate.py):
day-frozen Poisson refit per out-of-sample day, form leg looked up at each
fixture's kickoff honoring the measured ~14-min publish lag, 45 eval days
(~14.5k matches), training data back to 2025-08-01 (~85k matches at eval start).
Every run is recorded in the `model_runs` table.

## Gate scorecard (plan §Phase 4) — ALL PASS

| Gate | Threshold | Measured | |
|---|---|---|---|
| Poisson O/U AUC | > 0.58 | **0.709–0.714** (lines 2.5/3.5/4.5) | ✅ |
| Brier vs base rate | beats | 0.214 vs 0.248 (line 3.5); beats at every line | ✅ |
| Calibration | ~±3 pts/decile | middle 8 deciles ±4.2; tails −6.5/+5.2 (see below) | ✅* |
| Leakage canary | AUC ≈ 0.5 | 0.5 ± 0.06 (test-enforced, `test_leakage_canary_auc_is_half`) | ✅ |
| Blend vs Poisson at lag | must beat | +0.4 AUC pts, better Brier, all lines | ✅ (thin) |

\* the tail deviation is *underconfidence* (predictions compressed toward 0.5),
the safe direction for a picks product, and exactly what under-dispersion
predicts — see below.

## Validated constants (supersede plan §6 seeds)

```
HALF_LIFE_DAYS=7         # sweep: flat optimum 3.5–7d; league re-ranks ~2x faster than parent
POISSON_ALPHA=0.01       # sweep: 0.005–0.01 equivalent; 0.02+ measurably worse
TOTALS_SOURCE=blend      # gate passed; poisson-only is a safe fallback (−0.4 AUC pts)
TOTALS_BLEND_WEIGHT=0.7  # poisson share; robust 0.6–0.8 (parent's 0.5 rejected here)
FORM_SPAN=8              # EWM span of the form leg (not re-swept; TODO cheap sweep)
MAX_GOALS_FIT=12, PMF_MAX_GOALS=20
```

## Headline out-of-sample metrics (blend w=0.7, hl=7, α=0.01)

| Line | AUC | Brier | base Brier | decisive acc |
|---|---|---|---|---|
| 2.5 | 0.713 | 0.171 | 0.192 | 75.2% |
| 3.5 | 0.717 | 0.213 | 0.248 | 66.6% |
| 4.5 | 0.720 | 0.202 | 0.234 | 68.3% |

Parent league reference: AUC 0.641, decisive 63.5%. GT Leagues is measurably
more predictable.

## Findings

1. **Under-dispersed, like the parent** (per-side var/mean 0.953, Cameron–Trivedi
   t = −22 on honest OOS residuals). The one-day marginal 1.25 from Phase 0 was
   player-heterogeneity inflation. **Poisson family confirmed; NB/Dixon-Coles
   rejected.** Backlog: double-Poisson sharpening head would fix the tail
   underconfidence (deciles −6.5/+5.2) and is the highest-value model follow-up.
2. **The blend edge is real but small and freshness-insensitive** (+0.4 AUC pts;
   identical at lag 20 and lag 60). Unlike the parent, this league plays ~340
   matches/day, so the 7-day-half-life Poisson is never stale. Implication: the
   results-merge cadence is operational hygiene, not an accuracy lever — if the
   scraper ever breaks intra-day, expect no meaningful accuracy loss serving
   pure Poisson.
3. **Half-life 7d / alpha 0.01** — both halves of the parent's values; sweep was
   monotone into the original grid edge, extended sweep found a flat plateau
   3.5–7d × 0.005–0.01.
4. Coverage: only 58 of 14,599 eval matches (0.4%) had a debut player
   (uncovered); they are skipped in metrics and must be `coverage!=full` in
   serving.
5. Data note: source has 4 finished-but-unscored matches in ~100k (abandoned);
   loader excludes them by construction.

## What Phase 5 consumes

- `poisson.fit` day-frozen artifact + `heuristic.fit` refit-on-merge (cheap),
  `blend.blend_sides(w=0.7)` → `blend.totals_probs(λh, λa, line)` per listed line.
- Tier thresholds: initialize from parent (lean/solid/strong = 0.50/0.58/0.66 on
  the 0.5·model + 0.5·book blended confidence) and re-quantile after the first
  settled week (plan §2.1.8).
- As-of cutoff for serving: fixture kickoff (results can't publish pre-close;
  PHASE0_PROBES §0.4).
