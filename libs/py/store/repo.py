"""Typed repositories — the only code that writes SQL against the tables."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from core.schema import (FixtureRow, MatchRow, OddsPrice, PredictionRow,
                         X12PredictionRow)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class IngestReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dup_ignored: int = 0  # rows rejected by the dedup UNIQUE constraint

    def __str__(self) -> str:
        return (
            f"inserted={self.inserted} updated={self.updated} "
            f"unchanged={self.unchanged} dup_ignored={self.dup_ignored}"
        )


class MatchRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_many(self, rows: list[MatchRow]) -> IngestReport:
        """Idempotent merge keyed on source_match_id; the (date, kickoff,
        players, score) UNIQUE constraint absorbs any source double-listing."""
        rep = IngestReport()
        with self.conn:
            for r in rows:
                cur = self.conn.execute(
                    "SELECT id, raw_hash FROM matches WHERE source_match_id = ?",
                    (r.source_match_id,),
                ).fetchone()
                if cur is None:
                    try:
                        self.conn.execute(
                            "INSERT INTO matches (source_match_id, date, kickoff_ts,"
                            " competition, home_player, away_player, home_club,"
                            " away_club, status, home_ft, away_ft, raw_hash, scraped_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r.source_match_id, r.date, _iso(r.kickoff_ts),
                             r.competition, r.home_player, r.away_player, r.home_club,
                             r.away_club, r.status, r.home_ft, r.away_ft, r.raw_hash,
                             _iso(r.scraped_at)),
                        )
                        rep.inserted += 1
                    except sqlite3.IntegrityError:
                        rep.dup_ignored += 1
                elif cur["raw_hash"] != r.raw_hash:
                    self.conn.execute(
                        "UPDATE matches SET status=?, home_ft=?, away_ft=?,"
                        " raw_hash=?, scraped_at=? WHERE id=?",
                        (r.status, r.home_ft, r.away_ft, r.raw_hash,
                         _iso(r.scraped_at), cur["id"]),
                    )
                    rep.updated += 1
                else:
                    rep.unchanged += 1
        return rep

    def count(self, status: int | None = None) -> int:
        q = "SELECT COUNT(*) c FROM matches"
        args: tuple = ()
        if status is not None:
            q += " WHERE status = ?"
            args = (status,)
        return self.conn.execute(q, args).fetchone()["c"]

    def day_counts(self) -> list[tuple[str, int]]:
        return [
            (r["date"], r["c"])
            for r in self.conn.execute(
                "SELECT date, COUNT(*) c FROM matches GROUP BY date ORDER BY date"
            )
        ]


class FixtureRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert_many(self, rows: list[FixtureRow], seen_at: datetime) -> None:
        ts = _iso(seen_at)
        with self.conn:
            for f in rows:
                self.conn.execute(
                    "INSERT INTO fixtures (event_id, start_time_utc, competition,"
                    " home_raw, away_raw, home_player, away_player, coverage,"
                    " first_seen_at, last_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(event_id) DO UPDATE SET"
                    " start_time_utc=excluded.start_time_utc,"
                    " home_player=excluded.home_player,"
                    " away_player=excluded.away_player,"
                    " coverage=excluded.coverage,"
                    " last_seen_at=excluded.last_seen_at",
                    (f.event_id, _iso(f.start_time_utc), f.competition, f.home_raw,
                     f.away_raw, f.home_player, f.away_player, f.coverage, ts, ts),
                )


class PredictionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append_many(self, rows: list[PredictionRow]) -> int:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO predictions (event_id, predicted_at, totals_source,"
                " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
                " confidence, tier, value_flag, screen_pass, screen_reason,"
                " model_version, as_of_cutoff_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r.event_id, _iso(r.predicted_at), r.totals_source, r.line,
                  r.p_over, r.p_push, r.p_under, r.lambda_home, r.lambda_away,
                  r.pick, r.confidence, r.tier, int(r.value_flag),
                  r.screen_pass, r.screen_reason,
                  r.model_version, _iso(r.as_of_cutoff_ts)) for r in rows],
            )
        return len(rows)


class X12PredictionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append_many(self, rows: list[X12PredictionRow]) -> int:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO predictions_x12 (event_id, predicted_at, p_home,"
                " p_draw, p_away, lambda_home, lambda_away, pick, confidence,"
                " value_flag, screen_pass, screen_reason, model_version,"
                " as_of_cutoff_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r.event_id, _iso(r.predicted_at), r.p_home, r.p_draw,
                  r.p_away, r.lambda_home, r.lambda_away, r.pick, r.confidence,
                  int(r.value_flag), r.screen_pass, r.screen_reason,
                  r.model_version, _iso(r.as_of_cutoff_ts))
                 for r in rows],
            )
        return len(rows)


class OddsRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append_many(self, prices: list[OddsPrice], fetched_at: datetime) -> int:
        ts = _iso(fetched_at)
        with self.conn:
            self.conn.executemany(
                "INSERT INTO odds_snapshots (event_id, fetched_at, market, line,"
                " selection, odds, implied_prob) VALUES (?,?,?,?,?,?,?)",
                [(p.event_id, ts, p.market, p.line, p.selection, p.odds,
                  p.implied_prob) for p in prices],
            )
        return len(prices)
