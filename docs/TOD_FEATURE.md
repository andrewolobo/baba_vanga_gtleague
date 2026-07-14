# Time-of-day as a model feature — measurement (2026-07-10)

Status: **measured, PASSES the gate — both standalone and on top of the
served club model (tod-club table below). Phase 1 (measurement harness) and
Phase 2 (serving) landed 2026-07-11; ENABLED in production 2026-07-10 ~21:17
UTC (`TOD_ENABLED=true`; rollback: set it false, next cycle prices
hour-blind). Live baseline at enable time recorded below.**

The UTC kickoff hour carries real, non-redundant signal for totals: matches
kicked off 03–05 UTC run materially under the player-expected total, matches
at 09–10 and 17–19 UTC run over it.

    python -m model.evaluate tod --eval-days 90   # reproduces the table below

## The dataset finding that motivated this

On all 100,070 finished matches (2025-08-01 … 2026-07-10):

- Mean total swings by UTC hour from ~3.51 (04:00) to ~4.22 (10:00);
  over-3.5 rate 45.8% → 58.4%. ANOVA p ≈ 5e-97, eta² ≈ 0.5%.
- **It is not player-shift composition.** 93 of 94 players appear in nearly
  every hour block, and the effect survives subtracting per-player mean
  totals (residual ANOVA p ≈ 5e-89): hour 4 runs −0.43 goals below
  player-expected, hours 9–10 / 17–19 run +0.14 to +0.22 above.
- **It is stable and strengthening.** Split-half (Aug–Jan vs Feb–Jul) hourly
  residual profiles correlate at r = 0.744 (p = 3e-5); the 03–05 trough
  deepened from −0.19 to −0.60 goals in the recent half.
- Day of week carries ~nothing (max gap 0.09 goals). Not a feature.

## How it enters the model

A cyclic Fourier basis on the fractional UTC kickoff hour, fitted **jointly**
with the player (and optionally club) factors in the existing ridge Poisson
GLM (`poisson.fit(..., with_tod=True)`):

    log λ = c + home·is_home + att[player] + dfn[opponent]
              + Σ_k  s_k·sin(2πk·h/24) + c_k·cos(2πk·h/24)

Both side-rows of a match share the hour — it is a pace effect on the match.
Joint fitting is what makes it a *residual* hour effect rather than a proxy
for who is on shift (same argument as CLUB_FEATURE.md vs marginal Elo).
A caller that passes no hour gets the hour-blind λ — the degradation path,
mirror of the unseen club.

The coefficients refit daily inside the day-frozen artifact under the 7-day
half-life, so the measured drift in the profile is tracked automatically.

## The gate

Walk-forward, day-frozen Poisson refit each eval day, cutoff-aware form leg,
20-minute publish lag — the standard protocol. Served blend
(`0.7·Poisson + 0.3·form`) vs the same blend with the tod term in the
Poisson leg. **90 eval days, ~2026-04-12 … 2026-07-10, 28,891 out-of-sample
matches, K = 3:**

| Line | blend | blend+tod | ΔAUC (95% CI, paired bootstrap) | ΔBrier |
|---|---|---|---|---|
| 2.5 | 0.7103 | 0.7124 | **+0.21 pts** [+0.09, +0.32] | −0.0004 |
| 3.5 | 0.7156 | 0.7171 | **+0.15 pts** [+0.06, +0.26] | −0.0004 |
| 4.5 | 0.7200 | 0.7220 | **+0.20 pts** [+0.11, +0.30] | −0.0006 |

AUC and Brier improve at every line; every CI excludes zero. At 60 eval days
(n = 19,401) the effect is the same size but line 3.5 grazes zero
([−0.00, +0.23]) — the 90-day window resolves it. `model_runs` kinds `tod`.

For scale against prior features on this protocol: club bought +0.59, the
entire form leg +0.4, multi-span form was rejected at +0.07, shipped Platt
recal −0.0009 Brier. tod at +0.15…+0.21 is a real but modest feature —
roughly a third of club.

### The basis sweep is the strongest evidence it is the real profile

60-day window, all lines:

