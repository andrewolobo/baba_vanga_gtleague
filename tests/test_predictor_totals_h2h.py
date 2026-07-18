"""Pair-pace term in the totals recal maps (docs/TOTALS_H2H.md Phase 2).

Rigged so a silent no-op cannot pass: every seeded fit row was served the
SAME λ (constant raw p_over, so the plain Platt map flattens toward 0.5 and
can never clear the 0.60 pick gate), while outcomes follow each row's pair
exactly — hot rematches go over, cold ones under. Only the pace column can
explain the fit residual, so its coefficient saturates, and a pick on the
E3/E4 slate IS the proof the term reached serving; its absence under the
flag is the rollback proof. The fit leans 19 over / 21 under so the plain
map's side ('under') differs from the extended map's on the hot pair
('over') — which is what makes the version-aware regen tests discriminate.
"""

from datetime import datetime, timedelta, timezone

import pytest
from scipy.stats import poisson as sp_poisson

from core.config import settings
from core.schema import FixtureRow, MatchRow, OddsPrice
from predictor.cycle import run_cycle
from settlement.settle import run as settle_run
from store.repo import FixtureRepo, MatchRepo, OddsRepo
from test_predictor_club import FAST, NOW, SLOW, club_on, club_seeded  # noqa: F401
from test_settlement import _play_result

LATER = NOW + timedelta(hours=2)
PACE_LINE = 4.5
HOT_H, HOT_A = "P6", "P7"    # rematches run total 8 vs league ~5
COLD_H, COLD_A = "P4", "P5"  # rematches run total 1


@pytest.fixture
def totals_h2h_on(monkeypatch):
    monkeypatch.setattr(settings(), "totals_h2h_enabled", True)


@pytest.fixture
def recal_small_n(monkeypatch):
    """40 seeded fit rows must clear engagement; the shared thin-line tier
    is off so every engaged line's fit shape is its own."""
    monkeypatch.setattr(settings(), "recal_min_n", 20)
    monkeypatch.setattr(settings(), "recal_min_n_line", 0)


@pytest.fixture
def pace_seeded(club_seeded):
    """Pair pace history + settled priced fit rows + the E3/E4 slate."""
    conn, db_path = club_seeded

    # 24 meetings per pair, alternating sides, published well before every
    # cutoff the tests price at
    rows = []
    for d in range(24):
        day = NOW - timedelta(days=25) + timedelta(days=d)
        kick = day.replace(hour=23, minute=45)
        flip = d % 2 == 0
        rows.append(MatchRow(
            source_match_id=f"HT{d}", date=day.date().isoformat(),
            kickoff_ts=kick, competition="SYN",
            home_player=HOT_H if flip else HOT_A,
            away_player=HOT_A if flip else HOT_H,
            home_club=FAST, away_club=FAST, status=3,
            home_ft=5, away_ft=3, raw_hash=f"hot{d}", scraped_at=NOW))
        rows.append(MatchRow(
            source_match_id=f"CD{d}", date=day.date().isoformat(),
            kickoff_ts=kick + timedelta(minutes=1), competition="SYN",
            home_player=COLD_H if flip else COLD_A,
            away_player=COLD_A if flip else COLD_H,
            home_club=SLOW, away_club=SLOW, status=3,
            home_ft=1 if flip else 0, away_ft=0 if flip else 1,
            raw_hash=f"cold{d}", scraped_at=NOW))
    MatchRepo(conn).upsert_many(rows)

    # 40 settled priced fit rows at PACE_LINE: constant served λ, outcomes
    # follow the pair. settled_at must sit inside regen's wall-clock fit
    # window (the test_settlement schedule-regen trick).
    lam_side = 2.2
    rep_p = float(sp_poisson.sf(int(PACE_LINE), 2 * lam_side))
    wall = datetime.now(timezone.utc).isoformat()
    fixtures, preds, setts = [], [], []
    for i in range(40):
        hot = i < 19
        h, a = (HOT_H, HOT_A) if hot else (COLD_H, COLD_A)
        start = NOW - timedelta(hours=26) + timedelta(minutes=10 * i)
        ev = f"PC{i}"
        fixtures.append(FixtureRow(
            event_id=ev, start_time_utc=start, competition="GT",
            home_raw=f"{FAST} ({h})", away_raw=f"{FAST} ({a})",
            home_player=h, away_player=a, coverage="full"))
        preds.append((ev, (start - timedelta(minutes=10)).isoformat(), "blend",
                      PACE_LINE, rep_p, 0.0, 1 - rep_p, lam_side, lam_side,
                      None, None, None, 0, "test",
                      (start - timedelta(minutes=15)).isoformat()))
        setts.append((ev, None, 0.0, 6 if hot else 3, None, 0, None, None,
                      wall))
    FixtureRepo(conn).upsert_many(fixtures, seen_at=NOW)
    with conn:
        conn.executemany(
            "INSERT INTO predictions (event_id, predicted_at, totals_source,"
            " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
            " confidence, tier, value_flag, model_version, as_of_cutoff_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", preds)
        conn.executemany("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)",
                         setts)

    # the slate: hot pair E3 (with an integer 4.0 line too), cold pair E4
    slate = [
        FixtureRow(event_id="E3", start_time_utc=NOW + timedelta(minutes=40),
                   competition="GT", home_raw=f"{FAST} ({HOT_H})",
                   away_raw=f"{SLOW} ({HOT_A})", home_player=HOT_H,
                   away_player=HOT_A, coverage="full"),
        FixtureRow(event_id="E4", start_time_utc=NOW + timedelta(minutes=55),
                   competition="GT", home_raw=f"{FAST} ({COLD_H})",
                   away_raw=f"{SLOW} ({COLD_A})", home_player=COLD_H,
                   away_player=COLD_A, coverage="full"),
    ]
    FixtureRepo(conn).upsert_many(slate, seen_at=NOW)
    prices = []
    for ev, lines in (("E3", (PACE_LINE, 4.0)), ("E4", (PACE_LINE,))):
        for line in lines:
            prices += [
                OddsPrice(event_id=ev, market="ou", line=line,
                          selection="over", odds=1.8, implied_prob=0.51),
                OddsPrice(event_id=ev, market="ou", line=line,
                          selection="under", odds=1.9, implied_prob=0.49),
            ]
    OddsRepo(conn).append_many(prices, fetched_at=NOW)
    return conn, db_path


