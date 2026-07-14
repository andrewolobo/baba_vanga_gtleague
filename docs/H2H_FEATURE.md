# Head-to-head as a 1x2 feature — measurement + build plan (2026-07-13)

Status: **measured, PASSES the gate — and survives the long-run-skill
control that was the one confound that could have killed it. Phase 1
(measurement harness) LANDED 2026-07-13; Phase 2 (serving) CODE LANDED
2026-07-13, dark: `X12_H2H_ENABLED` defaults false and nothing served
changes until it is set. Enabling is a deploy decision — see §Enabling.**

Origin: the model favored Kingston over Kangal against a head-to-head record
that clearly favored Kangal. The measured answer is that the record carries
real signal the served model cannot see: prior meetings between the *same two
players* improve 1x2 discrimination by **+2.9 AUC pts** on top of the served
blend+club score — roughly **5× the club feature, ~20× tod** on their own
gates.

## The dataset finding that motivated this

On all 100,802 finished matches (2025-08-01 … 2026-07-13):

- **This league is extremely H2H-rich.** 94 players; the median match has
  **91 prior meetings** between the exact same pair at kickoff; 96.3% have
  ≥ 5, 85.6% have ≥ 20. The median rematch gap is **~1 hour** (p75 ≈ 1.4h),
  so pair history is deep *and* fresh.
- Raw persistence: among decisive matches with ≥ 5 prior meetings, the
  lifetime H2H majority winner wins the next one 57.9% of the time, rising
  monotonically to **74.3%** when the record is lopsided (edge > 0.6).
- That raw number is confounded — a stronger player tops both the H2H record
  and the model rating (corr(model logit, lifetime H2H edge) = +0.54). The
  gate below is the deconfounded measurement.

## Why the GLM cannot see it

The Poisson head is additive in per-player factors:
`log λ = c + home + att[player] + dfn[opponent] (+ club + tod)`. A pairwise
interaction — player A specifically beats player B beyond what their field-
wide rates imply — has no term to land in. It is structurally invisible, not
merely shrunk away.

The plausible alternative explanation was that "H2H signal" is really
**long-run player strength** the 7-day half-life forgets (lifetime H2H record
≈ slow skill differential). That is a different feature (a slow-λ leg, not a
pairwise term), so the gate carries an explicit control arm for it.

## The gate

Walk-forward, standard protocol: day-frozen `build_predictions(with_club=True)`,
45 eval days (2026-05-30 … 2026-07-13), cutoff-aware form leg, 20-minute
publish lag. Base score = the served 1x2 arm (`x12_report`'s blend+club λs →
decisive home-vs-away probability). Stacking arms are logistic layers refit
**daily on prior eval days only** (mirrors serving: fit on settled
predictions), 7 warmup days, scored on 9,482 decisive out-of-sample matches.
Paired bootstrap on identical rows, house rule.

H2H features, all computed from the HOME player's perspective, using only
meetings **published by kickoff** (kickoff + 20-min lag — with ~1h rematch
gaps this rule is load-bearing, same class as FormIndex):

- `h2h_edge_life` — lifetime H2H win diff, shrunk: (w_home − w_away)/(n + 10)
- `h2h_edge_decay` — same, exponentially decayed, 7d half-life, /(n_d + 2)
- `h2h_gd_decay` — decayed mean H2H goal diff, 7d half-life, /(n_d + 2)

| Arm | decisive AUC | ΔAUC vs base (95% CI) | verdict |
|---|---|---|---|
| base (served score, refit stacker) | 0.5802 | — | — |
| +edge (life + decayed) | 0.6055 | **+2.53 pts** [+1.65, +3.42] | SIG |
| +gd | 0.5978 | **+1.75 pts** [+0.99, +2.40] | SIG |
| +h2h (all three) | 0.6087 | **+2.85 pts** [+2.00, +3.67] | SIG |
| +skill (control: lifetime + 60d overall win-rate diff, non-pairwise) | 0.5869 | +0.67 pts [+0.10, +1.24] | SIG but small |
| +skill+h2h | 0.6103 | +3.00 pts [+2.13, +3.93] | SIG |

