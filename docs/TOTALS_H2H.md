# Totals H2H (pair pace) — serving build plan (2026-07-15)

Status: **PLAN, no code yet.** The measurement is DONE and PASSED
([H2H_FEATURE.md](H2H_FEATURE.md) §Totals impact + the recorded 90-day run,
`model_runs` kind `h2h`, 2026-07-13): pair pace is worth **+0.8…+1.3 AUC
pts per line** with Brier improving everywhere, the non-pairwise
player-pace control dead flat, split-half strengthening. `pace_decay`
alone is the serving set (`h2h.TOTALS_FEATURES`); nothing new to measure.
This doc is the build spec. The build lands **dark** — flag off, serving
byte-identical — exactly how the x12 stacker landed. Enabling is a
separate decision with its own gates (§Enabling); do not conflate the two.

Sequencing context that shaped this plan: the 1x2 H2H stacker went live
2026-07-14 ~22:04Z (first `-h2h` rows) and the recal2 verification gate
reads `-recal2` rows ~2026-07-18. Both want the totals path left alone
this week — hence dark.

## Design: extend the recal map, do not stack a second layer

The 1x2 stacker could land as a standalone logistic layer because it was
the FIRST learned map on its path. The totals path already has one — the
per-line Platt maps ([RECAL_SERVING.md](RECAL_SERVING.md)), engaged and
refit every cycle. Two independently-fit maps applied in sequence, each
refit on a rolling window that fills with the other's output, is the
closed-loop failure class recal already hit once (2026-07-13). So the pace
term does not get its own layer; it extends the existing one:

    p' = sigmoid(a·logit(p_raw) + b + c·pace_decay)     per line, per population

- Plain recal is exactly the `c = 0` special case, and a row with no pair
  history has `pace_decay = 0` (shrunk toward 0 by construction), so the
  extended map degrades per-row to a plain Platt map — the unseen-pair
  path needs no special casing.
- The gate measured THIS shape: the 90-day totals arm stacked
  `(logit P(over), pace)` per line on the walk-forward rows. The serving
  math is the measured math; only the fit source differs (settled
  predictions, the recal pattern — same relationship recal itself has to
  its walk-forward validation).
- `p_push` handling is inherited: maps exist only for push-free
  half-lines, `p_push` passes through untouched, `p_under` is the
  remainder. No new invariant.

One serving map set per population: when the flag is on, every line that
clears the existing recal engagement tiers gets the extended fit — there
is no "plain map for some lines, extended for others" mixture inside a
population (a line's pace coefficient is fit on the same rows its Platt
parameters are; if the line is thick enough to map, it is thick enough to
carry `c` — see tiers below).

## Fit contract

Mirrors `recal.fit_line_maps` + `h2h.fit_stacker`, combined:

- **Raw basis (structural closed-loop guard):** the fit's x is the raw
  `p_over` recomputed from each settled row's stored λs via
  `totals_probs` — never the stored (post-map) `p_over`. Unchanged from
  the 2026-07-13 recal fix; the pace column changes nothing about it.
