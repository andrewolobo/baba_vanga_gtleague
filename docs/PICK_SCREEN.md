# Pick Screen — a dynamic, rule-based veto layer over served picks

Status: Phase 0 SHIPPED DARK 2026-07-18 (columns accruing, SCREEN_UI_ENABLED
off; first live cycle annotated 72 picks, vetoed 2 — both cold_band). Judge
Phase 1 via `python -m settlement.settle screen` after ~7d of settlements.

A deterministic veto cascade that annotates every served pick — O/U and 1x2 —
with `screen_pass` / `screen_reason`. The rules are hand-written and static;
the *inputs* are rolling stats recomputed from settlements every cycle, which
is what makes the screen dynamic. It is a SEPARATE layer from the existing
gating schema (pick_prob_threshold, tiers, value_flag, unmapped-line guard),
and it never modifies them.

"Screen", not "gate": the perf UI already uses *gate* for the
pick-probability gate (picks vs no-picks), and overloading that word is the
confidence-column incident all over again.

## Why (probe, 2026-07-18, read-only over 2,349 settled book-priced rows)

Counterfactual grading (model lean vs result_total on EVERY settled row, not
just surfaced picks):

- book-opposed + conf < .66: **46.8%** over 821 rows; the live gate served
  64 picks in that quadrant hitting **45.3%**. Book-agree + conf ≥ .66: 61.9%.
- edge band 5–12 pts ("value" band): **47.2%** over 742 rows (~3.5σ below
  the 53.6% baseline) — mid-size disagreement with this book is adverse
  selection, not value. Band boundaries are provisional (chosen on the same
  data); the cold-band rule below self-corrects them.
- cold lines: 2.5 served picks at 40.4% (n=57); 6.5 counterfactual 46.4% —
  nothing in the current gate catches a line going cold.
- per-player spread (39.7%–67.8%, n ≥ 30) is mostly binomial noise at these
  sample sizes and the GLM already encodes player strength: **no player rule
  in v1**, but per-player stats appear in the report for observation.

## Design principles

1. **Annotation, never mutation.** The screen writes `screen_pass`/
   `screen_reason` next to the pick; `pick`/`tier`/`confidence` stay pure
   model outputs. Rationale: the screen's inputs are rolling stats that
   cannot be reproduced at settle time, so a screen that nulled picks would
   make regen_agrees unverifiable — the λ-honesty canary must keep reading
   the unscreened pick. Enforcement is presentation-level (UI toggle / API
   consumers filter on it). Settlement grading is untouched; screened hit
   rates are a WHERE clause.
2. **Default-open.** Every rule self-skips below its sample floor and when
   its data is missing (no book on schedule rows). Cold start serves
   screen_pass = 1 everywhere. Veto only on evidence.
3. **Populations never pooled** (docs/POPULATION_SPLIT.md). One ScreenStats
   per population per market. Book rules never fire for schedule rows.
4. **Counterfactual-fed, raw-basis.** Stats are computed from ALL settled
   rows (lean graded against result_total), not from surfaced picks —
   otherwise the screen judges itself through its own veto and we rebuild
   the recal feedback loop (docs/RECAL_SERVING.md 2026-07-13) in rule form.
5. **Dumb renderers stay dumb.** The API and UI read persisted columns;
   no screen computation outside the predictor cycle.

## Schema — migration 004_screen.sql

Columns, not side tables (unlike x12): the screen is a per-prediction-row
annotation with identical semantics in both tables, and every existing
consumer already selects `p.*`.

```sql
ALTER TABLE predictions     ADD COLUMN screen_pass INTEGER;  -- NULL = pre-feature / screen off
ALTER TABLE predictions     ADD COLUMN screen_reason TEXT;   -- NULL when passed
ALTER TABLE predictions_x12 ADD COLUMN screen_pass INTEGER;
ALTER TABLE predictions_x12 ADD COLUMN screen_reason TEXT;
```

