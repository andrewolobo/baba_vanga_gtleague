"""Minimal Telegram Bot API client — push-only.

Uses httpx (the project's HTTP client — ``requests`` is not a dependency here).
The bot deliberately never long-polls and runs no webhook on the normal path: it
only calls ``sendMessage`` to one configured chat. ``get_updates`` exists solely
for the one-off ``--print-updates`` setup helper that discovers the chat id; it
is never called when posting.
"""
from __future__ import annotations

import httpx

from .config import PARSE_MODE, TELEGRAM_API_BASE


class TelegramError(RuntimeError):
    """Raised when the Telegram API responds with ``ok: false`` or junk."""


def _api_url(token: str, method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


def url_button_markup(label: str, url: str) -> dict:
    """An inline keyboard holding a single tappable URL button."""
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    """Post one message to ``chat_id``; return the Telegram ``result`` object.

    Pass ``reply_markup`` (e.g. from :func:`url_button_markup`) to attach an
    inline keyboard. Raises :class:`TelegramError` carrying the API's description
    on failure (e.g. the bot is not an admin of the channel, or the chat id is
    wrong).
    """
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": PARSE_MODE,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    resp = httpx.post(_api_url(token, "sendMessage"), json=body, timeout=timeout)
    return _result(resp)


def get_updates(token: str, *, timeout: float = 15.0) -> list[dict]:
    """Fetch recent updates (setup-only — used to discover a chat id)."""
    resp = httpx.get(_api_url(token, "getUpdates"), timeout=timeout)
    return _result(resp)


def _result(resp: "httpx.Response"):
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise TelegramError(f"Non-JSON response from Telegram (HTTP {resp.status_code}).")
    if not payload.get("ok"):
        desc = payload.get("description", "unknown error")
        raise TelegramError(f"Telegram API error (HTTP {resp.status_code}): {desc}")
    return payload["result"]
