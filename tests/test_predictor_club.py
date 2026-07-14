"""Serving the club feature (docs/CLUB_FEATURE.md step 3).

Every test here is built around one hazard: club can be plumbed *almost* all
the way through and silently do nothing. A cached player-only artifact, a club
name that never resolves, a regen that ignores clubs — none of those raise, and
all of them look healthy on the scorecard. So the league below is rigged so the
club dominates λ: if club is not reaching the model, the assertions fail loudly.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from core.config import settings
from core.schema import FixtureRow, MatchRow, OddsPrice
from model import registry
from predictor.cycle import run_cycle
from settlement.settle import _Regen
from store.repo import FixtureRepo, MatchRepo, OddsRepo

NOW = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)

# Same players everywhere, so *only* the club can explain a λ difference.
FAST, SLOW = "Firepower FC", "Parkbus United"


@pytest.fixture
def club_on(monkeypatch):
    """Pin club on regardless of the deployed default, so these tests describe
    the feature rather than the current rollout state. The rollback test
    overrides this back to False in its body (monkeypatch: last write wins).

    The default itself is a *live switch*, not a deploy-time constant: the API
    spawns predictor cycles on a timer, so flipping it changes serving on the
    next cycle. It shipped unintentionally that way once.
    """
    monkeypatch.setattr(settings(), "club_enabled", True)


@pytest.fixture
def club_seeded(db, tmp_path):
    """Every player is identical; the club drives the scoring rate entirely."""
    rng = np.random.default_rng(3)
    players = [f"P{i}" for i in range(8)]
    rows, mid = [], 0
    for d in range(25):
        day = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=d)
        for k in range(40):
            h, a = rng.choice(players, 2, replace=False)
            hc, ac = rng.choice([FAST, SLOW], 2, replace=True)
            kick = day + timedelta(minutes=15 * k)
            rows.append(MatchRow(
                source_match_id=str(mid), date=day.date().isoformat(),
                kickoff_ts=kick, competition="SYN",
                home_player=h, away_player=a, home_club=hc, away_club=ac,
                status=3,
                home_ft=int(rng.poisson(4.5 if hc == FAST else 0.7)),
                away_ft=int(rng.poisson(4.5 if ac == FAST else 0.7)),
                raw_hash=f"h{mid}", scraped_at=NOW,
            ))
            mid += 1
    MatchRepo(db).upsert_many(rows)

    # E1: both sides FAST (high λ).  E2: both sides SLOW (low λ).
    fixtures = [
        FixtureRow(event_id="E1", start_time_utc=NOW + timedelta(minutes=30),
                   competition="GT", home_raw=f"{FAST} (P0)",
                   away_raw=f"{FAST} (P1)", home_player="P0",
                   away_player="P1", coverage="full"),
        FixtureRow(event_id="E2", start_time_utc=NOW + timedelta(minutes=45),
                   competition="GT", home_raw=f"{SLOW} (P0)",
                   away_raw=f"{SLOW} (P1)", home_player="P0",
                   away_player="P1", coverage="full"),
    ]
    FixtureRepo(db).upsert_many(fixtures, seen_at=NOW)
    prices = []
    for ev in ("E1", "E2"):
        prices += [
            OddsPrice(event_id=ev, market="ou", line=3.5, selection="over",
                      odds=1.8, implied_prob=0.51),
            OddsPrice(event_id=ev, market="ou", line=3.5, selection="under",
                      odds=1.9, implied_prob=0.49),
        ]
    OddsRepo(db).append_many(prices, fetched_at=NOW)
    return db, tmp_path / "test.db"


def _lams(conn, at):
    return {r["event_id"]: r["lambda_home"] + r["lambda_away"]
            for r in conn.execute(
                "SELECT event_id, lambda_home, lambda_away FROM predictions"
                " WHERE predicted_at = ?", (at.isoformat(),))}


def test_club_reaches_the_served_lambda(club_seeded, club_on):
    """Same players on both fixtures. Without club they get identical λ; with
    club, the FAST/FAST fixture must price far above the SLOW/SLOW one."""
    conn, db_path = club_seeded
    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["unresolved_clubs"] == []
    lam = _lams(conn, NOW)
    assert lam["E1"] > lam["E2"] + 3.0, lam


def test_version_tagged_club(club_seeded, club_on):
    conn, db_path = club_seeded
    run_cycle(conn, db_path, now=NOW)
    versions = {r["model_version"] for r in
                conn.execute("SELECT model_version FROM predictions")}
    assert all("-club" in v for v in versions), versions


def test_club_enabled_false_is_the_rollback(club_seeded, club_on, monkeypatch):
    """CLUB_ENABLED=false must reproduce pre-club serving: no tag, and the two
    fixtures (identical players) collapse onto the same λ."""
    conn, db_path = club_seeded
    monkeypatch.setattr(settings(), "club_enabled", False)
    run_cycle(conn, db_path, now=NOW)
    lam = _lams(conn, NOW)
    assert lam["E1"] == pytest.approx(lam["E2"], rel=1e-9)
    versions = {r["model_version"] for r in
                conn.execute("SELECT model_version FROM predictions")}
    assert all("-club" not in v for v in versions)


def test_registry_artifact_tag_separates_club_variants(club_seeded):
    """The silent no-op guard. Both variants are fit for the same day; if they
    shared a cache tag the second call would return the first's model."""
    conn, db_path = club_seeded
    plain = registry.get_poisson(conn, db_path, day="2026-01-26", with_club=False)
    clubbed = registry.get_poisson(conn, db_path, day="2026-01-26", with_club=True)
    assert not plain.with_club and clubbed.with_club
    assert clubbed.clubs == {FAST, SLOW}

    tags = {p.name for p in registry.artifact_dir(db_path).glob("*.pkl")}
    assert len(tags) == 2, tags
    assert any("_club.pkl" in t for t in tags)

    # re-reading each must return its own variant, not whichever was written last
    assert not registry.get_poisson(conn, db_path, day="2026-01-26",
                                    with_club=False).with_club
    assert registry.get_poisson(conn, db_path, day="2026-01-26",
                                with_club=True).with_club


