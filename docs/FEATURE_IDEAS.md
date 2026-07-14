# Feature ideas — player scoring profiles & multi-horizon match stats (2026-07-09)

Status: **options 1 and 3 measured and rejected; their redirect — per-line
Platt recalibration (§combiner option 4) — measured, PASSED, and SHIPPED to
serving 2026-07-09 (rollback: [RECAL_SERVING.md](RECAL_SERVING.md)).**
Every candidate must pass the walk-forward harness in
[libs/py/model/evaluate.py](../libs/py/model/evaluate.py) before it touches
serving. Baseline to beat is the Phase-4 blend
([PHASE4_RESULTS.md](PHASE4_RESULTS.md)).

> **Separate track — club as a feature: measured 2026-07-10, PASSES, not yet
> shipped.** See [CLUB_FEATURE.md](CLUB_FEATURE.md). +0.59 AUC pts and better
> Brier at every line over the served blend, near-orthogonal to the form leg.
> This is a bigger mean-side win than anything in this document, and it
> invalidates the inherited "club carries no marginal signal" assumption that
> kept the column out of [data.py](../libs/py/model/data.py) in the first
> place. The features below all take the *player* as the only entity; that
> premise is now known to be incomplete.

## Motivation

The served model captures per-player level through two *means*: the Poisson
GLM's att/dfn coefficients (7-day time decay) and the form leg's EWM (span 8
matches). Three things it does not capture:

1. **Multiple horizons** — one decay curve is a single compromise point.
   A short window disagreeing with a long window (player re-ranking, tilt,
   roster change behind the alias) is itself a signal.
2. **Variance profiles** — everything today feeds a mean λ. "Structurally
   high-variance attacker" vs "parks the bus" is a statement about the *shape*
   of a player's goal distribution, which nothing models. This plugs straight
   into the Phase-4 backlog item: the model's one measured deficiency is tail
   underconfidence (calibration deciles −6.5/+5.2), and the flagged fix is a
   double-Poisson sharpening head — variance features are its natural input.
3. **Direct personal over-rate** — P(over line) read off the player's own
   history rather than derived through Poisson(λ).

Expectation management: the mean-side ceiling is low — the entire form leg,
tuned, buys +0.4 AUC pts over pure Poisson. The unclaimed edge per Phase 4 is
calibration sharpness, which is where the variance features aim.

## Window semantics

Count windows in **matches, not days**. The league plays ~340 matches/day and
players play at different rates. Proposed horizons: **3 / 10 / 30 matches**
(short / mid / long).

A 3-match window here can span a couple of hours — it plausibly measures
within-session tilt/fatigue of the gamer, a signal with no real-football
analogue that the 7-day half-life cannot isolate.

## Candidate features (per player, at cutoff T)

All computed with the same publish-lag / as-of semantics as `FormIndex`
(entry visible from kickoff + lag; a match never sees its own result).

| Feature | Windows | What it adds |
|---|---|---|
| GF, GA rolling mean | 3, 10, 30 | multi-horizon form; the short−long *deltas* are the real feature |
| Match-total mean (GF+GA) | 10, 30 | pace/style — over rate is driven by involvement, not just attack |
| Match-total **std** | 10, 30 | the variance profile; feeds the sharpening head |
| Personal over-rate at 2.5/3.5/4.5 | 30, shrunk | direct read; must be shrunk (below) |
| Attack share GF/(GF+GA) | 30 | style axis: attacker vs bus-parker at the same total pace |
| Matches in last 24h; minutes since last match | — | session context for the 3-window |

### Statistical traps (name them now or they eat the experiment)

- **Short windows on goals are mostly Poisson noise.** Mean total ~3.9 → the
  std of a 3-match average total is ~±1.1 goals. Use 3-window values only as
  deltas against the 30-window (or shrink toward it) and let a combiner learn
  a small weight; never serve a raw 3-match mean.
- **Raw personal over-rate is a binomial on tiny n** (σ ≈ 0.15 at n=10).
  Empirical-Bayes shrink toward the league rate, `(k + m·p_league)/(n + m)`
  with m ≈ 10–20, or it will look predictive in-sample and add nothing
  walk-forward. Its incremental value over λ lives only in the variance
  component — expect partial redundancy with match-total std.

## Architecture insertion point

A `PlayerFeatureIndex` in a new `libs/py/model/features.py`, built exactly
like `FormIndex` (per-player arrays, `searchsorted` on publish time,
cutoff-addressable). The leakage discipline is a solved, test-covered pattern
(`test_leakage_canary_auc_is_half`); rolling windows are the same transform
with `.rolling(n).mean()` in place of `.ewm()`.

## Combiner options, escalating

