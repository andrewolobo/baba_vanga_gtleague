from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy.stats import poisson as sp_poisson

from core.config import settings
from core.schema import FixtureRow, MatchRow, OddsPrice
from model import recal
from predictor.cycle import _tier, run_cycle
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


LAM_SHRINK_CENTER = 4.25  # league-mean total the seeded λs are shrunk toward


def _served_lam(lam_true: float) -> float:
    """The λ the seeded row was 'served' with: shrunk toward the league mean
    while outcomes follow lam_true — the dynamic-range compression the live
    model exhibits. The recal fit recomputes raw probs from the STORED λs
    (docs/RECAL_SERVING.md 2026-07-13: fitting on stored p_over consumed the
    map's own output), so seeded miscalibration must live in λ-space; a
    p-space distortion of the stored probs is invisible to the fit by
    design."""
    return LAM_SHRINK_CENTER + 0.6 * (lam_true - LAM_SHRINK_CENTER)


def _seed_settled_predictions(conn, n_events=350, lines=(3.5, 4.5), seed=17,
                              start=0):
    """Settled, model-covered predictions whose served λs were compressed
    toward the league mean relative to the (calibrated) outcome distribution
    — the serving-side Platt fit must recover a sharpening map (a > 1).
    Stored p_over stays consistent with the stored λs, exactly as serving
    writes rows. Events are in the past so they never enter the run_cycle
    slate. `start` offsets the event ids so repeated calls seed disjoint
    batches (e.g. a thin line)."""
    rng = np.random.default_rng(seed)
    fx, pr, st = [], [], []
    for i in range(n_events):
        ev, lam = f"SETTLED{start + i}", rng.uniform(2.5, 6.0)
        lam_srv = _served_lam(lam)
        fx.append((ev, "2026-01-20T10:00:00+00:00", "GT Leagues", "x", "y",
                   "PX", "PY", "full",
                   "2026-01-19T00:00:00+00:00", "2026-01-19T00:00:00+00:00"))
        for line in lines:
            rep_p = float(sp_poisson.sf(int(line), lam_srv))
            pr.append((ev, "2026-01-20T09:50:00+00:00", "blend", line, rep_p,
                       0.0, 1 - rep_p, lam_srv / 2, lam_srv / 2, None, None,
                       None, 0, "test", "2026-01-20T09:45:00+00:00"))
        st.append((ev, None, 0.0, int(rng.poisson(lam)), None, 0, None, None,
                   "2026-01-25T12:00:00+00:00"))
    with conn:
        conn.executemany("INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?,?)", fx)
        conn.executemany(
            "INSERT INTO predictions (event_id, predicted_at, totals_source,"
            " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
            " confidence, tier, value_flag, model_version, as_of_cutoff_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pr)
        conn.executemany("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)", st)


def _seed_settled_schedule(conn, n_events=350, lines=(3.5, 4.5), seed=23,
                           start=0, under_bias=0.0, settled_at=None,
                           kickoff_base=None):
    """Settled schedule-population (gtl:) predictions: finished matches plus
    settlements joined by matched_match_id — the population-split Phase 2
    fit path. Served λs are compressed toward the league mean like the
    priced seed (see _served_lam). under_bias shifts realized totals below
    (negative: above) the λ the probs were computed from, which fits maps
    with a large intercept — used to prove regen keeps identity on rows
    served untagged. Kickoffs are unique per event to satisfy the matches
    dedup constraint. Pass a kickoff_base dated AFTER the fixtures under
    test to keep the seeded matches out of regen's day-frozen retraining
    (it trains on strictly earlier days only)."""
    rng = np.random.default_rng(seed)
    settled_at = settled_at or "2026-01-25T12:00:00+00:00"
    kickoff_base = kickoff_base or datetime(2026, 1, 20, 10, 0,
                                            tzinfo=timezone.utc)
    day = kickoff_base.date().isoformat()
    batch_ts = (kickoff_base - timedelta(minutes=10)).isoformat()
    pr, st = [], []
    with conn:
        for i in range(n_events):
            sid = f"GS{start + i}"
            ev, lam = f"gtl:{sid}", rng.uniform(2.5, 6.0)
            lam_srv = _served_lam(lam)
            kick = (kickoff_base + timedelta(minutes=i)).isoformat()
            total = int(rng.poisson(max(lam - under_bias, 0.1)))
            cur = conn.execute(
                "INSERT INTO matches (source_match_id, date, kickoff_ts,"
                " competition, home_player, away_player, home_club, away_club,"
                " status, home_ft, away_ft, raw_hash, scraped_at)"
                " VALUES (?,?,?,'SYN','PX','PY','H','A',3,?,?,?,?)",
                (sid, day, kick, total - total // 2, total // 2, f"gs-{sid}",
                 f"{day}T23:59:00+00:00"))
            for line in lines:
                rep_p = float(sp_poisson.sf(int(line), lam_srv))
                pr.append((ev, batch_ts, "blend", line,
                           rep_p, 0.0, 1 - rep_p, lam_srv / 2, lam_srv / 2,
                           None, None, None, 0, "test", batch_ts))
            st.append((ev, cur.lastrowid, 0.0, total, None, 0, None, None,
                       settled_at))
        conn.executemany(
            "INSERT INTO predictions (event_id, predicted_at, totals_source,"
            " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
            " confidence, tier, value_flag, model_version, as_of_cutoff_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pr)
        conn.executemany("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)", st)


def test_fit_line_maps_needs_volume_and_half_lines(seeded):
    conn, _ = seeded
    assert recal.fit_line_maps(conn, days=14, min_n=300, now=NOW) == {}  # cold
    _seed_settled_predictions(conn)
    maps = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW)
    assert set(maps) == {3.5, 4.5}
    for a, _b in maps.values():
        assert a > 1.15  # recovers the seeded compression as sharpening
    # min_n gate: 350 events exist but a higher bar must exclude them
    assert recal.fit_line_maps(conn, days=14, min_n=400, now=NOW) == {}


def test_fit_line_maps_hierarchical_tier(seeded):
    """A thin line engages through the shared-slope tier (pooled a, own b);
    a line under the tier floor stays unmapped; full lines keep their own
    2-parameter fits byte-identical to the tier-off behavior."""
    conn, _ = seeded
    _seed_settled_predictions(conn)                                # 3.5/4.5 x350
    _seed_settled_predictions(conn, n_events=120, lines=(5.5,), start=1000)
    _seed_settled_predictions(conn, n_events=40, lines=(6.5,), start=2000)

    solo = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW)
    assert set(solo) == {3.5, 4.5}          # tier off: original behavior

    maps = recal.fit_line_maps(conn, days=14, min_n=300, min_n_line=75, now=NOW)
    assert set(maps) == {3.5, 4.5, 5.5}     # 6.5 (n=40) stays below the floor
    assert maps[3.5] == solo[3.5] and maps[4.5] == solo[4.5]
    assert maps[5.5][0] > 1.15              # pooled slope recovers the squeeze

    # tier needs a big-enough pool: alone, the thin line must NOT engage
    thin_only = recal.fit_line_maps(conn, days=14, min_n=3000, min_n_line=75,
                                    now=NOW)
    assert thin_only == {}