def test_club_ratings_rank_pace(club_seeded):
    conn, db_path = club_seeded
    pm = registry.get_poisson(conn, db_path, day="2026-01-26", with_club=True)
    r = pm.club_ratings()
    assert r[FAST]["pace"] > r[SLOW]["pace"]


def test_unresolved_club_is_reported_and_prices_club_blind(club_seeded, club_on):
    """An unknown club must not raise, must not drop the fixture, and must be
    surfaced — it silently degrades to the player-only λ otherwise."""
    conn, db_path = club_seeded
    FixtureRepo(conn).upsert_many([FixtureRow(
        event_id="E3", start_time_utc=NOW + timedelta(minutes=50),
        competition="GT", home_raw="Atlantis FC (P0)", away_raw=f"{SLOW} (P1)",
        home_player="P0", away_player="P1", coverage="full")], seen_at=NOW)
    OddsRepo(conn).append_many([
        OddsPrice(event_id="E3", market="ou", line=3.5, selection="over",
                  odds=1.8, implied_prob=0.51),
        OddsPrice(event_id="E3", market="ou", line=3.5, selection="under",
                  odds=1.9, implied_prob=0.49)], fetched_at=NOW)

    rep = run_cycle(conn, db_path, now=NOW)
    assert rep["unresolved_clubs"] == ["Atlantis FC"]
    lam = _lams(conn, NOW)
    assert "E3" in lam  # priced, not dropped
    # unknown home club contributes 0 -> E3 sits between FAST/FAST and SLOW/SLOW
    assert lam["E2"] < lam["E3"] < lam["E1"]


def test_regen_is_version_aware(club_seeded):
    """A row served before club shipped must be regenerated club-blind, or
    every pre-club settlement disagrees during the transition and reads as a
    leak. A '-club' row must regenerate with clubs."""
    conn, db_path = club_seeded
    fixture = conn.execute("SELECT * FROM fixtures WHERE event_id='E1'").fetchone()
    regen = _Regen(conn, db_path)

    pre = regen.pick(fixture, 3.5, "blend-w0.7-hl7-a0.01")
    post = regen.pick(fixture, 3.5, "blend-w0.7-hl7-a0.01-club")
    # FAST/FAST: with clubs the total is ~9 -> over; club-blind the λ is the
    # league mean and the pick can only come from the (identical) players
    assert post == "over"
    assert pre in ("over", "under")
    assert len(regen._pms) == 2, "regen must cache the two variants separately"


def test_regen_agrees_with_serving_on_club_rows(club_seeded, club_on):
    """The regen_agrees canary: serving and the honest path must resolve clubs
    through the same code, or this diverges and looks exactly like a leak."""
    conn, db_path = club_seeded
    run_cycle(conn, db_path, now=NOW)
    served = conn.execute(
        "SELECT * FROM predictions WHERE event_id='E1' AND line=3.5").fetchone()
    fixture = conn.execute("SELECT * FROM fixtures WHERE event_id='E1'").fetchone()

    regen = _Regen(conn, db_path)
    r_pick = regen.pick(fixture, served["line"], served["model_version"])
    sel = "over" if served["p_over"] > served["p_under"] else "under"
    assert r_pick == sel


def test_registry_refits_artifact_pickled_before_club_fields(club_seeded):
    """Unpickling skips __init__, so a PoissonModel serialized before the
    catt/cdfn fields existed comes back WITHOUT them and crashes on first
    predict — the 2026-07-10 prod incident. The registry must treat such an
    artifact as stale and refit, not return it."""
    import pickle

    conn, db_path = club_seeded
    day = "2026-01-26"
    pm = registry.get_poisson(conn, db_path, day=day, with_club=False)

    # forge a pre-club artifact: strip the club fields, keep the fingerprint
    path = next(p for p in registry.artifact_dir(db_path).glob("*.pkl")
                if "_club" not in p.name)
    with path.open("rb") as f:
        payload = pickle.load(f)
    del payload["model"].__dict__["catt"]
    del payload["model"].__dict__["cdfn"]
    with path.open("wb") as f:
        pickle.dump(payload, f)

    reloaded = registry.get_poisson(conn, db_path, day=day, with_club=False)
    assert hasattr(reloaded, "catt")  # refit, not the stale pickle
    # and it must actually predict with clubs passed (the crashing call shape)
    lam = reloaded.predict_sides("P0", "P1", FAST, SLOW)
    assert lam == pytest.approx(pm.predict_sides("P0", "P1"), rel=1e-9)