def _row(conn, event_id, line, at):
    return conn.execute(
        "SELECT * FROM predictions WHERE event_id = ? AND line = ?"
        " AND predicted_at = ?", (event_id, line, at.isoformat())).fetchone()


def test_flag_off_and_below_engagement_are_identity(pace_seeded, club_on,
                                                    recal_small_n,
                                                    monkeypatch):
    """Flag off: plain '-recal2' serving, no '-h2h' anywhere. Flag on but
    below recal engagement: identity, no tag — engagement is recal's own
    gate, not a new one. Flag on with recal off: overridden entirely."""
    conn, db_path = pace_seeded
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["recal_lines"] == [PACE_LINE]
    assert rep["recal_pace_lines"] == []
    r = _row(conn, "E3", PACE_LINE, NOW)
    assert "-recal2" in r["model_version"] and "-h2h" not in r["model_version"]

    monkeypatch.setattr(settings(), "totals_h2h_enabled", True)
    monkeypatch.setattr(settings(), "recal_min_n", 10_000)  # never engages
    at = NOW + timedelta(minutes=1)
    rep = run_cycle(conn, db_path, now=at)
    assert rep["recal_lines"] == [] and rep["recal_pace_lines"] == []
    r = _row(conn, "E3", PACE_LINE, at)
    assert "-recal" not in r["model_version"]

    monkeypatch.setattr(settings(), "recal_min_n", 20)
    monkeypatch.setattr(settings(), "recal_enabled", False)  # overrides flag
    at2 = NOW + timedelta(minutes=2)
    rep = run_cycle(conn, db_path, now=at2)
    assert rep["recal_lines"] == [] and rep["recal_pace_lines"] == []
    r2 = _row(conn, "E3", PACE_LINE, at2)
    assert "-recal" not in r2["model_version"]
    assert r2["p_over"] == r["p_over"]  # identity twice = the same identity


