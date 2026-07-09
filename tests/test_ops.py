"""Phase 8 hardening: artifact staleness guard + backup/retention."""

from datetime import datetime, timedelta, timezone

from core.schema import MatchRow
from model import registry
from store.backup import run as backup_run
from store.db import connect
from store.repo import MatchRepo
from tests.test_model import synthetic_df

NOW = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
DAY = "2026-01-26"


def _seed(conn, df):
    rows = [MatchRow(
        source_match_id=str(r.source_match_id), date=r.date,
        kickoff_ts=r.kickoff_ts, competition="SYN",
        home_player=r.home_player, away_player=r.away_player,
        home_club="X", away_club="Y", status=3,
        home_ft=int(r.home_ft), away_ft=int(r.away_ft),
        raw_hash=f"h{r.match_id}", scraped_at=NOW,
    ) for r in df.itertuples()]
    MatchRepo(conn).upsert_many(rows)


def test_artifact_refits_when_backfill_lands(db, tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db, synthetic_df(days=20))
    m1 = registry.get_poisson(db, db_path, day=DAY)
    assert "NEWBIE" not in m1.players
    assert len(list((tmp_path / "artifacts").glob("*.pkl"))) == 1

    # cached: same fingerprint -> same object contents, no refit needed
    m2 = registry.get_poisson(db, db_path, day=DAY)
    assert m2.players == m1.players

    # a catch-up backfill lands past-dated rows -> fingerprint changes -> refit
    late = synthetic_df(days=3, per_day=20, players=["NEWBIE", "P1", "P2", "P3"],
                        seed=11)
    late["source_match_id"] = "late" + late["source_match_id"].astype(str)
    _seed(db, late)
    m3 = registry.get_poisson(db, db_path, day=DAY)
    assert "NEWBIE" in m3.players


def test_backup_creates_and_prunes(tmp_path):
    db_path = tmp_path / "gt.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO player_aliases (book_name, player) VALUES ('a','b')")
    conn.commit()
    conn.close()

    # stale sibling files that retention must remove
    art = tmp_path / "artifacts"
    art.mkdir()
    old_pkl = art / "poisson_old.pkl"
    old_pkl.write_bytes(b"x")
    import os
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(old_pkl, (old, old))

    rep = backup_run(db_path)
    assert (tmp_path / "backups").is_dir()
    assert rep["backup_bytes"] > 0
    assert rep["artifacts_pruned"] == 1
    assert not old_pkl.exists()

    # restored backup opens and contains the data
    import sqlite3
    back = sqlite3.connect(rep["backup"])
    assert back.execute("SELECT COUNT(*) FROM player_aliases").fetchone()[0] == 1
