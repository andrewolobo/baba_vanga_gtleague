# 1x2 UI rollout (planned 2026-07-12)

Serving head enabled 2026-07-12 (`X12_ENABLED=true`, docs/X12_SERVING.md).
This doc phases its exposure through the API and web app. Each phase is
independently shippable and safe alone — the repo's live timer makes anything
on disk deployed, so no phase may depend on a later one.

**UI contract (user decision, corrected 2026-07-12):** the toggle **switches
the market view** on both pages — 1X2 ON means the O/U details are hidden and
the same surfaces (slate cards, scorecard, settled cards, masthead badge,
performance sections) show 1x2 instead; OFF restores the O/U view unchanged.
One shared toggle (`x12ui` in localStorage), **off by default**. The first
build rendered 1x2 *alongside* O/U; the user corrected this to a swap — the
markets never share the screen.

## Phase 0 — enable the head, dark ✅ 2026-07-12

`X12_ENABLED=true` in `.env`. Rows accrue in `predictions_x12` /
`settlements_x12`; nothing reads them yet. Expect ~35–40 picks/day. Watch
`x12_settled` / `regen_agrees` in `settlement.settle run` for the first day.

## Phase 1 — API exposure (read-only, UI-invisible) ✅ 2026-07-12

`services/api/src/routes.ts` only; the web app ignores unknown fields, so
this ships before any UI.

- `latestX12(eventId)` mirroring `latestPredictions` against
  `predictions_x12` (latest `predicted_at` batch; one row per event — no
  line fan-out).
- `/api/slate`: attach `x12: null | { p_home, p_draw, p_away, pick,
  confidence, value, odds: {home,draw,away}, book: {home,draw,away},
  edge }` on both branches. `latestOdds` already parses the 1x2 snapshot —
  it is returned and unused today. `gtl:` rows get odds/book/edge null and
  `value` false, same as totals. `edge` = model prob − book implied on the
  model's argmax side.
- `/api/metrics`: add `x12: { priced, schedule }` — populations split
  exactly like totals (docs/POPULATION_SPLIT.md; gtl x12 rows settle via
  `matched_match_id`). Per population: settled, graded picks, hit rate,
  regen agreement, value {n, hit}. **No tiers** — none exist until bands
  are quantiled on served confidence (docs/X12_SERVING.md).
- `/api/settlements`: LEFT JOIN `settlements_x12` + its served row; add
  `x12_pick`, `x12_confidence`, `x12_result`, `x12_outcome`
  (correct/wrong/no-pick) to each event. Same event ids, so attach —
  don't emit a second array.
- Headline rule is shared with totals grading: last batch served before
  kickoff.

## Phase 2 — Ledger view, behind the toggle ✅ 2026-07-12

- Toggle: a pill in the shared chrome next to `nav.tabs` (visible from both
  views), label `1X2 · OFF/ON`, default off, persisted in `localStorage`
  so the two views always agree. All x12 rendering keys off one
  `state.x12` boolean.
- Fixture card: separate sub-block under the O/U content, own top border
  and `MATCH WINNER · 1X2` label — 3-segment home/draw/away bar, pick +
  confidence (no tier chip, deliberately), book odds + edge when priced,
  `▲ VALUE` badge. Model-only cards show `awaiting odds`, matching totals.
- Scorecard: two additional tile rows (model-only 1x2, book-priced 1x2)
  below the totals rows — never pooled with totals, never with each other.
- Settled cards: one extra grading line (`1X2: HOME ✓` / `✕` / `· no pick`)
  separate from the totals tag.
- Just-enabled grace: toggle always renders; blocks with no x12 rows say
  `no 1x2 data yet` rather than hiding.

## Phase 3 — Model performance view, behind the same toggle ✅ 2026-07-12

- `/api/analysis` gains an `x12` section (this phase ships API + UI
  together): per settled event — argmax side, confidence, pick vs
  suppressed (gate is 0.50, **never** the totals 0.60), book implied,
  result, population.
- UI: a `Match winner (1x2)` section appearing when the toggle is on —
  tiles (events, gradable, hit rate, avg confidence, regen agree),
  picked-vs-suppressed and by-population breakdowns, daily hit-rate chart
  via the existing `renderRateChart`, sortable settled table. Reuse the
  counterfactual caveat pattern: suppressed rows are what the gate
  refused, not results.
- Hit rate is judged against the walk-forward 59% only after a few hundred
  graded picks; the section should say so.
- Added on request 2026-07-12: a `Model conf` column (the model's confidence
  in its read, i.e. its top-outcome probability, shown for every row) and
  filters — population (model-only/book-priced), read (home/away), outcome
  (hit/miss) — scoping the tiles, breakdowns, chart and table together,
  same pattern as the totals view.
- Added on request 2026-07-13: a pick-gate filter (`x-gate`: picks only /
  no-picks only), completing parity with the totals view's `p-gate` —
  picked here means the 0.50 x12 gate, and the section's counterfactual
  caveat already handles the no-picks-only view.
- Added on request 2026-07-12 (second pass): clubs in the settled table's
  Match column (player leads, club muted after a `·`; club alone when no
  player name) plus two more filters — a match text search over player and
  club names (same haystack as the totals `p-match`) and a team dropdown
  (`x-club`) built from the clubs present in the loaded window, so newly
  aliased clubs (docs/CLUB_FEATURE.md) appear as soon as they have a
  settled 1x2 row. Both scope the whole section like the existing filters.
- **Bug found & fixed during this phase (2026-07-12):** `_run_x12` walked
  fixtures only, so schedule-population (`gtl:`) x12 rows never settled —
  the totals population-split bug recurring in the new head. Fixed with
  `_run_x12_schedule` in `settlement.settle` (same exact-id join and
  `schedule_settle_enabled` guard as the totals gtl loop; `NOT EXISTS`
  back-settled history on its first live run). `settle run` now reports
  `x12_sched_settled` / `x12_sched_pending`. Tests:
  `tests/test_predictor_x12.py::test_x12_schedule_*`.

## Phase 4 — data-gated, explicitly not now

- Tier bands quantiled on *served* pick confidence (the totals tier
  incident), then tier chips/badges appear in Phases 2–3 surfaces.
- 1x2 vs-book scorecard (edge-coefficient regression; needs a few
  thousand graded rows).

## Rollback

Any phase: toggle off restores today's DOM; `X12_ENABLED=false` stops new
rows; API fields go null/empty and the UI already renders that state.
