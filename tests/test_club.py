"""Club as a second GLM entity (docs/CLUB_FEATURE.md).

Three invariants carry this feature:

  - with_club=False must reproduce the pre-club λ exactly. It is the rollback
    path and the reason the change is safe to land ahead of serving.
  - an unseen club must degrade to the player-only λ, never raise and never
    suppress a prediction. Regime switches (club comps -> World Cup) make
    ~all clubs cold for a week.
  - the leakage canary must hold on the club path too: on a league with no
    signal, AUC stays 0.5. Doubling the entity count doubles the ways to leak.
"""

import numpy as np
import pandas as pd
import pytest

from model import data, evaluate, poisson
from test_model import synthetic_df  # tests/ is on sys.path (no __init__.py)


def with_clubs(df: pd.DataFrame, clubs=("Alpha", "Beta", "Gamma", "Delta"),
               seed=11, club_rates=None) -> pd.DataFrame:
    """Assign a club to each side, rotated independently of the player.

    Rotation is what identifies the club factor: if club were nested in
    player, no joint fit could separate them.
    """
    rng = np.random.default_rng(seed)
    df = df.copy()
    df["home_club"] = rng.choice(clubs, len(df))
    df["away_club"] = rng.choice(clubs, len(df))
    if club_rates:  # re-draw goals so the club genuinely drives the rate
        df["home_ft"] = rng.poisson([club_rates[c] for c in df["home_club"]])
        df["away_ft"] = rng.poisson([club_rates[c] for c in df["away_club"]])
        df["total"] = df["home_ft"] + df["away_ft"]
    return df


def _fit(df, with_club):
    ld = data.long_format(df)
    return poisson.fit(ld, ref_time=pd.Timestamp("2026-02-01", tz="UTC"),
                       with_club=with_club)


def test_long_format_carries_clubs_when_present():
    df = with_clubs(synthetic_df(days=3))
    ld = data.long_format(df)
    assert {"club", "opp_club"} <= set(ld.columns)
    m0 = ld[ld["match_id"] == df.iloc[0]["match_id"]]
    home = m0[m0["is_home"] == 1].iloc[0]
    assert home["club"] == df.iloc[0]["home_club"]
    assert home["opp_club"] == df.iloc[0]["away_club"]


def test_long_format_without_clubs_roundtrips():
    """Synthetic frames have no club columns; they must still work."""
    ld = data.long_format(synthetic_df(days=3))
    assert "club" not in ld.columns


def test_without_club_is_identical_to_pre_club_fit():
    """The rollback invariant: club columns present, with_club=False, and the
    λs match a fit on a frame that never had clubs at all."""
    base = synthetic_df(days=15)
    clubbed = with_clubs(base)
    a, b = _fit(base, False), _fit(clubbed, False)
    assert not a.with_club and not b.with_club
    assert a.catt == {} and a.cdfn == {}
    for h, w in (("P0", "P1"), ("P3", "P7")):
        assert a.predict_sides(h, w) == pytest.approx(b.predict_sides(h, w),
                                                      rel=1e-12)


def test_unseen_club_falls_back_to_player_only_lambda():
    """Cold start must be a no-op, not an error and not a dropped fixture."""
    df = with_clubs(synthetic_df(days=15))
    cm = _fit(df, True)
    assert cm.with_club and cm.clubs == {"Alpha", "Beta", "Gamma", "Delta"}

    seen = cm.predict_sides("P0", "P1", "Alpha", "Beta")
    unseen = cm.predict_sides("P0", "P1", "NEVER_SEEN_FC", "ALSO_NEW_FC")
    no_club = cm.predict_sides("P0", "P1")
    assert unseen == pytest.approx(no_club, rel=1e-12)
    assert seen != pytest.approx(no_club, rel=1e-12)


def test_with_club_requires_club_columns():
    with pytest.raises(ValueError, match="club/opp_club"):
        _fit(synthetic_df(days=15), True)


def test_club_leakage_canary_auc_is_half():
    """No-signal league, club path on: AUC 0.5 for the club arm too."""
    df = with_clubs(synthetic_df(days=30, per_day=40))
    preds = evaluate.build_predictions(df, eval_days=10, with_club=True)
    assert {"lam_pc_h", "lam_pc_a", "club_cov"} <= set(preds.columns)
    rep = evaluate.club_report(preds)
    for line in evaluate.EVAL_LINES:
        assert abs(rep[f"blend+club_auc_{line}"] - 0.5) < 0.06, rep