**The decisive comparison: +skill → +skill+h2h = +2.34 pts [+1.50, +3.17],
SIG.** Long-run skill explains under a quarter of the effect; the rest is
genuinely pairwise. This kills the "it's just the half-life forgetting slow
skill" explanation.

Match-level size (why it matters for picks, not just AUC): bucketing scored
matches by decayed H2H edge, the served p_home is **−5.1 pts too high in the
strongest anti-H2H quintile and +5.3 pts too low in the strongest pro-H2H
quintile**, monotone across all five buckets. That is the Kangal/Kingston
error, quantified.

Known fit artifact: `h2h_edge_life` dominates (coef ≈ 1.8) and
`h2h_edge_decay`'s coefficient goes *negative* in the joint fit — the two are
collinear. ~~The served feature set must be pruned/re-swept in Phase 1.~~
**Resolved by the recorded 90-day sweep (below): keep all three.** The
negative decayed-edge coefficient is a stable *contrast* against the lifetime
edge (recent results discounted relative to the long record), worth ~+0.2 pts
over the prune at every half-life — signal, not an artifact to remove.

### The recorded 90-day run (`model_runs` kind `h2h`, 2026-07-13)

`python -m model.evaluate h2h --eval-days 90` — 2026-04-15 … 2026-07-13,
28,711 covered matches, 20,946 decisive scored. The headline gate:

| Measurement | ΔAUC (95% CI) | verdict |
|---|---|---|
| 1x2 +h2h vs base (0.6061 → 0.6245) | **+1.84 pts** [+1.48, +2.21] | SIG |
| 1x2 +skill control alone | +0.37 pts [+0.19, +0.53] | SIG but ~⅕ of h2h |
| **1x2 pairwise increment (+skill → +skill+h2h)** | **+1.48 pts** [+1.13, +1.84] | **SIG** |
| totals +pace, line 2.5 (0.7193 → 0.7273) | **+0.80 pts** [+0.62, +0.97] | SIG, ΔBrier −0.0018 |
| totals +pace, line 3.5 | **+0.79 pts** [+0.62, +0.97] | SIG, ΔBrier −0.0025 |
| totals +pace, line 4.5 | **+0.96 pts** [+0.81, +1.15] | SIG, ΔBrier −0.0030 |
| totals +pace, line 5.5 | **+1.29 pts** [+1.09, +1.49] | SIG, ΔBrier −0.0026 |

- The player-pace control is a dead-flat wash at every line (dAUC −0.01…+0.01
  pts, CIs straddling zero) — the cleanest possible statement that the totals
  effect is pairwise; the pairwise increment is +0.93…+1.46 pts, SIG at
  every line.
- **Split-half: same sign in both halves, strengthening in the recent half**
  (1x2 +1.18 → +2.58 pts; totals 3.5 +0.43 → +1.10) — the tod-style
  stability read passes, and the 45-day numbers above are the recent-half
  regime, not an inflated fluke.
- Effects are smaller than the 45-day window (+1.8 vs +2.9 on 1x2) because
  the wide window includes the weaker early regime — still ~3× club's worth.
- **Sweep: a flat plateau everywhere.** Shrink K 5/10/20 indistinguishable
  (±0.06 pts); half-life 3.5/7/14 indistinguishable on 1x2 (full set +1.95 /
  +1.83 / +1.76 pts) and totals (+0.79 / +0.79 / +0.72). Feature sets: full
  winner triple > edge_life+gd_decay > edge_life alone at every half-life;
  `pace_decay` alone = the pace pair. Served sets: `X12_FEATURES` = all
  three winner features, `TOTALS_FEATURES` = `pace_decay`, defaults hl=7 /
  K_life=10 / K_decay=2 confirmed.

## How it enters serving: a stacking layer on the 1x2 head, not a GLM term

