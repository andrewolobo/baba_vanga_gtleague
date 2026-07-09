# betPawa Odds Scraper (`oddsfeed/`) — Endpoints & Payload Reference

A technical write-up of the betPawa sportsbook client: what it calls, what it sends,
what comes back on the wire, and the clean JSON it produces. Written to be portable —
everything here transfers to the new-league spin-off (see `SPINOFF_PLAN.md` §2.3 and
Phase 3) with only the category/market IDs changing.

## 1. Overview

betPawa has **no public API and no JSON endpoint** for its slate. The scraper replays the
same XHR the betpawa.ug web app makes, which returns **protobuf** (`application/x-protobuf`)
with **no published `.proto` schema**. The pipeline is four small, independently testable
stages:

```
fetch.py            HTTP GET (browser-identity headers + session cookies) → raw bytes
protobuf_decode.py  schema-free wire-format walker → field tree
normalize.py        reverse-engineered field map → clean fixture dicts
store.py            → data/upcoming.json (+ optional raw .bin archive)
```

The whole request identity was captured once from Chrome devtools into
`data/better.curl.template`; `oddsfeed/config.py` mirrors it, with the expiring secrets
lifted into environment variables.

## 2. Endpoints

| Purpose | Method | URL |
|---|---|---|
| Slate listing (the one actually used) | GET | `https://www.betpawa.ug/api/sportsbook/v4/events/lists/by-queries?q=<url-encoded JSON>` |
| Single-event detail (defined in config, unused so far) | GET | `https://www.betpawa.ug/api/sportsbook/v4/events/{event_id}` |

Despite the `content-type: application/x-protobuf` **request** header, the request body is
empty — the entire query rides in the `q` URL parameter; only the **response** is protobuf.

## 3. Request payload — the `q` query JSON

Built by `config.build_query()` and URL-encoded into the query string:

```json
{
  "queries": [
    {
      "query": {
        "eventType": "UPCOMING",
        "categories": ["101"],
        "zones": {},
        "hasOdds": true
      },
      "view": { "marketTypes": ["3743", "5000"] },
      "skip": 0,
      "take": 50
    }
  ]
}
```

| Field | Meaning | Current value |
|---|---|---|
| `eventType` | Slate segment | `"UPCOMING"` (pre-match only) |
| `categories` | Sport/category ID | `"101"` = **eFootball**. *League-specific — recapture for a new league.* |
| `hasOdds` | Drop fixtures with no priced market | `true` |
| `view.marketTypes` | Which markets to embed per event | `"3743"` = **1X2 – Full Time**, `"5000"` = **Total Score Over/Under – FT** (both in one request) |
| `skip` / `take` | Paging | `take=50` default; `fetch_all` pages `skip = page*take` until a near-empty body (`len < 8` bytes) signals the tail, max 20 pages |

## 4. Request identity — headers & cookies

The endpoint sits behind Cloudflare and rejects anonymous clients, so every request
carries a full browser identity (from `config.headers()` / `config.cookies()`):

**Headers**

| Header | Value / role |
|---|---|
| `accept`, `content-type` | `application/x-protobuf` |
| `user-agent` | Real Chrome 149 UA string |
| `referer` | `https://www.betpawa.ug/events?categoryId=101&marketId=1X2` |
| `devicetype` | `web` |
| `x-pawa-brand` | `betpawa-uganda` |
| `x-pawa-language` | `en` |
| `x-device-fingerprint` | **Secret** — env `BETPAWA_FINGERPRINT` |

**Cookies**

| Cookie | Role | Lifetime |
|---|---|---|
| `x-pawa-token` | Session token — env `BETPAWA_TOKEN` | Expires with the session |
| `__cf_bm` | Cloudflare bot-management cookie — env `BETPAWA_CF_BM` | **~30 min TTL** (the usual cause of failures) |
| `bp_country` | `UG` | Static |

**Auth lifecycle.** All three secrets have captured fallbacks in `config.py` but expire.
On `401`/`403`, `fetch.py` raises `FeedError` with the fix: re-capture the request from
devtools into `better.curl.template` and re-export the three env vars — **no code change**.
Two other guarded failure modes: `304 Not Modified` (the captured curl carried an
`if-modified-since` header — the client deliberately never sends it) and a non-protobuf
`content-type` (usually a Cloudflare challenge HTML page).

## 5. Response payload — the protobuf wire format

### 5.1 Decoding without a schema (`protobuf_decode.py`, ~100 lines)

The decoder walks raw protobuf: each field key is a varint `(field_no << 3) | wire_type`,
then the value is read per wire type:

| Wire | Encoding | Used here for |
|---|---|---|
| 0 | varint | ints (timestamps, side flags) |
| 1 | 64-bit fixed | **little-endian IEEE-754 doubles** — odds and implied probabilities |
| 2 | length-delimited | UTF-8 strings *or* nested messages — **ambiguous**, so the decoder returns raw bytes and the caller chooses (`as_str` / `as_msg` / `as_f64`) per known field, never guessing (prevents a string like `"Under"` being mis-parsed as a sub-message) |
| 5 | 32-bit fixed | (not needed) |

