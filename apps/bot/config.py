"""Configuration for the strong-pick Telegram alert bot.

Single source of truth is :func:`core.config.settings` — the same typed ``.env``
every other Python service reads (loaded relative to the repo root, which is the
cwd the scheduler spawns jobs with). No separate ``dotenv`` and no artifact path:
the bot reads the predictions tables directly, so all it needs from config is the
Telegram credentials, the alert knobs, and a couple of message constants.
"""
from __future__ import annotations

from core.config import settings

# --- Telegram Bot API -------------------------------------------------------
TELEGRAM_API_BASE = "https://api.telegram.org"
PARSE_MODE = "HTML"  # forgiving vs MarkdownV2; we only emit <b>/<i> and escape the rest

# --- Message constraints / formatting ---------------------------------------
MAX_MESSAGE_CHARS = 4096            # Telegram's hard limit for a single message
WEBSITE_BUTTON_LABEL = "Open the dashboard"


def telegram_token() -> str | None:
    """Bot token from settings/.env (None if unset)."""
    token = settings().telegram_bot_token
    return token.strip() or None


def telegram_chat_id() -> str | None:
    """The single target channel/group chat id (None if unset)."""
    chat_id = str(settings().telegram_chat_id)
    return chat_id.strip() or None


def website_url() -> str | None:
    """Dashboard URL for the post's link button (None if unset)."""
    url = settings().alerts_web_url
    return url.strip() or None