| K (harmonics) | ΔAUC 2.5 / 3.5 / 4.5 | verdict |
|---|---|---|
| 1 | −0.02 / −0.03 / −0.02 | wash |
| 2 | −0.04 / −0.03 / −0.03 | wash |
| **3** | **+0.19 / +0.12 / +0.21** | 2.5 & 4.5 SIG |
| 4 | +0.18 / +0.12 / +0.21 | identical to K=3 |

The measured profile has two peaks and one trough; K ≤ 2 cannot represent
that shape and finds nothing, K = 3 is the smallest basis that can and finds
the whole effect, K = 4 adds nothing. A spurious fit would not switch on at
exactly the harmonic the profile requires. `TOD_FOURIER_K = 3` is served.

### Mechanism: cross-hour re-ranking, not within-hour sharpening

Within the 02–05 UTC trough segment alone, AUC barely moves (0.6939 →
0.6946); the gain comes from re-ranking matches *across* hours — exactly
what a level shift on λ should do. `sd(λ_total)` widens 0.796 → 0.806,
the same dynamic-range expansion club produced (0.822 → 0.842).

## Caveats

- **Whether the book prices it is unmeasured.** ~290 settled events with
  joined odds is far too few. If betPawa's lines already carry the overnight
  trough, tod improves calibration but not edge vs the book. Recheck via
  `settle vs-book` once a few thousand tod-tagged settlements exist.
- The trough hours are also the thinnest slate hours (02–04 UTC ≈ half the
  volume of peak hours), so the live pick-rate impact skews smaller than the
  headline ΔAUC suggests.
- ~~tod was measured against the player-only blend, not player+club.~~
  **Resolved 2026-07-10** — measured via `python -m model.evaluate tod-club
  --eval-days 90` (the combined GLM fits both blocks in one design matrix;
  all arms score identical rows, paired bootstrap on club → club+tod):

  | Line | blend | +tod | +club (served) | +club+tod | ΔAUC over served (95% CI) | ΔBrier |
  |---|---|---|---|---|---|---|
  | 2.5 | 0.7103 | 0.7124 | 0.7184 | 0.7202 | **+0.18 pts** [+0.08, +0.28] | −0.0004 |
  | 3.5 | 0.7156 | 0.7171 | 0.7232 | 0.7243 | **+0.11 pts** [+0.04, +0.20] | −0.0004 |
  | 4.5 | 0.7200 | 0.7220 | 0.7275 | 0.7290 | **+0.15 pts** [+0.06, +0.23] | −0.0005 |

  All three lines significant, Brier improves at each. Club absorbs only
  ~0.03 pts of tod's standalone worth (+0.21/+0.15/+0.20 → +0.18/+0.11/+0.15)
  — the two signals are near-orthogonal, same as club vs form. The features
  also stack cleanly: club+tod is the best arm at every line. `model_runs`
  kind `tod-club`. **Phase 2 should serve tod alongside club in one GLM.**

## Phase 1 code (landed; changes no served output)

- `model/poisson.py` — `tod_basis`, `fit(..., with_tod=False, tod_k=3)`
  appends the dense Fourier block; `side_lambda`/`predict_sides` take
  optional `hour`; `tod_z` is the per-hour log-λ contribution. `with_tod`
  False fits an empty block: the rollback invariant.
- `model/registry.py` — the stale-pickle schema check now requires the `tod`
  field (the 2026-07-10 club incident class: unpickle skips `__init__`, so
  pre-tod artifacts crash on first predict unless refit).
- `model/evaluate.py` — `build_predictions(with_tod=True, tod_k=…)` fits a
  second day-frozen GLM per eval day (both arms score identical rows), and
  with `with_club=True` too a third combined GLM (`lam_pct_h/_a`);
  `tod_report` = paired-bootstrap gate + trough/rest split;
  `tod_club_report` = the four-arm table above; `tod` / `tod-club` CLI modes
  with `--tod-k`.
- `tests/test_tod.py` — 7 tests: rollback invariant, hour-blind degradation,
  24h periodicity, leakage canary on the tod path, planted-signal recovery,
  combined club+hour signal recovery (also pins the design-matrix column
  layout), registry refit of pre-tod pickles.

## Phase 2 — serving — **CODE LANDED 2026-07-11, ENABLED IN PROD 2026-07-10 ~21:17 UTC**

Mirror of CLUB_FEATURE.md step 3. `TOD_ENABLED` defaults **false** in code;
production runs with it set true in `.env` (a live switch: the API spawns
cycles on a timer, no deploy needed). First tod-tagged batch:
2026-07-10T21:17 UTC.

