"""Cyclic time-of-day term in the GLM (docs/TOD_FEATURE.md).

The same three invariants that carry the club feature (test_club.py):

  - with_tod=False must reproduce the pre-tod λ exactly (the rollback path),
    and a fitted tod term must be a no-op when no hour is passed — serving
    code that never learns about hours keeps pricing hour-blind λs.
  - the leakage canary must hold on the tod path: on a league with no signal,
    AUC stays 0.5. The hour columns see kickoff_ts, the one field every
    leak in this codebase has ever come through.
  - a planted hour signal must be recovered — a model that cannot find a
    rigged 03:00-trough cannot find the real one.
"""

import pickle
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.schema import MatchRow
from model import data, evaluate, poisson, registry
from store.repo import MatchRepo
from test_model import synthetic_df  # tests/ is on sys.path (no __init__.py)


def with_tod_signal(df: pd.DataFrame, rate_fn, seed=13) -> pd.DataFrame:
    """Redraw goals so the kickoff hour (not the player) drives the rate.

    Both sides share the hour, mirroring the real effect: a pace factor on
    the match, not on either side.
    """
    rng = np.random.default_rng(seed)
    df = df.copy()
    hours = df["kickoff_ts"].dt.hour + df["kickoff_ts"].dt.minute / 60.0
    lam = hours.map(rate_fn).to_numpy()
    df["home_ft"] = rng.poisson(lam)
    df["away_ft"] = rng.poisson(lam)
    df["total"] = df["home_ft"] + df["away_ft"]
    return df


def _fit(df, with_tod, tod_k=poisson.TOD_FOURIER_K):
    ld = data.long_format(df)
    return poisson.fit(ld, ref_time=pd.Timestamp("2026-02-01", tz="UTC"),
                       with_tod=with_tod, tod_k=tod_k)


def test_without_tod_is_identical_to_pre_tod_fit():
    """The rollback invariant: with_tod=False fits an empty tod block, and
    passing an hour into the predict path is then a strict no-op."""
    pm = _fit(synthetic_df(days=15), False)
    assert not pm.with_tod and pm.tod == []
    assert pm.params["with_tod"] is False and pm.params["tod_k"] is None
    for h, w in (("P0", "P1"), ("P3", "P7")):
        assert pm.predict_sides(h, w, hour=4.0) == pytest.approx(
            pm.predict_sides(h, w), rel=1e-12)


def test_missing_hour_degrades_to_hour_blind_lambda():
    """The cold path must be a no-op, not an error: a caller that does not
    pass an hour gets exactly the hour-blind λ (mirrors the unseen club)."""
    df = with_tod_signal(synthetic_df(days=15),
                         lambda h: 3.5 if h < 5 else 1.0)
    tm = _fit(df, True)
    assert tm.with_tod and len(tm.tod) == 2 * poisson.TOD_FOURIER_K

    early = tm.predict_sides("P0", "P1", hour=2.0)
    late = tm.predict_sides("P0", "P1", hour=8.0)
    blind = tm.predict_sides("P0", "P1")
    assert early != pytest.approx(late, rel=1e-3)  # the term is live
    # and the hour-blind call is the pure player path: z has no tod term
    z_home = tm.intercept + tm.home + tm.att["P0"] + tm.dfn["P1"]
    z_away = tm.intercept + tm.att["P1"] + tm.dfn["P0"]
    assert blind == pytest.approx((np.exp(z_home), np.exp(z_away)), rel=1e-12)


def test_tod_basis_is_periodic():
    """Midnight is midnight: the basis (hence λ) is continuous across 24h."""
    assert poisson.tod_basis([0.0]) == pytest.approx(poisson.tod_basis([24.0]))
    df = with_tod_signal(synthetic_df(days=15),
                         lambda h: 3.5 if h < 5 else 1.0)
    tm = _fit(df, True)
    assert tm.tod_z(0.0) == pytest.approx(tm.tod_z(24.0))


