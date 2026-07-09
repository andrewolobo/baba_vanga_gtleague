"""Time-weighted ridge Poisson attack/defense GLM — the base totals model.

Replicated structurally from the parent (plan §2.1.1): per-player attack and
defense rates, exponential recency weighting (half-life ~14 days, re-tuned in
Phase 4), ridge shrinkage (alpha ~0.02), home-side effect, goal cap. All
market probabilities read off the one total-goals distribution downstream.

log λ = intercept + home·is_home + att[player] + dfn[opponent]
Unseen players get att = dfn = 0, i.e. league-average rates (shrinkage target).
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.linear_model import PoissonRegressor

# Phase-4 walk-forward sweep on GT Leagues (2026-07): flat optimum across
# half-life 3.5–7d and alpha 0.005–0.01; mid-plateau values chosen. The league
# re-ranks ~2x faster than the parent's (14d / 0.02).
HALF_LIFE_DAYS = 7.0
ALPHA = 0.01
MAX_GOALS_FIT = 12


@dataclass
class PoissonModel:
    intercept: float
    home: float
    att: dict[str, float]
    dfn: dict[str, float]
    train_rows: int
    params: dict = field(default_factory=dict)

    @property
    def players(self) -> set[str]:
        return set(self.att)

    def side_lambda(self, player: str, opponent: str, is_home: bool) -> float:
        z = (self.intercept + (self.home if is_home else 0.0)
             + self.att.get(player, 0.0) + self.dfn.get(opponent, 0.0))
        return float(np.exp(z))

    def predict_sides(self, home_player: str, away_player: str) -> tuple[float, float]:
        return (self.side_lambda(home_player, away_player, True),
                self.side_lambda(away_player, home_player, False))


def fit(
    long_df: pd.DataFrame,
    ref_time: pd.Timestamp,
    half_life_days: float = HALF_LIFE_DAYS,
    alpha: float = ALPHA,
    max_goals: int = MAX_GOALS_FIT,
) -> PoissonModel:
    """Fit on side-rows strictly before ref_time (caller slices; re-asserted)."""
    d = long_df[long_df["kickoff_ts"] < ref_time]
    if d.empty:
        raise ValueError("no training rows before ref_time")

    players = sorted(set(d["player"]) | set(d["opponent"]))
    idx = {p: i for i, p in enumerate(players)}
    n, k = len(d), len(players)

    rows = np.arange(n)
    att_cols = d["player"].map(idx).to_numpy()
    dfn_cols = d["opponent"].map(idx).to_numpy() + k
    x = sparse.csr_matrix(
        (np.ones(2 * n),
         (np.concatenate([rows, rows]), np.concatenate([att_cols, dfn_cols]))),
        shape=(n, 2 * k),
    )
    x = sparse.hstack([d["is_home"].to_numpy().reshape(-1, 1), x], format="csr")

    age_days = (ref_time - d["kickoff_ts"]).dt.total_seconds() / 86400.0
    weights = np.power(0.5, age_days / half_life_days)
    y = d["goals_for"].clip(upper=max_goals).to_numpy(dtype=float)

    reg = PoissonRegressor(alpha=alpha, fit_intercept=True, max_iter=500)
    reg.fit(x, y, sample_weight=weights)

    return PoissonModel(
        intercept=float(reg.intercept_),
        home=float(reg.coef_[0]),
        att={p: float(reg.coef_[1 + i]) for p, i in idx.items()},
        dfn={p: float(reg.coef_[1 + k + i]) for p, i in idx.items()},
        train_rows=n,
        params={"half_life_days": half_life_days, "alpha": alpha,
                "max_goals": max_goals, "ref_time": str(ref_time)},
    )
