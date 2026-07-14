# Club as a model feature — measurement and ship plan (2026-07-10)

Status: **measured, PASSES the gate, and SHIPPED to serving 2026-07-10
(`CLUB_ENABLED=true`; rollback: set it false, next cycle prices player-only).**
Steps 0–3 landed. Step 4's Platt concern dissolved on measurement; the tier
re-quantile is blocked on data with tooling now in place; club-ridge tuning
and the regime-switch watch remain open.
The club/country each player drives carries real, non-redundant signal for
totals, and much stronger signal for 1x2 (a market we do not price today).

This overturns a claim the codebase asserted as settled.

    python -m model.evaluate club --eval-days 60   # reproduces the table below

## The claim being overturned

[`core/schema.py`](../libs/py/core/schema.py) annotates `home_club` with
*"team/country label; no marginal signal (parent §2.1.2)"*, and
[`model/data.py`](../libs/py/model/data.py) does not even `SELECT` the column —
club is dropped at the data layer and never reaches a feature.

That annotation was inherited from the **parent league**, where club explained
0.75% of player-adjusted residual. [`SPINOFF_PLAN.md`](SPINOFF_PLAN.md) §2 says
plainly: *"Expect the new league to behave the same, **but re-check once**."*
No evidence the re-check ran. It did not hold.

## Why it is identifiable here

96 players, 103 clubs, and clubs **rotate**: most players have used 40–80
distinct clubs over the sample. Club is not collinear with player, so a club
factor is cleanly separated from player skill by a joint fit.

This is the crux. A *marginal* club rating (Elo on raw results) would absorb
player skill — if stronger players pick stronger teams, the rating is
confounded. Fitting club **jointly** with player attack/defense in the existing
GLM identifies both. Do not build a standalone Elo as the estimator.

## The gate

Walk-forward, day-frozen Poisson refit each eval day, cutoff-aware form leg
with the measured 20-minute publish lag — the same protocol as
[FEATURE_IDEAS.md](FEATURE_IDEAS.md). **60 eval days, 2026-05-12 … 2026-07-10,
19,267 out-of-sample matches.**

Served blend (`0.7·Poisson + 0.3·form`) vs the same blend with club factors
added to the Poisson leg:

    log λ = c + home·is_home + att[player] + dfn[opponent]
                             + catt[club]  + cdfn[opp_club]

| Line | blend | blend+club | ΔAUC (95% CI, paired bootstrap) | ΔBrier |
|---|---|---|---|---|
| 2.5 | 0.7151 | 0.7209 | **+0.59 pts** [+0.36, +0.80] | −0.0010 |
| 3.5 | 0.7171 | 0.7230 | **+0.59 pts** [+0.41, +0.79] | −0.0017 |
| 4.5 | 0.7233 | 0.7287 | **+0.53 pts** [+0.33, +0.74] | −0.0015 |

AUC and Brier both improve at every line; every CI excludes zero. That is the
acceptance gate.

### Club is not redundant with the form leg

The one thing that could have killed this. Against **pure Poisson** club buys
+0.65 AUC pts; against the **blend** it still buys +0.59. The form leg absorbs
only ~0.06 pts. The signals are near-orthogonal: form tracks how the player is
playing, club tracks what they are driving.

For scale on the same pure-Poisson baseline, the *entire form leg* buys +0.4
AUC pts, multi-span form was rejected at +0.07, and the Platt recalibration
shipped 2026-07-09 bought −0.0009 Brier at line 3.5 with zero AUC by
construction. Club is the largest mean-side signal found so far.

### It holds in both competition regimes

The league ran club competitions until 2026-06-09, then switched 100% to World
Cup (national teams). The effect survives the switch but is ~2× weaker under it:

| Regime | n | ΔAUC 2.5 | ΔAUC 3.5 | ΔAUC 4.5 |
|---|---|---|---|---|
| Club competitions | 8,227 | +0.89 | +0.81 | +0.74 |
| World Cup | 11,040 | +0.34 | +0.43 | +0.35 |

All six are significant. **We are currently serving the World Cup regime**, i.e.
the one where club helps least. Size expectations accordingly.

