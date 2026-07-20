"""Select and de-dupe the strong Over/Under picks to broadcast.

Pure DB logic, no network. Reads the latest served predictions, keeps the
headline strong priced O/U pick per upcoming fixture, drops any already sent,
and records the ones that go out. The one write path here is the bot-owned
``alerts_sent`` table — operational state, never a model table; the prediction
and settlement loops neither read nor write it.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from core.config import Settings

# Same rule the API/wagers use to split "Club (Player)" — strip the trailing
# parenthesised player to recover the club label.
_STRIP_PLAYER = re.compile(r"\s*\([^)]*\)\s*$")


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _club(raw: str | None) -> str:
    raw = raw or ""
    return _STRIP_PLAYER.sub("", raw).strip() or raw


def _latest_side_odds(conn: sqlite3.Connection, event_id: str, line: float,
                      selection: str) -> float | None:
    """Latest snapshotted book odds for the picked side — enriches the message
    (never gates the pick). None when the book hasn't priced that exact side."""
    row = conn.execute(
        "SELECT odds FROM odds_snapshots WHERE event_id = ? AND market = 'ou'"
        " AND line = ? AND selection = ? ORDER BY fetched_at DESC LIMIT 1",
        (event_id, line, selection),
    ).fetchone()
    return row["odds"] if row else None


def _shape(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    return {
        "event_id": r["event_id"], "line": r["line"], "selection": r["selection"],
        "confidence": r["confidence"], "tier": r["tier"],
        "value_flag": bool(r["value_flag"]), "kickoff": r["kickoff"],
        "competition": r["competition"],
        "home_club": _club(r["home_raw"]), "home_player": r["home_player"],
        "away_club": _club(r["away_raw"]), "away_player": r["away_player"],
        "book_odds": _latest_side_odds(conn, r["event_id"], r["line"],
                                       r["selection"]),
    }


def candidates(conn: sqlite3.Connection, s: Settings,
               now: datetime | None = None) -> list[dict]:
    """The headline strong priced O/U pick per still-upcoming fixture.

    Scope is book-priced only: schedule-only ``gtl:`` predictions have no
    fixtures row and no bettable line, so the JOIN excludes them (and the
    NOT LIKE clause makes that intent explicit). Within an event we keep the
    single highest-confidence strong line — one alert per match, not one per
    line — mirroring the codebase's headline-row convention.
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT p.event_id, p.line, p.pick AS selection, p.confidence, p.tier,
               p.value_flag, f.start_time_utc AS kickoff, f.competition,
               f.home_raw, f.away_raw, f.home_player, f.away_player
        FROM predictions p
        JOIN fixtures f ON f.event_id = p.event_id
        WHERE p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                                WHERE event_id = p.event_id)
          AND p.tier = ?
          AND p.pick IN ('over', 'under')
          AND p.event_id NOT LIKE 'gtl:%'
          AND f.start_time_utc > ?
        ORDER BY p.event_id, p.confidence DESC, p.line
        """,
        (s.alerts_tier, _now_iso(now)),
    ).fetchall()

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        out.append(_shape(conn, r))
    return out


def already_sent(conn: sqlite3.Connection, event_id: str, line: float,
                 selection: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM alerts_sent WHERE event_id = ? AND line = ? AND selection = ?",
        (event_id, line, selection),
    ).fetchone() is not None


def filter_unsent(conn: sqlite3.Connection, cands: list[dict]) -> list[dict]:
    """Drop candidates whose exact (event, line, side) was already broadcast."""
    return [c for c in cands
            if not already_sent(conn, c["event_id"], c["line"], c["selection"])]


def record_sent(conn: sqlite3.Connection, c: dict, message_id: int | None,
                now: datetime | None = None) -> None:
    """Mark a candidate as sent so no later cycle repeats it. INSERT OR REPLACE
    so a re-fire of the same key (shouldn't happen — filter_unsent guards it)
    refreshes rather than raising on the primary key."""
    now = now or datetime.now(timezone.utc)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO alerts_sent"
            " (event_id, line, selection, tier, confidence, message_id, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c["event_id"], c["line"], c["selection"], c["tier"],
             c["confidence"], message_id, _now_iso(now)),
        )