Two candidate shapes; the stacker is the one that was measured, and it wins
on engineering grounds too:

1. ~~Pairwise block in the Poisson GLM~~ — a `patt[pair]` block is ~4.4k
   extra coefficients, needs its own ridge scale sweep, slows every day-frozen
   fit, and is a *goals* model being asked to carry a *win* effect. Nothing
   measured here says the effect lives in λs.
2. **Logistic stacking on the decisive share** (recal-shaped) — the gate
   measured exactly this: a daily-refit map from (model score, H2H features)
   to the decisive outcome. One-flag rollback, no GLM or artifact changes,
   totals path untouched.

Serving math (mirror of how recal preserves p_push): let
`s = p_home / (p_home + p_away)` from the served λs, then

    s' = sigmoid(b0 + b1·logit(s) + b2·h2h_edge_life + b3·h2h_gd_decay)
    p_home' = s'·(1 − p_draw)      p_away' = (1 − s')·(1 − p_draw)

`p_draw` is untouched — the draw head was not part of the measurement and the
x12 gate found draw calibration fine. Probabilities still sum to 1. With no
H2H history both features are 0 and the map reduces to a Platt-style
recalibration of `s`; with the stacker disengaged (below), rows are
byte-identical to today.

Fit source: **settled x12 predictions** (`settlements_x12` ⋈
`predictions_x12` at the served-row rule, decisive results only), joined to
H2H features recomputed as of each row's kickoff — the recal pattern
(`fit_line_maps`), not the eval harness. The head has been accruing dark
since 2026-07-12 (~340 covered matches/day), so at a `min_n` of ~500 decisive
graded rows the stacker engages within days of Phase 2 landing, still dark.

- **Per population, never pooled** (docs/POPULATION_SPLIT.md rule): priced
  and `gtl:` rows get separate stacker fits, engaging independently as each
  population's settled volume clears `min_n`. Identity below `min_n`.
- Rows a stacker actually touched carry a `-h2h` `model_version` suffix
  (order: `-club-tod-h2h`), never the whole batch — the `-recal` convention.

## Phase 1 — promote the harness — **LANDED 2026-07-13 (changes no served output)**

1. `model/h2h.py` — `H2HIndex` (FormIndex contract: per-pair history
   addressable by any cutoff, meetings visible from publish time; unseen
   pair → all-zero features), the five features on both timescales, and the
   stacker math (`fit_stack`/`Stack.apply`/`restack_x12` — the draw-mass
   invariant lives in `restack_x12`). Shrinkage/half-life are constructor
   params, module defaults `H2H_HALF_LIFE_DAYS=7`, `SHRINK_LIFE=10`,
   `SHRINK_DECAY=2`; serving-candidate subsets `X12_FEATURES` /
   `TOTALS_FEATURES`.
2. `model/evaluate.py` — `h2h` mode (kind=`h2h` in `model_runs`):
   `h2h_report` runs BOTH market gates with their control arms baked in
   (+skill for 1x2, +ppace for totals — the pairwise verdict is the
   increment over the control, not over base), split-half stability on the
   headline measurements, and the census/confound-corr block; `h2h_sweep`
   covers feature subsets × half-life (3.5/7/14) × shrink (5/10/20).
   `_control_frame` is the one-pass visibility-honest control-feature
   builder. Headline run: `python -m model.evaluate h2h --eval-days 90`.
3. `tests/test_h2h.py` — 10 tests: publish-boundary visibility canary (a
   match never sees itself; 19 min invisible, 21 min visible; loser
   perspective flips sign), decay-forgets/lifetime-does-not, planted
   dominator/tempo sign recovery, full-gate leakage canary (all arms ~0.5 on
   a no-signal league), planted pairwise-winner recovery WITH the +skill arm
   staying flat (the flat control is part of the contract), planted
   pair-pace recovery with +ppace flat, zero-feature stacker monotonicity,
   draw-mass preservation. Suite 144/144.

