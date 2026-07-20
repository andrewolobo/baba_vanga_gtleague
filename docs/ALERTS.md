# Strong-pick Telegram alerts

A push-only Telegram bot that broadcasts newly-surfaced **strong** tier
Over/Under picks to one channel. Reworked from the foreign `apps/bot` insights
digest into a real-time alerter that reads the prediction tables directly.

## What fires an alert

The **headline** (highest-confidence) pick per fixture where, in the latest
served batch:

- `tier = 'strong'` (confidence ≥ `TIER_STRONG`, currently 0.69), and
- `pick ∈ (over, under)`, and
- the fixture is **book-priced** (`event_id NOT LIKE 'gtl:%'`), and
- kickoff is still in the future.

One alert per match, not one per line — the same headline-row convention the API
and settlement grading use. Schedule-only (`gtl:`) model picks are out of scope:
they have no bettable line.

## How it runs — no extra command

The Node API's embedded scheduler ([services/api/src/jobs.ts](../services/api/src/jobs.ts))
is the orchestrator. `predict`'s `onSuccess` chain now ends with the `alert`
job, so `python -m apps.bot run` fires right after every prediction batch lands
(i.e. on each odds/results refresh). `npm run dev` / `npm run start` is all that's
needed. `GTL_NO_JOBS=1` disables the whole scheduler, alerts included.

When `ALERTS_ENABLED=false` (the default) the job is a fast no-op that exits 0
without opening the DB, so leaving it chained costs nothing until enabled.

## De-dup — "only re-alert when the pick changes"

Sent picks are recorded in the `alerts_sent` table
([migration 006](../libs/py/store/migrations/006_alerts.sql)), keyed by
`(event_id, line, selection)`. Because `predict` runs every few minutes, this
key is load-bearing: a re-price that keeps the same strong line + side collides
and stays quiet; a flipped side or a newly-strong line is a fresh key and fires.
The bot writes only this table — operational state, never a model table (the
same doctrine as the wagers surface).

## First enable & the per-run cap

When the flag is first switched on, every currently-qualifying upcoming strong
pick is sent (no priming). `ALERTS_MAX_PER_RUN` (default 20) bounds one run;
anything over the cap is left unrecorded and picked up on the next cycle, so a
busy slate drains over a few cycles instead of blasting at once.

## Config

All in the repo-root `.env` via `core.config` — see
[.env.example](../.env.example) and [apps/bot/README.md](../apps/bot/README.md).

| Key                  | Default  | Meaning                                            |
| -------------------- | -------- | -------------------------------------------------- |
| `ALERTS_ENABLED`     | `false`  | One-flag rollback; live switch (next cycle).       |
| `TELEGRAM_BOT_TOKEN` | —        | From @BotFather.                                   |
| `TELEGRAM_CHAT_ID`   | —        | Channel/group id (`-100…` or `@name`); bot = admin.|
| `ALERTS_WEB_URL`     | —        | Optional dashboard link button.                    |
| `ALERTS_TIER`        | `strong` | Served tier that triggers an alert.                |
| `ALERTS_MAX_PER_RUN` | `20`     | Safety cap on messages per run.                    |

## Exit codes / health

`0` on success, disabled, nothing-to-send, **or a partial send** — a transient
Telegram hiccup must not flip `/api/health`. `1` only when the run had picks to
send and *every* send failed. `2` on missing token/chat id. `jobStatus.alert`
surfaces the last line and failure count like every other job.

## Message shape

```
🔥 STRONG Over/Under pick
⚽ Barcelona (Ace) vs Real Madrid (Viper)
🏆 GT League 12 mins · kickoff 14:30 UTC (in 42m)
📈 Pick: OVER 3.5 — confidence 71%
💰 Book 1.85 · value ✓
Entertainment only. Bet responsibly.
```

The book-odds line is enrichment from the latest snapshot for the picked side;
it's omitted if the side isn't priced. `value ✓` shows when the row's
`value_flag` is set.

### Buttons

Each alert carries inline URL buttons, actionable link first:

1. **🎟 Bet on betPawa** — deep link straight to the event's betting page,
   `{BETPAWA_BASE}/event/{event_id}?filter=all`. `fixtures.event_id` **is** the
   betPawa event id for priced rows (the odds feed writes it verbatim), so no
   extra lookup or column is needed. Always present, since alerts are
   priced-only; schedule-only `gtl:` ids resolve to no URL by design.
2. **Open the dashboard** — only when `ALERTS_WEB_URL` is set.

## Rollback

`ALERTS_ENABLED=false` — the job no-ops on the next cycle. The `alerts_sent`
table persists, so re-enabling doesn't re-blast picks that already went out.