### 5.2 Reverse-engineered field map (`normalize.py`)

```
top.1                          response wrapper
  .2 (repeated)                EVENT
     .1  str                   event id
     .2  str                   name  "Club (Player) - Club (Player)"
     .5  (repeated) msg        participant { .1 id, .2 name, .3 side (1=home, 2=away) }
     .6  msg { .1 varint }     start time (unix seconds, UTC)
     .7  (repeated) msg        MARKET
         .1 msg markettype     { .1 id ("3743"/"5000"), .2 short, .3 name }
         .2 msg market         { .1 id, .4 (repeated) PRICE }
            PRICE .1  id
                  .3  type id
                  .4  fixed64  decimal odds (LE double)
                  .6  str      line, O/U only (e.g. "6.5")
                  .8  str      label: "1"/"X"/"2", or "Over …"/"Under …"
                  .10 msg      { .1 fixed64  margin-free implied probability (LE double) }
     .10 msg category          { .1 id, .2 name }
     .12 msg competition       { .1 id, .2 name }
```

Notes that took real effort to establish:

- **`PRICE.10.1` is the book's own margin-free implied probability** — the 1X2 triple sums
  to ~1.0 exactly, so no de-vig step is needed downstream.
- O/U selections arrive as independent Over/Under price rows sharing a `PRICE.6` line
  string; `_parse_ou` regroups them into one row per line keyed on the label prefix
  ("Over…"/"Under…").
- Participant `side` (field `.5.3`) is the home/away authority — never infer from name
  order.
- Event names are `Club (Player)`; the **player** inside the parentheses is the modelable
  entity, extracted into `home_player`/`away_player` (regex `\(([^)]*)\)`). The join to
  training data goes through `data/player_aliases.json`.
- Pages are deduped by `event_id` (paging can overlap) and sorted by
  `(start_time, event_id)`.

## 6. Output payload — `data/upcoming.json`

`store.write_upcoming` wraps the normalized fixtures with fetch metadata. Real example
(2026-07-08 slate):

```json
{
  "metadata": {
    "fetched_at": "2026-07-08T17:36:39.642116+00:00",
    "source_url": "https://www.betpawa.ug/api/sportsbook/v4/events/lists/by-queries",
    "category": "101",
    "market_types": ["3743", "5000"],
    "event_count": 21
  },
  "fixtures": [
    {
      "event_id": "36413957",
      "name": "Atletico Madrid (Adriano) - Real Sociedad (Noah)",
      "start_time": "2026-07-08T17:45:00+00:00",
      "competition": "eAdriatic League",
      "home": "Atletico Madrid (Adriano)",
      "away": "Real Sociedad (Noah)",
      "home_player": "Adriano",
      "away_player": "Noah",
      "markets": {
        "1x2": [
          { "label": "1", "outcome": "home", "odds": 2.21, "implied_prob": 0.402673 },
          { "label": "X", "outcome": "draw", "odds": 4.9,  "implied_prob": 0.158296 },
          { "label": "2", "outcome": "away", "odds": 2.04, "implied_prob": 0.439034 }
        ],
        "ou": [
          { "line": 6.5, "over_odds": 1.83, "under_odds": 1.8,
            "over_implied": 0.495219, "under_implied": 0.504781 }
        ]
      }
    }
  ]
}
```

Field conventions: odds rounded to 3 dp, implied probabilities to 6 dp; `start_time` is
ISO-8601 UTC; a market the book didn't price is an empty list. **Timing caveat:** the
feed's `start_time` is the *advertised broadcast* start — the physical match plays ~60 min
earlier, and the results-site clock adds another +60 (UTC+3), so the feed↔results join
offset is **+120 min** (`config.FEED_OFFSET_MIN`; see `model/vsbook.py`).

## 7. Usage

```bash
python -m oddsfeed.cli fetch                             # live slate -> data/upcoming.json
python -m oddsfeed.cli fetch --raw data/events.sample.bin   # also archive the raw protobuf
python -m oddsfeed.cli fetch --from-file data/events.sample.bin  # fully offline decode
```

Exit code 3 = `FeedError` (expired auth). The saved `.bin` doubles as the offline test
fixture for `tests/test_oddsfeed.py`, so decoder/normalizer changes are testable with no
network and no live token.

## 8. Porting checklist for a new league

1. Open the new league's betPawa page with devtools → copy the `by-queries` request as
   curl → new `better.curl.template`.
2. Read the new **category ID** (and market type IDs if they differ) out of the captured
   `q` — the only expected config change; note which **O/U lines** the book lists.
3. Save one raw response as the new `.bin` test fixture; confirm `protobuf_decode.walk`
   parses it and the §5.2 field map still holds (same API version ⇒ expected yes).
4. Check the event-name convention — if it isn't `Club (Player)`, adjust `_player()` and
   rebuild the alias table.
5. Export fresh `BETPAWA_TOKEN` / `BETPAWA_CF_BM` / `BETPAWA_FINGERPRINT`.
