"""H2H stacker on the 1x2 serving head (docs/H2H_FEATURE.md Phase 2).

Rigged so a silent no-op cannot pass: P6 has beaten P7 3-0 in every prior
meeting, but the E4/E5 fixtures are SLOW/SLOW — a low-λ near-coin-flip whose
unstacked max prob sits far under the 0.50 gate (p_draw is huge at λ ≈ 0.7 a
side). Only the stacker can produce a pick on these fixtures, so a pick IS
the proof the feature reached serving, and its absence under the flag is the
rollback proof. The stacker is fit on 12 seeded settled 1x2 rows whose
outcomes follow the H2H edge perfectly — the coefficients saturate and the
restacked split goes decisively toward the H2H favourite.
"""

from datetime import timedelta

import pytest

from core.config import settings
from core.schema import FixtureRow, MatchRow, OddsPrice, X12PredictionRow
from model import data, h2h
from predictor.cycle import run_cycle
from settlement.settle import run as settle_run
from store.repo import FixtureRepo, MatchRepo, OddsRepo, X12PredictionRepo
from test_predictor_club import FAST, NOW, SLOW, club_on, club_seeded  # noqa: F401
from test_predictor_x12 import x12_on  # noqa: F401
from test_settlement import _play_result

LATER = NOW + timedelta(hours=2)
DOM, SUB = "P6", "P7"  # DOM won every prior meeting


@pytest.fixture
def h2h_on(monkeypatch):
    monkeypatch.setattr(settings(), "x12_h2h_enabled", True)
    monkeypatch.setattr(settings(), "x12_h2h_min_n", 10)


@pytest.fixture
def h2h_seeded(club_seeded):
    """Dominance history + settled 1x2 rows for the fit + the E4/E5 slate."""
    conn, db_path = club_seeded

    # 24 meetings, DOM wins 3-0 from either side (no home-coefficient alibi)
    rows = []
    for d in range(24):
        day = NOW - timedelta(days=25) + timedelta(days=d)
        home_is_dom = d % 2 == 0
        rows.append(MatchRow(
            source_match_id=f"D{d}", date=day.date().isoformat(),
            kickoff_ts=day.replace(hour=23, minute=45), competition="SYN",
            home_player=DOM if home_is_dom else SUB,
            away_player=SUB if home_is_dom else DOM,
            home_club=FAST, away_club=FAST, status=3,
            home_ft=3 if home_is_dom else 0, away_ft=0 if home_is_dom else 3,
            raw_hash=f"dom{d}", scraped_at=NOW,
        ))
    MatchRepo(conn).upsert_many(rows)

    # 12 settled priced 1x2 rows: the outcome follows the H2H edge exactly,
    # the served split is a constant coin flip -> the fit's ONLY signal is h2h
    fixtures, preds = [], []
    for i in range(12):
        start = NOW - timedelta(hours=26) + timedelta(minutes=10 * i)
        h, a = (DOM, SUB) if i % 2 == 0 else (SUB, DOM)
        fixtures.append(FixtureRow(
            event_id=f"H{i}", start_time_utc=start, competition="GT",
            home_raw=f"{FAST} ({h})", away_raw=f"{FAST} ({a})",
            home_player=h, away_player=a, coverage="full"))
        preds.append(X12PredictionRow(
            event_id=f"H{i}", predicted_at=start - timedelta(minutes=10),
            p_home=0.4, p_draw=0.2, p_away=0.4, lambda_home=2.0,
            lambda_away=2.0, pick=None, confidence=None, value_flag=False,
            model_version="blend-test", as_of_cutoff_ts=start,
        ))
    FixtureRepo(conn).upsert_many(fixtures, seen_at=NOW)
    X12PredictionRepo(conn).append_many(preds)
    with conn:
        for i in range(12):
            conn.execute(
                "INSERT INTO settlements_x12 (event_id, matched_match_id,"
                " result, pick_correct, regen_pick, regen_agrees, settled_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"H{i}", None, "home" if i % 2 == 0 else "away",
                 None, None, None, (NOW - timedelta(hours=1)).isoformat()))

    # E4/E5: SLOW/SLOW rematches, opposite sides. Unstacked max prob is far
    # below the 0.50 gate; stacked, the split saturates toward DOM.
    slate = [
        FixtureRow(event_id="E4", start_time_utc=NOW + timedelta(minutes=40),
                   competition="GT", home_raw=f"{SLOW} ({DOM})",
                   away_raw=f"{SLOW} ({SUB})", home_player=DOM,
                   away_player=SUB, coverage="full"),
        FixtureRow(event_id="E5", start_time_utc=NOW + timedelta(minutes=55),
                   competition="GT", home_raw=f"{SLOW} ({SUB})",
                   away_raw=f"{SLOW} ({DOM})", home_player=SUB,
                   away_player=DOM, coverage="full"),
    ]
    FixtureRepo(conn).upsert_many(slate, seen_at=NOW)
    prices = []
    for ev in ("E4", "E5"):
        prices += [
            OddsPrice(event_id=ev, market="ou", line=3.5, selection="over",
                      odds=1.8, implied_prob=0.51),
            OddsPrice(event_id=ev, market="ou", line=3.5, selection="under",
                      odds=1.9, implied_prob=0.49),
        ]
    OddsRepo(conn).append_many(prices, fetched_at=NOW)
    return conn, db_path


