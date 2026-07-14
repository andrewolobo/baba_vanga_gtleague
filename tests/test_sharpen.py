"""Sharpening-head tests (FEATURE_IDEAS option 3): the double-Poisson math
must be a strict Poisson generalization, the MLEs must recover known
dispersion from synthetic data, and the variance index inherits the
no-own-match invariant."""

import numpy as np
import pandas as pd

from core.markets import poisson_pmf
from model import data, evaluate, sharpen
from tests.test_model import synthetic_df


def test_dp_pmf_phi_one_is_poisson():
    for mu in (1.5, 3.9, 6.0):
        dp = sharpen.dp_pmf(mu, 1.0, 20)[0]
        ref = np.array(poisson_pmf(mu, 20))
        assert np.allclose(dp, ref / ref.sum(), atol=1e-9)


def test_dp_pmf_sharpens_variance_keeps_mean():
    mu, phi = 3.9, 1.4
    k = np.arange(21)
    pmf = sharpen.dp_pmf(mu, phi, 20)[0]
    mean = float((k * pmf).sum())
    var = float(((k - mean) ** 2 * pmf).sum())
    assert abs(mean - mu) < 0.05          # mean ≈ μ (Efron's property)
    assert abs(var - mu / phi) < 0.15     # Var ≈ μ/φ
    assert var < mu                       # φ > 1 means sharper than Poisson


def _sample_dp(rng, mu, phi, n):
    k = np.arange(21)
    return np.array([rng.choice(k, p=sharpen.dp_pmf(m, phi, 20)[0])
                     for m in mu[:n]])


def test_fit_phi_recovers_true_dispersion():
    rng = np.random.default_rng(3)
    mu = rng.uniform(2.0, 6.0, 3000)
    totals = _sample_dp(rng, mu, 1.4, len(mu))
    assert abs(sharpen.fit_phi(mu, totals) - 1.4) < 0.12


def test_fit_phi_model_recovers_heterogeneity():
    """Two populations: x=0.8 consistent (φ high), x=1.2 volatile (φ low).
    b1 must come out negative; a homogeneous league must give b1 ≈ 0."""
    rng = np.random.default_rng(5)
    mu = rng.uniform(2.0, 6.0, 3000)
    x = np.where(rng.random(len(mu)) < 0.5, 0.8, 1.2)
    phi_true = np.exp(0.3 - 1.0 * (x - 1.0))
    totals = np.array([rng.choice(np.arange(21),
                                  p=sharpen.dp_pmf(m, f, 20)[0])
                       for m, f in zip(mu, phi_true)])
    b0, b1 = sharpen.fit_phi_model(mu, totals, x)
    assert b1 < -0.4
    assert abs(b0 - 0.3) < 0.2

    totals_h = _sample_dp(rng, mu, np.exp(0.3), len(mu))
    _, b1_h = sharpen.fit_phi_model(mu, totals_h, x)
    assert abs(b1_h) < 0.25


def test_var_index_cannot_see_own_match():
    df = synthetic_df(days=10, per_day=30)
    ld = data.long_format(df)
    idx = sharpen.VarIndex(ld, window=25, min_periods=5, lag_min=20.0)
    p = ld.iloc[-1]["player"]
    last = ld[ld["player"] == p].iloc[-1]
    at_kick = idx.ratio(p, last["kickoff_ts"].to_datetime64())
    after = idx.ratio(
        p, (last["kickoff_ts"] + pd.Timedelta(minutes=21)).to_datetime64())
    assert at_kick != after  # own result only lands after publish


def test_var_index_separates_styles():
    """A player who alternates 0-0 and 4-4 games must profile as higher
    variance than one who always plays 2-2 (same mean total)."""
    rows = []
    kick = pd.Timestamp("2026-01-01", tz="UTC")
    for i in range(40):
        rows.append({"match_id": i, "kickoff_ts": kick + pd.Timedelta(hours=i),
                     "player": "Volatile", "goals_for": 4 * (i % 2),
                     "goals_against": 4 * (i % 2)})
        rows.append({"match_id": 100 + i, "kickoff_ts": kick + pd.Timedelta(hours=i),
                     "player": "Steady", "goals_for": 2, "goals_against": 2})
    idx = sharpen.VarIndex(pd.DataFrame(rows), window=25, min_periods=10)
    cut = (kick + pd.Timedelta(days=30)).to_datetime64()
    assert idx.ratio("Volatile", cut) > 1.0 > idx.ratio("Steady", cut)
    assert idx.ratio("NEVER_SEEN", cut) is None


def test_sharpen_report_no_leak_and_calibrated():
    """No-signal league: sharpening must not conjure discrimination (AUC
    stays ~0.5 for every variant) and fitted φ must be sane."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=12, with_var=True)
    rep = evaluate.sharpen_report(preds, warmup_days=4)
    for name in ("base", "global", "player"):
        assert abs(rep[f"{name}_auc_3.5"] - 0.5) < 0.06, f"{name} leaked"
    # synthetic league IS Poisson: φ must sit near 1, profiles near-useless
    assert 0.85 < rep["fit_last"]["phi_global"] < 1.15
