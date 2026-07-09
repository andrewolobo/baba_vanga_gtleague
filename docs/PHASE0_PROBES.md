# Phase 0 probe results — GT Leagues (probed 2026-07-08)

Source: `https://api.gtleagues.com/api/fixtures` — a **clean JSON API**, not an HTML page.
Captured browser request in [scraper/scrape.curl](../scraper/scrape.curl).

## 0.1 Results source probe — PASSED (better than parent on every axis)

| Question | Finding |
|---|---|
| Plain HTTP? | **Yes.** Bare request → HTTP 451; adding only `origin: https://www.gtleagues.com`, `referer`, and a Chrome `user-agent` → HTTP 200. No auth, no cookies, no Cloudflare challenge, **no Playwright needed**. |
| History depth | **≥ 1 year** reachable (spot-checked 2025-07-08, 2026-01-08, 2026-04-08, 2026-06-08 — all return full pages). Parent had a 24-day carousel; thin-history risk is gone. |
| Volume | **340 fixtures/day** (2026-07-07 full day, paged at limit=50): 298 status 3 (finished, scores present), 42 status 4 (cancelled/void, `stats` scores null). ~39 distinct players, ~24 matches each per day. |
| Double-listing? | **None found.** 340 rows → 340 unique ids AND 340 unique `(kickoff, home_player, away_player, score)` keys. Keep the DB UNIQUE constraint anyway. |
| Clock/timezone | Kickoffs are proper **UTC ISO-8601** (`...Z`). No timezone guessing needed. |
| Paging | `limit`/`offset` + `sort=-kickoff,-matchNr`; `status=in:3,5,4,6` filter; supports **ETag/`if-none-match`** → cheap conditional polling. |

### Response shape (per fixture)
`id`, `kickoff` (UTC), `status`, `week`, `matchNr`, `channel`, `createdAt`, `updatedAt`,
`result.stats.{home_score, away_score}`,
`participants[].side` + `participant.player.nickname` (**the model entity** — `player.name` is blank, nickname is the stable key) + `participant.team.name`,
`season.name` (e.g. "World Cup I (07-07-2026)"), `season.format` ([1,1]), `tournament.name` (group), `category.name` ("GT Leagues"), `sport.name` ("FC25").

Status codes observed: **3 = finished** (all have scores), **4 = cancelled/void** (null scores, sometimes `updatedAt` before kickoff — explains negative lags). 5/6 not yet observed; presumed live/other terminal states. Upcoming statuses (likely 1/2) to be confirmed when building the fixtures view.

## 0.2 Intra-day publish probe — PASSED (blend precondition strongly met)

Results appear **~14 min after kickoff (median; max 33 min among finished)** — measured as
`updatedAt − kickoff` on finished fixtures. Verified live: at 18:05 UTC the 17:45 UTC
kickoff was already settled. Parent saw 65–85 min lag; GT Leagues is ~5× fresher.
→ `TOTALS_SOURCE=blend` is viable pending the Phase-4 batch-mode gate. A 10–15 min results
merge cadence captures essentially everything.

## Modeling red flag for Phase 4 (dispersion)

One-day sample (298 matches): **mean total 3.89, var 4.88, var/mean ≈ 1.25 → OVER-dispersed
marginally** — opposite sign to the parent (0.80). Caveats: single day; marginal (pooled)
var/mean overstates dispersion vs the per-side *conditional* measure the parent used, because
player heterogeneity inflates it. Per plan §4.3, run the proper Cameron–Trivedi on residuals
before choosing the family — but NB/Dixon-Coles are back on the candidate list here.

Total-goals distribution (1 day): 0:3%, 1:9%, 2:17%, 3:22%, 4:14%, 5:14%, 6:9%, 7:5%, 8+:7%.
Mean ~3.9 → expect betPawa O/U lines around **3.5/4.5** (parent was 6.5).

## 0.3 betPawa feed probe — PASSED (capture in [scraper/betpawa.curl](../scraper/betpawa.curl))

