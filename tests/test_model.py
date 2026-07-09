"""Model hygiene tests: the leakage canary and the shift-invariant are the
two tests the parent project proved are worth their weight in gold."""

import numpy as np
import pandas as pd
import pytest

from model import data, evaluate, heuristic, poisson


def synthetic_df(days=25, per_day=40, players=None, rates=None, seed=7) -> pd.DataFrame:
    """League where each player scores Poisson(rate) regardless of opponent."""
    rng = np.random.default_rng(seed)
    players = players or [f"P{i}" for i in range(12)]
    rates = rates or {p: 2.0 for p in players}
    rows = []
    mid = 0
    for d in range(days):
        date = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=d)
        for k in range(per_day):
            h, a = rng.choice(players, 2, replace=False)
            kick = date + pd.Timedelta(minutes=15 * k)
            hf = int(rng.poisson(rates[h]))
            af = int(rng.poisson(rates[a]))
            rows.append({
                "match_id": mid, "source_match_id": str(mid),
                "date": date.date().isoformat(), "kickoff_ts": kick,
                "competition": "SYN", "home_player": h, "away_player": a,
                "home_ft": hf, "away_ft": af, "total": hf + af,
            })
            mid += 1
    return pd.DataFrame(rows)


def test_long_format_two_rows_per_match():
    df = synthetic_df(days=3)
    ld = data.long_format(df)
    assert len(ld) == 2 * len(df)
    m0 = ld[ld["match_id"] == df.iloc[0]["match_id"]]
    assert set(m0["is_home"]) == {0, 1}
    assert m0[m0["is_home"] == 1]["goals_for"].iloc[0] == df.iloc[0]["home_ft"]


def test_leakage_canary_auc_is_half():
    """No-signal league: any AUC away from 0.5 means a feature saw its own
    match's result (the parent's 46%-dupe bug presented exactly this way)."""
    df = synthetic_df(days=30, per_day=40)
    preds = evaluate.build_predictions(df, eval_days=10)
    for source in ("poisson", "blend"):
        m = evaluate.metrics(preds, source)
        assert abs(m["auc_3.5"] - 0.5) < 0.06, f"{source} leaked: {m['auc_3.5']}"


def test_signal_is_recovered():
    """A genuinely high-scoring player must be separable out of sample."""
    players = [f"P{i}" for i in range(10)]
    rates = {p: (4.0 if i < 5 else 1.0) for i, p in enumerate(players)}
    df = synthetic_df(days=30, per_day=40, players=players, rates=rates)
    preds = evaluate.build_predictions(df, eval_days=10)
    m = evaluate.metrics(preds, "poisson")
    assert m["auc_3.5"] > 0.75


def test_poisson_orders_players_by_rate():
    players = ["Hot", "Cold", "A", "B"]
    rates = {"Hot": 5.0, "Cold": 0.8, "A": 2.0, "B": 2.0}
    df = synthetic_df(days=20, per_day=30, players=players, rates=rates)
    ld = data.long_format(df)
    pm = poisson.fit(ld, ref_time=pd.Timestamp("2026-02-01", tz="UTC"))
    lam_hot, _ = pm.predict_sides("Hot", "A")
    lam_cold, _ = pm.predict_sides("Cold", "A")
    assert lam_hot > lam_cold
    assert 3.0 < lam_hot < 7.0  # shrunk toward but recognisably above mean


def test_form_index_cannot_see_own_match():
    df = synthetic_df(days=10, per_day=30)
    ld = data.long_format(df)
    idx = evaluate.FormIndex(ld, span=8, lag_min=20.0)
    p = ld.iloc[-1]["player"]
    last = ld[ld["player"] == p].iloc[-1]
    at_kickoff = idx.state(p, last["kickoff_ts"].to_datetime64())
    after_publish = idx.state(
        p, (last["kickoff_ts"] + pd.Timedelta(minutes=21)).to_datetime64())
    assert at_kickoff != after_publish  # own result only lands after publish


def test_form_model_refuses_empty():
    with pytest.raises(ValueError):
        heuristic.fit(pd.DataFrame(columns=["player", "kickoff_ts",
                                            "goals_for", "goals_against"]))


def test_unseen_player_gets_league_mean():
    df = synthetic_df(days=15)
    ld = data.long_format(df)
    pm = poisson.fit(ld, ref_time=pd.Timestamp("2026-02-01", tz="UTC"))
    lam_h, lam_a = pm.predict_sides("NEVER_SEEN", "ALSO_NEW")
    league = np.exp(pm.intercept)
    assert lam_a == pytest.approx(league, rel=1e-9)
    assert lam_h == pytest.approx(league * np.exp(pm.home), rel=1e-9)