def test_fit_line_maps_populations_are_disjoint(seeded):
    """The priced fit must be blind to schedule settlements and vice versa —
    pooling across populations is what the split measurement forbids."""
    conn, _ = seeded
    _seed_settled_schedule(conn)
    # schedule data alone: priced fit stays identity, schedule fit engages
    assert recal.fit_line_maps(conn, days=14, min_n=300, now=NOW) == {}
    maps = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                               population="schedule")
    assert set(maps) == {3.5, 4.5}
    for a, _b in maps.values():
        assert a > 1.15  # recovers the seeded compression from its own pool

    # adding priced data engages the priced fit without moving the schedule fit
    _seed_settled_predictions(conn)
    assert set(recal.fit_line_maps(conn, days=14, min_n=300, now=NOW)) \
        == {3.5, 4.5}
    assert recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                               population="schedule") == maps

    with pytest.raises(ValueError):
        recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                            population="pooled")


def test_fit_is_blind_to_stored_probabilities(seeded):
    """The fit consumes raw probs recomputed from the stored λs, never the
    stored p_over. Stored p_over is the post-map served value, and fitting
    on it feeds the map its own output — the 2026-07-13 closed loop
    (docs/RECAL_SERVING.md): fitted slopes decayed toward identity as
    recal-tagged rows filled the window, and the priced shared slope went
    negative. Poisoning every stored probability must not move the fit."""
    conn, _ = seeded
    _seed_settled_predictions(conn)
    before = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW)
    assert set(before) == {3.5, 4.5}
    with conn:
        conn.execute("UPDATE predictions SET p_over = 0.5, p_under = 0.5")
    assert recal.fit_line_maps(conn, days=14, min_n=300, now=NOW) == before


