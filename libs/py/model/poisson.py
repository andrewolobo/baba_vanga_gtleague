"""Time-weighted ridge Poisson attack/defense GLM — the base totals model.

Replicated structurally from the parent (plan §2.1.1): per-player attack and
defense rates, exponential recency weighting (half-life ~14 days, re-tuned in
Phase 4), ridge shrinkage (alpha ~0.02), home-side effect, goal cap. All
market probabilities read off the one total-goals distribution downstream.

log λ = intercept + home·is_home + att[player] + dfn[opponent]
Unseen players get att = dfn = 0, i.e. league-average rates (shrinkage target).

With with_club=True the club each side drove enters as a second entity:

log λ = intercept + home·is_home + att[player] + dfn[opponent]
                                 + catt[club]  + cdfn[opp_club]

Clubs rotate freely across players here, so the two factors are separately
identified by the joint fit — which is exactly why a *marginal* club rating
(Elo on raw results) is the wrong estimator: it would absorb player skill.
Measured worth: +0.59 AUC pts over the served blend, near-orthogonal to the
form leg (docs/CLUB_FEATURE.md). Unseen clubs get catt = cdfn = 0, so the
prediction degrades to the player-only λ rather than failing.

With with_tod=True a cyclic time-of-day term enters as a Fourier basis on the
UTC kickoff hour (docs/TOD_FEATURE.md — measured: the 03–05 UTC trough and
09–10/17–19 peaks survive a player-mix control and a year of split-half
stability):

    log λ = ... + Σ_k  s_k·sin(2πk·h/24) + c_k·cos(2πk·h/24)

The same hour applies to both sides of a match — it is a pace effect, not a
side effect. Jointly fitting it with player (and club) factors is what makes
it a *residual* hour effect rather than a proxy for who is on shift. A missing
hour contributes 0, so the prediction degrades to the hour-blind λ.
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
# The hourly profile has two peaks and one trough (docs/TOD_FEATURE.md), so
# K=1 cannot represent it; K=3 is the smallest basis that can. Swept in
# model.evaluate tod.
TOD_FOURIER_K = 3


def tod_basis(phase_hours, k: int = TOD_FOURIER_K) -> np.ndarray:
    """(n, 2k) matrix [sin(2π·1·h/24), cos(2π·1·h/24), sin(2π·2·h/24), ...].

    phase_hours is the fractional UTC hour (h + min/60), so :15/:30/:45
    kickoffs land between the integer hours instead of snapping back.
    """
    w = 2.0 * np.pi * np.asarray(phase_hours, dtype=float) / 24.0
    cols = []
    for j in range(1, k + 1):
        cols += [np.sin(j * w), np.cos(j * w)]
    return np.column_stack(cols)


def _phase_hours(ts: pd.Series) -> np.ndarray:
    return (ts.dt.hour + ts.dt.minute / 60.0).to_numpy(dtype=float)


@dataclass
class PoissonModel:
    intercept: float
    home: float
    att: dict[str, float]
    dfn: dict[str, float]
    train_rows: int
    params: dict = field(default_factory=dict)
    catt: dict[str, float] = field(default_factory=dict)
    cdfn: dict[str, float] = field(default_factory=dict)
    # Fourier coefficients [s1, c1, s2, c2, ...]; empty = fitted without tod.
    # NOTE: unpickle skips __init__, so artifacts serialized before this field
    # existed lack it — registry.get_poisson schema-checks for it on load.
    tod: list[float] = field(default_factory=list)

    @property
    def players(self) -> set[str]:
        return set(self.att)

    @property
    def clubs(self) -> set[str]:
        return set(self.catt)

    @property
    def with_club(self) -> bool:
        return bool(self.catt)

    @property
    def with_tod(self) -> bool:
        return bool(self.tod)

    def tod_z(self, hour: float) -> float:
        """log-λ contribution of the UTC kickoff hour (0 when fitted without)."""
        if not self.tod:
            return 0.0
        return float(tod_basis([hour], k=len(self.tod) // 2)[0] @ self.tod)

    def side_lambda(self, player: str, opponent: str, is_home: bool,
                    club: str | None = None, opp_club: str | None = None,
                    hour: float | None = None) -> float:
        z = (self.intercept + (self.home if is_home else 0.0)
             + self.att.get(player, 0.0) + self.dfn.get(opponent, 0.0)
             + self.catt.get(club, 0.0) + self.cdfn.get(opp_club, 0.0)
             + (self.tod_z(hour) if hour is not None else 0.0))
        return float(np.exp(z))

    def predict_sides(self, home_player: str, away_player: str,
                      home_club: str | None = None,
                      away_club: str | None = None,
                      hour: float | None = None) -> tuple[float, float]:
        return (self.side_lambda(home_player, away_player, True,
                                 home_club, away_club, hour),
                self.side_lambda(away_player, home_player, False,
                                 away_club, home_club, hour))

    def club_ratings(self) -> dict[str, dict[str, float]]:
        """Per club: the two axes the coefficients decompose into.

        pace     = catt + cdfn  — both sides score more; drives TOTALS
        strength = catt − cdfn  — score more, concede less; drives 1x2

        Measured on this league: pace dominates (sd 0.29 vs 0.15 goals/side),
        and corr(catt, cdfn) is *positive* — so a club that looks "weak" on
        raw goals scored is usually just low-pace, not bad. Any club table or
        Elo-style leaderboard should be derived from these, never fit
        independently on raw results.
        """
        return {c: {"catt": self.catt[c], "cdfn": self.cdfn[c],
                    "pace": self.catt[c] + self.cdfn[c],
                    "strength": self.catt[c] - self.cdfn[c]}
                for c in self.catt}


def fit(
    long_df: pd.DataFrame,
    ref_time: pd.Timestamp,
    half_life_days: float = HALF_LIFE_DAYS,
    alpha: float = ALPHA,
    max_goals: int = MAX_GOALS_FIT,
    with_club: bool = False,
    with_tod: bool = False,
    tod_k: int = TOD_FOURIER_K,
    club_scale: float = 1.0,
) -> PoissonModel:
    """Fit on side-rows strictly before ref_time (caller slices; re-asserted).

    with_club appends catt/cdfn blocks to the design matrix; it needs
    club/opp_club columns (data.long_format supplies them). With with_club
    False the design matrix is exactly the pre-club one, so λs are unchanged.

    club_scale gives the club block its own ridge strength through one global
    alpha: club indicator entries are club_scale instead of 1, so a club
    coefficient's effective penalty is alpha / club_scale² (>1 = shrink clubs
    less than players, <1 = more). Stored catt/cdfn are pre-multiplied back by
    club_scale, so prediction semantics never see the trick; club_scale=1.0 is
    exactly the unscaled fit.

    with_tod appends 2·tod_k Fourier columns on the fractional UTC kickoff
    hour, derived from kickoff_ts directly — no extra data column needed.
    Both side-rows of a match share the hour.
    """
    d = long_df[long_df["kickoff_ts"] < ref_time]
    if d.empty:
        raise ValueError("no training rows before ref_time")
    if with_club and not {"club", "opp_club"} <= set(d.columns):
        raise ValueError("with_club needs club/opp_club columns")

    players = sorted(set(d["player"]) | set(d["opponent"]))
    idx = {p: i for i, p in enumerate(players)}
    n, k = len(d), len(players)

    rows = np.arange(n)
    block_rows = [rows, rows]
    block_cols = [d["player"].map(idx).to_numpy(),
                  d["opponent"].map(idx).to_numpy() + k]
    width = 2 * k

    cidx: dict[str, int] = {}
    if with_club:
        clubs = sorted(set(d["club"]) | set(d["opp_club"]))
        cidx = {c: i for i, c in enumerate(clubs)}
        m = len(clubs)
        block_rows += [rows, rows]
        block_cols += [d["club"].map(cidx).to_numpy() + 2 * k,
                       d["opp_club"].map(cidx).to_numpy() + 2 * k + m]
        width = 2 * k + 2 * m

    values = [np.ones(n), np.ones(n)]
    if with_club:
        values += [np.full(n, club_scale), np.full(n, club_scale)]
    x = sparse.csr_matrix(
        (np.concatenate(values),
         (np.concatenate(block_rows), np.concatenate(block_cols))),
        shape=(n, width),
    )
    parts = [d["is_home"].to_numpy().reshape(-1, 1), x]
    if with_tod:  # dense trailing block; sin/cos are O(1) like the one-hots
        parts.append(tod_basis(_phase_hours(d["kickoff_ts"]), k=tod_k))
    x = sparse.hstack(parts, format="csr")

    age_days = (ref_time - d["kickoff_ts"]).dt.total_seconds() / 86400.0
    weights = np.power(0.5, age_days / half_life_days)
    y = d["goals_for"].clip(upper=max_goals).to_numpy(dtype=float)

    reg = PoissonRegressor(alpha=alpha, fit_intercept=True, max_iter=500)
    reg.fit(x, y, sample_weight=weights)

    c0 = 1 + 2 * k  # first club column, if any
    t0 = 1 + width  # first tod column, if any (tod block is always last)
    return PoissonModel(
        intercept=float(reg.intercept_),
        home=float(reg.coef_[0]),
        att={p: float(reg.coef_[1 + i]) for p, i in idx.items()},
        dfn={p: float(reg.coef_[1 + k + i]) for p, i in idx.items()},
        # ×club_scale: a scaled column's coefficient is the effect per
        # club_scale units — stored values must be the per-match contribution
        catt={c: float(reg.coef_[c0 + i]) * club_scale for c, i in cidx.items()},
        cdfn={c: float(reg.coef_[c0 + len(cidx) + i]) * club_scale
              for c, i in cidx.items()},
        tod=[float(v) for v in reg.coef_[t0:t0 + 2 * tod_k]] if with_tod else [],
        train_rows=n,
        params={"half_life_days": half_life_days, "alpha": alpha,
                "max_goals": max_goals, "ref_time": str(ref_time),
                "with_club": with_club, "with_tod": with_tod,
                "tod_k": tod_k if with_tod else None},
    )
