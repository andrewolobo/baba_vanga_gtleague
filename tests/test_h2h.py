"""Pairwise H2H features + 1x2 stacking math (docs/H2H_FEATURE.md).

The invariants that carry every feature in this repo, in H2H shape:

  - visibility: a meeting enters the features only once published
    (kickoff + lag). Pairs here rematch ~hourly, so an off-by-one on the
    publish boundary is the likeliest real bug, not a theoretical one.
  - leakage canary: on a no-signal league every arm of the gate stays at
    AUC 0.5 and the dAUCs stay ~0.
  - planted-signal recovery: a rigged pairwise effect must be found by the
    +h2h/+pace arms AND missed by the control arms — the controls failing
    to move is part of the contract, it is what makes a pass pairwise.
  - degradation: an unseen pair gets all-zero features, and the stacker on
    zero features reduces to a monotone map of the served score.
  - draw mass: restacking preserves p_draw exactly and sums to 1.
"""

import numpy as np
import pandas as pd
import pytest

from model import data, evaluate, h2h
from test_model import synthetic_df  # tests/ is on sys.path (no __init__.py)


def _pair_dominators(df: pd.DataFrame, seed=11) -> dict[tuple, str]:
    """A deterministic dominator per unordered pair, ~half per player, so
    per-player marginals stay flat and the additive GLM has nothing to fit."""
    rng = np.random.default_rng(seed)
    pairs = sorted({tuple(sorted((h, a))) for h, a in
                    zip(df["home_player"], df["away_player"])})
    return {p: p[int(rng.integers(2))] for p in pairs}


def with_pair_signal(df: pd.DataFrame, seed=11) -> pd.DataFrame:
    """Redraw goals so the PAIR (not the player) decides the winner: the
    pair's dominator scores Poisson(2.8), the other side Poisson(1.2)."""
    rng = np.random.default_rng(seed)
    dom = _pair_dominators(df, seed)
    df = df.copy()
    is_dom = np.array([dom[tuple(sorted((h, a)))] == h
                       for h, a in zip(df["home_player"], df["away_player"])])
    df["home_ft"] = rng.poisson(np.where(is_dom, 2.8, 1.2))
    df["away_ft"] = rng.poisson(np.where(is_dom, 1.2, 2.8))
    df["total"] = df["home_ft"] + df["away_ft"]
    return df


def with_pair_pace(df: pd.DataFrame, seed=11) -> pd.DataFrame:
    """Redraw goals so the PAIR decides the tempo: high-pace pairs play
    Poisson(3.2) a side, low-pace pairs Poisson(1.2). Winner-neutral."""
    rng = np.random.default_rng(seed)
    dom = _pair_dominators(df, seed)  # reuse the balanced pair split
    df = df.copy()
    hot = np.array([dom[p := tuple(sorted((h, a)))] == p[0]
                    for h, a in zip(df["home_player"], df["away_player"])])
    lam = np.where(hot, 3.2, 1.2)
    df["home_ft"] = rng.poisson(lam)
    df["away_ft"] = rng.poisson(lam)
    df["total"] = df["home_ft"] + df["away_ft"]
    return df


# ---- H2HIndex: visibility and degradation


def test_unseen_pair_features_are_zero():
    idx = h2h.H2HIndex(synthetic_df(days=5))
    f = idx.features("NOBODY", "ALSO-NOBODY",
                     pd.Timestamp("2026-06-01").to_datetime64())
    assert f == {"h2h_n": 0.0, **{k: 0.0 for k in h2h.FEATURES}}


def test_meeting_is_invisible_until_published():
    """The FormIndex visibility contract on the pair path: a meeting at T
    enters at T + lag, not at T — and a match can never see its own result."""
    kick = pd.Timestamp("2026-01-01 12:00", tz="UTC")
    df = pd.DataFrame([{
        "match_id": 0, "date": "2026-01-01", "kickoff_ts": kick,
        "home_player": "A", "away_player": "B", "home_ft": 3, "away_ft": 0,
        "total": 3,
    }])
    idx = h2h.H2HIndex(df, lag_min=20.0)

    def at(minutes):
        return idx.features("A", "B",
                            (kick + pd.Timedelta(minutes=minutes))
                            .tz_localize(None).to_datetime64())

    assert at(0)["h2h_n"] == 0.0      # its own kickoff: invisible
    assert at(19)["h2h_n"] == 0.0     # played but unpublished: invisible
    seen = at(21)
    assert seen["h2h_n"] == 1.0       # published: visible
    assert seen["edge_life"] > 0 and seen["gd_decay"] > 0
    # and from the loser's perspective the signs flip
    assert idx.features("B", "A", (kick + pd.Timedelta(minutes=21))
                        .tz_localize(None).to_datetime64())["edge_life"] < 0