### Blend weight needs no re-tuning

Sweeping the Poisson share for blend+club at line 3.5 gives a flat plateau
across w = 0.7–0.8 (AUC 0.7230 vs 0.7235). The served
`TOTALS_BLEND_WEIGHT = 0.7` sits inside it. Leave it alone.

### Mechanism: the club effect is pace, not strength

Decomposing the fitted club coefficients:

- **pace** = `catt + cdfn` (score more *and* concede more) — sd ≈ 0.29 goals/side
- **strength** = `catt − cdfn` (score more, concede less) — sd ≈ 0.15 goals/side

`corr(club_attack_resid, club_defense_resid) = +0.568` — **positive**. Clubs
where you score more are clubs where you also concede more. Pace dominates, and
pace is exactly a totals signal, which is why the gate passes.

A naive read of raw club residuals looks like team quality and is a trap: weak
national teams show negative residuals on *both* attack and defense, which is
low match pace, not weakness. Genuine weakness would be negative attack and
**positive** defense.

## The larger finding: club drives 1x2, which we do not price

`1x2` odds are ingested and stored
([`odds_ingest/normalize.py`](../services/py/odds_ingest/normalize.py)) but
**never predicted**. The model emits only `p_over/p_push/p_under`.

Regressing goal difference on club strength *after* subtracting the player
model's expected goal difference, club competitions, n = 25,622:

| Club-strength quartile | actual GD | model's expected GD | home win % |
|---|---|---|---|
| home much weaker | −0.318 | −0.026 | 33.1% |
| — | −0.083 | −0.022 | 37.4% |
| — | +0.042 | −0.027 | 40.1% |
| home much stronger | +0.234 | −0.025 | 45.4% |

`resid_gd = +0.713 × club_edge`, **t = +19.2**. World Cup era: same sign,
t = +4.4. Actual goal difference swings 0.55 goals and home-win rate 12 points
across the quartiles while the player model's expectation moves by 0.001 — it
is structurally blind to this.

In club competitions `corr(club_edge, goal_diff) = 0.120` versus
`corr(player_expected_gd, goal_diff) = 0.064`. **The team a player picks
predicts the result about twice as well as the model's rating of the player
does.**

Note the draw rate is *flat* (0.198 → 0.204) across club strength. Mismatches
are not converted into draws; the model simply never had a view on who was
favored.

## Cold start is bounded and fails safe

At a regime switch the club entities are new. Coverage (both clubs having ≥100
prior side-rows) collapsed to **2.4% for ~7 days** after 2026-06-09, recovered
to ~94% by 06-16, and is 100% now.

This fails safe by construction: an unseen club gets coefficient 0, i.e.
league-average, and the prediction degrades to exactly today's player-only λ.
Restricting the gate to club-covered matches only (n = 18,874) leaves ΔAUC
unchanged (+0.59 / +0.60 / +0.53). **No coverage gate is needed** — do not
suppress picks on unseen clubs.

## Caveats on these numbers

- Club factors were fit with the same ridge `alpha = 0.01` as players — 103
  club parameters bolted onto 192 player ones, with no tuning. A separate
  shrinkage strength for the club block should help. **This is a floor.**
- The 1x2 result used a shrunk raw goal-difference club rating as `club_edge`,
  which is the confounded marginal estimator. It is adequate to establish the
  effect's sign, size, and the player model's blindness to it, but a 1x2 head
  must use jointly-fitted `catt`/`cdfn`.

---

# Ship plan

Ordered so that every step is independently revertible and the risky ones come
after the cheap ones. **Steps 1–2 change no served output.**

### 0. Correct the stale claim — **DONE 2026-07-10**

[`core/schema.py`](../libs/py/core/schema.py) — the `home_club` comment
asserted "no marginal signal". Now points here. Nothing else depended on it;
it was a trap for the next reader.

### 1. Make the measurement reproducible — **DONE 2026-07-10**

Landed; changes no served output (`with_club` defaults False everywhere).

