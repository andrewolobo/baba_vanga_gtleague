"""Pick screen (docs/PICK_SCREEN.md): cascade rules, rolling-stats fit,
and the annotation contract — screen columns never alter pick/tier."""

from datetime import datetime, timedelta, timezone

import pytest

from core.config import settings
from model import screen
from model.screen import ScreenStats

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


# ── bands ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("edge,band", [
    (-0.10, "agree"), (0.0, "agree"),
    (0.01, "noise"), (0.05, "noise"),
    (0.06, "value"), (0.12, "value"),
    (0.13, "stretch"), (0.20, "stretch"),
    (0.21, "absurd"), (0.50, "absurd"),
])
def test_edge_band_tiles_the_line(edge, band):
    assert screen.edge_band(edge) == band


# ── cascade: O/U ─────────────────────────────────────────────────────────────

def test_cold_start_passes_everything():
    # cold stats veto nothing; book_opposed is static and needs an opposed
    # book, so an agreeing (or absent) book passes clean
    s = settings()
    assert screen.apply_ou(3.5, 0.61, "lean", 0.55, ScreenStats(), s) == (1, None)
    assert screen.apply_ou(3.5, 0.61, "lean", None, ScreenStats(), s) == (1, None)
    assert screen.apply_x12(0.51, None, "home", ScreenStats(), s) == (1, None)


def test_book_opposed_needs_conviction_and_a_book():
    s = settings()
    st = ScreenStats()
    assert screen.apply_ou(3.5, 0.62, "lean", 0.45, st, s) == (0, "book_opposed")
    # emphatic model overrides the opposition
    assert screen.apply_ou(3.5, s.screen_override_conf, "solid", 0.45, st, s) \
        == (1, None)
    # no book -> the rule cannot fire
    assert screen.apply_ou(3.5, 0.62, "lean", None, st, s) == (1, None)


def test_cold_line_vetoes_only_at_sample_floor():
    s = settings()
    cold = ScreenStats(line_hit={3.5: (10, 30)})  # 33% at n=30
    thin = ScreenStats(line_hit={3.5: (10, 29)})  # same rate, n below floor
    warm = ScreenStats(line_hit={3.5: (20, 30)})
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, cold, s) == (0, "cold_line")
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, thin, s) == (1, None)
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, warm, s) == (1, None)
    # a different line's chill is not this line's problem
    assert screen.apply_ou(4.5, 0.62, "lean", 0.55, cold, s) == (1, None)


def test_cold_band_reads_the_rows_own_edge():
    s = settings()
    st = ScreenStats(band_hit={"value": (10, 40)})  # 25% at n=40
    # edge 0.62-0.55 = 0.07 -> value band -> veto
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, st, s) == (0, "cold_band")
    # edge 0.62-0.60 = 0.02 -> noise band, no stats -> pass
    assert screen.apply_ou(3.5, 0.62, "lean", 0.60, st, s) == (1, None)
    # no book -> band undefined -> pass
    assert screen.apply_ou(3.5, 0.62, "lean", None, st, s) == (1, None)


def test_drawdown_spares_strong():
    s = settings()
    st = ScreenStats(trailing=(20, 50))  # 40% < breaker floor
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, st, s) == (0, "drawdown")
    assert screen.apply_ou(3.5, 0.70, "strong", 0.55, st, s) == (1, None)
    # below the trailing sample floor the breaker stays open
    thin = ScreenStats(trailing=(19, 49))
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, thin, s) == (1, None)


def test_cascade_order_first_veto_wins():
    s = settings()
    st = ScreenStats(line_hit={3.5: (10, 30)}, trailing=(20, 50))
    # book_opposed outranks cold_line outranks drawdown
    assert screen.apply_ou(3.5, 0.62, "lean", 0.45, st, s) == (0, "book_opposed")
    assert screen.apply_ou(3.5, 0.62, "lean", 0.55, st, s) == (0, "cold_line")


# ── cascade: 1x2 ─────────────────────────────────────────────────────────────

def test_x12_book_opposed_via_argmax():
    s = settings()
    st = ScreenStats(market="x12")
    opposed = {"home": 0.38, "draw": 0.22, "away": 0.40}
    assert screen.apply_x12(0.52, opposed, "home", st, s) == (0, "book_opposed")
    assert screen.apply_x12(s.screen_x12_override_conf, opposed, "home", st, s) \
        == (1, None)
    aligned = {"home": 0.45, "draw": 0.22, "away": 0.33}
    assert screen.apply_x12(0.52, aligned, "home", st, s) == (1, None)
    # a partial book (missing outcome) cannot name an argmax
    partial = {"home": 0.45, "draw": None, "away": 0.33}
    assert screen.apply_x12(0.52, partial, "home", st, s) == (1, None)


def test_x12_drawdown_has_no_tier_exemption():
    s = settings()
    st = ScreenStats(market="x12", trailing=(20, 50))
    assert screen.apply_x12(0.70, None, "home", st, s) == (0, "drawdown")


# ── rolling stats fit ────────────────────────────────────────────────────────