`screen_pass` is written on rows that carry a pick; pick-less rows keep NULL
(there is nothing to veto). `screen_reason` is the first rule that fired
(the cascade is ordered, one reason per row — keeps the report legible).

## Rules v1

Evaluated in order; first veto wins. All thresholds live in Settings.

O/U (per picked line; `edge = model_p(pick) − book_p(pick)`):

| # | reason | condition | skip when |
|---|--------|-----------|-----------|
| 1 | `book_opposed` | book_p(pick) < 0.50 and confidence < screen_override_conf (0.66) | no book |
| 2 | `cold_line` | line_hit < screen_line_floor (0.48) with n ≥ screen_min_n_line (30) | thin n |
| 3 | `cold_band` | band_hit(edge band) < screen_band_floor (0.50) with n ≥ screen_min_n_band (30) | no book / thin n |
| 4 | `drawdown` | trailing served-pick hit (last screen_trailing_n = 50 graded) < screen_breaker_floor (0.45) and tier != strong | thin n |

Edge bands: `agree ≤ 0 < noise ≤ .05 < value ≤ .12 < stretch ≤ .20 < absurd`.
No hard band vetoes (the probe showed 20+ hitting fine on small n) — bands
only go dark via rule 3 when their own rolling stats say so.

1x2 (per picked fixture; edge on the picked outcome):

| # | reason | condition | skip when |
|---|--------|-----------|-----------|
| 1 | `book_opposed` | book argmax != model pick and confidence < screen_x12_override_conf | no book |
| 2 | `cold_band` | same band construction on the picked-outcome edge | no book / thin n |
| 3 | `drawdown` | trailing x12 served-pick hit < floor | thin n |

No line rule (no lines), no tier exemption in rule 3 (x12 has no tiers).

## ScreenStats — rebuilt once per predictor cycle

`libs/py/model/screen.py`:

- `fit(conn, population, market, s) -> ScreenStats` — one SQL pass over the
  last `screen_days` (14) of settlements, same joins as settle's
  `_VS_BOOK_QUERY` / the population-split queries. Produces:
  - `line_hit[line], n_line[line]` — counterfactual, O/U only
  - `band_hit[band], n_band[band]` — counterfactual, rows with a book close
  - `trailing_hit, n_trailing` — served picks only (pick_correct), last N
- `apply(pick_ctx, stats, s) -> (pass: bool, reason: str | None)` — pure,
  no I/O, unit-testable with fixture stats.

Cycle integration: build 4 stats objects (priced/schedule × ou/x12) next to
the recal-map fit; `_line_row` and `_x12_row` call `apply` after the pick is
assigned. Cost: two aggregate queries per cycle — same order as the recal fit.

No model_version tag: the screen changes no probabilities. `screen_pass IS
NOT NULL` is the feature's own regime marker.

## Config (Settings)

```
screen_enabled: bool = True          # annotation is safe-by-construction; dark by default because
screen_ui_enabled: bool = False      # ...the UI flag is what actually ships it (timer live-switch caveat)
screen_days: int = 14
screen_override_conf: float = 0.66
screen_x12_override_conf: float = 0.62   # provisional; re-read from x12 served confidences before Phase 2
screen_line_floor: float = 0.48
screen_min_n_line: int = 30
screen_band_floor: float = 0.50
screen_min_n_band: int = 30
screen_trailing_n: int = 50
screen_breaker_floor: float = 0.45
```

`SCREEN_ENABLED=false` is the one-flag rollback: columns stop being written
(NULL), UI toggle hides (API reports the feature off), nothing else changes.

## Settlement / CLI report — the tuning loop

`python -m settlement.settle screen` — per population per market:

