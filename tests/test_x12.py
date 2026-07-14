"""1x2 measurement gate (docs/CLUB_FEATURE.md, 1x2 track).

The market is new but the hazards are the ones this repo already knows:
probabilities that don't sum to 1, features seeing their own match, and an
arm that silently prices without its feature. Plus one new one — the scalar
math in core.markets and the vectorized math in evaluate drifting apart.
"""

import numpy as np
import pytest

from core.markets import x12_probs
from model import evaluate
from test_club import with_clubs
from test_model import synthetic_df


def test_x12_probs_sum_to_one_and_bound():
    for lh, la in ((2.0, 2.0), (0.3, 4.5), (6.0, 0.1), (0.0, 0.0)):
        h, d, a = x12_probs(lh, la, 20)
        assert h >= 0 and d >= 0 and a >= 0
        assert h + d + a == pytest.approx(1.0, abs=1e-9)


def test_x12_probs_known_cases():
    # both λ = 0: the only outcome is 0-0, a draw
    assert x12_probs(0.0, 0.0, 20) == pytest.approx((0.0, 1.0, 0.0))
    # symmetric λ: home and away must mirror exactly
    h, d, a = x12_probs(2.3, 2.3, 20)
    assert h == pytest.approx(a, abs=1e-12)
    # a stronger home side must be favored, monotonically
    h1, *_ = x12_probs(2.0, 2.0, 20)
    h2, *_ = x12_probs(3.0, 2.0, 20)
    assert h2 > h1


def test_vectorized_matrix_matches_scalar():
    """evaluate._x12_matrix and markets.x12_probs are two implementations of
    one definition; a drift between them means serving and the gate disagree."""
    rng = np.random.default_rng(0)
    lam_h, lam_a = rng.uniform(0.2, 6.0, 25), rng.uniform(0.2, 6.0, 25)
    m = evaluate._x12_matrix(lam_h, lam_a)
    for i in range(25):
        assert m[i] == pytest.approx(x12_probs(lam_h[i], lam_a[i], 20),
                                     abs=1e-9)


def test_x12_leakage_canary_auc_is_half():
    """No-signal league: decisive AUC away from 0.5 means a leak."""
    df = with_clubs(synthetic_df(days=30, per_day=40))
    preds = evaluate.build_predictions(df, eval_days=10, with_club=True)
    rep = evaluate.x12_report(preds)
    for arm in ("blend", "blend+club"):
        assert abs(rep[f"{arm}_auc_decisive"] - 0.5) < 0.06, rep


def test_x12_club_signal_is_recovered():
    """When the club decides the winner (players identical), the club arm must
    dominate on decisive AUC and the player arm must stay near coin-flip."""
    rates = {p: 2.0 for p in [f"P{i}" for i in range(12)]}
    base = synthetic_df(days=30, per_day=40, rates=rates)
    df = with_clubs(base, club_rates={"Alpha": 4.5, "Beta": 4.0,
                                      "Gamma": 1.0, "Delta": 0.8})
    preds = evaluate.build_predictions(df, eval_days=10, with_club=True)
    rep = evaluate.x12_report(preds)
    assert rep["blend_auc_decisive"] < 0.60          # players carry ~nothing
    assert rep["blend+club_auc_decisive"] > 0.75     # club carries the winner
    assert rep["significant"], rep
    assert rep["blend+club_brier3"] < rep["blend_brier3"]
    assert rep["blend+club_brier3"] < rep["brier3_base"]


def test_x12_draw_mass_is_calibrated_on_poisson_league():
    """A synthetic league IS independent-Poisson, so predicted draw mass must
    match realized within noise — this pins the pmf math, and it is the
    baseline against which the real league's draw gap will be judged."""
    df = with_clubs(synthetic_df(days=30, per_day=40))
    preds = evaluate.build_predictions(df, eval_days=10, with_club=True)
    rep = evaluate.x12_report(preds)
    assert rep["blend+club_p_draw_mean"] == pytest.approx(
        rep["realized"]["draw"], abs=0.03)