- **Feature cutoff:** `pace_decay` re-derived from the `H2HIndex` at each
  fit row's kickoff − `OFFSET_TOL_MIN` — the cutoff the graded (last
  pre-kickoff) batch priced with and the one regen reconstructs. Meetings
  only accrue, so historical cutoffs reproduce. Fit, serve, and regen
  share one feature definition (the x12 stacker's rule, verbatim).
- **Populations never pooled** ([POPULATION_SPLIT.md](POPULATION_SPLIT.md)):
  priced and schedule get separate extended fits, engaging independently.
  The fit queries are the existing recal queries widened to select
  player names + kickoff; the priced side resolves names through
  `player_aliases` exactly as `h2h._FIT_QUERY` does.
- **Engagement tiers are recal's, with `c` added to each:**
  - `n ≥ recal_min_n` (300): per-line `(a_L, b_L, c_L)` — 3-param fit,
    one added parameter on ≥300 rows.
  - `recal_min_n_line` (75) `≤ n <` 300: the thin-line tier borrows the
    pooled slope AND a pooled pace coefficient, fitting only its own
    intercept: shared `a`, shared `c`, per-line `b` (the pool must clear
    300, as today). The 90-day sweep supports pooling: the joint pace
    coefficient is stable at ≈0.53–0.65 across lines.
  - Below 75: unmapped, untouched — and while priced maps are engaged the
    existing unmapped-line pick guard already suppresses picks there.
- **Expected coefficient regime:** the gate's `c` was measured on the
  unconditional walk-forward. The priced serving fit sees the
  book-conditional population, where recal's own slope flattens to
  a ≈ 0.14–0.4 — expect the fitted `c` there to differ from 0.53–0.65 and
  judge it via `model.evaluate conditional` / live results, not against
  the gate's number (the recal lesson, applied in advance).

Current 14-day leak-clean volumes (queried 2026-07-15): priced
2.5/3.5/4.5/5.5 = 213/483/323/209 — 3.5 and 4.5 take the 3-param fit,
2.5 and 5.5 ride the pooled tier, 6.5 (63) stays unmapped. Schedule
2.5/3.5/4.5/5.5 = 386/1043/1076/517 — all four take the 3-param fit. The
feature engages at full strength on schedule (the volume/product
population) from day one.

## Version tag and regen semantics

Rows priced through a map with an engaged pace term carry
**`-recal2-h2h`** (order: `…-recal2-h2h`, after any club/tod parts).
Rationale: the tag must contain `-recal` (settle's totals regen routes map
application on that substring today) and `-h2h` (the established marker
for "pair history reshaped this row"); the meaning of the map changed, so
the tag changed (the `-recal`→`-recal2` lesson). Plain `-recal2` remains
what it is: a 2-param map touched this row. Cross-table note: `-h2h` on a
`predictions` row means the extended totals map; on a `predictions_x12`
row it means the decisive stacker — separate tables, separate regen paths,
no routing collision.

Regen (`settle._Regen.pick`) becomes version-aware one level deeper:

| served tag contains | regen applies |
|---|---|
| `-h2h` | this population's extended maps (refit at settle time) |
| `-recal` only | this population's plain maps (refit at settle time) |
| neither | identity |

So at settle time, when the flag is on, `_Regen` fits BOTH shapes per
population from the same query rows (cheap — one query, two fits); the
cycle fits only the extended set. Same documented drift tolerance as
recal and the x12 stacker: maps refit in the hours between predict and
settle, near-gate flips are config drift, not leakage. Flag off → `-h2h`
rows regen through plain maps; combined with `RECAL_ENABLED=false` they
regen through identity — recal's exact rollback ladder.

## Phase 1 — map math + fit (no serving change)

1. `libs/py/model/recal.py` — `fit_platt`/`fit_platt_shared` gain an
   optional feature column (design matrix grows by one column; pooled-`c`
   in the shared fit), `apply_platt` handles `(a, b)` and `(a, b, c)`
   values, `apply_to_line(maps, line, p_over, p_push, pace=0.0)`.
   `fit_line_maps(..., h2h_idx=None)`: when an index is passed the
   population's query variant selects player names + kickoff (priced via
   `player_aliases`) and every tier fits the pace column; `h2h_idx=None`
   is byte-identical to today. Keeping the fit in `recal.py` keeps ONE
   totals map path — the no-second-layer decision expressed in code.
2. `libs/py/model/h2h.py` — nothing new needed (`H2HIndex.features`
   already computes `pace_decay` under the visibility rule;
   `TOTALS_FEATURES` already recorded). Docstring pointer to this doc.
3. `tests/test_recal.py` additions — extended fit recovers a planted pace
   signal (rigged rematch pairs running hot/cold vs league mean); `c = 0`
   / zero-pace reduction reproduces the plain map exactly; pooled-tier
   `c` shared across thin lines; `h2h_idx=None` byte-identity canary.

## Phase 2 — serving, dark behind the flag

1. `libs/py/core/config.py` + `.env.example` —
   `totals_h2h_enabled: bool = False` (requires `recal_enabled`: the pace
   term rides the recal map; with recal off there is no map to extend —
   document, and make the cycle treat `recal_enabled=false` as overriding).
   No new min_n knobs: engagement is recal's existing
   `recal_days`/`recal_min_n`/`recal_min_n_line`, one flag total. Same
   live-switch caveat as every flag (API timer ⇒ True default is an
   instant ship).