- `model/poisson.py` — `fit(..., with_club=False)` extends the design matrix
  with `catt`/`cdfn` blocks. `side_lambda`/`predict_sides` take optional
  `club`/`opp_club`, and `.get(club, 0.0)` makes the unseen-club fallback the
  identity. Adds `club_ratings()` → per-club `pace` / `strength`, the correct
  basis for any club table or Elo-style leaderboard.
- `model/data.py` — `load_matches` selects the club columns; `long_format`
  carries `club`/`opp_club` when present, and round-trips club-less frames
  unchanged (synthetic test leagues).
- `model/evaluate.py` — `build_predictions(with_club=True)` fits a second
  day-frozen GLM per eval day so both arms score identical rows; `club_report`
  scores blend vs blend+club with a paired bootstrap; `club` CLI mode records
  to `model_runs` (kind=`club`).
- `tests/test_club.py` — 8 tests: `with_club=False` reproduces pre-club λs
  exactly (the rollback invariant), unseen club degrades to the player-only λ,
  leakage canary holds on the club path, planted club signal is recovered,
  `club_ratings` decomposition.

**Exit criterion met.** `python -m model.evaluate club --eval-days 60`
(n = 19,274, club coverage 0.980, `model_runs` id 14):

| Line | blend | blend+club | ΔAUC (95% CI) | ΔBrier |
|---|---|---|---|---|
| 2.5 | 0.7152 | 0.7210 | +0.58 pts [+0.37, +0.77] | −0.0011 |
| 3.5 | 0.7171 | 0.7231 | +0.60 pts [+0.40, +0.77] | −0.0017 |
| 4.5 | 0.7234 | 0.7288 | +0.54 pts [+0.33, +0.71] | −0.0015 |

Matches the scratch measurement within bootstrap noise. `sd(λ_total)` widens
0.822 → 0.842, which is the dynamic-range expansion step 4.1 warns about.

### 2. Club identity resolution — **DONE 2026-07-10**

Landed; changes no served output (nothing reads it yet). Serving reads clubs
from betPawa fixture names (`"Germany (Tifosi)"`), training reads them from the
results source; they disagreed on 8 of 46 fixture names.

- `store/migrations/002_club_aliases.sql` — `club_aliases(book_name, club)`,
  mirroring `player_aliases`, seeded with the six true aliases:

  | betPawa fixture | results source |
  |---|---|
  | `Bosnia And Herzegovina` | `Bosnia Herzegovina` |
  | `Congo Dr` | `DR Congo` |
  | `Czechia` | `Czech Republic` |
  | `Ivory Coast` | `Cote d' Ivoire` |
  | `Korea Republic` | `South Korea` |
  | `Turkiye` | `Turkey` |

  `USA` and `United States` are **deliberately unseeded**: betPawa lists both,
  neither has a finished match, so there is no canonical spelling to point at
  and a guess would silently mis-join once they play. They are a cold start,
  not an alias.

- `store/aliases.py` — `club_from_raw` (regex kept in step with the Node API's
  `CLUB()`), `club_alias_map`, `resolve_club`, `known_clubs`, and
  `unresolved_clubs`. Unmapped names **pass through unrewritten** so they land
  in the unresolved set rather than being coerced onto a wrong club.

- `tests/test_club_aliases.py` — 12 tests. The load-bearing ones: seeds are
  terminal (no chained alias, which `resolve_club` would only follow one hop
  of), unmapped names pass through, `known_clubs` counts finished matches only
  (a club that appears solely on the upcoming schedule has taught the GLM
  nothing), and the apostrophe in `Cote d' Ivoire` survives SQL escaping.

**Verified against the production feed:** all six seeds resolve onto real
results-feed clubs; across all 77 distinct fixture side-names the only
unresolved clubs are `USA` and `United States`, exactly as intended.

**Wire `unresolved_clubs` into the cycle's stdout/report in step 3.** Silent
alias drift is the main way this feature rots: an unjoined name is not an
error, it just quietly becomes league-average. Expect it non-empty for ~a week
after every regime switch; alert only when a *playing* club stays unresolved.

Note `_scheduled_games` in `predictor/cycle.py` builds `home_raw` from
`matches.home_club`, which is already the canonical results-side name and needs
no alias — only betPawa fixtures do. `club_from_raw` is idempotent, so running
both through the same resolver is safe.