| Question | Finding |
|---|---|
| Category / competition | Same **category `101`** (eFootball) as the parent, scoped by **`zones.competitions: ["17491"]`** (= "GT Leagues" competition). Query shape otherwise identical to the parent's. |
| Market type IDs | **Unchanged**: `3743` (1X2 FT), `5000` (O/U FT) — a live fetch requesting both returned both, priced. |
| O/U lines | **Per-match lines** — one slate showed 2.5, 3.5, 5.5, 6.5 (book centers the line per player pair). The full-distribution model prices these for free; pick logic must be line-aware from day one. Only ONE line listed per event (so far). |
| Auth | **No cookies needed.** The captured request carried no `x-pawa-token` / `__cf_bm`, and a live replay with only the headers (brand, fingerprint, UA, referer, accept) returned HTTP 200. The parent's 30-min cookie-churn pain may not apply — keep the loud-alert-on-401/403 design anyway. |
| Decoder | Parent wire-format field map (§5.2 of [BETPAWA_FEED.md](BETPAWA_FEED.md)) **parses unchanged** — event id/name/participants/start-time/markets/prices/implied probs all land where documented. O/U labels arrive as `Over {formattedHandicap}` templates + `PRICE.6` line string. |
| Fixture | Raw response saved: [scraper/fixtures/betpawa_gtleagues_2026-07-08.bin](../scraper/fixtures/betpawa_gtleagues_2026-07-08.bin) (6 events, both markets). Results-API sample: [scraper/fixtures/gtleagues_results_2026-07-07_page0.json](../scraper/fixtures/gtleagues_results_2026-07-07_page0.json). |

Slate size note: UPCOMING+hasOdds returned only ~6 events (~1 h ahead) — the book prices a
short forward window, so odds cadence matters more than depth. Event names are
`Country (Player)` — same `(...)` extraction as the parent.

## 0.4 Join geometry — PASSED (the parent's nightmare does not exist here)

- **`FEED_OFFSET_MIN = 0`.** Every feed event matched a GT Leagues API fixture with the
  **identical UTC kickoff** (delta = 0 min for all 6 events probed). No delayed rebroadcast,
  no timezone skew.
- Same-pair replays produce decoy candidates at ±150 / ±315 min — confirming the plan's
  "never nearest-join" rule; join on `(kickoff exact, home_player, away_player)`.
- **No pre-close result leak:** upcoming (status 0) fixtures carry *pre-created result rows
  with null scores*; real scores appear only when status flips to 3, ~13–15 min **after**
  kickoff (matches run ~12–13 min wall-clock). A result can never publish before its own
  betting closes. As-of cutoff = fixture kickoff, `OFFSET_TOL_MIN` can be tiny (±5 min).
- Ingestion rule: a row counts as a result only if `status == 3` **and** both scores non-null
  (never key on the presence of `result`).

## 0.5 Name-join sample — PASSED (near-trivial)

Feed player names inside parentheses ("Hulk", "Professor", "Viper", "Sensei", "Snail",
"Razvan", "Tifosi", "Fred", "Arthur"…) **exactly match** GT Leagues API `nickname` values —
6/6 probed events joined with zero aliasing. Keep the `player_aliases` table as a safety
net, but expect near-100% coverage. (API `player.id` exists too — nickname is the join key
to the feed; store both.)

## Status-code map (GT Leagues API, observed)

`0` = scheduled (null scores) · `3` = finished (scores present) · `4` = cancelled/void
(null scores) · `5`/`6` = accepted by the status filter, not yet observed (presumed
live/other terminal — capture when seen).

**Gate status:** (a) results scrapeable with deep history — ✅; (b) feed carries priced O/U
for GT Leagues — ✅. **Phase 0 complete; all gates pass. Cleared for Phase 1/2.**

## Measured seed constants (updates to plan §6)

```
FEED_OFFSET_MIN=0        OFFSET_TOL_MIN=5      AS_OF_CUTOFF = fixture kickoff
SCRAPE_LAG_MIN≈14        (results publish ~13–15 min after kickoff = at final whistle)
BETPAWA_CATEGORY=101     BETPAWA_COMPETITION=17491   MARKETS=3743,5000
BETPAWA auth: headers only (brand/fingerprint/UA); no cookies observed — keep env-var + alert design
O/U lines: per-match (2.5–6.5 seen) → line-aware pick logic required
```
