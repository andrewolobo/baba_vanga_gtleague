"""Per-line monotone recalibration tests: the calibrators must undo a known
probability compression, be near-identity on already-calibrated input, and
the walk-forward report must inherit the no-leak invariant. The pace
extension (docs/TOTALS_H2H.md) is tested here too: the extended map must
recover a planted pair-pace signal, and every degradation path (no index,
plain map, zero pace) must reproduce the plain behavior exactly."""

import numpy as np
import pandas as pd
from scipy.stats import poisson as sp_poisson

from model import evaluate, h2h, recal
from tests.test_model import synthetic_df
from tests.test_predictor import (  # noqa: F401  (fixture reuse)
    NOW, _seed_settled_predictions, seeded)


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


def test_platt_pace_recovers_planted_coefficient():
    """The extended fit must recover all three parameters of a planted
    p' = sigmoid(a·logit(p) + b + c·pace) map."""
    rng = np.random.default_rng(31)
    n = 12000
    prob = rng.uniform(0.05, 0.95, n)
    pace = rng.normal(0.0, 1.5, n)
    s = np.log(prob) - np.log1p(-prob)
    p_true = 1.0 / (1.0 + np.exp(-(1.3 * s + 0.1 + 0.5 * pace)))
    y = (rng.random(n) < p_true).astype(int)
    abc = evaluate.fit_platt(prob, y, feat=pace)
    assert len(abc) == 3
    assert abs(abc[0] - 1.3) < 0.1
    assert abs(abc[1] - 0.1) < 0.1
    assert abs(abc[2] - 0.5) < 0.05


def test_platt_pace_zero_feature_reduces_to_plain():
    """An all-zero pace column has no gradient: c pins at 0, (a, b) match
    the plain fit, and applying either map gives the same probabilities —
    the unseen-pair degradation path needs no special casing."""
    rng = np.random.default_rng(11)
    _p_true, reported, y = _compressed_probs(rng, n=10000)
    ab = evaluate.fit_platt(reported, y)
    abc = evaluate.fit_platt(reported, y, feat=np.zeros(len(y)))
    assert abs(abc[0] - ab[0]) < 1e-4 and abs(abc[1] - ab[1]) < 1e-4
    assert abs(abc[2]) < 1e-6
    grid = np.linspace(0.05, 0.95, 19)
    assert np.allclose(evaluate.apply_platt(abc, grid, 0.0),
                       evaluate.apply_platt(ab, grid), atol=1e-4)


def test_platt_shared_pools_pace_coefficient():
    """With the feature column the shared fit pools c exactly like the
    slope: one pace coefficient across lines, per-line intercepts, and a
    thin line inherits both pooled parameters."""
    rng = np.random.default_rng(29)
    a_true, c_true, b_true = 1.4, 0.6, {3.5: -0.2, 5.5: 0.4}
    by_line = {}
    for line, n in ((3.5, 8000), (5.5, 150)):
        prob = rng.uniform(0.05, 0.95, n)
        pace = rng.normal(0.0, 1.5, n)
        s = np.log(prob) - np.log1p(-prob)
        p_cal = 1.0 / (1.0 + np.exp(-(a_true * s + b_true[line]
                                      + c_true * pace)))
        by_line[line] = (prob, (rng.random(n) < p_cal).astype(int), pace)
    a, b, c = evaluate.fit_platt_shared(by_line)
    assert abs(a - a_true) < 0.1
    assert abs(c - c_true) < 0.1
    assert abs(b[3.5] - b_true[3.5]) < 0.1
    assert abs(b[5.5] - b_true[5.5]) < 0.35


# ── pace extension through fit_line_maps (docs/TOTALS_H2H.md Phase 1) ───────

def _rigged_pace_index() -> h2h.H2HIndex:
    """An H2HIndex where one rematch pair runs hot (total 8 per meeting) and
    one cold (total 1) against a filler-pinned league mean of ~4.5, all
    meetings published well before the seeded fixtures' cutoffs."""
    rows, mid = [], 0
    base = pd.Timestamp("2026-01-12T10:00:00Z")
    for i in range(30):
        t = base + pd.Timedelta(hours=3 * i)
        for home, away, hf, af in (("HotA", "HotB", 5, 3),
                                   ("ColdA", "ColdB", 1, 0),
                                   ("F1", "F2", 2, 2), ("F3", "F4", 3, 2)):
            mid += 1
            rows.append({"match_id": mid, "kickoff_ts": t,
                         "home_player": home, "away_player": away,
                         "home_ft": hf, "away_ft": af})
    return h2h.H2HIndex(pd.DataFrame(rows))