2. `services/py/predictor/cycle.py` — build the `H2HIndex` when EITHER
   head needs it (today it is gated on the x12 flag pair); pass it to
   both populations' `fit_line_maps` when the flag is on; `_line_row` and
   `_schedule_row` compute `pace_decay` at the ROW'S OWN cutoff (not
   kickoff — pairs rematch between an early batch and kickoff; the x12
   rule verbatim) and pass it to `apply_to_line`; `_row_version` emits
   `-recal2-h2h` for lines with an engaged extended map. Report/stdout:
   extend `recal_lines`/`recal_lines_sched` to show which lines are
   pace-extended.
3. `services/py/settlement/settle.py` — `_Regen` fits both map shapes per
   population when the flag is on (plain set for `-recal`-only rows) and
   routes per the table above; index built once (shared with the x12
   stackers' build when both flags are on).
4. `tests/test_predictor.py` / new `tests/test_predictor_totals_h2h.py` —
   the x12 h2h test pattern on the totals head: flag-off byte-identity
   (including tags); a rigged rematch slate where ONLY the pace term can
   lift a row over the 0.60 pick gate (a silent no-op cannot pass);
   `p_push` preserved exactly; per-population isolation (schedule engaged,
   priced identity-and-untagged, and vice versa); regen agreement on
   extended rows; version-aware regen of plain `-recal2` rows and of
   pre-h2h rows through the flag transition; pick-guard behavior
   unchanged on unmapped lines.
5. **Dry run** (production DB copy, flag on, one cycle, off/on diff):
   rows and row count identical; tags only where expected; per-population
   per-line Δp_over distribution; `max |Δp_push| = 0.0` exactly; pick
   count delta per population; fitted `(a, b, c)` per line printed and
   sanity-read against §Fit contract's expected regimes.

## Enabling (NOT part of this build — a later decision)

Preconditions, in order, all of which currently say "not yet":

1. **Recal2 verification gate (~2026-07-18) reads clean** on `-recal2`
   rows only. Enabling earlier changes the totals serving path
   mid-evidence-window and perturbs the exact map generation under
   verification.
2. **The 1x2 stacker has 2–3 clean live days** (live since 2026-07-14
   ~22:04Z): `regen_agrees` through its transition, hit rate on
   gate-clearing stacked picks sane after a few hundred graded. It is the
   production shakedown of everything this build reuses; a defect found
   there gets fixed in one head, not two.
3. Then: `TOTALS_H2H_ENABLED=true` in `.env`, live switch, next cycle.

Watch after enabling:

- `regen_agrees` through the transition (version-awareness is
  test-covered; this is the live canary — the x12 rule).
- A short regen-disagreement burst as pre-flag rows settle is the known
  artifact class (RECAL_SERVING.md 2026-07-13, POPULATION_SPLIT.md
  pre-Phase-0 rows) — expected, not a leak signal.
- Fitted `c` per population each cycle for the first days: priced should
  sit below the gate's 0.53–0.65 (conditional population); a sign flip or
  wild swings on a full window → suspect the fit, rollback is one flag.
- **Totals tier bands must be re-quantiled on extended-map confidences**
  after the first settled window — the pace term reshapes the confidence
  scale, and the bands were re-banded 2026-07-12 on pre-h2h confidences
  (the totals tier incident rule: never band on a scale about to shift).
  Until re-banded, judge tier hit rates only within rows sharing a tag
  generation.
- Pick volume: the map widens dynamic range where pairs run hot/cold;
  expect pick-rate movement on both populations, judge hit rate on
  `-recal2-h2h` rows only.

## Non-goals and open items

- **Not a GLM term.** Same reasoning as the 1x2 head: nothing measured
  says the effect lives in λs, and a pairwise GLM block is ~4.4k
  coefficients of regularization risk.
- **`pace_life` stays out** (90-day sweep: carries nothing over
  `pace_decay`).
- **Book overlap unmeasured** — if betPawa's totals prices already carry
  pair pace, the priced win is calibration, not edge vs the book; the
  schedule population (the product surface) benefits regardless. The
  vs-book scorecard answers it eventually; same caveat class as tod.
- **Optional pre-read (nice-to-have, not blocking):** extend
  `model.evaluate conditional` with a pace arm to preview the priced
  (book-conditional) effect size before enabling. The unconditional gate
  is done; this would only sharpen expectations for watch item 3.
- **Draw head / 1x2 interplay: none.** The extended map touches
  `predictions` rows only; λs, the x12 head, and its stacker are
  untouched.