def _seed_settled(db, event_id, kickoff, line, p_over, result_total,
                  book_over=None, pick=None, pick_correct=None,
                  population="priced"):
    """One settled event with one served prediction line (+ optional book)."""
    k = kickoff.isoformat()
    if population == "priced":
        db.execute(
            "INSERT INTO fixtures (event_id, start_time_utc, competition,"
            " home_raw, away_raw, home_player, away_player, coverage,"
            " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, k, "GT", "A (P0)", "B (P1)", "P0", "P1", "full", k, k))
    db.execute(
        "INSERT INTO predictions (event_id, predicted_at, totals_source, line,"
        " p_over, p_push, p_under, lambda_home, lambda_away, pick, confidence,"
        " tier, value_flag, model_version, as_of_cutoff_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, (kickoff - timedelta(minutes=10)).isoformat(), "blend",
         line, p_over, 0.0, 1.0 - p_over, 2.0, 2.0, pick,
         max(p_over, 1 - p_over), None, 0, "t", k))
    if book_over is not None:
        db.execute(
            "INSERT INTO odds_snapshots (event_id, fetched_at, market, line,"
            " selection, odds, implied_prob) VALUES (?,?,?,?,?,?,?)",
            (event_id, (kickoff - timedelta(minutes=5)).isoformat(), "ou",
             line, "over", 1.9, book_over))
    db.execute(
        "INSERT INTO settlements (event_id, matched_match_id, offset_min_used,"
        " result_total, pick_correct, leak_risk, settled_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (event_id, None, 0.0, result_total, pick_correct, 0,
         (kickoff + timedelta(hours=1)).isoformat()))


def test_fit_ou_counterfactual_lines_and_bands(db):
    s = settings()
    k = NOW - timedelta(hours=6)
    # pick-less rows still count: counterfactual doctrine
    _seed_settled(db, "E1", k, 3.5, 0.62, 5, book_over=0.55)   # over-lean hit
    _seed_settled(db, "E2", k, 3.5, 0.61, 2, book_over=0.58)   # over-lean miss
    _seed_settled(db, "E3", k, 4.5, 0.40, 6, book_over=0.62)   # under-lean miss
    st = screen.fit_ou(db, s, "priced", now=NOW)
    assert st.line_hit == {3.5: (1, 2), 4.5: (0, 1)}
    # E1 edge 0.07 (value), E2 edge 0.03 (noise); E3 under: model 0.60 vs
    # book under 0.38 -> edge 0.22 (absurd)
    assert st.band_hit == {"value": (1, 1), "noise": (0, 1), "absurd": (0, 1)}


def test_fit_ou_excludes_pushes_and_respects_population(db):
    s = settings()
    k = NOW - timedelta(hours=6)
    _seed_settled(db, "E1", k, 4.0, 0.62, 4, book_over=0.55)  # integer push
    # schedule row must not leak into the priced stats (and vice versa);
    # gtl rows join through matches, so seed the match it settles against
    db.execute(
        "INSERT INTO matches (source_match_id, date, kickoff_ts, competition,"
        " home_player, away_player, home_club, away_club, status, home_ft,"
        " away_ft, raw_hash, scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("M1", k.date().isoformat(), k.isoformat(), "GT", "P2", "P3", "H",
         "A", 3, 3, 2, "h1", NOW.isoformat()))
    mid = db.execute("SELECT id FROM matches WHERE source_match_id='M1'") \
        .fetchone()["id"]
    _seed_settled(db, "gtl:M1", k, 4.5, 0.70, 5, population="schedule")
    db.execute("UPDATE settlements SET matched_match_id=? WHERE event_id=?",
               (mid, "gtl:M1"))
    priced = screen.fit_ou(db, s, "priced", now=NOW)
    sched = screen.fit_ou(db, s, "schedule", now=NOW)
    assert priced.line_hit == {}          # the push graded nothing
    assert sched.line_hit == {4.5: (1, 1)}
    assert sched.band_hit == {}           # no book on this population, ever


def test_trailing_reads_served_grades_only(db):
    s = settings()
    k = NOW - timedelta(hours=6)
    _seed_settled(db, "E1", k, 3.5, 0.62, 5, pick="over", pick_correct=1)
    _seed_settled(db, "E2", k, 3.5, 0.62, 2, pick="over", pick_correct=0)
    _seed_settled(db, "E3", k, 3.5, 0.55, 5)  # no pick -> not trailing fodder
    st = screen.fit_ou(db, s, "priced", now=NOW)
    assert st.trailing == (1, 2)


def test_fit_all_returns_all_four(db):
    got = screen.fit_all(db, settings(), now=NOW)
    assert set(got) == {("priced", "ou"), ("schedule", "ou"),
                        ("priced", "x12"), ("schedule", "x12")}
    assert all(v.trailing == (0, 0) for v in got.values())  # cold start


# ── cycle annotation contract ────────────────────────────────────────────────

from predictor.cycle import run_cycle  # noqa: E402
from tests.test_predictor import NOW as CYCLE_NOW  # noqa: E402
from tests.test_predictor import seeded  # noqa: E402,F401  (pytest fixture)


def test_cycle_annotates_picks_and_only_picks(seeded):
    conn, db_path = seeded
    rep = run_cycle(conn, db_path, now=CYCLE_NOW)
    rows = conn.execute(
        "SELECT pick, screen_pass, screen_reason FROM predictions").fetchall()
    assert rows
    for r in rows:
        if r["pick"] is None:
            assert r["screen_pass"] is None and r["screen_reason"] is None
        else:  # no settlements seeded -> cold start -> default-open
            assert r["screen_pass"] == 1 and r["screen_reason"] is None
    assert rep["screen_vetoed"] == 0


def test_cycle_screen_off_leaves_columns_null(seeded, monkeypatch):
    conn, db_path = seeded
    monkeypatch.setattr(settings(), "screen_enabled", False)
    run_cycle(conn, db_path, now=CYCLE_NOW)
    got = conn.execute(
        "SELECT screen_pass, screen_reason FROM predictions").fetchall()
    assert got and all(r["screen_pass"] is None and r["screen_reason"] is None
                       for r in got)
