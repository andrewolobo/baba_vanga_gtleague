"""Model data loading with the parent's hygiene invariants baked in:

- only finished, scored matches enter the frame
- defensive dedup on (date, kickoff, players, score) — the DB constraint
  should have absorbed double-listing already, but re-check cheaply
- canonical sort (date, kickoff_ts, match_id) so "strictly earlier" is
  well-defined everywhere
- time filtering happens through as_of()/day_frozen() cutoffs only; no
  rolling feature may ever see its own match (test-covered)
"""

import sqlite3

import pandas as pd

from core.config import STATUS_FINISHED

DEDUP_KEY = ["date", "kickoff_ts", "home_player", "away_player", "home_ft", "away_ft"]


def load_matches(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT id AS match_id, source_match_id, date, kickoff_ts, competition,"
        " home_player, away_player, home_ft, away_ft"
        " FROM matches WHERE status = ? AND home_ft IS NOT NULL AND away_ft IS NOT NULL",
        conn,
        params=(STATUS_FINISHED,),
    )
    df["kickoff_ts"] = pd.to_datetime(df["kickoff_ts"], utc=True, format="ISO8601")
    df = df.drop_duplicates(subset=DEDUP_KEY, keep="first")
    df = df.sort_values(["date", "kickoff_ts", "match_id"]).reset_index(drop=True)
    df["total"] = df["home_ft"] + df["away_ft"]
    return df


def long_format(df: pd.DataFrame) -> pd.DataFrame:
    """Two rows per match: one per side. The entity is the player."""
    home = df.assign(
        player=df["home_player"], opponent=df["away_player"], is_home=1,
        goals_for=df["home_ft"], goals_against=df["away_ft"],
    )
    away = df.assign(
        player=df["away_player"], opponent=df["home_player"], is_home=0,
        goals_for=df["away_ft"], goals_against=df["home_ft"],
    )
    cols = ["match_id", "date", "kickoff_ts", "player", "opponent", "is_home",
            "goals_for", "goals_against"]
    out = pd.concat([home[cols], away[cols]], ignore_index=True)
    return out.sort_values(["kickoff_ts", "match_id", "is_home"],
                           ascending=[True, True, False]).reset_index(drop=True)


def day_frozen(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """Training slice for a day-frozen fit: strictly earlier calendar days."""
    return df[df["date"] < day]


def as_of(df: pd.DataFrame, cutoff_ts: pd.Timestamp, publish_lag_min: float) -> pd.DataFrame:
    """Results *visible* at cutoff: published (kickoff + lag) at or before it.

    publish_lag_min is the measured kickoff→scores-visible delay (~14 min,
    PHASE0_PROBES §0.2) plus any refresh margin the caller wants to add.
    """
    published = df["kickoff_ts"] + pd.Timedelta(minutes=publish_lag_min)
    return df[published <= cutoff_ts]