def _x12(conn, event_id, at):
    return conn.execute(
        "SELECT * FROM predictions_x12 WHERE event_id = ? AND predicted_at = ?",
        (event_id, at.isoformat())).fetchone()


def test_h2h_off_and_below_engagement_are_identity(h2h_seeded, club_on,
                                                   x12_on, monkeypatch):
    """Flag off: no tag, no pick on the coin-flip slate. Flag on but below
    min_n: byte-identical probs to the flag-off run and still no tag — the
    engagement threshold is a real gate, not a formality."""
    conn, db_path = h2h_seeded
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["x12_h2h_n"] == {"priced": 0, "schedule": 0}
    off = {ev: _x12(conn, ev, NOW) for ev in ("E4", "E5")}
    for r in off.values():
        assert "-h2h" not in r["model_version"]
        assert r["pick"] is None  # SLOW/SLOW max-of-three is far below 0.50

    monkeypatch.setattr(settings(), "x12_h2h_enabled", True)
    monkeypatch.setattr(settings(), "x12_h2h_min_n", 10_000)  # never engages
    at = NOW + timedelta(minutes=1)
    rep = run_cycle(conn, db_path, now=at)
    assert rep["x12_h2h_n"] == {"priced": 0, "schedule": 0}
    for ev in ("E4", "E5"):
        r = _x12(conn, ev, at)
        assert "-h2h" not in r["model_version"]
        for col in ("p_home", "p_draw", "p_away"):
            assert r[col] == pytest.approx(off[ev][col], abs=1e-9)


def test_stacker_reaches_served_probs_and_tags_rows(h2h_seeded, club_on,
                                                    x12_on, monkeypatch):
    """Engaged: the split saturates toward DOM from both sides, the rows are
    tagged, p_draw is preserved exactly, and the gtl population — with no
    settled rows of its own — stays identity and untagged."""
    conn, db_path = h2h_seeded
    run_cycle(conn, db_path, now=NOW)  # flag off: the unstacked reference
    off = {ev: _x12(conn, ev, NOW) for ev in ("E4", "E5")}
    monkeypatch.setattr(settings(), "x12_h2h_enabled", True)
    monkeypatch.setattr(settings(), "x12_h2h_min_n", 10)
    MatchRepo(conn).upsert_many([MatchRow(
        source_match_id="XS9", date=(NOW + timedelta(minutes=50)).date().isoformat(),
        kickoff_ts=NOW + timedelta(minutes=50), competition="GT Leagues",
        home_player=DOM, away_player=SUB, home_club=SLOW, away_club=SLOW,
        status=0, home_ft=None, away_ft=None, raw_hash="sch-XS9",
        scraped_at=NOW)])
    at = NOW + timedelta(minutes=1)
    rep = run_cycle(conn, db_path, now=at)
    assert rep["x12_h2h_n"]["priced"] == 12
    assert rep["x12_h2h_n"]["schedule"] == 0

    e4, e5 = _x12(conn, "E4", at), _x12(conn, "E5", at)
    for r, dom_side in ((e4, "home"), (e5, "away")):
        assert "-h2h" in r["model_version"]
        assert r["p_home"] + r["p_draw"] + r["p_away"] == pytest.approx(1.0, abs=1e-5)
        assert r["pick"] == dom_side  # only the stacker can clear the gate here
    # p_draw preserved exactly (up to 6dp storage rounding)
    assert e4["p_draw"] == pytest.approx(off["E4"]["p_draw"], abs=2e-6)
    assert e5["p_draw"] == pytest.approx(off["E5"]["p_draw"], abs=2e-6)
    assert e4["p_home"] > off["E4"]["p_home"]
    assert e5["p_away"] > off["E5"]["p_away"]

    # the schedule row prices the same pair but its population is not engaged
    gtl = _x12(conn, "gtl:XS9", at)
    assert gtl is not None and "-h2h" not in gtl["model_version"]
    assert gtl["pick"] is None