The current blend is a fixed-weight average of two λs and cannot absorb a
feature vector, so each tier needs its own combiner:

1. **Multi-span blend (cheapest — do first).** Fit the form leg at spans
   ≈ 3/10/30 and extend `sweep-blend` to a weight sweep over
   (poisson, form₃, form₁₀, form₃₀). No new architecture; also retires the
   Phase-4 TODO "FORM_SPAN not re-swept". Directly answers: do multiple
   horizons help the *mean*?

   **MEASURED 2026-07-09 — wash; multi-horizon rejected for the mean.**
   `python -m model.evaluate sweep-multiform` (99.7k matches, 45 eval days,
   14,440 covered, 286 simplex combos; recorded in `model_runs`):
   - Best combo (0.7 poisson / 0.1 form₁₀ / 0.2 form₃₀): AUC 0.7181 at 3.5
     vs baseline blend 0.7174 — +0.07 AUC pts, far under the 0.3-pt noise
     floor. Brier identical (0.2133). Line 4.5 slightly *worse*.
   - **w_form₃ = 0.0 in the entire top 10** — the 3-match window adds
     nothing to the mean, exactly as the short-window-noise trap predicted.
   - form₁₀/form₃₀/span-8 are interchangeable: the plateau is flat in how
     the non-Poisson 0.3 is split among them. One EWM span already captures
     everything the mean can absorb from recent form.
   - Serving constants unchanged (`FORM_SPAN=8`, `TOTALS_BLEND_WEIGHT=0.7`).
     If short windows carry signal at all, it must be in the *variance*
     channel → proceed to option 3.
2. **Stacked per-line classifier.** Keep the blend λ as the anchor feature,
   add the table above, train a walk-forward logistic regression (or small
   GBM) per line predicting P(over). This is where over-rate, attack share,
   and window-deltas can contribute — they don't fit the λ formulation — and
   it recalibrates the tails as a side effect.