1. `core/config.py` — `tod_enabled: bool = False`, the one-flag rollback,
   documented in `.env.example`.
2. `model/registry.py` — `get_poisson(..., with_tod=)`; the artifact tag
   gains `_tod` (`poisson_{day}_hl7_a0.01_club_tod.pkl`), so a cached
   hour-blind pickle cannot silently load into a tod-enabled cycle. The
   stale-pickle schema check already covers the `tod` field (Phase 1).
3. `predictor/cycle.py` — `_fixture_lambdas` passes the fixture's fractional
   kickoff hour to `predict_sides` **unconditionally**: against a model
   fitted without the tod block the hour is a strict no-op, so the flag's
   only effect is which artifact is fitted. `model_version` gains `-tod`
   (order: `-club-tod-recal`).
4. `settlement/settle.py` — `_Regen` reads `with_tod` off the served row's
   `model_version` (exactly as `-club`) and caches a Poisson per
   `(day, with_club, with_tod)`, so pre-tod rows regenerate hour-blind and
   `regen_agrees` survives the transition.
5. `tests/test_predictor_tod.py` — 6 tests on a league where the kickoff
   hour drives λ entirely (both fixtures share players AND clubs; a silent
   no-op cannot pass): tod reaches the served λ, `-tod` tag, rollback flag,
   artifact-tag separation, regen version-awareness, regen/serving
   agreement. Suite 104/104.

**Dry run against a copy of production** (2026-07-10 slate, 29 book fixtures
+ 57 scheduled, `TOD_ENABLED` false vs true, same `now`, club active in both
arms):

| | club only | club+tod |
|---|---|---|
| model_version | `blend-w0.7-hl7-a0.01-club` | `blend-w0.7-hl7-a0.01-club-tod` |
| λ_total mean | 3.637 | 3.660 |
| λ_total sd | 0.722 | 0.734 |
| picks surfaced | 67 | 70 |

140 paired covered rows, **zero with identical λ** (mean \|Δλ\| 0.076, max
0.19) — the feature is demonstrably reaching serving. 11 picks changed side,
25 tier moves. Per-hour Δλ direction matches the recent-half residual
profile (20–21 UTC pushed down ~0.12, 23–00 UTC up ~0.10). 3 of 143 rows per
arm had no counterpart — schedule-only games derive their canonical lines
from `round(λ_total)`, the same expected side effect the club dry run had.

## Live diagnostics at enable time (recorded 2026-07-11)

The baseline the first tod week has to beat. Rollback remains
`TOD_ENABLED=false` — pricing is byte-identical to pre-tod (test-covered).

- **λ slope 1.305, 95% CI [1.078, 1.524]** (`vs-book --days 7`, 385 settled
  model-covered rows — **all pre-tod**; zero tod-tagged rows settled yet at
  recording time). Club-enable baseline for reference: 1.364 [1.093, 1.619].
  The two are statistically indistinguishable; window-to-window wobble of
  ±0.05–0.10 is expected at this n and must not be read as feature impact.
- Recal is in identity mode (best-supplied line 118 of the 300 graded
  samples `RECAL_MIN_N` requires), so no served row carries `-recal` and
  the tod transition cannot interact with the Platt maps yet.
- 268 tod-tagged prediction rows served in the first batch window
  (`blend-w0.7-hl7-a0.01-club-tod`).

What to watch, in order of how fast it resolves:

1. **λ slope, tod arm vs pre-tod arm.** Expected direction: DOWN toward 1.0
   — tod widens λ's dynamic range with calibrated variance (dry run: sd
   0.722 → 0.734), which raises cov(λ, total) and var(λ) together. The
   worry condition, once a few hundred tod-tagged settlements exist: the
   tod arm's slope sits *above* the pre-tod arm's with separated CIs. That
   means the hour term adds noise-variance live, and is the signal to flip
   the flag back. Pooled-window wiggles are not.
2. **`regen_agrees` through the transition** (must stay ~100%; `_Regen` is
   version-aware, so a collapse means the serving path diverged, not the
   transition itself).
3. **Overnight-trough picks (02–05 UTC)** — where the model now disagrees
   most with its old self, and where "does the book already price the
   trough" gets answered first.