def test_cycle_fits_and_applies_per_population_maps(seeded, monkeypatch):
    """Population split Phase 2: schedule rows are priced through
    schedule-population maps and tagged per row, while the priced side keeps
    its identity fallback when its own pool is empty."""
    conn, db_path = seeded
    _schedule_game(conn, NOW + timedelta(hours=1))
    _seed_settled_schedule(conn)  # only schedule settlements exist
    s = settings()

    monkeypatch.setattr(s, "recal_enabled", False)
    run_cycle(conn, db_path, now=NOW)
    control = {(r["event_id"], r["line"]): r for r in conn.execute(
        "SELECT * FROM predictions WHERE predicted_at = ?",
        (NOW.isoformat(),)).fetchall()}

    monkeypatch.setattr(s, "recal_enabled", True)
    now2 = NOW + timedelta(minutes=5)
    rep = run_cycle(conn, db_path, now=now2)
    assert rep["recal_lines"] == []  # priced pool empty: identity fallback
    assert rep["recal_lines_sched"] == [3.5, 4.5]

    # refit what the cycle fitted (the settled pool is unchanged by the run)
    maps_sched = recal.fit_line_maps(conn, days=14, min_n=300, min_n_line=75,
                                     now=now2, population="schedule")
    sched_mapped = 0
    for r in conn.execute("SELECT * FROM predictions WHERE predicted_at = ?",
                          (now2.isoformat(),)).fetchall():
        c = control[(r["event_id"], r["line"])]
        if r["event_id"].startswith("gtl:") and r["line"] in (3.5, 4.5):
            sched_mapped += 1
            assert "-recal" in r["model_version"]
            exp = recal.apply_to_line(maps_sched, r["line"], c["p_over"],
                                      c["p_push"])[0]
            assert r["p_over"] == pytest.approx(exp, abs=1e-5)
            assert r["p_over"] != c["p_over"]  # the map actually moved it
        else:  # priced rows and unmapped lines: identity and untagged
            assert "-recal" not in r["model_version"]
            assert r["p_over"] == c["p_over"]
    assert sched_mapped  # schedule lines straddle E[total] ~ 4


def test_cycle_recal_engages_sharpens_and_tags_version(seeded, monkeypatch):
    """With enough settled history the cycle must fit maps, sharpen covered
    probs, and tag '-recal' on exactly the rows a map touched; with
    RECAL_ENABLED=false (the rollback flag) the same cycle must reproduce
    pre-recal output."""
    conn, db_path = seeded
    _seed_settled_predictions(conn)
    s = settings()

    monkeypatch.setattr(s, "recal_enabled", False)
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["recal_lines"] == []
    control = {(r["event_id"], r["line"]): r for r in conn.execute(
        "SELECT * FROM predictions WHERE predicted_at = ?",
        (NOW.isoformat(),)).fetchall()}
    assert all("-recal" not in r["model_version"] for r in control.values())

    monkeypatch.setattr(s, "recal_enabled", True)
    now2 = NOW + timedelta(minutes=5)
    rep = run_cycle(conn, db_path, now=now2)
    assert rep["recal_lines"] == [3.5, 4.5]
    for r in conn.execute("SELECT * FROM predictions WHERE predicted_at = ?",
                          (now2.isoformat(),)).fetchall():
        assert abs(r["p_over"] + r["p_push"] + r["p_under"] - 1) < 1e-6
        c = control[(r["event_id"], r["line"])]
        if r["lambda_home"] is None:  # book fallback: untouched AND untagged
            assert "-recal" not in r["model_version"]
            assert r["p_over"] == c["p_over"]
        else:  # λs identical, prob strictly sharper than the control run
            assert "-recal" in r["model_version"]
            assert r["lambda_home"] == c["lambda_home"]
            assert abs(r["p_over"] - 0.5) > abs(c["p_over"] - 0.5)


def test_priced_recal_maps_never_touch_schedule_rows(seeded, monkeypatch):
    """Population split Phase 0 (docs/POPULATION_SPLIT.md): recal maps are
    fit on settled — i.e. book-priced — rows, and the populations do not
    share a calibration curve. Schedule (gtl:) rows must pass through
    identity byte-for-byte and carry no '-recal' tag, even on the exact
    lines where the priced maps are engaged."""
    conn, db_path = seeded
    _schedule_game(conn, NOW + timedelta(hours=1))
    _seed_settled_predictions(conn)
    s = settings()

    monkeypatch.setattr(s, "recal_enabled", False)
    run_cycle(conn, db_path, now=NOW)
    control = {r["line"]: r for r in conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'gtl:SCHED1'"
        " AND predicted_at = ?", (NOW.isoformat(),)).fetchall()}
    assert control

    monkeypatch.setattr(s, "recal_enabled", True)
    now2 = NOW + timedelta(minutes=5)
    rep = run_cycle(conn, db_path, now=now2)
    assert rep["recal_lines"] == [3.5, 4.5]
    rows = conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'gtl:SCHED1'"
        " AND predicted_at = ?", (now2.isoformat(),)).fetchall()
    assert rows
    # synthetic league totals ~4 -> schedule lines 3.5/4.5, exactly the
    # lines the priced maps engaged on — the strongest isolation check
    assert {r["line"] for r in rows} & set(rep["recal_lines"])
    for r in rows:
        assert "-recal" not in r["model_version"]
        c = control[r["line"]]
        for col in ("p_over", "p_push", "p_under", "pick", "confidence",
                    "tier", "model_version"):
            assert r[col] == c[col], col


