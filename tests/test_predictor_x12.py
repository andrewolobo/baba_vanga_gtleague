"""1x2 serving head (docs/X12_SERVING.md).

Reuses the rigged club league from test_predictor_club: FAST/FAST fixtures are
high-λ, SLOW/SLOW low-λ, players identical. For 1x2 what matters is asymmetry,
so E3 here is FAST v SLOW — a near-certain home win the head must pick — while
E1/E2 are symmetric matchups whose max prob sits under the gate (identical
players and clubs on both sides ⇒ p_home ≈ p_away ≈ 0.40).
"""

from datetime import timedelta

import pytest

from core.config import settings
from core.schema import FixtureRow, MatchRow, OddsPrice
from predictor.cycle import run_cycle
from settlement.settle import run as settle_run
from store.repo import FixtureRepo, MatchRepo, OddsRepo
from test_predictor_club import FAST, NOW, SLOW, club_on, club_seeded  # noqa: F401
from test_settlement import _play_result

LATER = NOW + timedelta(hours=2)


@pytest.fixture
def x12_on(monkeypatch):
    monkeypatch.setattr(settings(), "x12_enabled", True)


@pytest.fixture
def mismatch(club_seeded):
    """Add E3 = FAST (P2) v SLOW (P3) with 1x2 odds priced against the model."""
    conn, db_path = club_seeded
    FixtureRepo(conn).upsert_many([FixtureRow(
        event_id="E3", start_time_utc=NOW + timedelta(minutes=40),
        competition="GT", home_raw=f"{FAST} (P2)", away_raw=f"{SLOW} (P3)",
        home_player="P2", away_player="P3", coverage="full")], seen_at=NOW)
    OddsRepo(conn).append_many([
        OddsPrice(event_id="E3", market="1x2", line=None, selection="home",
                  odds=1.60, implied_prob=0.625),
        OddsPrice(event_id="E3", market="1x2", line=None, selection="draw",
                  odds=4.5, implied_prob=0.20),
        OddsPrice(event_id="E3", market="1x2", line=None, selection="away",
                  odds=5.0, implied_prob=0.175),
        # an O/U pair too, so the totals path also prices this event
        OddsPrice(event_id="E3", market="ou", line=3.5, selection="over",
                  odds=1.8, implied_prob=0.51),
        OddsPrice(event_id="E3", market="ou", line=3.5, selection="under",
                  odds=1.9, implied_prob=0.49)], fetched_at=NOW)
    return conn, db_path


