# External FC25 team ratings (sofifa)

Status: **scraper BUILT 2026-07-22; model-feature gate RAN 2026-07-22 →
confirmed NO-GO.** The ratings are ingested, joined, and stored, but **nothing
in the model reads them and nothing should** — the go/no-go analysis ran and
sofifa carries no usable signal for this league (see "Gate result" below). This
is now **reference/display data only**.

An external, data-derived strength signal for the clubs GT Leagues players use.
Scraped from `https://sofifa.com/teams` (EA Sports FC 25 team ratings): Overall,
Attack, Midfield, Defence, domestic/international prestige, squad size, starting
XI average age, transfer budget, club worth. Reference data — refreshed by hand,
never on the predictor timer.

## Why it was worth testing — and the honest scope

The joint GLM already contains each club's **goal-derived** strength
(`catt−cdfn`), and docs/CLUB_FEATURE.md §Re-evaluation showed club strength *in
isolation* is redundant with it — a within-goals Elo adds nothing out of sample.
An **external** rating is different: it is not derived from GT Leagues goals, so
its one plausibly non-redundant use was the **cold-start prior** — a club with no
finished match gets `catt=cdfn=0` today, and an external Overall/Attack/Defence
might fill that gap for the ~week every club is cold after a regime switch. That
was the specific hypothesis. It was tested and it failed (below).

## Gate result — confirmed no-go (2026-07-22)

Two measurements, both scratch (`scratchpad/screen_sofifa_club.py`,
`scratchpad/pace_gate.py`), nothing wired:

- **Correlation pre-gate.** On 73 sofifa-rated clubs with ≥30 club-era side-rows
  (incl. mega-clubs with 6–8k rows, barely shrunk), the fitted joint `catt/cdfn`
  vs sofifa overall/attack/defence correlate at **R²≈0.01 on both axes.** The
  strength/1x2 axis is dead even *raw* — raw per-club goal-diff vs sofifa overall
  is R²=0.007 and slightly *negative* (Mexico 76 / Como 74 among the biggest raw
  winners; Fenerbahçe 80 / Fiorentina 77 among the worst). GT Leagues results are
  driven by the human player, not FIFA team quality. sofifa's only real
  correlation is raw pace/totals (R²=0.14), which collapses to ~0.016 once the
  player model is accounted for.
- **OOS pace gate.** Day-frozen walk-forward on the club era (n=19,232), a rank-1
  sofifa club offset fit jointly against goals, paired bootstrap. sofifa buys
  **+0.06–0.15 AUC pts** on totals (sig at only 2 of 4 lines, by n-power) vs the
  **+0.9–1.0 pts** the free club block buys on the same rows — i.e. sofifa
  recovers ~10% of the club block, and it's the redundant *pace* part it already
  owns (fitted coefs βₐ>0 **and** β_d>0 = pace, not strength). That is in the
  multi-span-form *rejected* band (+0.07). Club is already served, so sofifa on
  top ≈ 0 (collinear); its only "new" value is a cold club, +0.1 pt over a
  transient week — not worth a serving path.

**Verdict: do not wire sofifa into the model.** Re-open only if the schedule's
club↔result mechanism changes (results start tracking FIFA ratings). The test to
NOT re-run is "does an external rating beat the live joint model in-sample" — it
won't, for the same reason the isolation Elo didn't.

## The source, as measured (2026-07-22)

- **Paginated HTML table**, 60 rows/page, `?offset=0,60,120,…`. The first HTML
  source in the repo (results = JSON, betPawa = protobuf), parsed with stdlib
  `re` keyed on the stable `data-col` attributes — no HTML-parser dependency.
