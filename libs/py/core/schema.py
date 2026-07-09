"""Pydantic models shared across services — the cross-language contract.

These mirror the SQLite tables (plan §3.2). TS types for the Node API are
generated from their JSON Schema (Phase 7).
"""

from datetime import datetime

from pydantic import BaseModel


class MatchRow(BaseModel):
    """One completed (or terminal) match from the results source."""

    source_match_id: str
    date: str  # UTC calendar date of kickoff, YYYY-MM-DD
    kickoff_ts: datetime  # UTC
    competition: str  # e.g. "World Cup I / Group D"
    home_player: str  # nickname — the model entity
    away_player: str
    home_club: str  # team/country label; no marginal signal (parent §2.1.2)
    away_club: str
    status: int  # 0 scheduled / 3 finished / 4 cancelled (core.config)
    home_ft: int | None  # null unless status == 3
    away_ft: int | None
    raw_hash: str  # sha1 of source row, for change auditing
    scraped_at: datetime


class FixtureRow(BaseModel):
    """One betPawa event (upcoming or live)."""

    event_id: str
    start_time_utc: datetime
    competition: str
    home_raw: str  # e.g. "Germany (Tifosi)"
    away_raw: str
    home_player: str | None  # extracted; None if the pattern ever fails
    away_player: str | None
    coverage: str = "none"  # full|partial|none — model coverage via alias join


class PredictionRow(BaseModel):
    """One priced O/U line for one fixture at one moment (append-only)."""

    event_id: str
    predicted_at: datetime
    totals_source: str  # blend | poisson | book (book = no model coverage)
    line: float
    p_over: float
    p_push: float
    p_under: float
    lambda_home: float | None  # None when totals_source == 'book'
    lambda_away: float | None
    pick: str | None  # over/under; None if below pick gates or uncovered
    confidence: float | None  # 0.5*model + 0.5*book prob of the picked side
    tier: str | None  # lean | solid | strong
    value_flag: bool = False
    model_version: str
    as_of_cutoff_ts: datetime


class OddsPrice(BaseModel):
    """One priced selection at one moment (append-only odds_snapshots row)."""

    event_id: str
    market: str  # "1x2" | "ou"
    line: float | None  # O/U only
    selection: str  # home/draw/away | over/under
    odds: float
    implied_prob: float | None  # book's margin-free prob (PRICE.10.1)