def test_regen_agrees_on_stacked_rows(h2h_seeded, club_on, x12_on, h2h_on):
    """A '-h2h' row must regen THROUGH the stacker: the served pick exists
    only because of it, so a regen that skips stacking cannot agree."""
    conn, db_path = h2h_seeded
    run_cycle(conn, db_path, now=NOW)
    assert _x12(conn, "E4", NOW)["pick"] == "home"
    _play_result(conn, NOW + timedelta(minutes=40), home=DOM, away=SUB,
                 hf=2, af=0, source_id="R4")
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["x12_settled"] >= 1
    s = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'E4'").fetchone()
    assert (s["result"], s["pick_correct"]) == ("home", 1)
    assert s["regen_pick"] == "home" and s["regen_agrees"] == 1


def test_regen_does_not_stack_pre_h2h_rows(h2h_seeded, club_on, x12_on,
                                           monkeypatch):
    """Transition safety: a row served before the flag flipped has no '-h2h'
    tag and must regen unstacked even when settlement runs with the stacker
    live — otherwise every pre-h2h row disagrees during the transition."""
    conn, db_path = h2h_seeded
    run_cycle(conn, db_path, now=NOW)  # flag off: E5 untagged, pick None
    served = _x12(conn, "E5", NOW)
    assert "-h2h" not in served["model_version"] and served["pick"] is None

    monkeypatch.setattr(settings(), "x12_h2h_enabled", True)
    monkeypatch.setattr(settings(), "x12_h2h_min_n", 10)
    _play_result(conn, NOW + timedelta(minutes=55), home=SUB, away=DOM,
                 hf=0, af=3, source_id="R5")
    settle_run(conn, db_path, now=LATER)
    s = conn.execute(
        "SELECT * FROM settlements_x12 WHERE event_id = 'E5'").fetchone()
    assert s["result"] == "away"
    # a stacking regen would have re-picked 'away' here; version-awareness
    # means it reproduces the served no-pick instead
    assert s["regen_pick"] is None


def test_stacker_fit_is_blind_to_stored_probabilities(h2h_seeded):
    """Once '-h2h' rows settle, stored p_home/p_away are post-stack values;
    the fit must recompute the raw decisive share from the stored λs or it
    consumes its own output — the recal closed loop
    (docs/RECAL_SERVING.md, 2026-07-13). Poisoning every stored probability
    must not move the fitted coefficients."""
    conn, _ = h2h_seeded
    idx = h2h.H2HIndex(data.load_matches(conn))
    before = h2h.fit_stacker(conn, days=14, min_n=10, index=idx, now=NOW)
    assert before is not None and before.n_fit == 12
    with conn:
        conn.execute("UPDATE predictions_x12 SET p_home = 0.9, p_away = 0.05")
    after = h2h.fit_stacker(conn, days=14, min_n=10, index=idx, now=NOW)
    assert after.coef.tolist() == before.coef.tolist()