### 3. Serve it — **CODE LANDED 2026-07-10, NOT YET ENABLED IN PROD**

The only step that changes output. Everything below is merged and tested;
flipping it on in production is a deploy decision, not a code one.

- `core/config.py` — `club_enabled: bool = True`, the one-flag rollback,
  matching the `recal_enabled` precedent. Documented in `.env.example`.
- `model/registry.py` — the artifact tag now includes the club flag
  (`poisson_{day}_hl7_a0.01_club.pkl`). Without this a cached player-only
  pickle from earlier the same day loads silently and the feature does nothing
  while every metric looks healthy. Test-covered
  (`test_registry_artifact_tag_separates_club_variants`).

  *Incident (2026-07-10, fixed same day):* artifacts pickled **before** the
  `catt`/`cdfn` fields existed crash on first predict after the deploy —
  unpickle skips `__init__`, so dataclass defaults do not backfill new fields,
  and `settle`'s `_Regen` (which loads the player-only variant for pre-club
  rows) died with `AttributeError: 'PoissonModel' object has no attribute
  'catt'`. The registry now schema-checks the payload on load and refits stale
  pickles (`test_registry_refits_artifact_pickled_before_club_fields`). Any
  future field added to `PoissonModel` inherits this guard only if the check
  is updated with it.
- `predictor/cycle.py` — clubs are resolved once per cycle from `home_raw` /
  `away_raw` via `store.aliases` and passed to `pm.predict_sides`. **Coverage
  logic untouched: club never gates a pick** — an unknown club contributes 0.
  `unresolved_clubs` is reported in the cycle dict and printed to stderr.
- `model_version` gains a `-club` suffix (`blend-w0.7-hl7-a0.01-club-recal`),
  so settled rows stay attributable exactly as `-recal` does.
- `settlement/settle.py` — `_Regen` resolves clubs through the *same*
  `store.aliases` calls, and is now **version-aware**: `with_club` is read off
  the served row's `model_version`, not from current settings. Rows priced
  before club shipped are regenerated club-blind, so `regen_agrees` does not
  collapse across the transition. It caches a Poisson per `(day, with_club)`.
- Tests: `tests/test_predictor_club.py` (8) on a rigged league where the club
  drives λ entirely and the players are identical — a silent no-op cannot pass.
  Covers the `-club` tag, the rollback flag, artifact-tag separation, unknown
  clubs pricing club-blind rather than dropping, regen version-awareness, and
  regen/serving agreement.

**Dry run against a copy of production** (13 book fixtures + 77 scheduled,
`CLUB_ENABLED` false vs true, same slate):

| | player-only | +club |
|---|---|---|
| λ_total mean | 3.657 | 3.617 |
| λ_total sd | 0.550 | 0.593 |
| picks surfaced | 79 | 81 |

158 paired covered rows, **zero with identical λ** (mean \|Δλ\| 0.122, max
0.383) — the feature is demonstrably reaching serving, not no-op'ing. 26 picks
changed side; tiers migrate in both directions. Book-fallback rows are byte-
identical. All six aliases resolved on live fixture names (`Turkiye`,
`Bosnia And Herzegovina`), and no club was unresolved on the current slate.

One expected side effect: schedule-only games derive their two canonical lines
from `round(λ_total)`, so a moved λ moves the lines. 9 of 167 rows in the dry
run had no counterpart in the control batch for exactly this reason.

### 4. After shipping — **investigated 2026-07-10; two of four items dissolved**

Club was **enabled in production 2026-07-10** (`CLUB_ENABLED=true`).

