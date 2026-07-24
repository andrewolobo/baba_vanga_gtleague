# Next steps (as of 2026-07-13)

Context: club GLM + tod live in prod; 1x2 head ENABLED 2026-07-12, accruing
dark (no API/UI). H2H stacker Phases 1+2 landed 2026-07-13, dark behind
`X12_H2H_ENABLED` (unset, defaults off). Population split Phases 0–3 live.
Recal closed-loop fix (`-recal2`) landed 2026-07-13 — see IMPORTANT below.
Full history: [docs/CLUB_FEATURE.md](docs/CLUB_FEATURE.md),
[docs/X12_SERVING.md](docs/X12_SERVING.md),
[docs/POPULATION_SPLIT.md](docs/POPULATION_SPLIT.md),
[docs/H2H_FEATURE.md](docs/H2H_FEATURE.md).

## IMPORTANT — recal2 follow-ups (closed-loop fix, landed 2026-07-13)

The recal maps had been fitting on served (post-map) `p_over` — consuming
their own output; the priced shared slope had gone negative and was
inverting picks on line 4.5. Fixed 2026-07-13: fit recomputes raw probs
from stored λs, map-touched rows tagged `-recal2`, unmapped priced lines
never pick while maps are engaged; `h2h.fit_stacker` had the identical
latent loop and was fixed BEFORE first enablement. Full write-up:
[docs/RECAL_SERVING.md](docs/RECAL_SERVING.md) §Update 2026-07-13. It is a
live switch — serving flipped on the first cycle after the code landed.

- [ ] **Commit the working tree** — the fix is serving but uncommitted;
      everything above rides the next timer cycle either way.
- [ ] **Expect (do NOT leak-hunt) a regen_agrees dip through ~2026-07-15**
      — rows served under the inverted 4.5 priced map settle and regen
      flips their side under the honest maps. Concentrated on line 4.5,
      same artifact class as the 19 pre-Phase-0 rows. Investigate only if
      it persists after the pre-fix serving window has fully settled.
- [ ] **Confirm the new fit generation live** — `-recal2` on new
      `model_version`s (cycle stdout `recal_lines`/`recal_lines_sched`
      unchanged); verified on a prod-copy dry run 2026-07-13 (132/135 rows
      tagged, priced a ≈ +0.49 flatten, schedule 1.09–1.51 — the honest
      raw-basis shapes; the priced/schedule slope "convergence" artifact
      should now disappear).
- [ ] **Judge the 2026-07-18 Phase 2 gate on `-recal2` rows ONLY** — plain
      `-recal` rows (07-11..07-13) were served under the contaminated fit
      and are not evidence about the fixed maps. If `-recal2` accrual is
      thin by 07-18, slide the gate to ~07-20; never pool generations
      (gate amendment: docs/POPULATION_SPLIT.md §Phase 2).
- [ ] **Watch the priced pick shape, not the count** — the guard suppresses
      unmapped lines (6.5 was the 47.6% leak); a handful of
      extreme-confidence priced picks (stated ≳ 0.80) may now surface that
      the inverted maps suppressed wholesale. Rare-to-zero remains the
      intended state; Phase 4 gates unchanged.
- [ ] **Rule for any future recal-style layer** — fit on inputs recomputed
      from stored λs, never on stored served probs, and add a
      poison-the-stored-probs test (see
      `test_fit_is_blind_to_stored_probabilities` and the stacker twin).

## Decisions to make

- [x] **Enable the 1x2 head** — DONE 2026-07-12 (`X12_ENABLED=true` in
      `.env`; accrues dark). Rollback: set false.
- [x] **Enable the H2H stacker** (docs/H2H_FEATURE.md §Enabling) — the next
      action, in order:
  1. Wait for both populations to clear `X12_H2H_MIN_N=500` decisive
     graded rows — check `x12_h2h_n={'priced': …, 'schedule': …}` in
     cycle stdout (2026-07-13 baseline: 184 / 228; ~2–3 days at current
     volume). Flipping early is safe but serves identity, untagged.
  2. Add `X12_H2H_ENABLED=true` to `.env` (needs `X12_ENABLED=true`,
     already set). Live switch: next timer cycle, no deploy.
  3. Confirm engagement: `-h2h` suffix on new `predictions_x12`
     model_versions, nonzero `x12_h2h_n` both populations.
     Rollback: set false — tagged rows regen through identity by design
     (test-covered), so a post-rollback `regen_agrees` dip on near-gate
     picks is expected drift, not leakage.
     Note 2026-07-13: `fit_stacker` was switched to the raw λ-basis fit
     (the recal closed-loop fix, see IMPORTANT above) before enablement —
     the x12 fit pool was never contaminated; no extra step needed here.

## Watch (blocked on data accruing, in likely unblock order)

- [ ] **x12_h2h_n → 500 per population** — the H2H-enable unblock trigger
      (cycle stdout; baseline 184/228 on 2026-07-13, ~2–3 days out).
