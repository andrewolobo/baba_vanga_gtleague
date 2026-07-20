# apps/bot — strong-pick Telegram alert bot

A **push-only** Telegram bot that broadcasts newly-surfaced **strong** tier
Over/Under picks to **one** channel (or group). It reads the `predictions` /
`fixtures` tables directly, sends one message per new strong **book-priced** pick,
and records what it sent so it never repeats.

It runs by itself: the Node API's embedded scheduler chains it as the `alert`
job after every predict cycle ([services/api/src/jobs.ts](../../services/api/src/jobs.ts)),
so `npm run dev` / `npm run start` is all you need — no extra command, no cron.

## How it works

- After each `predictor.cycle`, the scheduler runs `python -m apps.bot run`.
- The bot selects the **headline** (highest-confidence) `strong` O/U pick per
  still-upcoming, book-priced fixture — see [alerts.py](alerts.py).
- It drops anything already in the `alerts_sent` table (keyed by
  `event_id, line, selection`), sends the rest, and records them.
- **De-dup rule:** a re-price that keeps the same strong line + side stays quiet;
  a flipped side or a newly-strong line is a fresh key and fires again.
- Scope is **priced-only**: schedule-only `gtl:` predictions have no bettable
  line and are never alerted.
- Every alert carries a **🎟 Bet on betPawa** button deep-linking to that event's
  betting page (`{BETPAWA_BASE}/event/{event_id}?filter=all` — `fixtures.event_id`
  *is* the betPawa event id), plus an optional dashboard button.

It never long-polls and runs no webhook — the only Bot API call on the normal
path is `sendMessage`. `getUpdates` is used solely by the `--print-updates`
setup helper.

## Why it can't receive DMs or post elsewhere

With no update-listening loop there is nothing that can read a direct message or
react in another chat: "one channel, no DMs" is enforced by design, not by
filtering. It also does **no** modelling — it only reads the picks Python already
computed and persisted.

## Configuration

Everything lives in the **repo-root `.env`** (read by `core.config`, the same
config every Python service uses). See [.env.example](../../.env.example).

```ini
ALERTS_ENABLED=true                    # default false — the one-flag rollback
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...   # from @BotFather
TELEGRAM_CHAT_ID=-1001234567890        # channel/group id (-100… or @channelname)
ALERTS_WEB_URL=https://your-app/       # optional: adds an "Open the dashboard" button
ALERTS_TIER=strong                     # tier that triggers an alert
ALERTS_MAX_PER_RUN=20                  # safety cap on messages per run
```

`ALERTS_ENABLED` is a **live switch**: the job re-reads it on every spawn, so
flipping it takes effect on the next predict cycle with no restart. While it's
`false` the job is a fast no-op (exit 0) that doesn't even open the DB.

## Setup (one time)

1. **Create the bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the token.
2. **Add the bot to your channel as an ADMIN** (channels require admin rights to
   post). For a group, just add it and post any message.
3. **Find the chat id:**
   ```bash
   .venv/Scripts/python -m apps.bot --print-updates
   ```
   Copy the target's id (channels/supergroups start with `-100`). You can also
   use `@channelusername` directly.
4. Put the values above in the repo-root `.env`, then flip `ALERTS_ENABLED=true`.

## Usage

Run from the **repo root** using the project venv:

```bash
# Preview the pending alerts — no token, no network, nothing sent, no state written:
.venv/Scripts/python -m apps.bot run --dry-run

# Broadcast now (normally the scheduler does this for you):
.venv/Scripts/python -m apps.bot run

# Setup helper: list chats the bot can see, to find the id:
.venv/Scripts/python -m apps.bot --print-updates
```

### Options

| Flag              | Purpose                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| `--dry-run`       | Render pending alerts to stdout; send nothing, write no state (no token). |
| `--limit N`       | Cap messages this run (default: `ALERTS_MAX_PER_RUN`).                     |
| `--print-updates` | List chats the bot can see, to find the channel/group id.                 |
| `--db PATH`       | Use a different SQLite store (default: `GTL_DB_PATH`).                     |

### Exit codes

`0` success (or disabled / nothing to send / partial send) · `1` hard failure
(had picks to send but every send failed) · `2` missing configuration
(token / chat id).

## Layout

```
apps/bot/
  config.py     Telegram credentials + message constants (from core.config)
  telegram.py   push-only sendMessage (+ getUpdates for --print-updates setup)
  alerts.py     pure DB: select headline strong picks, de-dupe, record sent,
                build the betPawa event deep link
  format.py     pure, network-free render of one pick -> an HTML message
  main.py       CLI: select -> dry-run print | send + record
  __main__.py   enables `python -m apps.bot`
```

The `alerts_sent` de-dup table is created by
[migration 006](../../libs/py/store/migrations/006_alerts.sql).