## Phase 2 — serving — **CODE LANDED 2026-07-13, ships dark (flag off)**

1. `core/config.py` — `x12_h2h_enabled: bool = False` (the one-flag
   rollback; same live-switch caveat as every flag: the API timer makes a
   True default an instant ship), `x12_h2h_min_n: int = 500`,
   `x12_h2h_days: int = 14`. Documented in `.env.example`.
2. `model/h2h.py` — `fit_stacker(conn, days, min_n, index, population, now)`
   → `Stack` or `None` (identity), the `fit_line_maps` shape. Fit on decisive
   settled `predictions_x12` (last pre-kickoff batch, the row settlement
   grades); priced-side player names resolve through `player_aliases` in the
   query, mirroring the cycle's `_alias`. Features re-derived at
   kickoff − OFFSET_TOL_MIN — the cutoff the graded batch priced with and
   the one regen reconstructs, so fit/serve/regen share one feature
   definition (meetings only accrue, so historical cutoffs reproduce).
   **Fit basis fixed 2026-07-13, before first enablement:** the fit
   recomputes the raw decisive share `s` from each row's stored λs
   (`x12_probs`), never from stored `p_home/p_away` — those are post-stack
   on `-h2h` rows, and reading them back is the recal closed loop
   (docs/RECAL_SERVING.md 2026-07-13 update) replicated in this head. The
   x12 fit pool was never contaminated: the flag has never been on.
3. `predictor/cycle.py` — `_x12_row` applies the population's stacker after
   `x12_probs`, before the pick gate (the gate reads restacked probs — that
   is the point). Features at the row's own `cutoff`, NOT kickoff: pairs can
   rematch between an early batch and kickoff, and an early row must not see
   that meeting. `-h2h` suffix on touched rows only; `x12_h2h_n` per
   population in cycle stdout.
