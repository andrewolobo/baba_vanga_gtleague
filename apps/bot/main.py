"""Strong-pick Telegram alert bot: broadcast newly-surfaced strong O/U picks.

Push-only. It never long-polls and runs no webhook on the normal path: it reads
the predictions/fixtures tables directly, sends one message per NEW strong
book-priced Over/Under pick to the configured channel, and records what it sent
so no later run repeats it. ``getUpdates`` exists solely for the one-off
``--print-updates`` setup helper that finds the channel/group id.

Normally the Node scheduler runs this as the ``alert`` job it chains after every
predict cycle (services/api/src/jobs.ts) — no command needed. Manually:

    # preview candidates without sending or writing state (no token needed):
    .venv/Scripts/python -m apps.bot run --dry-run

    # broadcast now (what the scheduler does for you):
    .venv/Scripts/python -m apps.bot run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import httpx

from core.config import settings
from store.db import connect

from . import alerts, config
from .format import render_alert
from .telegram import TelegramError, get_updates, send_message, url_buttons_markup


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="apps.bot",
        description="Broadcast newly-surfaced strong Over/Under picks to the "
                    "configured Telegram channel.",
    )
    p.add_argument("command", nargs="?", default="run", choices=["run"],
                   help="run: select new strong picks and send them (default).")
    p.add_argument("--dry-run", action="store_true",
                   help="Render candidate alerts to stdout. No token, no network, "
                        "no send, and no state written.")
    p.add_argument("--print-updates", action="store_true",
                   help="Setup helper: print the chats the bot can see (to find "
                        "the channel/group id).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap messages this run (default: ALERTS_MAX_PER_RUN).")
    p.add_argument("--db", default=None,
                   help="Path to the SQLite store (default: GTL_DB_PATH).")
    return p.parse_args(argv)


def _force_utf8() -> None:
    # Emojis/Unicode in the message crash the default cp1252 Windows console,
    # so force UTF-8 on both streams before anything is printed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _print_updates(token: str) -> int:
    """Discover chat ids: list chats from getUpdates so the user can pick the
    target. Channels often need a message forwarded to the bot (or the bot added
    to the channel's discussion group) before they surface here; the numeric
    ``-100…`` id or ``@channelusername`` also works directly."""
    try:
        updates = get_updates(token)
    except (TelegramError, httpx.HTTPError) as exc:
        print(f"Failed to fetch updates: {exc}", file=sys.stderr)
        return 1
    seen: dict = {}
    for upd in updates:
        src = (upd.get("message") or upd.get("channel_post")
               or upd.get("my_chat_member") or {})
        chat = src.get("chat") or {}
        if "id" in chat:
            seen[chat["id"]] = chat.get("title") or chat.get("username") \
                or chat.get("type", "")
    if not seen:
        print("No chats seen yet. Add the bot to the target channel/group, post "
              "any message there (or forward one to the bot), then retry.",
              file=sys.stderr)
        return 0
    print("Chats the bot can see (use the target's id as TELEGRAM_CHAT_ID):")
    for cid, title in seen.items():
        print(f"  {cid}\t{title}")
    return 0


def _dashboard_button() -> tuple[str, str] | None:
    """(label, url) for the optional dashboard button (ALERTS_WEB_URL), or None.

    Resolved once per run — the URL is global, unlike the per-event betPawa link
    — so a misconfigured value warns once instead of once per message.
    """
    website = config.website_url()
    if not website:
        return None
    if not website.startswith(("http://", "https://")):
        print(f"Warning: ALERTS_WEB_URL={website!r} is not an http(s) URL; "
              "skipping the link button.", file=sys.stderr)
        return None
    return (config.WEBSITE_BUTTON_LABEL, website)


def _buttons(c: dict, dashboard: tuple[str, str] | None) -> list[tuple[str, str]]:
    """Inline buttons for one alert: the betPawa event page first (the
    actionable link — straight to the wager), then the optional dashboard."""
    buttons = []
    if c.get("bet_url"):
        buttons.append((config.BETPAWA_BUTTON_LABEL, c["bet_url"]))
    if dashboard:
        buttons.append(dashboard)
    return buttons


def _dry_run(capped: list[dict], total: int, cap: int, now: datetime) -> int:
    dashboard = _dashboard_button()
    for c in capped:
        print(render_alert(c, now=now))
        for label, url in _buttons(c, dashboard):
            print(f"[{label}] -> {url}")
        print("—")
    print(f"[dry-run] {total} new strong pick(s); {len(capped)} within cap "
          f"{cap}. Nothing sent.", file=sys.stderr)
    return 0


def _broadcast(conn, capped: list[dict], token: str, chat_id: str,
               now: datetime) -> tuple[int, int]:
    """Send each candidate; record only confirmed sends so a failed pick stays a
    candidate and retries next cycle. Returns (sent, failed)."""
    dashboard = _dashboard_button()
    sent = failed = 0
    for c in capped:
        buttons = _buttons(c, dashboard)
        try:
            result = send_message(
                token, chat_id, render_alert(c, now=now),
                reply_markup=url_buttons_markup(buttons) if buttons else None)
        except (TelegramError, httpx.HTTPError) as exc:
            print(f"send failed for {c['event_id']} {c['selection']} "
                  f"{c['line']}: {exc}", file=sys.stderr)
            failed += 1
            continue
        alerts.record_sent(conn, c, result.get("message_id"), now=now)
        sent += 1
    return sent, failed


def main(argv=None) -> int:
    args = parse_args(argv)
    _force_utf8()
    s = settings()

    if args.print_updates:
        token = config.telegram_token()
        if not token:
            print("TELEGRAM_BOT_TOKEN is not set. Add it to .env (see "
                  ".env.example).", file=sys.stderr)
            return 2
        return _print_updates(token)

    # Disabled = fast no-op: the scheduler runs us every predict cycle, so the
    # common case must be cheap and touch nothing (not even the DB). --dry-run
    # still previews, so you can eyeball the message before flipping the flag.
    if not s.alerts_enabled and not args.dry_run:
        print("alerts disabled (ALERTS_ENABLED=false); nothing to do.",
              file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    conn = connect(args.db)
    cands = alerts.filter_unsent(conn, alerts.candidates(conn, s, now=now))
    cap = args.limit if args.limit is not None else s.alerts_max_per_run
    capped = cands[:cap] if cap >= 0 else cands

    if args.dry_run:
        return _dry_run(capped, len(cands), cap, now)

    token, chat_id = config.telegram_token(), config.telegram_chat_id()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set. Add them to "
              ".env (see .env.example), or use --dry-run to preview.",
              file=sys.stderr)
        return 2

    sent, failed = _broadcast(conn, capped, token, chat_id, now)
    if len(cands) > len(capped):
        print(f"note: {len(cands) - len(capped)} pick(s) over the cap deferred "
              "to the next run.", file=sys.stderr)
    print(f"strong alerts: sent {sent}, failed {failed}, candidates "
          f"{len(cands)} (chat {chat_id}).", file=sys.stderr)
    # Partial success stays exit 0 so a transient Telegram hiccup doesn't flip
    # /api/health; only a run that sent nothing while it had something to send
    # is a hard failure worth surfacing.
    return 1 if sent == 0 and failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
