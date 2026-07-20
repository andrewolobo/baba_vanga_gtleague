"""Render one strong Over/Under pick into a Telegram-ready HTML message.

Pure and network-free: takes a candidate dict (from :mod:`apps.bot.alerts`) and
returns a single string formatted for ``parse_mode="HTML"``. This is the testable
core of the bot; all I/O lives in :mod:`apps.bot.main`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import MAX_MESSAGE_CHARS

_FOOTER = "<i>Entertainment only. Bet responsibly.</i>"
_TIER_BADGE = {
    "strong": "🔥 <b>STRONG</b>",
    "solid": "✅ <b>SOLID</b>",
    "lean": "📎 <b>LEAN</b>",
}


def _escape(text) -> str:
    """Escape the three characters Telegram's HTML parse mode is sensitive to."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pct(p) -> int:
    try:
        return round(float(p) * 100)
    except (TypeError, ValueError):
        return 0


def _odds(o) -> str:
    """Two-decimal odds string (e.g. 1.85); empty when missing/unparseable."""
    try:
        return f"{float(o):.2f}"
    except (TypeError, ValueError):
        return ""


def _line_str(line) -> str:
    try:
        return f"{float(line):g}"
    except (TypeError, ValueError):
        return str(line)


def _team(club, player) -> str:
    label = _escape(club or "?")
    return f"{label} ({_escape(player)})" if player else label


def _kickoff_str(kickoff: str, now: datetime) -> str:
    """'14:30 UTC (in 42m)' — absolute UTC time plus a relative countdown."""
    try:
        kt = datetime.fromisoformat(kickoff)
    except (TypeError, ValueError):
        return _escape(kickoff)
    if kt.tzinfo is None:
        kt = kt.replace(tzinfo=timezone.utc)
    hhmm = kt.astimezone(timezone.utc).strftime("%H:%M")
    mins = round((kt - now).total_seconds() / 60)
    when = f"in {mins}m" if mins >= 1 else "now"
    return f"{hhmm} UTC ({when})"


def render_alert(c: dict, *, now: datetime | None = None,
                 max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Build the single-message alert for one strong pick. `now` drives the
    relative kickoff countdown (defaults to the current UTC time)."""
    now = now or datetime.now(timezone.utc)
    tier = str(c.get("tier", ""))
    badge = _TIER_BADGE.get(tier, f"<b>{_escape(tier.upper())}</b>")
    competition = _escape(c.get("competition", "")).strip()

    lines = [
        f"{badge} Over/Under pick",
        f"⚽ <b>{_team(c.get('home_club'), c.get('home_player'))} vs "
        f"{_team(c.get('away_club'), c.get('away_player'))}</b>",
        f"🏆 {competition + ' · ' if competition else ''}"
        f"kickoff {_kickoff_str(c.get('kickoff', ''), now)}",
        f"📈 Pick: <b>{_escape(str(c.get('selection', '')).upper())} "
        f"{_line_str(c.get('line'))}</b> — confidence {_pct(c.get('confidence'))}%",
    ]
    odds = _odds(c.get("book_odds"))
    if odds:
        lines.append(f"💰 Book {odds}{' · value ✓' if c.get('value_flag') else ''}")
    lines.append(_FOOTER)

    # A single alert is far under the limit; the slice is a hard guarantee only.
    return "\n".join(lines)[:max_chars]
