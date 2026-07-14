"""Double-Poisson sharpening head (FEATURE_IDEAS option 3).

The blend mean passed the Phase-4 gate but prices O/U off Poisson(λ_total)
while honest residuals are under-dispersed (Cameron–Trivedi t = −22), which
is exactly the measured tail underconfidence (deciles −6.5/+5.2). This head
keeps the mean untouched and models only dispersion:

    total ~ DP(μ = λ_blend, φ)   with  E ≈ μ,  Var ≈ μ/φ   (Efron 1986)

φ = 1 is Poisson; φ > 1 sharpens (under-dispersion). Two heads, both fit
walk-forward by MLE on previously-seen outcomes only:

- global:  one φ for the league.
- player:  log φ = b0 + b1·(x − 1), x = matchup variance profile from each
  player's rolling match-total std vs its Poisson-expected std (VarIndex).
  Beats the global head only if player dispersion heterogeneity is signal.
"""

import numpy as np
import pandas as pd
from scipy.optimize import minimize, minimize_scalar
from scipy.special import gammaln, logsumexp

PHI_BOUNDS = (0.4, 3.5)  # stability clamp; fitted values live well inside
VAR_WINDOW = 25          # matches in the rolling total-std profile
VAR_MIN_PERIODS = 10     # profile is NaN (caller falls back) below this


def dp_pmf(mu, phi, max_goals: int) -> np.ndarray:
    """P(total == k), k = 0..max_goals, one row per match; numerically
    normalized double-Poisson. mu and phi broadcast (scalar or length-n)."""
    mu = np.atleast_1d(np.asarray(mu, dtype=float))[:, None]
    phi = np.atleast_1d(np.asarray(phi, dtype=float))[:, None]
    y = np.arange(max_goals + 1, dtype=float)[None, :]
    ylogy = np.where(y > 0, y * np.log(np.maximum(y, 1.0)), 0.0)
    logp = (0.5 * np.log(phi) - phi * mu
            + (-y + ylogy - gammaln(y + 1))          # log(e^-y y^y / y!)
            + phi * (y + y * np.log(mu) - ylogy))    # φ·y·log(eμ/y)
    return np.exp(logp - logsumexp(logp, axis=1, keepdims=True))


def _mean_nll(mu: np.ndarray, totals: np.ndarray, phi, max_goals: int) -> float:
    pmf = dp_pmf(mu, phi, max_goals)
    picked = pmf[np.arange(len(totals)), np.minimum(totals, max_goals)]
    return float(-np.mean(np.log(np.maximum(picked, 1e-300))))


def fit_phi(mu: np.ndarray, totals: np.ndarray, max_goals: int = 20) -> float:
    """Global-head MLE: the single φ that best explains observed totals."""
    res = minimize_scalar(lambda f: _mean_nll(mu, totals, f, max_goals),
                          bounds=PHI_BOUNDS, method="bounded")
    return float(res.x)


def fit_phi_model(mu: np.ndarray, totals: np.ndarray, x: np.ndarray,
                  max_goals: int = 20) -> tuple[float, float]:
    """Player-head MLE: log φ_i = b0 + b1·(x_i − 1), x the matchup variance
    ratio (1 = Poisson-consistent). Returns (b0, b1); b1 ≈ 0 means player
    dispersion profiles carry nothing beyond the global head."""
    phi0 = fit_phi(mu, totals, max_goals)

    def nll(b):
        return _mean_nll(mu, totals, phi_from(b, x), max_goals)

    res = minimize(nll, x0=np.array([np.log(phi0), 0.0]), method="Nelder-Mead")
    return float(res.x[0]), float(res.x[1])


def phi_from(b: tuple[float, float], x: np.ndarray) -> np.ndarray:
    return np.clip(np.exp(b[0] + b[1] * (np.asarray(x, dtype=float) - 1.0)),
                   *PHI_BOUNDS)


class VarIndex:
    """Per-player rolling match-total dispersion, cutoff-addressable with the
    same publish-lag semantics as heuristic.FormIndex.

    Profile after match i: std/√mean of the player's last VAR_WINDOW match
    totals. √mean is the Poisson-expected std, so ratio 1 = Poisson-like,
    < 1 = consistent ("parks the bus"), > 1 = high-variance attacker.
    NaN (returned as None) until VAR_MIN_PERIODS matches.
    """

    def __init__(self, long_df: pd.DataFrame, window: int = VAR_WINDOW,
                 min_periods: int = VAR_MIN_PERIODS, lag_min: float = 20.0):
        d = long_df.sort_values(["player", "kickoff_ts"])
        totals = d["goals_for"] + d["goals_against"]
        g = totals.groupby(d["player"])
        std = g.transform(lambda s: s.rolling(window, min_periods=min_periods).std())
        mean = g.transform(lambda s: s.rolling(window, min_periods=min_periods).mean())
        ratio = std / np.sqrt(mean)
        publish = (d["kickoff_ts"].dt.tz_convert("UTC").dt.tz_localize(None)
                   + pd.Timedelta(minutes=lag_min))
        self._by_player: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for p, i in d.groupby("player").indices.items():
            self._by_player[p] = (publish.iloc[i].to_numpy(),
                                  ratio.iloc[i].to_numpy())

    def ratio(self, player: str, cutoff: np.datetime64) -> float | None:
        e = self._by_player.get(player)
        if e is None:
            return None
        k = int(np.searchsorted(e[0], cutoff, side="right")) - 1
        if k < 0 or np.isnan(e[1][k]):
            return None
        return float(e[1][k])
