from datetime import datetime, timedelta, timezone

from core.schema import MatchRow
from predictor.cycle import run_cycle
from settlement.settle import run as settle_run, scorecard
from store.repo import MatchRepo
from tests.test_predictor import NOW, seeded  # noqa: F401  (fixture reuse)

LATER = NOW + timedelta(hours=2)


def _play_result(db, kickoff, home="P0", away="P1", hf=3, af=2,
                 scraped_at=None, source_id="RES1"):
    MatchRepo(db).upsert_many([MatchRow(
        source_match_id=source_id, date=kickoff.date().isoformat(),
        kickoff_ts=kickoff, competition="GT Leagues",
        home_player=home, away_player=away, home_club="A", away_club="B",
        status=3, home_ft=hf, away_ft=af, raw_hash=f"rh-{source_id}",
        scraped_at=scraped_at or kickoff + timedelta(minutes=14),
    )])


def test_settles_and_grades(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    kickoff = NOW + timedelta(minutes=30)  # E1
    _play_result(conn, kickoff, hf=3, af=2)  # total 5 -> over 3.5 and 4.5 hit

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 1  # E1; E2 has no matching result -> pending
    assert rep["pending"] == 1

    s = conn.execute("SELECT * FROM settlements").fetchone()
    assert s["event_id"] == "E1"
    assert s["result_total"] == 5
    assert s["offset_min_used"] == 0.0
    assert s["leak_risk"] == 0  # result scraped after prediction
    assert s["regen_pick"] in ("over", "under")
    served = conn.execute(
        "SELECT pick FROM predictions WHERE event_id='E1'"
        " ORDER BY (pick IS NULL), confidence DESC LIMIT 1").fetchone()
    if served["pick"] is not None:
        assert s["pick_correct"] == (1 if served["pick"] == "over" else 0)

    # idempotent: second run settles nothing new
    rep2 = settle_run(conn, db_path, now=LATER)
    assert rep2["settled"] == 0


def test_leak_risk_flagged_when_result_precedes_prediction(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(minutes=30)
    # result lands in the DB BEFORE the prediction cycle runs (the hazard)
    _play_result(conn, kickoff, scraped_at=NOW - timedelta(minutes=5))
    run_cycle(conn, db_path, now=NOW)

    settle_run(conn, db_path, now=LATER)
    s = conn.execute("SELECT leak_risk FROM settlements WHERE event_id='E1'").fetchone()
    assert s["leak_risk"] == 1


def test_integer_line_push_is_ungraded(seeded):  # noqa: F811
    conn, db_path = seeded
    kickoff = NOW + timedelta(minutes=30)
    conn.execute(
        "INSERT INTO predictions (event_id, predicted_at, totals_source, line,"
        " p_over, p_push, p_under, pick, confidence, tier, value_flag,"
        " model_version, as_of_cutoff_ts)"
        " VALUES ('E1', ?, 'blend', 4.0, 0.5, 0.1, 0.4, 'over', 0.6, 'solid',"
        " 0, 'test', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    _play_result(conn, kickoff, hf=2, af=2)  # total 4 == line -> push
    settle_run(conn, db_path, now=LATER)
    s = conn.execute("SELECT pick_correct FROM settlements WHERE event_id='E1'").fetchone()
    assert s["pick_correct"] is None


def test_no_ambiguous_join(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    kickoff = NOW + timedelta(minutes=30)
    # two candidate results for the same pair inside the window -> pending
    _play_result(conn, kickoff, source_id="RES1")
    _play_result(conn, kickoff + timedelta(minutes=4), source_id="RES2")
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] == 0
    assert rep["pending"] == 2


def test_scorecard_renders(seeded):  # noqa: F811
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=30))
    settle_run(conn, db_path, now=LATER)
    out = scorecard(conn, days=365)  # synthetic clock is months behind wall time
    assert "settled events: 1" in out