def test_club_signal_is_recovered():
    """When the club (not the player) drives the rate, the club arm must beat
    the player-only arm. This is the inverse of the leakage canary: a model
    that cannot find a planted club effect cannot find a real one."""
    rates = {p: 2.0 for p in [f"P{i}" for i in range(12)]}
    base = synthetic_df(days=30, per_day=40, rates=rates)
    df = with_clubs(base, club_rates={"Alpha": 4.5, "Beta": 4.0,
                                      "Gamma": 1.0, "Delta": 0.8})
    preds = evaluate.build_predictions(df, eval_days=10, with_club=True)
    rep = evaluate.club_report(preds)
    line = evaluate.HEADLINE_LINE
    assert rep[f"blend+club_auc_{line}"] > rep[f"blend_auc_{line}"] + 0.10
    assert rep[f"blend+club_brier_{line}"] < rep[f"blend_brier_{line}"]


def test_club_ratings_decompose_into_pace_and_strength():
    """A high-pace club (both sides score) must rank high on pace and neutral
    on strength; the raw catt alone would mislabel it."""
    df = with_clubs(synthetic_df(days=20, per_day=40),
                    club_rates={"Alpha": 4.5, "Beta": 2.0,
                                "Gamma": 2.0, "Delta": 0.8})
    cm = _fit(df, True)
    r = cm.club_ratings()
    assert set(r) == {"Alpha", "Beta", "Gamma", "Delta"}
    assert r["Alpha"]["pace"] > r["Delta"]["pace"]
    for c, v in r.items():
        assert v["pace"] == pytest.approx(v["catt"] + v["cdfn"])
        assert v["strength"] == pytest.approx(v["catt"] - v["cdfn"])


def test_club_scale_one_is_identity():
    """club_scale=1.0 must reproduce the unscaled fit exactly — it is the
    serving configuration and the sweep's baseline."""
    df = with_clubs(synthetic_df(days=15))
    ld = data.long_format(df)
    ref = pd.Timestamp("2026-02-01", tz="UTC")
    a = poisson.fit(ld, ref_time=ref, with_club=True)
    b = poisson.fit(ld, ref_time=ref, with_club=True, club_scale=1.0)
    assert a.catt == b.catt and a.cdfn == b.cdfn


def test_club_scale_controls_shrinkage_not_semantics():
    """Larger scale = weaker penalty = larger fitted club effects, monotone.
    And the stored coefficients are effective contributions: predictions from
    a scaled fit must be exp-additive in catt exactly like an unscaled one
    (the ×scale re-multiplication is invisible downstream)."""
    df = with_clubs(synthetic_df(days=20, per_day=40),
                    club_rates={"Alpha": 4.5, "Beta": 2.0,
                                "Gamma": 2.0, "Delta": 0.8})
    ld = data.long_format(df)
    ref = pd.Timestamp("2026-02-01", tz="UTC")
    spread = {}
    for c in (0.25, 1.0, 4.0):
        pm = poisson.fit(ld, ref_time=ref, with_club=True, club_scale=c)
        spread[c] = float(np.std(list(pm.catt.values())))
        lam_with = pm.side_lambda("P0", "P1", True, "Alpha", "Delta")
        lam_wout = pm.side_lambda("P0", "P1", True)
        assert lam_with == pytest.approx(
            lam_wout * np.exp(pm.catt["Alpha"] + pm.cdfn["Delta"]), rel=1e-9)
    assert spread[0.25] < spread[1.0] < spread[4.0]


def test_sweep_club_harness_shape_and_baseline():
    """The sweep machinery itself: per-scale λ columns on identical rows, and
    on a strong club-signal league heavy shrinkage (c=0.25) must not beat the
    baseline — planted signal punishes over-shrinking, which is the direction
    the sweep exists to detect."""
    rates = {p: 2.0 for p in [f"P{i}" for i in range(12)]}
    df = with_clubs(synthetic_df(days=30, per_day=40, rates=rates),
                    club_rates={"Alpha": 4.5, "Beta": 4.0,
                                "Gamma": 1.0, "Delta": 0.8})
    scales = (0.25, 1.0)
    preds = evaluate.sweep_club_predictions(df, scales, eval_days=8)
    assert {"lam_c0.25_h", "lam_c1_h", "lam_f_h"} <= set(preds.columns)
    rep = evaluate.sweep_club_report(preds, scales)
    by_scale = {r["club_scale"]: r for r in rep}
    line = evaluate.HEADLINE_LINE
    assert by_scale[1.0][f"auc_{line}"] >= by_scale[0.25][f"auc_{line}"]
    assert f"dauc_ci_{line}" in by_scale[0.25]
    assert f"dauc_ci_{line}" not in by_scale[1.0]  # baseline has no CI vs itself