def test_x12_off_by_default_and_totals_untouched(mismatch, club_on):
    conn, db_path = mismatch
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["x12_rows"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM predictions_x12").fetchone()["c"] == 0
    assert rep["rows"] > 0  # totals unaffected


def test_x12_rows_probs_and_gate(mismatch, club_on, x12_on):
    """E3 (FAST v SLOW) must clear the gate as a home pick with value. E1/E2
    are symmetric in expectation but the form leg adds real player asymmetry,
    so their pick is noise — what is invariant is the gate CONTRACT: a pick
    exists iff max prob >= threshold, and no 1x2 odds means no value flag."""
    conn, db_path = mismatch
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["x12_rows"] == 3
    rows = {r["event_id"]: r for r in conn.execute(
        "SELECT * FROM predictions_x12").fetchall()}
    thr = settings().x12_pick_prob_threshold
    for r in rows.values():
        assert r["p_home"] + r["p_draw"] + r["p_away"] == pytest.approx(1.0, abs=1e-5)
        assert "-club" in r["model_version"]
        top = max(r["p_home"], r["p_draw"], r["p_away"])
        if r["pick"] is None:
            assert top < thr
        else:
            assert r["confidence"] == pytest.approx(top, abs=1e-6)
            assert top >= thr

    e3 = rows["E3"]
    assert e3["pick"] == "home"
    # model's home prob on a ~9-goal-vs-league-mean mismatch dwarfs the book's
    # 0.625 -> the value flag must fire (edge >= MIN_EDGE)
    assert e3["value_flag"] == 1
    for ev in ("E1", "E2"):  # no 1x2 odds stored -> never value, pick or not
        assert rows[ev]["value_flag"] == 0


def test_x12_value_needs_a_book_price(mismatch, club_on, x12_on, monkeypatch):
    """A pick without stored 1x2 odds can never be value — there is no price
    to beat. E3's odds are deleted; the pick must survive, the flag must not."""
    conn, db_path = mismatch
    conn.execute("DELETE FROM odds_snapshots WHERE market = '1x2'")
    run_cycle(conn, db_path, now=NOW)
    e3 = conn.execute(
        "SELECT * FROM predictions_x12 WHERE event_id = 'E3'").fetchone()
    assert e3["pick"] == "home"
    assert e3["value_flag"] == 0


def test_x12_settlement_grades_and_regen_agrees(mismatch, club_on, x12_on):
    conn, db_path = mismatch
    run_cycle(conn, db_path, now=NOW)
    # E3 kicked at NOW+40min: FAST side wins 5-1 -> home; E1 draws 3-3
    _play_result(conn, NOW + timedelta(minutes=40), home="P2", away="P3",
                 hf=5, af=1, source_id="R3")
    _play_result(conn, NOW + timedelta(minutes=30), home="P0", away="P1",
                 hf=3, af=3, source_id="R1")

    rep = settle_run(conn, db_path, now=LATER)
    assert rep["x12_settled"] >= 2
    s3 = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'E3'").fetchone()
    assert (s3["result"], s3["pick_correct"]) == ("home", 1)
    assert s3["regen_pick"] == "home" and s3["regen_agrees"] == 1

    # E1's pick is form-noise-dependent; the grading contract is not:
    # no pick -> no grade; any pick -> graded against the draw result (0)
    s1 = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'E1'").fetchone()
    served1 = conn.execute(
        "SELECT pick FROM predictions_x12 WHERE event_id = 'E1'").fetchone()
    assert s1["result"] == "draw"
    if served1["pick"] is None:
        assert s1["pick_correct"] is None and s1["regen_agrees"] is None
    else:
        assert s1["pick_correct"] == 0  # nothing picked draw at these λs
        assert s1["regen_agrees"] == 1

    # idempotent: second run settles nothing new
    assert settle_run(conn, db_path, now=LATER)["x12_settled"] == 0


# ── schedule-only (gtl:) x12 settlement ──────────────────────────────────────
# Found live 2026-07-12: _run_x12 walks fixtures, so schedule-population x12
# rows never settled — the totals population-split bug recurring in this head.

def _x12_sched_game(db, kickoff, status=0, hf=None, af=None,
                    source_id="XS1"):
    """FAST (P4) v SLOW (P5): the same rigged mismatch as E3, so the head
    must pick home. status=3 + goals re-scrapes it finished (UPDATE path)."""
    MatchRepo(db).upsert_many([MatchRow(
        source_match_id=source_id, date=kickoff.date().isoformat(),
        kickoff_ts=kickoff, competition="GT Leagues",
        home_player="P4", away_player="P5", home_club=FAST, away_club=SLOW,
        status=status, home_ft=hf, away_ft=af,
        raw_hash=f"{'fin' if status == 3 else 'sch'}-{source_id}",
        scraped_at=kickoff + timedelta(minutes=14) if status == 3 else NOW,
    )])


def test_x12_schedule_population_settles(mismatch, club_on, x12_on):
    conn, db_path = mismatch
    kickoff = NOW + timedelta(minutes=50)
    _x12_sched_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    served = conn.execute(
        "SELECT * FROM predictions_x12 WHERE event_id = 'gtl:XS1'").fetchone()
    assert served is not None and served["pick"] == "home"
    assert served["value_flag"] == 0  # gtl rows never flag: no price

    _x12_sched_game(conn, kickoff, status=3, hf=6, af=0)
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["x12_sched_settled"] == 1
    s = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'gtl:XS1'").fetchone()
    assert (s["result"], s["pick_correct"]) == ("home", 1)
    assert s["matched_match_id"] is not None
    assert s["regen_pick"] == "home" and s["regen_agrees"] == 1

    # NOT EXISTS idempotency: second run settles nothing new
    assert settle_run(conn, db_path, now=LATER)["x12_sched_settled"] == 0


def test_x12_schedule_respects_settle_flag(mismatch, club_on, x12_on,
                                           monkeypatch):
    """The gtl x12 loop sits behind the same schedule_settle_enabled switch
    as the totals gtl loop — one flag governs the population."""
    conn, db_path = mismatch
    kickoff = NOW + timedelta(minutes=50)
    _x12_sched_game(conn, kickoff)
    run_cycle(conn, db_path, now=NOW)
    _x12_sched_game(conn, kickoff, status=3, hf=6, af=0)
    monkeypatch.setattr(settings(), "schedule_settle_enabled", False)
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["x12_sched_settled"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM settlements_x12"
        " WHERE event_id LIKE 'gtl:%'").fetchone()["c"] == 0


def test_x12_settlement_grades_losses(mismatch, club_on, x12_on):
    """An upset must grade 0, not disappear: pick home, result away."""
    conn, db_path = mismatch
    run_cycle(conn, db_path, now=NOW)
    _play_result(conn, NOW + timedelta(minutes=40), home="P2", away="P3",
                 hf=0, af=2, source_id="R3")
    settle_run(conn, db_path, now=LATER)
    s3 = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'E3'").fetchone()
    assert (s3["result"], s3["pick_correct"]) == ("away", 0)
    assert s3["regen_agrees"] == 1  # regen re-picks home too: honest disagreement
                                    # with reality, agreement with serving