1. **Re-fit the Platt maps — NO ACTION NEEDED.** The concern was that club
   widens λ's dynamic range (`sd(λ_total)` 0.822 → 0.842), so maps fit on
   pre-club probabilities would over-sharpen club probabilities. Measured
   walk-forward (60 days, 16,973 matches):

   | line | `a` blend | `a` +club | Brier blend | Brier +club | Brier **mixed** |
   |---|---|---|---|---|---|
   | 2.5 | 1.225 | 1.239 | 0.1700 | 0.1690 | 0.1690 |
   | 3.5 | 1.264 | 1.278 | 0.2119 | 0.2100 | 0.2100 |
   | 4.5 | 1.258 | 1.266 | 0.2006 | 0.1989 | 0.1989 |

   "mixed" = club probabilities pushed through pre-club maps, i.e. exactly what
   production does while the 14-day fit window drains. It is **identical to the
   club arm at four decimals**, and tail deciles stay inside the ±3-pt gate
   (worst 2.2 vs 2.0). `a` moves ~1%. **Do not add a `model_version` filter to
   `recal._FIT_QUERY`** — it would zero the maps for 14 days and buy nothing.

   Moot in any case right now: **the Platt maps have never engaged in
   production.** `RECAL_MIN_N=300` per line; the best-supplied line has 71
   graded samples. Serving is in identity mode and has been since recal
   shipped.

2. **Re-quantile tier thresholds — BLOCKED ON DATA, tooling added.**
   The eval frame cannot supply these bands. It prices fixed lines 2.5/3.5/4.5
   and (in the harness) applies Platt; production prices the book's lines,
   which sit near each match's expected total, and serves *unrecalibrated*
   probabilities. The distributions are not comparable:

   | | eval frame | production |
   |---|---|---|
   | covered rows becoming picks | 73.8% | 26.0% |
   | 95th pctile confidence | ~0.90 | 0.6666 |
   | eval-derived `strong` band | 0.8719 | **unreachable** |

   Applying the eval quantiles would have silently retired the `strong` tier —
   the same class of bug as a band below the pick gate. Instead:
   `python -m settlement.settle tiers` proposes bands **from served
   predictions**, refuses below `TIER_MIN_PICKS` (500), and refuses any band
   outside the observed confidence range. Run it once recal engages and a
   settled week of club-tagged picks exists.

   *Hygiene note it exposed:* 5,521 historical served rows carry a `confidence`
   that does not mean what it means today — 627 blended the book's probability
   into it, 4,894 assigned a tier below the pick gate. All are from
   2026-07-09 or earlier; **zero rows from 2026-07-10 break the gate**. Neither
   change bumped `model_version`, so `_TIER_QUERY` filters on *semantics*
   (`confidence == max(p_over, p_under)` and `>= pick_prob_threshold`) rather
   than on the tag. Any future query over historical `confidence` must do the
   same.

3. **Tune the club ridge block — MEASURED 2026-07-11, NO CHANGE.** Swept the
   club-column scale `c` (effective penalty `alpha/c²`) over 0.25–4 via
   `python -m model.evaluate sweep-club` (45 eval days, 13,908 matches,
   `model_runs` kind=`sweep-club`; `club_scale` param on `poisson.fit`,
   test-pinned identity at 1.0). Result is an inverted-U peaking exactly at
   the served `c = 1.0`: both 0.25 and 4.0 are significantly worse (paired
   dAUC CIs exclude zero), 0.5 and 2.0 are washes-to-slightly-worse. The
   shared `alpha = 0.01` is right for the club block — expected in hindsight,
   since clubs carry per-entity data comparable to players (45 countries ×
   ~800 recent side-rows vs 96 players × ~400). The earlier "the +0.59 is a
   floor" claim is retracted: this knob has no headroom. Serving unchanged;
   `club_scale` stays a harness-only parameter.

4. **Watch the next regime switch — OUTSTANDING.** Coverage will collapse
   again; the fail-safe should make it invisible in the scorecard. Watch
   `unresolved_clubs` in the cycle output. If it is *not* invisible, the
   fallback path is wrong.

### Live diagnostics at enable time (2026-07-10)

Recorded so the first club week has a baseline to beat. **Every number below is
badly underpowered** — 251 settlements spanning 1.2 days, and `vs-book` reports
it needs ~5,335 settled predictions to resolve a ±0.002 Brier gap.

- Scorecard: 65 graded picks, hit rate **47.7%**, Brier 0.2592.
  `lean` 36.1% (36), `solid` 62.1% (29) — non-monotone, but n is tiny.