4. `settlement/settle.py` — `_Regen.x12_pick(population=)` is version-aware
   on `-h2h` exactly as `pick` is on `-recal`: stackers refit at settle time
   (threaded through the run's `now`), applied only to tagged rows, each
   settlement loop passing its population. Same documented tolerance: the
   stacker drifts by the hours between predict and settle; near-gate flips
   are config drift, not leakage. Flag off → tagged rows regen through
   identity, recal's exact rollback behavior.
5. `tests/test_predictor_x12_h2h.py` — 4 tests on a rigged SLOW/SLOW
   rematch slate where ONLY the stacker can clear the 0.50 gate (a silent
   no-op cannot pass): flag-off + below-engagement identity, stacker
   reaches served probs from both sides with p_draw preserved and the
   unengaged gtl population untagged, regen agreement on stacked rows,
   version-aware regen of pre-h2h rows through the flag transition.
   Suite 148/148.
6. **Dry run** (production copy, `now` = 2026-07-13T05:00Z, 84 covered
   fixtures, min_n lowered to 150 so the ~1-day dark pool engages —
   production keeps 500):

   | | h2h off | h2h on |
   |---|---|---|
   | x12 rows | 84 | 84 (all paired, none dropped) |
   | fit rows engaged | — | 184 priced / 228 schedule |
   | rows tagged `-h2h` | 0 | 84 |
   | mean / max \|Δp_home\| | — | 0.078 / 0.202 |
   | max \|Δp_draw\| | — | **0.0 exactly** (draw preserved) |
   | picks | 17 | 24 (21 rows changed pick status) |

   The feature demonstrably reaches serving and reshapes the gate region,
   consistent with the eval (the stacker widens the decisive score's
   dynamic range). NOTE: 184 fit rows is a noisy stacker — the dry run
   demonstrates plumbing, not calibrated output; that is what
   `x12_h2h_min_n = 500` is for.

### Enabling (the decision this doc leaves open)

Set `X12_H2H_ENABLED=true` in `.env` (needs `X12_ENABLED=true`; live switch,
next cycle). At current volume each population crosses min_n = 500 decisive
graded rows ~2–3 days after the x12 head's own dark accrual reaches it —
check `x12_h2h_n` in cycle stdout. Watch after enabling: `regen_agrees` in
`settle run` output through the transition (version-awareness is
test-covered, but this is the live canary), and the x12 pick RATE — the
stacker lifts more scores over 0.50 (dry run: +40% picks), so judge the
hit rate on gate-clearing picks only after a few hundred graded, and
re-band any future x12 tiers on stacked confidences only.

## Interactions and sequencing

- **x12 tier bands do not exist yet — band AFTER the stacker engages.** The
  stacker deliberately reshapes the confidence scale (it flattens the model
  logit: joint coef ≈ 0.45 vs 1.02 alone). Quantiling bands on pre-h2h
  confidences and then enabling h2h is the totals tier incident again.
- The x12 pick threshold (0.50) was measured on unstacked probs. The stacker
  widens the score's dynamic range, so pick volume will rise. Re-read the
  pick-rate/hit-rate table off the harness's stacked scores in Phase 1 and
  re-confirm 0.50 (or adjust) before enabling.
- The x12 UI rollout (docs/X12_UI.md) is orthogonal: rows change values, not
  shape. Sequencing preference: land h2h while the head is still dark, so
  the first user-visible 1x2 numbers already include it.

## Caveats

- **Whether the book prices H2H is unmeasured** — same caveat class as tod.
  If betPawa's 1x2 odds already carry pair history, this improves calibration
  and model-only picks but not edge vs the book. The 1x2 vs-book scorecard
  (X12_SERVING.md "not done" list) answers it once a few thousand
  `settlements_x12` rows exist; until then `value_flag` comparisons are the
  weak proxy.
- One 45-day window so far; the recorded Phase 1 run must be 90 days +
  split-half. Effect size (~5× club) leaves a lot of room to shrink and still
  clear every prior feature's bar.
- The stacker is fit on *decisive* outcomes and never touches p_draw; if the
  draw head is ever revisited (Dixon-Coles-style), re-measure the stack on
  top of it.
- Totals impact: the winner-signed features (win edge, goal diff) are the
  wrong shape for totals — the totals analogue is **pair pace** (mean total
  of prior meetings vs the league running mean). Measured 2026-07-13 on the
  same cached walk-forward rows, stacked per line on the served blend+club
  P(over), with a NON-pairwise player-pace control arm:

  | Line | base AUC | +pair-pace ΔAUC (95% CI) | +player-pace (control) | ΔBrier |
  |---|---|---|---|---|
  | 2.5 | 0.7114 | **+1.36 pts** [+0.95, +1.73] | −0.02 pts, wash | −0.0028 |
  | 3.5 | 0.7205 | **+1.11 pts** [+0.82, +1.44] | −0.01 pts, wash | −0.0036 |
  | 4.5 | 0.7250 | **+1.39 pts** [+1.04, +1.76] | −0.04 pts, wash | −0.0044 |
  | 5.5 | 0.7397 | **+1.65 pts** [+1.26, +2.04] | −0.02 pts, wash | −0.0034 |

  Player pace alone is a wash at every line (the GLM + form leg already carry
  it — the control failing to move is the arms behaving correctly), while the
  pairwise increment over it is +1.1…+1.9 pts, SIG everywhere, Brier improving
  at every line — ~2–3× club's totals worth. Unlike the 1x2 head (lifetime
  edge dominates), the totals signal is the **7d-decayed** pace (joint coef
  ≈ 0.53–0.65; lifetime ≈ 0): recent pair pace, not ancient history. A totals
  H2H feature is therefore ALSO worth building — likely as pace features in
  the recal/stacking tier on P(over), per line per population. The 90-day
  recorded run + sweep + split-half (see gate section) confirm it:
  +0.8…+1.3 pts per line, Brier improving everywhere, `pace_decay` alone
  sufficient. Serving it stays a separate Phase-2-style decision from the
  1x2 stacker.
