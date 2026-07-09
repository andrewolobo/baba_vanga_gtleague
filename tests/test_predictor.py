from datetime import datetime, timedelta, timezone

import pytest

from core.schema import FixtureRow, MatchRow, OddsPrice
from predictor.cycle import run_cycle
from store.repo import FixtureRepo, MatchRepo, OddsRepo
from tests.test_model import synthetic_df

NOW = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(db, tmp_path):
    df = synthetic_df(days=25, per_day=40)  # ends 2026-01-25, players P0..P11
    rows = [
        MatchRow(
            source_match_id=str(r.source_match_id), date=r.date,
            kickoff_ts=r.kickoff_ts, competition="SYN",
            home_player=r.home_player, away_player=r.away_player,
            home_club="X", away_club="Y", status=3,
            home_ft=int(r.home_ft), away_ft=int(r.away_ft),
            raw_hash=f"h{r.match_id}", scraped_at=NOW,
        )
        for r in df.itertuples()
    ]
    assert MatchRepo(db).upsert_many(rows).inserted == len(rows)

    fixtures = [
        FixtureRow(event_id="E1", start_time_utc=NOW + timedelta(minutes=30),
                   competition="GT Leagues", home_raw="A (P0)", away_raw="B (P1)",
                   home_player="P0", away_player="P1", coverage="full"),
        FixtureRow(event_id="E2", start_time_utc=NOW + timedelta(minutes=45),
                   competition="GT Leagues", home_raw="C (Ghost)", away_raw="D (P2)",
                   home_player="Ghost", away_player="P2", coverage="partial"),
    ]
    FixtureRepo(db).upsert_many(fixtures, seen_at=NOW)

    prices = []
    for ev in ("E1", "E2"):
        for line in (3.5, 4.5):
            prices += [
                OddsPrice(event_id=ev, market="ou", line=line, selection="over",
                          odds=1.8, implied_prob=0.51),
                OddsPrice(event_id=ev, market="ou", line=line, selection="under",
                          odds=1.9, implied_prob=0.49),
            ]
    OddsRepo(db).append_many(prices, fetched_at=NOW)
    return db, tmp_path / "test.db"


def test_cycle_appends_predictions(seeded):
    conn, db_path = seeded
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["fixtures"] == 2
    assert rep["rows"] == 4  # 2 events x 2 lines

    got = conn.execute(
        "SELECT event_id, totals_source, line, p_over, p_push, p_under, pick,"
        " lambda_home FROM predictions ORDER BY event_id, line"
    ).fetchall()
    covered = [r for r in got if r["event_id"] == "E1"]
    fallback = [r for r in got if r["event_id"] == "E2"]

    for r in covered:
        assert r["totals_source"] == "blend"
        assert r["lambda_home"] is not None
        assert abs(r["p_over"] + r["p_push"] + r["p_under"] - 1) < 1e-6
        assert r["p_push"] == 0.0  # half lines
    # synthetic league: every side ~Poisson(2), total ~4 => over 3.5 likelier
    assert covered[0]["p_over"] > 0.5

    for r in fallback:  # unknown player -> book probs, never a pick
        assert r["totals_source"] == "book"
        assert r["pick"] is None
        assert r["lambda_home"] is None


def test_cycle_is_append_only(seeded):
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    run_cycle(conn, db_path, now=NOW + timedelta(minutes=10))
    n = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
    assert n == 8  # two cycles x 4 rows, appended not overwritten


def _schedule_game(db, kickoff, home="P3", away="P4", source_id="SCHED1"):
    MatchRepo(db).upsert_many([MatchRow(
        source_match_id=source_id, date=kickoff.date().isoformat(),
        kickoff_ts=kickoff, competition="GT Leagues",
        home_player=home, away_player=away, home_club="H", away_club="A",
        status=0, home_ft=None, away_ft=None, raw_hash=f"sh-{source_id}",
        scraped_at=NOW,
    )])


def test_cycle_predicts_scheduled_games_without_odds(seeded):
    conn, db_path = seeded
    _schedule_game(conn, NOW + timedelta(hours=1))
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["scheduled"] == 1
    rows = conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'gtl:SCHED1' ORDER BY line"
    ).fetchall()
    assert len(rows) == 2  # two canonical half-lines straddling E[total]
    for r in rows:
        assert r["lambda_home"] is not None
        assert r["value_flag"] == 0  # no book to beat -> never value
        assert abs(r["p_over"] + r["p_push"] + r["p_under"] - 1) < 1e-6
    # lines straddle the blended expected total
    lam_tot = rows[0]["lambda_home"] + rows[0]["lambda_away"]
    assert rows[0]["line"] < lam_tot < rows[1]["line"] + 1


def test_scheduled_game_skipped_when_book_prices_it(seeded):
    conn, db_path = seeded
    # same players/kickoff as book fixture E1 -> must not double-predict
    kickoff = NOW + timedelta(minutes=30)
    _schedule_game(conn, kickoff, home="P0", away="P1", source_id="DUP1")
    run_cycle(conn, db_path, now=NOW)
    n = conn.execute(
        "SELECT COUNT(*) c FROM predictions WHERE event_id = 'gtl:DUP1'"
    ).fetchone()["c"]
    assert n == 0


def test_cycle_respects_horizon(seeded):
    conn, db_path = seeded
    rep = run_cycle(conn, db_path, now=NOW + timedelta(hours=2))  # kickoffs passed
    assert rep["fixtures"] == 0
    assert rep["rows"] == 0
