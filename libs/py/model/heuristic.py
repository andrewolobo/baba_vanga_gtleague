"""Short-memory recent-form totals heuristic — the blend's fresh leg.

Deliberately cheap (one groupby, refit every batch): per player, an
exponentially-weighted mean of goals scored and conceded over their most
recent matches. Freshness, not depth, is this leg's entire job (plan §2.1.3);
it must be refit on every results merge, using only results visible at the
prediction cutoff (the caller slices with data.as_of).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

FORM_SPAN = 8  # matches; EWM span for the short memory


@dataclass
class FormModel:
    att: dict[str, float]  # recent goals scored per match
    dfn: dict[str, float]  # recent goals conceded per match
    league_side_mean: float
    train_rows: int

    def side_lambda(self, player: str, opponent: str) -> float:
        att = self.att.get(player, self.league_side_mean)
        dfn = self.dfn.get(opponent, self.league_side_mean)
        return (att + dfn) / 2.0

    def predict_sides(self, home_player: str, away_player: str) -> tuple[float, float]:
        return (self.side_lambda(home_player, away_player),
                self.side_lambda(away_player, home_player))


def fit(long_df: pd.DataFrame, span: int = FORM_SPAN) -> FormModel:
    """long_df must already be sliced to results visible at the cutoff."""
    if long_df.empty:
        raise ValueError("no visible rows to fit form on")
    d = long_df.sort_values(["player", "kickoff_ts"])
    ew = d.groupby("player")[["goals_for", "goals_against"]].apply(
        lambda g: g.ewm(span=span).mean().iloc[-1]
    )
    return FormModel(
        att=ew["goals_for"].to_dict(),
        dfn=ew["goals_against"].to_dict(),
        league_side_mean=float(long_df["goals_for"].mean()),
        train_rows=len(long_df),
    )


class FormIndex:
    """Per-player recent-form state addressable by any time cutoff.

    Entry i for a player is their EWM attack/defense AFTER match i, visible
    from that match's publish time (kickoff + lag). A lookup at cutoff T
    returns the state after the player's last visible match — a match can
    never see its own result (publish > its own kickoff).
    """

    def __init__(self, long_df: pd.DataFrame, span: int = FORM_SPAN,
                 lag_min: float = 20.0):
        d = long_df.sort_values(["player", "kickoff_ts"])
        g = d.groupby("player")
        att = g["goals_for"].transform(lambda s: s.ewm(span=span).mean())
        dfn = g["goals_against"].transform(lambda s: s.ewm(span=span).mean())
        # UTC-naive datetime64 so lookups compare against Timestamp.to_datetime64()
        publish = (d["kickoff_ts"].dt.tz_convert("UTC").dt.tz_localize(None)
                   + pd.Timedelta(minutes=lag_min))
        self._by_player: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for p, i in g.indices.items():
            self._by_player[p] = (
                publish.iloc[i].to_numpy(),  # already player-sorted by time
                att.iloc[i].to_numpy(),
                dfn.iloc[i].to_numpy(),
            )

    def state(self, player: str, cutoff: np.datetime64) -> tuple[float, float] | None:
        e = self._by_player.get(player)
        if e is None:
            return None
        k = int(np.searchsorted(e[0], cutoff, side="right")) - 1
        return (float(e[1][k]), float(e[2][k])) if k >= 0 else None

    def side_lambda(self, player: str, opponent: str, cutoff, fallback: float) -> float:
        ps, os_ = self.state(player, cutoff), self.state(opponent, cutoff)
        att = ps[0] if ps else fallback
        dfn = os_[1] if os_ else fallback
        return (att + dfn) / 2.0