def _seed_settled_pace(conn, n_events=400, line=3.5, seed=41, start=0):
    """Settled priced predictions all served the SAME λ (so raw probs carry
    zero discrimination) while outcomes follow each event's pair: hot pairs
    run over, cold pairs under. Only the pace column can explain the
    residual — a fit that ignores it must stay flat."""
    rng = np.random.default_rng(seed)
    lam_srv = 4.4
    fx, pr, st = [], [], []
    for i in range(n_events):
        hot = i % 2 == 0
        home, away = ("HotA", "HotB") if hot else ("ColdA", "ColdB")
        ev = f"PACE{start + i}"
        fx.append((ev, "2026-01-20T10:00:00+00:00", "GT Leagues", "x", "y",
                   home, away, "full",
                   "2026-01-19T00:00:00+00:00", "2026-01-19T00:00:00+00:00"))
        rep_p = float(sp_poisson.sf(int(line), lam_srv))
        pr.append((ev, "2026-01-20T09:50:00+00:00", "blend", line, rep_p,
                   0.0, 1 - rep_p, lam_srv / 2, lam_srv / 2, None, None,
                   None, 0, "test", "2026-01-20T09:45:00+00:00"))
        st.append((ev, None, 0.0, int(rng.poisson(5.6 if hot else 3.2)),
                   None, 0, None, None, "2026-01-25T12:00:00+00:00"))
    with conn:
        conn.executemany("INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?,?)", fx)
        conn.executemany(
            "INSERT INTO predictions (event_id, predicted_at, totals_source,"
            " line, p_over, p_push, p_under, lambda_home, lambda_away, pick,"
            " confidence, tier, value_flag, model_version, as_of_cutoff_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pr)
        conn.executemany("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)", st)


def test_fit_line_maps_pace_recovers_planted_signal(seeded):  # noqa: F811
    conn, _ = seeded
    _seed_settled_pace(conn)
    maps = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                               h2h_idx=_rigged_pace_index())
    assert set(maps) == {3.5}
    assert len(maps[3.5]) == 3
    assert maps[3.5][2] > 0.1  # hot pairs over / cold under: c must be positive
    # application: pace moves p_over its own way, p_push preserved exactly
    up = recal.apply_to_line(maps, 3.5, 0.5, 0.0, pace=1.0)
    dn = recal.apply_to_line(maps, 3.5, 0.5, 0.0, pace=-1.0)
    assert up[0] > dn[0]
    assert up[1] == 0.0 and abs(up[0] + up[2] - 1.0) < 1e-9


def test_fit_line_maps_pace_pools_c_for_thin_lines(seeded):  # noqa: F811
    """The thin tier borrows the pooled slope AND the pooled pace
    coefficient, fitting only its own intercept."""
    conn, _ = seeded
    _seed_settled_pace(conn)                                  # 3.5 x400
    _seed_settled_pace(conn, n_events=120, line=5.5, seed=43,
                       start=1000)  # thin
    maps = recal.fit_line_maps(conn, days=14, min_n=300, min_n_line=75,
                               now=NOW, h2h_idx=_rigged_pace_index())
    assert set(maps) == {3.5, 5.5}
    assert len(maps[5.5]) == 3
    assert maps[5.5][2] > 0.1  # inherited from the pool, not its own 120 rows


def test_fit_line_maps_h2h_degradation_paths(seeded):  # noqa: F811
    """h2h_idx=None is byte-identical to the plain call; an index whose
    pairs never met the fit rows' players yields an all-zero pace column,
    which reproduces the plain map with c pinned at 0."""
    conn, _ = seeded
    _seed_settled_predictions(conn)  # pairs PX/PY — unknown to the index
    plain = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW)
    assert plain == recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                                        h2h_idx=None)
    assert all(len(v) == 2 for v in plain.values())

    ext = recal.fit_line_maps(conn, days=14, min_n=300, now=NOW,
                              h2h_idx=_rigged_pace_index())
    assert set(ext) == set(plain)
    for line, (a, b, c) in ext.items():
        assert abs(a - plain[line][0]) < 1e-3
        assert abs(b - plain[line][1]) < 1e-3
        assert abs(c) < 1e-6  # zero column: no gradient, c never moves
        assert np.isclose(
            recal.apply_to_line(ext, line, 0.6, 0.0)[0],
            recal.apply_to_line(plain, line, 0.6, 0.0)[0], atol=1e-4)


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


def test_conditional_report_pace_arm_no_leak():
    """No-signal league + random pace: the pace arm must not conjure
    discrimination or damage Brier, and must report its fitted maps."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=12)
    pace = np.random.default_rng(7).normal(0.0, 1.5, len(preds))
    rep = evaluate.conditional_report(preds, warmup_days=4, pace=pace)
    for line, block in rep["by_line"].items():
        if "pace_auc" in block and block["n"] >= 200:
            assert abs(block["pace_auc"] - 0.5) < 0.08, f"pace@{line} leaked"
    assert rep["pace_brier"] <= rep["base_brier"] + 0.005
    assert rep["pace_last"]  # per-line (a, b, c) recorded for the pre-read