- [ ] **H2H transition, after enabling** — `regen_agrees` in `settle run`
      output through the flip (version-aware regen is test-covered; this is
      the live canary). Pick rate rises BY DESIGN (~+40% in the dry run) —
      judge the x12 hit rate on stacked picks only, after a few hundred
      graded; never compare pre/post-h2h pick rates as a health signal.
      Any future x12 tier bands quantile on stacked confidences only.
- [ ] **λ slope → 1.0** — `python -m settlement.settle vs-book`. Was 1.364
      (CI [1.09, 1.62]) at club enable time; club widening λ should pull it
      toward 1.0. Cheapest live confirmation club is helping; needs only a few
      settled days.
- [ ] **First x12 day** (after enabling) — `x12_settled` / `regen_agrees` in
      `python -m settlement.settle run` output. Regen disagreement reads like
      a leak; config drift on the 0.50 gate is the known benign cause.
- [x] **Recal engagement** — ENGAGED on both populations as of 2026-07-12:
      priced maps a ≈ 0.014 (full flattening — expected on the
      book-conditional population, not a λ regression), schedule maps
      a ≈ 1.17–1.84 (sharpen; picks survive). Per-population since the
      split — `recal_lines` / `recal_lines_sched` in cycle stdout,
      per-row `-recal` in `model_version` (since 2026-07-13: `-recal2`,
      the raw-basis fit generation — see IMPORTANT above). Rollback
      `RECAL_ENABLED=false` (both) or `RECAL_MIN_N_LINE=0` (hierarchical
      tier only).
- [ ] **Conditional (book-population) scorecard after recal engages** —
      `python -m model.evaluate conditional`. Baseline 2026-07-11: within
      the population that gets a 5.5 line the model's AUC is ~0.60 (0.73
      unconditional) and the high-E[total] segment under-predicts overs by
      ~4 pts, which recal closes. If conditional AUC at 4.5/5.5 is still
      ~0.60 once maps are live, that residual is a λ-side (ranking) problem
      — candidate: re-sweep `alpha` scored at tail lines — not a map
      problem; don't reach for more calibration.
- [x] **Totals tier re-quantile** — DONE 2026-07-12 from the schedule
      (model-only) population: `TIER_STRONG` 0.68 → 0.69, `TIER_SOLID`
      unchanged. Proposal stable across 7d/14d windows; graded
      59.5/64.6/77.8 under the new bands. Tier rows spanning 2026-07-12 mix
      band regimes — split on that date in tier analytics.
- [ ] **Phase 2 verification gate (~2026-07-18)** — one week after
      per-population maps shipped: rerun the calibration-decile probe per
      population on recal-ON rows only — **`-recal2` rows ONLY** (gate
      amendment 2026-07-13, see IMPORTANT above; slide to ~07-20 if
      accrual is thin). PASS = schedule deciles within
      ±8 pts with picks surviving; priced picks rare-to-zero (INTENDED —
      do not "fix"). Judge the priced map via
      `python -m model.evaluate conditional`, never by the fitted `a`
      (live maps flatten by design). Details:
      docs/POPULATION_SPLIT.md §Phase 2 verification gate.
- [ ] **Phase 4 gate 1: re-measure picked-vs-suppressed on priced
      recal-ON rows** (~1 week of saturated maps) — the 45%-hit priced
      pick population should be gone by construction. If picks still
      surface and still lose, something beyond calibration is wrong: stop
      and diagnose before any meta-model.
- [ ] **Phase 4 gate 2: vs-book edge coefficient at ≥ ~1k settled priced
      rows** — `python -m settlement.settle vs-book`; edge coef > 0 with
      CI clear of zero is the ONLY condition under which picks that fight
      the book can carry signal. Until then no confidence model (including
      a meta-model) can rescue them.
- [ ] **Phase 4 gate 3: meta-labeling model** — only if gates 1–2 leave
      value unexplained. Walk-forward logistic first, GBM at ≥ ~5k rows;
      exploration suggests its dominant feature is already encoded as
      architecture by the population split (docs/POPULATION_SPLIT.md
      §Phase 4).
- [ ] **x12 hit rate** — judge vs the walk-forward 59% only after a few
      hundred graded picks in `settlements_x12`.
- [~] **Regime switch IN PROGRESS — club-pool rotation 2026-07-21** (the
      "World Cup ends" switch). `competition` label stays "GT Leagues"; the
      shift is in the roster: stable 45-club pool ≤07-20 → transition day
      07-21 (52 distinct, 31 new club-appearances) → new 42-club set from
      07-22. Since 07-21, ~11–13% of settled matches involve a club with no
      pre-shift history (cold/fallback λ). Expect ~a week of cold clubs; a
      scorecard dip is the transient, not a broken feature. Leak flags 0
      throughout — pipeline healthy.
