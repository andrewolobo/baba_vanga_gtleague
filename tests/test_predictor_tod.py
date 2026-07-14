"""Serving the time-of-day feature (docs/TOD_FEATURE.md, Phase 2).

Same hazard as test_predictor_club.py: tod can be plumbed *almost* all the
way through and silently do nothing — a cached hour-blind artifact, a cycle
that never passes the hour, a regen that ignores it. None of those raise. So
the league below is rigged so the kickoff hour dominates λ: both fixtures
share the players AND the clubs, and only the hour can explain a λ gap.

The generating rate is smooth and cyclic — lam(h) peaks at 13:00 UTC and
troughs at 01:00 — so the K=3 Fourier basis can represent it exactly and a
failed assertion means broken plumbing, not basis misfit.
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


def _rate(hour: float) -> float:
    """Peak 4.5 at 13:00, trough ~0.4 at 01:00 — K=1 shape, nested in K=3."""
    return 4.5 * float(np.exp(1.2 * (np.cos(2 * np.pi * (hour - 13) / 24) - 1)))


@pytest.fixture
def tod_on(monkeypatch):
    """Pin tod on regardless of the deployed default (same rationale as
    club_on: these tests describe the feature, not the rollout state)."""
    monkeypatch.setattr(settings(), "tod_enabled", True)


@pytest.fixture
def tod_seeded(db, tmp_path):
    """Every player identical, every match the same clubs; the kickoff hour
    drives the scoring rate entirely."""
    rng = np.random.default_rng(7)
    players = [f"P{i}" for i in range(8)]
    rows = []
    for d in range(25):
        day = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=d)
        for k in range(40):  # 36-min spacing -> kickoffs span the full day
            h, a = rng.choice(players, 2, replace=False)
            kick = day + timedelta(minutes=36 * k)
            lam = _rate(kick.hour + kick.minute / 60.0)
            rows.append(MatchRow(
                source_match_id=str(len(rows)), date=day.date().isoformat(),
                kickoff_ts=kick, competition="SYN",
                home_player=h, away_player=a, home_club="X", away_club="Y",
                status=3, home_ft=int(rng.poisson(lam)),
                away_ft=int(rng.poisson(lam)),
                raw_hash=f"h{len(rows)}", scraped_at=NOW,
            ))
    MatchRepo(db).upsert_many(rows)

    # E1 kicks at the 13:00 peak, E2 near the evening slide; same players,
    # same clubs — only the hour differs.
    fixtures = [
        FixtureRow(event_id="E1", start_time_utc=NOW + timedelta(minutes=60),
                   competition="GT", home_raw="X (P0)", away_raw="Y (P1)",
                   home_player="P0", away_player="P1", coverage="full"),
        FixtureRow(event_id="E2", start_time_utc=NOW + timedelta(minutes=345),
                   competition="GT", home_raw="X (P0)", away_raw="Y (P1)",
                   home_player="P0", away_player="P1", coverage="full"),
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


def test_tod_reaches_the_served_lambda(tod_seeded, tod_on):
    """Same players, same clubs on both fixtures. Without tod they price
    identically; with tod the 13:00 fixture must sit far above the 17:45 one."""
    conn, db_path = tod_seeded
    run_cycle(conn, db_path, now=NOW)
    lam = _lams(conn, NOW)
    assert lam["E1"] > lam["E2"] + 2.5, lam


def test_version_tagged_tod(tod_seeded, tod_on):
    conn, db_path = tod_seeded
    run_cycle(conn, db_path, now=NOW)
    versions = {r["model_version"] for r in
                conn.execute("SELECT model_version FROM predictions")}
    assert all("-tod" in v for v in versions), versions


def test_tod_enabled_false_is_the_rollback(tod_seeded, tod_on, monkeypatch):
    """TOD_ENABLED=false must reproduce pre-tod serving: no tag, and the two
    fixtures (identical players and clubs) collapse onto the same λ."""
    conn, db_path = tod_seeded
    monkeypatch.setattr(settings(), "tod_enabled", False)
    run_cycle(conn, db_path, now=NOW)
    lam = _lams(conn, NOW)
    assert lam["E1"] == pytest.approx(lam["E2"], rel=1e-9)
    versions = {r["model_version"] for r in
                conn.execute("SELECT model_version FROM predictions")}
    assert all("-tod" not in v for v in versions)


def test_registry_artifact_tag_separates_tod_variants(tod_seeded):
    """The silent no-op guard: a cached hour-blind pickle must never load
    into a tod-enabled cycle, and vice versa."""
    conn, db_path = tod_seeded
    plain = registry.get_poisson(conn, db_path, day="2026-01-26", with_tod=False)
    tod = registry.get_poisson(conn, db_path, day="2026-01-26", with_tod=True)
    assert not plain.with_tod and tod.with_tod

    tags = {p.name for p in registry.artifact_dir(db_path).glob("*.pkl")}
    assert len(tags) == 2, tags
    assert any("_tod.pkl" in t for t in tags)

    assert not registry.get_poisson(conn, db_path, day="2026-01-26",
                                    with_tod=False).with_tod
    assert registry.get_poisson(conn, db_path, day="2026-01-26",
                                with_tod=True).with_tod


def test_regen_is_version_aware_for_tod(tod_seeded):
    """A row served before tod shipped must be regenerated hour-blind; a
    '-tod' row must regenerate with the hour — or regen_agrees collapses
    across the transition and reads exactly like a leak."""
    conn, db_path = tod_seeded
    fixture = conn.execute("SELECT * FROM fixtures WHERE event_id='E1'").fetchone()
    regen = _Regen(conn, db_path)

    pre = regen.pick(fixture, 3.5, "blend-w0.7-hl7-a0.01")
    post = regen.pick(fixture, 3.5, "blend-w0.7-hl7-a0.01-tod")
    # 13:00 peak: with tod the λ total is ~9 -> over; hour-blind the λ is the
    # league mean and the pick can only come from the (identical) players
    assert post == "over"
    assert pre in ("over", "under")
    assert len(regen._pms) == 2, "regen must cache the two variants separately"


def test_regen_agrees_with_serving_on_tod_rows(tod_seeded, tod_on):
    """The regen_agrees canary: serving and the honest path must derive the
    hour from the same kickoff, or this diverges and looks like a leak."""
    conn, db_path = tod_seeded
    run_cycle(conn, db_path, now=NOW)
    served = conn.execute(
        "SELECT * FROM predictions WHERE event_id='E1' AND line=3.5").fetchone()
    fixture = conn.execute("SELECT * FROM fixtures WHERE event_id='E1'").fetchone()

    regen = _Regen(conn, db_path)
    r_pick = regen.pick(fixture, served["line"], served["model_version"])
    sel = "over" if served["p_over"] > served["p_under"] else "under"
    assert r_pick == sel