def test_cycle_recal_identity_on_cold_start(seeded):
    """No settled data -> empty maps -> behavior and version tag unchanged."""
    conn, db_path = seeded
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["recal_lines"] == []
    versions = {r["model_version"] for r in conn.execute(
        "SELECT model_version FROM predictions").fetchall()}
    assert all("-recal" not in v for v in versions)


def test_pick_gate_needs_060_confidence(seeded):
    """Primary product rule: only surface probable wins. Near-coin-flip
    disagreements (confidence < PICK_PROB_THRESHOLD) carry no pick."""
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    rows = conn.execute(
        "SELECT pick, confidence FROM predictions WHERE lambda_home IS NOT NULL"
    ).fetchall()
    assert rows
    for r in rows:
        if r["pick"] is not None:
            assert r["confidence"] >= 0.60
        else:
            assert r["confidence"] is None or r["confidence"] < 0.60


def test_unmapped_priced_line_never_picks_while_maps_engaged(seeded):
    """Priced-population doctrine (docs/POPULATION_SPLIT.md): once this
    population's maps are engaged, a line without its own map has too few
    graded samples to know its correction, and its raw confidence is the
    adverse-selection surface — the residual priced picks measured
    2026-07-13 leaked through unmapped 6.5 at 47.6%. With maps empty
    (cold start / rollback) the line picks exactly as before."""
    conn, db_path = seeded
    # a 6.5 line on E1: at the synthetic league's λ_total ~ 4 the model is
    # confidently under (P(total <= 6) ~ 0.89), far above the 0.60 gate
    OddsRepo(conn).append_many([
        OddsPrice(event_id="E1", market="ou", line=6.5, selection="over",
                  odds=6.0, implied_prob=0.15),
        OddsPrice(event_id="E1", market="ou", line=6.5, selection="under",
                  odds=1.1, implied_prob=0.85),
    ], fetched_at=NOW)

    run_cycle(conn, db_path, now=NOW)  # cold start: no maps anywhere
    cold = conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'E1' AND line = 6.5"
        " AND predicted_at = ?", (NOW.isoformat(),)).fetchone()
    assert cold["pick"] == "under" and cold["confidence"] >= 0.60

    _seed_settled_predictions(conn)  # engages priced maps on 3.5/4.5 only
    now2 = NOW + timedelta(minutes=5)
    rep = run_cycle(conn, db_path, now=now2)
    assert rep["recal_lines"] == [3.5, 4.5]
    r = conn.execute(
        "SELECT * FROM predictions WHERE event_id = 'E1' AND line = 6.5"
        " AND predicted_at = ?", (now2.isoformat(),)).fetchone()
    assert r["pick"] is None and r["tier"] is None
    # suppressed by doctrine, not by confidence — the scale itself is intact
    assert r["confidence"] >= 0.60
    assert "-recal" not in r["model_version"]  # no map touched the row


def test_confidence_is_the_model_prob_not_blended_with_the_book(seeded):
    """A book-priced row and a schedule-only row must sit on one scale.
    Blending in book_p (~0.5 at the main line) used to shrink every priced
    row halfway to a coin flip and demote its tier."""
    conn, db_path = seeded
    run_cycle(conn, db_path, now=NOW)
    rows = conn.execute(
        "SELECT p_over, p_under, confidence FROM predictions"
        " WHERE lambda_home IS NOT NULL").fetchall()
    assert rows  # E1 is book-priced (implied 0.51/0.49) and model-covered
    for r in rows:
        assert r["confidence"] == pytest.approx(max(r["p_over"], r["p_under"]))


def test_tier_bands_partition_the_picked_region():
    """_tier only ever runs on rows that cleared the gate, so a band below the
    gate is dead code. This is the invariant that killed 'lean' for a week."""
    s = settings()
    assert s.tier_lean == s.pick_prob_threshold
    assert s.tier_lean < s.tier_solid < s.tier_strong < 1.0
    assert _tier(s.pick_prob_threshold, s) == "lean"
    assert _tier(s.tier_solid - 1e-9, s) == "lean"
    assert _tier(s.tier_solid, s) == "solid"
    assert _tier(s.tier_strong - 1e-9, s) == "solid"
    assert _tier(s.tier_strong, s) == "strong"
    assert _tier(1.0, s) == "strong"


def test_cycle_respects_horizon(seeded):
    conn, db_path = seeded
    rep = run_cycle(conn, db_path, now=NOW + timedelta(hours=2))  # kickoffs passed
    assert rep["fixtures"] == 0
    assert rep["rows"] == 0
