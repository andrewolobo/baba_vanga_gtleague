"""Per-line monotone recalibration tests: the calibrators must undo a known
probability compression, be near-identity on already-calibrated input, and
the walk-forward report must inherit the no-leak invariant."""

import numpy as np

from model import evaluate
from tests.test_model import synthetic_df


def _compressed_probs(rng, n=20000, squeeze=0.6):
    """True Bernoulli probs plus a reported version compressed toward 0.5 —
    the exact shape of the measured tail underconfidence."""
    p_true = rng.uniform(0.05, 0.95, n)
    reported = 0.5 + squeeze * (p_true - 0.5)
    y = (rng.random(n) < p_true).astype(int)
    return p_true, reported, y


def test_platt_undoes_compression():
    rng = np.random.default_rng(11)
    p_true, reported, y = _compressed_probs(rng)
    ab = evaluate.fit_platt(reported[:10000], y[:10000])
    assert ab[0] > 1.2  # a > 1 == sharpening, the underconfident direction
    fixed = evaluate.apply_platt(ab, reported[10000:])
    brier_base = np.mean((reported[10000:] - y[10000:]) ** 2)
    brier_fix = np.mean((fixed - y[10000:]) ** 2)
    assert brier_fix < brier_base - 0.003
    assert np.mean(np.abs(fixed - p_true[10000:])) < 0.03


def test_platt_is_identity_on_calibrated_input():
    rng = np.random.default_rng(13)
    p_true = rng.uniform(0.05, 0.95, 20000)
    y = (rng.random(len(p_true)) < p_true).astype(int)
    a, b = evaluate.fit_platt(p_true, y)
    assert abs(a - 1.0) < 0.08 and abs(b) < 0.08


def test_platt_shared_pools_slope_and_fits_per_line_intercepts():
    """The hierarchical fit must recover the common slope from the pooled
    data and each line's own intercept — including a line far too thin to
    support a standalone 2-parameter fit."""
    rng = np.random.default_rng(29)
    a_true, b_true = 1.4, {3.5: -0.2, 5.5: 0.4}
    by_line = {}
    for line, n in ((3.5, 8000), (5.5, 150)):
        prob = rng.uniform(0.05, 0.95, n)
        s = np.log(prob) - np.log1p(-prob)
        p_cal = 1.0 / (1.0 + np.exp(-(a_true * s + b_true[line])))
        by_line[line] = (prob, (rng.random(n) < p_cal).astype(int))
    a, b = evaluate.fit_platt_shared(by_line)
    assert abs(a - a_true) < 0.1          # slope pinned by the pooled 8k
    assert abs(b[3.5] - b_true[3.5]) < 0.1
    assert abs(b[5.5] - b_true[5.5]) < 0.35  # 1 param from 150 samples


def test_recal_report_no_leak_and_no_damage():
    """No-signal league: recalibration must not conjure discrimination, and
    remapping already-calibrated probs must not materially hurt Brier."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=12)
    rep = evaluate.recal_report(preds, warmup_days=4)
    for name in ("base", "platt", "iso", "hier"):
        assert abs(rep[f"{name}_auc_3.5"] - 0.5) < 0.06, f"{name} leaked"
        assert rep[f"{name}_brier_3.5"] <= rep["base_brier_3.5"] + 0.005


def test_conditional_report_no_leak_per_line():
    """No-signal league: within each conditional line population there must
    be no discrimination (the pooled block mixes lines by construction, so
    the clean no-leak read is per line), and the recal arm must not damage
    Brier on already-calibrated probs."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=12)
    rep = evaluate.conditional_report(preds, warmup_days=4)
    for line, block in rep["by_line"].items():
        for arm in ("base", "recal"):
            if f"{arm}_auc" in block and block["n"] >= 200:
                assert abs(block[f"{arm}_auc"] - 0.5) < 0.08, f"{arm}@{line} leaked"
    assert rep["recal_brier"] <= rep["base_brier"] + 0.005
