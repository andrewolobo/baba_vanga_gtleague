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
    home_player: str  # nickname — the primary model entity
    away_player: str
    # Team/country label. The parent league found no marginal signal here; this
    # one does (+0.59 AUC pts over the served blend) — see docs/CLUB_FEATURE.md.
    home_club: str
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
    confidence: float | None  # model prob of the picked side (see cycle._line_row)
    tier: str | None  # lean | solid | strong
    value_flag: bool = False
    # Pick screen annotations (docs/PICK_SCREEN.md): advisory veto layer,
    # never mutates pick/tier. None = no pick to screen or screen off.
    screen_pass: int | None = None
    screen_reason: str | None = None
    model_version: str
    as_of_cutoff_ts: datetime


class X12PredictionRow(BaseModel):
    """One 1x2 (match-winner) prediction for one fixture at one moment
    (append-only). Priced off the same served side-λs as the totals rows —
    there is no separate model, so no totals_source and no book fallback:
    an uncovered fixture simply gets no x12 row."""

    event_id: str
    predicted_at: datetime
    p_home: float
    p_draw: float
    p_away: float
    lambda_home: float
    lambda_away: float
    pick: str | None  # home/draw/away; None below the x12 pick gate
    confidence: float | None  # max outcome prob (see X12_PICK_PROB_THRESHOLD)
    value_flag: bool = False
    # Pick screen annotations — same contract as PredictionRow's.
    screen_pass: int | None = None
    screen_reason: str | None = None
    model_version: str
    as_of_cutoff_ts: datetime


class TeamRatingRow(BaseModel):
    """One team's external FC25 ratings from sofifa.com/teams, for one edition.

    Reference data (docs/TEAM_RATINGS.md), keyed on (sofifa_id, edition). Every
    rating is Optional: sofifa occasionally renders a blank cell, and a missing
    number must not drop the whole row. Money fields are euros, already parsed
    from sofifa's '€19.9M' / '€308.7M' notation. sofifa_name is clean UTF-8;
    the join onto matches.*_club normalizes both sides (see store.team_aliases)."""

    sofifa_id: int
    edition: str  # roster version pin, e.g. '260045'
    sofifa_name: str
    nationality: str | None = None  # flag title; == country for national teams
    league: str | None = None
    league_id: int | None = None
    is_national: bool = False
    overall: int | None = None
    attack: int | None = None
    midfield: int | None = None
    defence: int | None = None
    domestic_prestige: int | None = None
    international_prestige: int | None = None
    num_players: int | None = None
    starting_age: float | None = None
    transfer_budget: float | None = None  # euros
    club_worth: float | None = None       # euros
    raw_hash: str  # sha1 of source cells, for idempotent upsert
    scraped_at: datetime


class OddsPrice(BaseModel):
    """One priced selection at one moment (append-only odds_snapshots row)."""

    event_id: str
    market: str  # "1x2" | "ou"
    line: float | None  # O/U only
    selection: str  # home/draw/away | over/under
    odds: float
    implied_prob: float | None  # book's margin-free prob (PRICE.10.1)
