# value_flag EV gate — fixing the value inversion

Status: Phase 1 BUILT 2026-07-19, flag OFF (VALUE_EV_ENABLED=false —
serving is byte-identical legacy until enabled). Phase 2 = env flip, then
judge via `vs-book` by-value regime split ('(ev)' rows) after ~a week.

`value_flag` is currently a pure edge threshold: `(model_p − book_p) >=
MIN_EDGE` ([cycle._line_row], `_x12_row` for 1x2). Measured 2026-07-19 over
2,477 settled book-priced totals rows at the actual listed prices:

| rows | n | hit | flat-1u ROI |
|---|---|---|---|
| value_flag = 1 (current rule) | 1,020 | 49.4% | **−7.7%** |
| value_flag = 0 | 1,457 | 57.4% | −4.2% |

The flag is inverted: it selects the adverse-selection region (the 5–12pt
edge band hits 48.0%, ROI −10.6% — mid-size disagreement with this book
means the book knows something, docs/PICK_SCREEN.md). Two more measured
facts that pin the fix's shape:

- **Hit rate alone cannot define value.** The agree band hits 59.8% but
  still loses −3.5%: its odds average 1.62 (break-even 62%). The row's own
  price must enter the rule.
- **Evidence-gated replay**: `edge >= MIN_EDGE AND band_hit * odds >= 1`
  keeps 243 rows at 53.9% hit, **ROI +1.6%** (full-window band stats,
  in-sample — treat as upper bound). Volume drops ~4×: INTENDED. A value
  flag that fires rarely and honestly beats one that fires often and loses
  (the population-split "vanishing picks are intended" precedent).

## New rule

A row is value iff ALL of:

1. `edge = model_p(pick side) − book_p(pick side) >= min_edge` (unchanged —
   value still means "we disagree with the price", it just now needs
   evidence that this KIND of disagreement has been paying);
2. its edge band's rolling counterfactual hit has real sample:
   `band_n >= screen_min_n_band`;
3. the band's rolling hit clears the row's own break-even:
   `band_hit >= 1/odds + value_ev_margin`.

The band stats are the pick screen's own `ScreenStats.band_hit`
(model/screen.py) — already refit every cycle, per population, per market,
counterfactual over all settled rows. No new stats machinery. Schedule rows
have no odds and keep `value_flag = False` always (unchanged).

x12: identical, using the ("priced", "x12") stats and the picked outcome's
odds from the fixture's 1x2 prices.

## Config

```
value_ev_enabled: bool = False   # deploy decision; timer live-switch caveat
value_ev_margin: float = 0.0     # extra ROI demanded above break-even
```

`VALUE_EV_ENABLED=false` is the one-flag rollback to the legacy edge rule.
Sample floor reuses `screen_min_n_band` — one notion of "enough band data".

## Semantics drift — the '-ev' tag

value_flag's meaning changes on rows priced under the new rule, and
append-only tables keep both regimes forever. The confidence-column incident
doctrine: semantics changes must be taggable. Rows where the new rule
ENGAGED (flag on AND stats warm enough to consult — regardless of the flag's
resulting value) get `model_version + '-ev'`, exactly the '-recal2' pattern;
untagged rows carry legacy semantics. When stats are cold (fresh DB,
SCREEN_ENABLED=false) the rule falls back to the LEGACY edge test, untagged —
`value_ev_enabled=true` never silently mutes the flag on a cold start.
('-ev' collides with no existing version-substring check: -club/-tod/
-recal/-recal2/-h2h are all distinct.)

Settlement regen compares picks only — value_flag is not regen-checked, so
no settlement changes.

## Touch list

- `core/config.py` — the two knobs.
- `predictor/cycle.py` — `_line_row`: replace the `value = ...` line with a
  helper `_value_flag(model_p, book_p, odds, scr, s)` (screen stats already
  flow in via `scr`); `_x12_row` mirror. Version tagging via the helper's
  engaged/legacy return, '-recal2'-style.
- `model/screen.py` — nothing (stats already exist); expose a small
  `band_ev_ok(edge, odds, stats, s)` next to the cascade so cycle and tests
  share one implementation.
- Consumer audit (no expected code changes, verify only): UI VALUE chips +
  filters, `/api/metrics` value block, scorecard value row, vs-book
  by-value breakdown, wagers surfaces — all read the stored flag and follow
  it automatically. Analytics spanning the enablement date must split on
  '-ev' (add one line to the vs-book by-value section header noting the
  regime split).
- `tests/test_value_ev.py` — EV pass / band-cold fallback-to-legacy /
  below-floor / margin / no-odds / schedule-stays-false / '-ev' tagging /
  flag-off byte-identical legacy behavior / x12 mirror.

## Phases

- **Phase 0 (done 2026-07-19)**: measurement above; margin pinned at 0.0 to
  start (the +3% margin variant kept only 25 rows — too thin to serve).
- **Phase 1 — implement + tests**, flag off. No dark accrual is possible
  for a UI-visible boolean, but the change is strictly volume-reducing and
  one-flag reversible.
- **Phase 2 — enable** (env change). Watch `vs-book --days 7` by-value
  breakdown on '-ev' rows after ~a week: the gate is ROI ≥ 0 on flagged
  rows at n ≥ ~50. If flagged volume is near-zero for a full week, that is
  the flag being HONEST (no band currently beats its price), not a bug —
  resist loosening the margin below 0.
- **Phase 3 (optional, separate decision)**: an odds-aware "confirmed"
  surface for agree-band + high-conf rows is NOT part of this fix — measured
  −3.5% ROI at current prices; revisit only if the book's pricing loosens.

## Hazards

- **In-sample optimism**: the +1.6% replay used full-window band stats; the
  served rule uses rolling 14d stats and will be noisier. The Phase 2 gate
  is deliberately just "ROI ≥ 0".
- **Coupling to the screen**: band stats arrive via `scr` — with the screen
  disabled the value rule degrades to legacy (documented above), so the two
  features stay independently rollback-able.
- **Odds hygiene**: one settled row carried odds ≈ 0 (probe's inf
  break-even); the helper must treat odds < 1.01 as no-odds (legacy path)
  rather than dividing by it.