def test_pace_reaches_served_probs_tags_and_gates(pace_seeded, club_on,
                                                  recal_small_n, totals_h2h_on,
                                                  monkeypatch):
    """Engaged: only the pace term can clear the 0.60 gate (the plain map
    flattens the constant-λ fit toward a coin flip), both directions move,
    p_push is preserved exactly, the integer line stays unmapped and
    pick-guarded, and the schedule population — no settled pool of its own —
    stays identity and untagged."""
    conn, db_path = pace_seeded
    monkeypatch.setattr(settings(), "totals_h2h_enabled", False)
    run_cycle(conn, db_path, now=NOW)  # flag off: the plain-map reference
    off = {(ev, li): _row(conn, ev, li, NOW)
           for ev, li in (("E3", PACE_LINE), ("E3", 4.0), ("E4", PACE_LINE))}
    assert off[("E3", PACE_LINE)]["pick"] is None  # flattened: below the gate
    assert off[("E4", PACE_LINE)]["pick"] is None

    monkeypatch.setattr(settings(), "totals_h2h_enabled", True)
    # schedule game of the hot pair: its population has no settled pool
    MatchRepo(conn).upsert_many([MatchRow(
        source_match_id="TS9",
        date=(NOW + timedelta(minutes=50)).date().isoformat(),
        kickoff_ts=NOW + timedelta(minutes=50), competition="GT Leagues",
        home_player=HOT_H, away_player=HOT_A, home_club=FAST, away_club=SLOW,
        status=0, home_ft=None, away_ft=None, raw_hash="sch-TS9",
        scraped_at=NOW)])
    at = NOW + timedelta(minutes=1)
    rep = run_cycle(conn, db_path, now=at)
    assert rep["recal_pace_lines"] == [PACE_LINE]
    assert rep["recal_pace_lines_sched"] == []

    e3, e4 = _row(conn, "E3", PACE_LINE, at), _row(conn, "E4", PACE_LINE, at)
    assert "-recal2-h2h" in e3["model_version"]
    assert e3["pick"] == "over"   # hot pace saturates the over side
    assert e4["pick"] == "under"  # cold pace, the other direction
    assert e3["p_over"] > off[("E3", PACE_LINE)]["p_over"]
    assert e4["p_over"] < off[("E4", PACE_LINE)]["p_over"]
    assert e3["p_push"] == 0.0
    assert abs(e3["p_over"] + e3["p_push"] + e3["p_under"] - 1) < 1e-6

    # integer line: unmapped, untagged, p_push preserved, pick-guarded
    e3_int = _row(conn, "E3", 4.0, at)
    assert "-recal" not in e3_int["model_version"]
    assert e3_int["pick"] is None
    assert e3_int["p_push"] == off[("E3", 4.0)]["p_push"] > 0.0
    assert e3_int["p_over"] == off[("E3", 4.0)]["p_over"]

    # schedule rows of the same pair: unengaged population, identity + no tag
    for r in conn.execute(
            "SELECT * FROM predictions WHERE event_id = 'gtl:TS9'"
            " AND predicted_at = ?", (at.isoformat(),)).fetchall():
        assert "-recal" not in r["model_version"]


def test_regen_agrees_on_pace_extended_rows(pace_seeded, club_on,
                                            recal_small_n, totals_h2h_on):
    """A '-recal2-h2h' row must regen THROUGH the extended map: the served
    pick exists only because of the pace term, so a regen that applies the
    plain map cannot agree."""
    conn, db_path = pace_seeded
    run_cycle(conn, db_path, now=NOW)
    assert _row(conn, "E3", PACE_LINE, NOW)["pick"] == "over"
    _play_result(conn, NOW + timedelta(minutes=40), home=HOT_H, away=HOT_A,
                 hf=4, af=3, source_id="R3")  # total 7: over 4.5 hits
    rep = settle_run(conn, db_path, now=LATER)
    assert rep["settled"] >= 1
    s = conn.execute(
        "SELECT * FROM settlements WHERE event_id = 'E3'").fetchone()
    assert s["pick_correct"] == 1
    assert s["regen_pick"] == "over" and s["regen_agrees"] == 1


def test_regen_routes_plain_recal2_rows_through_plain_maps(pace_seeded,
                                                           club_on,
                                                           recal_small_n,
                                                           monkeypatch):
    """Transition safety: a row served through the PLAIN map before the flag
    flipped has no '-h2h' tag and must regen through the plain map even when
    settlement runs with the extended set live. The plain side here is
    'under' (the 19/21 fit lean) while the extended side on the hot pair is
    'over' — a regen that wrongly stacked pace would flip it."""
    conn, db_path = pace_seeded
    monkeypatch.setattr(settings(), "totals_h2h_enabled", False)
    run_cycle(conn, db_path, now=NOW)  # flag off: E3 tagged plain '-recal2'
    served = _row(conn, "E3", PACE_LINE, NOW)
    assert "-recal2" in served["model_version"]
    assert "-h2h" not in served["model_version"]

    monkeypatch.setattr(settings(), "totals_h2h_enabled", True)
    _play_result(conn, NOW + timedelta(minutes=40), home=HOT_H, away=HOT_A,
                 hf=4, af=3, source_id="R3")
    settle_run(conn, db_path, now=LATER)
    s = conn.execute(
        "SELECT * FROM settlements WHERE event_id = 'E3'").fetchone()
    assert s["regen_pick"] == "under"  # plain-map side, not the pace side