- [ ] **Re-read O/U vs-book across the shift ~2026-07-28** — baseline read
      done 2026-07-24 on the full `-recal2-h2h` generation (07-18..07-24,
      1741 rows): headline HEALTHY — edge coef +0.536, CI [+0.12,+0.93],
      P(>0)=0.99; conditional pace arm +2.9 AUC pts @4.5 / +4.0 @5.5. BUT
      pre/post-21 split shows directional degradation NOT yet significant:
        · pre-21  (≤07-20, n=759) edge +0.68; picks (n=210) edge +1.03, 62.9%
        · post-21 (≥07-22, n=695) edge +0.35; picks (n=214) edge −0.53,
          bootstrap CI [−1.80,+0.65], P(>0)=0.19 — noise, ~2.5d/214 picks
      Damage concentrated at **line 4.5** post-shift (picks 50.6%, model
      Brier 0.260 > book 0.253); lines 2.5/3.5/5.5 still beat book. Signature
      = cold-club tail-line λ error, consistent with the fallback, NOT h2h
      failing. ACTION: do NOT re-sweep/re-band (cold-club transient by
      doctrine); let the 42-club pool warm ~a week, then rerun the
      `--tag recal2-h2h` + kickoff-split read around 07-28 when post-shift n
      ~doubles and the CI can resolve. If line 4.5 is STILL a coin flip on
      picks at n>300 post-shift with clubs warmed, that is a real λ/pace tail
      problem (candidate: the standing λ slope 1.24) — act then, not now.

## Build (when data or priorities allow)

- [x] **H2H Phase 1 (measurement harness)** — DONE 2026-07-13:
      `model/h2h.py` (H2HIndex + stacker math), `python -m model.evaluate h2h` (gate with skill/pace control arms, split-half, half-life x
      shrinkage sweep, `model_runs` kind `h2h`), `tests/test_h2h.py`.
      Gate PASSED on both markets — 1x2 +2.9 AUC pts (pairwise increment
      +2.3 over the skill control), totals pair-pace +1.1…+1.7 pts every
      line. Plan + numbers: [docs/H2H_FEATURE.md](docs/H2H_FEATURE.md).
- [x] **H2H Phase 2 (serving)** — CODE LANDED 2026-07-13, dark: stacker on
      the x12 head behind `X12_H2H_ENABLED` (default off), fit on settled
      `predictions_x12` per population (never pooled), `-h2h` suffix on
      touched rows, version-aware regen, `x12_h2h_n` in cycle stdout.
      Dry run: 84/84 rows tagged, mean |Δp_home| 0.078, p_draw preserved
      exactly, picks 17→24. Suite 148/148. docs/H2H_FEATURE.md §Phase 2.
- [ ] **H2H totals-side (pair pace) serving** — gate PASSED (+0.8…+1.3
      pts/line) but serving is a separate build: pace features into the
      per-line recal tier. Decide after the 1x2 stacker has live mileage.
- [ ] **1x2 vs-book scorecard** — analogue of `settle vs-book` over
      `settlements_x12`; the edge-coefficient regression is the test that
      matters. Needs a few thousand graded rows to resolve.
- [ ] **x12 tier bands** — derive from served x12 confidence the `settle tiers` way. Never from the eval frame (documented trap).
- [ ] **API/UI exposure of 1x2** — `services/api/src/routes.ts` only reads
      `predictions`; `predictions_x12` is invisible to the web app until an
      endpoint + UI section exist. Fine to accrue a dark track record first.
- [x] **Tune the club ridge block** — DONE 2026-07-11, **no change**: swept
      `c` 0.25–4 (`python -m model.evaluate sweep-club`); inverted-U peaking
      exactly at the served `c=1.0`, both extremes significantly worse. The
      shared `alpha=0.01` is right for clubs too (per-entity data comparable
      to players). Details: docs/CLUB_FEATURE.md §step 4.3.
- [ ] **Club leaderboard product surface** — `PoissonModel.club_ratings()`
      already returns per-club `pace` / `strength` from the joint fit; a table
      in the web app is mostly presentation work.

## Deferred to the next club-competition regime (World Cup ends)

- [ ] **Club rivalry / H2H walk-forward gate** — the club-era re-evaluation
      (docs/CLUB_FEATURE.md §Re-evaluation 2026-07-12) found a *real but small*
      club×club matchup effect: ~0.05 goals stable, 18–26% split-half reliable,
      **zero in the World Cup**. Not worth serving now. IF the league returns
      to club competitions AND totals/1x2 edge is being actively hunted, gate a
      shrunk club-pair interaction term against the live joint model. Prior on
      what survives: the 18–26% reliability. Slightly more promising for 1x2
      (strength) than totals (pace). Scratch scripts in the session archive.

## Measured and closed (no action — recorded so they aren't re-explored)

- [x] **Competition / league as a GLM feature** — measured 2026-07-12, **null**.
      Walk-forward, competition fixed effect over player+home+club: ΔAUC ~0.00,
      every CI spans zero, both eras. League-level scoring differences are fully
      absorbed by the player and club entities. Do not add.
- [x] **Club strength in isolation / club Elo** — measured 2026-07-12,
      **redundant**. Isolation strength r=0.95–0.99 with the live joint
      strength; walk-forward 1x2 gate ΔAUC −0.0009, Elo coef CI [−0.035,+0.040].
      The served joint model already contains it. Do not add.