3. **Sharpening head (the variance idea, properly).** Keep the blend mean
   untouched (it passed the gate); learn only a per-match dispersion
   multiplier from the variance features (both players' total-std profiles)
   and price O/U off a double-Poisson. Direct attack on the −6.5/+5.2 tail
   miss.

   **MEASURED 2026-07-09 — fails the gate; tail miss is NOT a
   distribution-family problem.** `python -m model.evaluate sharpen`
   (45 eval days, 7-day φ-warmup, 12,175 metric matches, expanding-window
   MLE refit daily; recorded in `model_runs`). Implementation:
   [sharpen.py](../libs/py/model/sharpen.py) (Efron double-Poisson pmf,
   global-φ and player-φ MLE heads, `VarIndex` rolling total-std profiles).
   - **The head measures exactly the right thing:** fitted φ ≈ 1.045,
     precisely the Phase-4 dispersion (var/mean 0.953 → 1/0.953 ≈ 1.049).
   - **But a ~4.5% variance cut barely moves O/U prices:** Brier −0.0002
     at lines 3.5/4.5, AUC unchanged, tail deciles −7.1/+4.9 → −6.8/+4.2.
     Gate (tails toward ±3) not met.
   - **Player head adds nothing over global.** b1 ≈ −0.25..−0.30, stable
     sign across the walk-forward (high-variance profiles → flatter pmf,
     coherent) and 100% profile coverage — but zero Brier/AUC gain.
     Player dispersion heterogeneity is not incremental signal once μ is
     conditioned.
   - **Implication:** if the family explained the −7/+5 tail gap, the MLE
     would have fitted φ far above 1.05. It didn't → the miscalibration
     lives in the probability *map* (μ's dynamic range compressed), not in
     the pmf shape. The cheap direct fix is per-line monotone recalibration
     (Platt/isotonic, fit walk-forward on settled predictions): AUC
     untouched by construction, expected Brier gain ~0.001–0.002, tails
     pulled toward ±3. That is a strict subset of option 2's machinery.
   - Note: baseline numbers here (e.g. AUC 0.7145 at 3.5) differ from the
     option-1 table because metrics exclude the 7 φ-warmup eval days.

4. **Per-line monotone recalibration (option 3's redirect).** Map the served
   blend's Poisson P(over) through a per-line monotone calibrator fit
   walk-forward on settled predictions: Platt `sigmoid(a·logit(p) + b)`
   (a > 1 sharpens) vs nonparametric isotonic. Cannot change per-line
   ranking, so it risks nothing on AUC; it attacks the probability map
   directly, which is where option 3 proved the tail miss lives.

   **MEASURED 2026-07-09 — PLATT PASSES THE GATE.**
   `python -m model.evaluate recal` (same protocol as sharpen: 45 eval
   days, 7-day calibrator warmup, 12,179 metric matches, daily
   expanding-window refits; recorded in `model_runs`).
   - **Tail deciles (line 3.5): −7.1/+4.9 → −1.6/+0.8**, every decile
     within ±2.9 — the ±3 gate is met and the Phase-4 calibration caveat
     is closed.
   - **Brier improves at every line:** 0.1743→0.1741 (2.5),
     0.2150→0.2141 (3.5), 0.2000→0.1996 (4.5). AUC unchanged within noise
     (−0.04 pts at 3.5; maps are monotone within a day, tiny cross-day
     drift only).
   - **Fitted maps are stable and interpretable:** a ≈ 1.21–1.27, |b| ≤ 0.2
     across all three lines and the whole walk-forward — one consistent
     "sharpen by ~25% in logit space" correction, exactly the
     underconfidence Phase 4 measured.
   - **Isotonic rejected:** Brier worse than Platt at every line, AUC
     −0.3 pts (cross-day step-function drift), no calibration advantage.
     The 2-parameter map wins; do not ship the nonparametric one.
   - Product note beyond Brier: serving consumes these probs through the
     0.60 pick gate and tier thresholds; underconfident tails were
     suppressing true strong picks (top-decile mean pred 0.787 → 0.829
     under Platt). Expect more surfaced picks at unchanged hit quality,
     and more value flags at the tails. Tier thresholds should be
     re-quantiled if this ships.

Recommended order: **1 → 3 → 2**. Option 2 is the biggest lift and only worth
it if option 1 shows the horizon deltas carry real signal. *(Both 1 and 3
measured and rejected — see above. What remains live is option 2's cheapest
component: per-line monotone recalibration of the blend's probabilities,
which directly targets the tail gap that option 3 proved is a map problem,
not a family problem. The full stacked classifier stays deprioritized: its
mean-side features were killed by option 1 and its variance features by
option 3.)*

## Blend-weight re-sweep after the club+tod regime change (2026-07-13)

`TOTALS_BLEND_WEIGHT=0.7` was swept 2026-07-09 against the player-only
Poisson leg; serving now blends the club+tod combined GLM. Re-swept
walk-forward against both legs on identical rows (45 eval days, 14,191
covered matches, step 0.05; `model_runs` kind='sweep-blend', params note
"re-sweep after club+tod regime change"):

- **Keep 0.7.** The optimum against the club+tod leg is 0.75–0.80, but the
  0.65–0.90 plateau is flat within ~0.1 AUC pts and moving 0.7 → 0.8 buys
  +0.08 pts at 3.5 (0.7238 → 0.7246) — under the ~0.3-pt noise floor, and a
  weight change is a served-λ regime switch that transiently pollutes the
  recal fit windows ([RECAL_SERVING.md](RECAL_SERVING.md) mixed-regime note).
- **The form leg is NOT retired by the better Poisson leg.** Pure Poisson
  (w=1.0) costs +0.23/+0.29/+0.51/+0.57 AUC pts at 2.5/3.5/4.5/5.5 vs the
  plateau — form's marginal edge *grew* at tail lines. The legs are
  complementary, not redundant.
- Control: the player-only leg re-swept on the same rows reproduces the
  original plateau (peak 0.7179 at 0.75–0.80, w=0.7 within 0.04 pts), so
  the shift is the leg change, not data drift. Plateau-to-plateau the
  club+tod leg is worth +0.67 AUC pts at 3.5.
- **Policy:** re-sweep after λ-regime changes, not on a calendar; never
  auto-refit a flat-plateau hyperparameter (it wanders on noise). Learned
  context-varying weights and the full stacker remain declined per the
  option 1–3 measurements above.

## Acceptance gate

Walk-forward via `build_predictions`/`metrics`, same protocol as Phase 4.
Beat the served blend at every line:

| Line | AUC | Brier |
|---|---|---|
| 2.5 | 0.713 | 0.171 |
| 3.5 | 0.717 | 0.213 |
| 4.5 | 0.720 | 0.202 |
| 5.5 | 0.734 | 0.153 |

(5.5 added to `EVAL_LINES` 2026-07-11 — the book quotes it on high-total
fixtures and serving was measurably miscalibrated there while the harness
couldn't see it; baseline row measured that day, 45 eval days. Caveat for
tail lines: unconditional AUC flatters them — check `model.evaluate
conditional` too, which scores each match only at the lines straddling its
own E[total]. Conditional AUC at 5.5 is ~0.60, so most of the 0.734 is
cross-population ranking the book's line choice removes.)

Plus, for the sharpening head: tail calibration deciles improving toward
±3 pts with no middle-decile degradation. With ~14.5k eval matches,
differences under ~0.3 AUC pts are within noise — treat smaller wins as a
wash, not a pass.