- passed vs vetoed: n, graded, hit rate (from settlements joined on the
  served row's screen columns)
- per reason: n vetoed, graded hit of vetoed rows ("rules that veto winners
  get loosened or dropped")
- per band and per line: current rolling stats the serving screen would see
- per player: observation-only counterfactual table (feeds the decision on
  whether a player rule ever earns a v2 slot)

`scorecard` gains one line per population: screened hit rate next to the
existing overall hit rate.

## API

- `/api/slate`: each line object and the x12 view gain `screen_pass` and
  `screen_reason` (read straight off the row). Response top level gains
  `screen_enabled` (from a new `/api/health`-style settings read or an env
  mirror in the API config) so the UI knows whether to render the toggle.
- `/api/analysis`: settlements rows and x12 rows gain both columns (they ride
  the existing `p.*` selects).
- `/api/metrics`: `summarize`/`summarizeX12` gain a `screened` sub-block:
  `{ n, hit_rate }` over graded picks with screen_pass = 1, per population.

## UI

### Slate — the toggleable option

Masthead toggle **SCREEN** next to the existing 1X2 toggle, same pattern:
`localStorage('screenui')`, off by default, rendered only when the API says
`screen_enabled`. Toggle = swap, never both (docs/X12_UI.md doctrine):

- **off**: slate exactly as today.
- **on**: vetoed picks are demoted in place — the card shows the market but
  the pick chip is replaced by a `✂ screened · <reason>` badge (styling
  mirrors the model-only badge). Cards whose only pick was vetoed drop out
  of the pick-count in the status line. The filter chip rows stay the same;
  chips count only surviving picks while the toggle is on.
- The toggle applies to whichever market view is active (O/U or 1X2) — it is
  one screen state, not one per market, mirroring the shared x12 toggle.

### Model performance page — filters

- New select `p-screen` beside `p-gate`: `all / passed / vetoed /` one entry
  per reason (`book_opposed`, `cold_line`, `cold_band`, `drawdown`). Wire
  into `perf.filters` + `perfMatches` like every other select. Rows with
  NULL screen columns (pre-feature history) match only `all` — same
  convention as probability filters on rows without a book price.
- New breakdown table **SCREEN** next to PICKED VS SUPPRESSED:
  passed / vetoed(reason…) rows through the existing `renderBreakdown`.
- The x12 mode of the perf page gets the same filter and breakdown (minus
  `cold_line`).

## Phases

- **Phase 0 — plumbing (dark).** Migration 004; screen.py + fit/apply;
  cycle integration; tests (`tests/test_screen.py`: each rule fires and
  self-skips, cold-start passes everything, populations isolated, x12
  variant); `screen` CLI report. Ship with SCREEN_ENABLED=true,
  SCREEN_UI_ENABLED=false — columns accrue, nothing visible.
- **Phase 1 — accrual + judge (~7 days).** Gate to advance: on graded served
  picks, vetoed hit rate < passed hit rate by ≥ 3 pts per market (priced
  population; schedule is drawdown/cold-line only and judged on direction,
  not margin), and vetoed volume ≤ ~25% of picks. A rule that vetoes above
  the passed rate gets its floor loosened or is dropped before Phase 2.
- **Phase 2 — UI.** Slate toggle + perf filters/breakdown behind
  SCREEN_UI_ENABLED; flip it after a look at the dark report. Re-read
  screen_x12_override_conf from served x12 confidence quantiles first.
- **Phase 3 — revisit list.** Player rule (only if the observation table
  shows a stable offender at real n); fitted meta-gate (revisit when graded
  picks are 5–10× today's 344 — the screen's own columns are its training
  data); band boundary re-read.

## Hazards ledger

- **Regen honesty**: untouched by construction (annotation-only). Do not
  "fix" this later by letting the screen null picks — that breaks the canary.
- **Feedback loop**: stats must stay counterfactual over ALL settled rows.
  Adding a `WHERE pick IS NOT NULL` to the fit query is the exact bug shape
  recal already had once.
- **Timer live-switch**: config defaults take effect on the next cycle with
  no deploy (club_enabled precedent) — both flags default to the dark state;
  enabling is a deliberate env change.
- **Tier analytics drift**: none — tiers are untouched. But scorecard
  "screened" lines mix regimes across the enablement date exactly like every
  other feature; `screen_pass IS NOT NULL` scopes queries to the regime.
- **cp1252 console**: the report is ASCII-only, like scorecard.