- `regen_agrees`: **100%**, i.e. the serving path and the honest path match.
- vs-book: model Brier 0.2527 vs book 0.2484, **not significant**; edge coef
  +0.153, CI spans zero.
- **λ slope 1.364, CI [1.093, 1.619]** — realized totals move *more* than λ
  does, i.e. the model shrinks toward the league mean and the market does not.
  Club widens λ's dynamic range, so it should push this slope toward 1.0. That
  is the cheapest live check that club is helping, and it needs far less data
  than a Brier comparison. **Watch this first.**

Note the gap between walk-forward AUC (~0.72) and live pick hit rate (~48%):
the harness scores fixed lines 2.5/3.5/4.5, where over/under is often lopsided
and easy, while the book lists lines near each match's expected total. The
walk-forward numbers rank *models*; they do not forecast live hit rate.

## Rollback

`CLUB_ENABLED=false` and restart the predictor timer. With the flag off the
cycle prices byte-identically to pre-club, exactly as `RECAL_ENABLED=false`
does. Rows priced with club carry `-club` in `model_version`.

## 1x2 measurement gate — **MEASURED 2026-07-11, PASSES; serving head BUILT
same day (ships dark behind `X12_ENABLED`, see [X12_SERVING.md](X12_SERVING.md))**

Reproduce: `python -m model.evaluate x12 --eval-days 60` (records to
`model_runs` kind=`x12`). Math: `core.markets.x12_probs` (scalar, will be the
serving path) and `evaluate._x12_matrix` (vectorized), test-pinned to agree.

**Walk-forward, 60 days, 19,278 matches** — 1x2 read off the same blended side
λs that price totals, independent-Poisson convolution, no new parameters:

| | decisive AUC | Brier-3 | mean p_draw |
|---|---|---|---|
| base rates | 0.5000 | 0.6421 | — |
| blend (player-only) | 0.5840 | 0.6323 | 0.2098 |
| blend+club | **0.5925** | **0.6308** | 0.2101 |

- **Club buys +0.85 AUC pts** (CI [+0.44, +1.25], paired bootstrap, SIG) —
  larger than the +0.59 it bought on totals, confirming that club's strength
  axis (`catt − cdfn`) pays in this market specifically.
- **The draw diagonal is calibrated as-is**: predicted 0.2101 vs realized
  0.2055, quintiles tracking within ~1 pt (worst: lowest quintile, 0.177 pred
  vs 0.155 real). Real-football folklore says independent Poisson underprices
  draws; measured HERE it does not. **Do not add a Dixon-Coles diagonal** —
  the synthetic-league test pins that the pmf math is exact, so any future
  draw gap is a league property, not a bug.

**Against the book** (398 settled events with stored pre-kickoff 1x2 prices;
served λs from the predictions table, only 34% club-tagged):

| | Brier-3 | logloss | decisive AUC |
|---|---|---|---|
| model (served λs) | 0.6247 | 1.0374 | 0.6503 |
| book (de-vig close) | 0.6189 | 1.0292 | 0.6690 |
| base rates | 0.6442 | 1.0622 | 0.5000 |

Paired Brier-3 (model − book): **+0.0059, CI [−0.0023, +0.0147]** — behind but
not significantly, on λs that were two-thirds club-blind and with zero
parameters spent on this market. Note the live decisive AUC (0.650) runs above
the walk-forward number (0.593): the book's slate is a selected, smaller
population — the two numbers rank different things and should not be averaged.

**Verdict:** the existing distribution prices 1x2 nearly as well as the book
out of the box, club's edge is real and concentrated here, and the draw needs
no correction. A serving head is justified. Not built yet — that is market
plumbing (a `market` column or 1x2 rows in `predictions`, pick gates, vs-book
scoring per outcome), and the pick gate must be re-derived for a 3-outcome
market rather than copied from totals (a 0.60 threshold on a 3-class prob is a
much higher bar than on a binary one).

## What this does not do

It does not *serve* 1x2 — the gate above justifies building that head, but the
plumbing is deliberately separate work. Shipping club to totals first was a
strict prerequisite, since a 1x2 head on club-blind λs would inherit exactly
the blindness measured above (and the vs-book table shows it: two-thirds of
those rows are pre-club).