def test_decay_forgets_and_lifetime_does_not():
    kick = pd.Timestamp("2026-01-01 12:00", tz="UTC")
    df = pd.DataFrame([{
        "match_id": 0, "date": "2026-01-01", "kickoff_ts": kick,
        "home_player": "A", "away_player": "B", "home_ft": 2, "away_ft": 0,
        "total": 2,
    }])
    idx = h2h.H2HIndex(df, half_life=7.0)
    fresh = idx.features("A", "B", (kick + pd.Timedelta(hours=1))
                         .tz_localize(None).to_datetime64())
    stale = idx.features("A", "B", (kick + pd.Timedelta(days=70))
                         .tz_localize(None).to_datetime64())
    assert stale["edge_life"] == pytest.approx(fresh["edge_life"])
    assert 0 < stale["edge_decay"] < 0.05 * fresh["edge_decay"]


def test_edge_sign_tracks_planted_dominator():
    df = with_pair_signal(synthetic_df(days=20, per_day=40))
    dom = _pair_dominators(df, seed=11)
    idx = h2h.H2HIndex(df)
    end = (df["kickoff_ts"].max() + pd.Timedelta(days=1)) \
        .tz_localize(None).to_datetime64()
    hits = [np.sign(idx.features(a, b, end)["edge_life"]) == (1 if d == a else -1)
            for (a, b), d in dom.items()]
    assert np.mean(hits) > 0.9


def test_pace_sign_tracks_planted_tempo():
    df = with_pair_pace(synthetic_df(days=20, per_day=40))
    dom = _pair_dominators(df, seed=11)
    idx = h2h.H2HIndex(df)
    end = (df["kickoff_ts"].max() + pd.Timedelta(days=1)) \
        .tz_localize(None).to_datetime64()
    hits = [(idx.features(a, b, end)["pace_life"] > 0) == (d == a)
            for (a, b), d in dom.items()]
    assert np.mean(hits) > 0.9


# ---- the gate: canary and planted-signal recovery


def test_h2h_leakage_canary_all_arms_at_half():
    """No-signal league: every arm ~0.5 and no dAUC beyond noise. Any arm
    away from 0.5 means a feature saw its own match's result."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=10)
    rep = evaluate.h2h_report(preds, df)
    for a in ("base", "+h2h", "+skill", "+skill+h2h"):
        assert abs(rep["x12"][f"{a}_auc"] - 0.5) < 0.07, rep["x12"]
    line = f"{evaluate.HEADLINE_LINE:g}"
    assert abs(rep["totals"][line]["dauc_+pace"]) < 0.04, rep["totals"][line]


def test_x12_stack_recovers_planted_pairwise_signal():
    """Pair identity decides the winner, players are marginally identical:
    the served score must find ~nothing, +h2h must find it, and the +skill
    control must stay flat — that last assert is what makes it pairwise."""
    df = with_pair_signal(synthetic_df(days=30, per_day=40))
    preds = evaluate.build_predictions(df, eval_days=10)
    x12 = evaluate.h2h_report(preds, df)["x12"]
    # base is NOT asserted at 0.5: the random dominator split leaves a small
    # binomial per-player skill imbalance the GLM legitimately prices. The
    # contract is the increments.
    assert x12["dauc_+h2h"] > 0.15 and x12["significant_+h2h"]
    assert x12["dauc_+skill"] < 0.05
    assert x12["pairwise_dauc"] > 0.15 and x12["pairwise_significant"]


def test_totals_stack_recovers_planted_pair_pace():
    """Pair identity decides the tempo: +pace must find it at the headline
    line and the player-pace control must stay flat."""
    df = with_pair_pace(synthetic_df(days=30, per_day=40))
    preds = evaluate.build_predictions(df, eval_days=10)
    rec = evaluate.h2h_report(preds, df)["totals"][f"{evaluate.HEADLINE_LINE:g}"]
    assert rec["dauc_+pace"] > 0.10 and rec["significant_+pace"]
    assert rec["dauc_+ppace"] < 0.05
    assert rec["pairwise_dauc"] > 0.10 and rec["pairwise_significant"]


# ---- stacker math


def test_stack_on_zero_features_is_monotone_platt_of_score():
    rng = np.random.default_rng(3)
    s = rng.uniform(0.2, 0.8, 400)
    y = (rng.uniform(size=400) < s).astype(int)
    st = h2h.fit_stack(s, np.zeros((400, 2)), y, ("edge_life", "gd_decay"))
    out = st.apply(s, np.zeros((400, 2)))
    order = np.argsort(s)
    assert (np.diff(out[order]) >= -1e-12).all()  # monotone in s
    assert st.n_fit == 400 and st.features == ("edge_life", "gd_decay")


def test_restack_preserves_draw_mass_and_sums_to_one():
    for p_draw in (0.0, 0.18, 0.4):
        ph, pd_, pa = h2h.restack_x12(0.61, p_draw)
        assert pd_ == p_draw
        assert ph + pd_ + pa == pytest.approx(1.0)
        assert ph == pytest.approx(0.61 * (1 - p_draw))
    # a positive-edge feature must move the split toward home
    st = h2h.Stack(features=("edge_life",), coef=np.array([0.0, 1.0, 2.0]),
                   n_fit=1)
    lo = st.apply(np.array([0.5]), np.array([[-0.3]]))[0]
    hi = st.apply(np.array([0.5]), np.array([[+0.3]]))[0]
    assert lo < 0.5 < hi