def test_tod_leakage_canary_auc_is_half():
    """No-signal league, tod path on: AUC 0.5 for the tod arm too."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=10, with_tod=True)
    assert {"lam_pt_h", "lam_pt_a"} <= set(preds.columns)
    rep = evaluate.tod_report(preds)
    for line in evaluate.EVAL_LINES:
        assert abs(rep[f"blend+tod_auc_{line}"] - 0.5) < 0.06, rep


def test_tod_signal_is_recovered():
    """When the hour (not the player) drives the rate, the tod arm must beat
    the hour-blind arm. Inverse of the leakage canary."""
    rates = {p: 2.0 for p in [f"P{i}" for i in range(12)]}
    base = synthetic_df(days=30, per_day=40, rates=rates)
    df = with_tod_signal(base, lambda h: 3.5 if h < 5 else 1.0)
    preds = evaluate.build_predictions(df, eval_days=10, with_tod=True)
    rep = evaluate.tod_report(preds)
    line = evaluate.HEADLINE_LINE
    assert rep[f"blend+tod_auc_{line}"] > rep[f"blend_auc_{line}"] + 0.10
    assert rep[f"blend+tod_brier_{line}"] < rep[f"blend_brier_{line}"]


def test_combined_club_tod_arm_recovers_both_signals():
    """Club and hour each drive half the rate; the combined arm must beat
    both single-feature arms. Also pins the design-matrix column layout —
    the tod block sits after the club block, and a mis-indexed extraction
    would scramble catt/cdfn into tod coefficients."""
    from test_club import with_clubs

    rng_rates = {p: 2.0 for p in [f"P{i}" for i in range(12)]}
    base = synthetic_df(days=30, per_day=40, rates=rng_rates)
    df = with_clubs(base)
    # multiplicative: club picks the level, hour halves or keeps it
    rng = np.random.default_rng(17)
    club_lam = {"Alpha": 3.2, "Beta": 2.4, "Gamma": 1.6, "Delta": 1.0}
    hours = df["kickoff_ts"].dt.hour + df["kickoff_ts"].dt.minute / 60.0
    hmul = np.where(hours < 5, 1.6, 0.7)
    df["home_ft"] = rng.poisson(df["home_club"].map(club_lam).to_numpy() * hmul)
    df["away_ft"] = rng.poisson(df["away_club"].map(club_lam).to_numpy() * hmul)
    df["total"] = df["home_ft"] + df["away_ft"]

    preds = evaluate.build_predictions(df, eval_days=10,
                                       with_club=True, with_tod=True)
    assert {"lam_pct_h", "lam_pct_a"} <= set(preds.columns)
    rep = evaluate.tod_club_report(preds)
    line = evaluate.HEADLINE_LINE
    assert rep[f"blend+club+tod_auc_{line}"] > rep[f"blend+club_auc_{line}"] + 0.03
    assert rep[f"blend+club+tod_auc_{line}"] > rep[f"blend+tod_auc_{line}"] + 0.03
    assert rep[f"blend+club+tod_brier_{line}"] < rep[f"blend+club_brier_{line}"]


NOW = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def matches_seeded(db, tmp_path):
    rng = np.random.default_rng(5)
    players = [f"P{i}" for i in range(8)]
    rows = []
    for d in range(25):
        day = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=d)
        for k in range(40):
            h, a = rng.choice(players, 2, replace=False)
            rows.append(MatchRow(
                source_match_id=str(len(rows)), date=day.date().isoformat(),
                kickoff_ts=day + timedelta(minutes=15 * k), competition="SYN",
                home_player=h, away_player=a, home_club="X", away_club="Y",
                status=3, home_ft=int(rng.poisson(2.0)),
                away_ft=int(rng.poisson(2.0)),
                raw_hash=f"h{len(rows)}", scraped_at=NOW,
            ))
    MatchRepo(db).upsert_many(rows)
    return db, tmp_path / "test.db"


def test_registry_refits_artifact_pickled_before_tod_field(matches_seeded):
    """Same incident class as the 2026-07-10 club one: unpickling skips
    __init__, so an artifact serialized before the tod field existed comes
    back WITHOUT it and crashes on first hour-blind predict (side_lambda
    calls tod_z, which reads self.tod). The registry must refit, not return
    it. Guarded by the field list in registry.get_poisson."""
    conn, db_path = matches_seeded
    day = "2026-01-26"
    pm = registry.get_poisson(conn, db_path, day=day)

    path = next(iter(registry.artifact_dir(db_path).glob("*.pkl")))
    with path.open("rb") as f:
        payload = pickle.load(f)
    del payload["model"].__dict__["tod"]
    with path.open("wb") as f:
        pickle.dump(payload, f)

    reloaded = registry.get_poisson(conn, db_path, day=day)
    assert hasattr(reloaded, "tod")  # refit, not the stale pickle
    # the crashing call shape: any predict goes through tod_z
    lam = reloaded.predict_sides("P0", "P1")
    assert lam == pytest.approx(pm.predict_sides("P0", "P1"), rel=1e-9)