- **The `r=` edition param is INERT on the `/teams` list** (measured 2026-07-22:
  codes 240050/250044/260045 return the byte-identical team set + overalls, even
  in the promoted/relegated churn band). Every team-link in the HTML embeds
  edition **250044**, so sofifa's live `/teams` roster is **FC25** and FC26 is
  not retrievable here. The edition stored on each row is *parsed* from the HTML
  (=250044), not taken from `sofifa_edition` — that setting only ever rides the
  inert `r=`. (An earlier belief that the pin kept the crawl off an "FC26
  preview" was wrong; there is nothing to pin.)
- **Default order is ~team-id** and useless for a bounded crawl, so we request
  `col=oa&sort=desc` and stop at an Overall floor.
- **National teams are interleaved** with clubs (not a separate filter): they
  carry no league link, their "nationality" flag is the confederation
  (UEFA/CAF/…), and budget/worth are €0. `is_national` is derived structurally
  (`league is None`), so one crawl covers both.
- Money is `€19.9M` / `€308.7M` / `€1.2B` / `€0`; parsed to euros.
- **No mojibake.** Names are clean accented UTF-8 on both sides (`Fenerbahçe`,
  `Bodø/Glimt`, `München`). A `�` in any terminal output is the Windows console
  mis-rendering UTF-8, not corrupted data — do not "fix" it.

### The Cloudflare finding (the load-bearing one)

sofifa is behind Cloudflare and the block is at the **TLS layer**, not the
cookie. Measured with one valid `cf_clearance` cookie from the browser:

| client | same cookie + headers | result |
|---|---|---|
| httpx | ✓ | **403** |
| system curl.exe | ✓ | **403** |
| **curl_cffi** (impersonate Chrome) | ✓ | **200** |

`cf_clearance` is bound to Chrome's TLS/JA3 fingerprint — the handshake has to
look like Chrome, not just carry the cookie. So the fetch layer uses
**`curl_cffi`** (`impersonate="chrome"`). This keeps the manual cookie-paste
model (no headless browser); it just makes the handshake correct. The cookie is
still pasted from devtools into `data/auto-bet/cookie.txt` (gitignored — it holds
a live token), IP+UA-bound and hours-valid. A stale cookie 403s loudly and is
never retried (a challenge does not clear on repeat).

## Coverage reality

A floor-64 crawl captured **598 teams** (all edition 250044). After the alias
bridge, **79 of 106** canonical clubs carry a rating. The gap is entirely
national teams, in three tiers:

- **Present & captured** — the strong nations (France 85 … Argentina 83 … down
  to New Zealand 69) and, at floor 64, sub-floor ones like Qatar (67).
- **Absent from EA FC entirely, every edition** — Brazil, Belgium, Japan,
  Turkey, and most weaker nations (Iraq, Jordan, DR Congo, Cape Verde…). Verified
  2026-07-22 against codes 240050/250044/260045: none list them (their
  federations license to Konami/eFootball, not EA — Brazil's NT has never been in
  an EA FC title). **No floor and no edition recovers these**; the "schedule
  plays Brazil ⇒ it must be FC26" inference does not hold. sofifa can only ever
  cover the club slate + the licensed nations.

`store.team_aliases.clubs_without_rating` is the gap counter — a club (not a
nation) appearing there means a missing alias, exactly the silent-failure the
whole aliases layer fights.

## Schema (migration 007)

`team_ratings`, keyed `(sofifa_id, edition)`: sofifa_name, nationality, league
(+id), is_national, overall/attack/midfield/defence, domestic/international
prestige, num_players, starting_age, transfer_budget, club_worth (euros),
raw_hash (idempotent upsert), scraped_at.

`team_rating_aliases (sofifa_name → club)`: the sofifa→results name bridge,
mirroring `club_aliases` (the betPawa→results one). **Exact-string** join, not
fuzzy — every non-identity mapping is a reviewed row, because fuzzy silently
mislabels (`Inter Miami`→`Inter Milan`, `Wigan Athletic`→`Athletic Bilbao`).
Seeded from a verified audit: migration 008 (38 aliases) + 009 (2 the loose
audit missed — accent/case-only `Atlético Madrid`, `Olympique Lyonnais`). Both
seeds were **generated from the tables**, never hand-typed, so the accents are
byte-exact.

## Layout

```
services/py/ratings_ingest/
  fetch.py   # curl_cffi client (Chrome TLS), offset paging, loud 403 → SourceError
  parse.py   # pure HTML → TeamRatingRow (stdlib re), coverage_report
  cli.py     # `refresh`: crawl, archive, upsert, coverage + known-club audit
libs/py/store/team_aliases.py   # resolve_club, club_ratings, clubs_without_rating
libs/py/store/migrations/007_team_ratings.sql, 008_…_seed.sql, 009_…_fix.sql
```

## Config (libs/py/core/config.py)

```
sofifa_base           = "https://sofifa.com"
sofifa_cookie_file    = data/auto-bet/cookie.txt   # SOFIFA_COOKIE env wins if set
sofifa_impersonate    = "chrome"                   # curl_cffi TLS profile
sofifa_edition        = "250044"                   # FC25 pin (r=)
sofifa_min_overall    = 64                          # crawl floor
sofifa_request_delay_s = 1.5
sofifa_max_pages      = 120
```

## Refresh (run by hand)

1. In a browser logged past Cloudflare, copy the `/teams` request cookie from
   devtools into `data/auto-bet/cookie.txt` (raw `name=value; …`, a leading
   `Cookie:` is tolerated).
2. `python -m ratings_ingest.cli refresh` — crawls Overall-desc, pins FC25,
   stops at the floor, archives raw HTML under `data/raw/team_ratings/<date>/`,
   upserts idempotently, prints `coverage` + the known-club audit.
   - `--dry-run` crawls + reports without writing; `--min-overall` / `--max-pages`
     override the floor/cap.

## Hazards

- **Stale cookie**: `cf_clearance` lasts hours and is IP+UA-bound. A 403
  mid-crawl means re-paste — the job says so and stops; it never half-writes
  silently (archived pages up to the failure remain).
- **Impersonate drift**: `sofifa_impersonate` must stay a real Chrome build, and
  `USER_AGENT` must match the UA the cookie was issued under, or Cloudflare
  re-challenges.
- **Edition drift**: if sofifa flips its default title, only the explicit
  `r=250044` pin keeps the crawl on FC25 — do not remove it. Re-verify the
  edition in `coverage_report` after a sofifa site change.
- **Silent club gap**: a missing alias makes a present club unrated with no
  error. Watch `clubs_without_rating` after any refresh — a *club* (not a
  nation) there is the alert.

## Not in scope — and now closed

Wiring the rating into the GLM (as a cold-start prior on `catt`/`cdfn`). This was
the one open step; it was gated 2026-07-22 and **failed** (see "Gate result"
above). The table stays populated and bridged as reference/display data; there is
no serving path and none is planned. Do not resurrect the cold-start-prior idea
without a change in the schedule's club↔result mechanism.
