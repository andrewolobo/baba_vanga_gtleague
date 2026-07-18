"""Head-to-head pairwise features + the 1x2 stacking math (docs/H2H_FEATURE.md).

The Poisson GLM is additive in per-player factors, so a pairwise effect —
player A beats player B beyond what their field-wide rates imply — has no
term to land in. Measured 2026-07-13: that effect is real on this league
(+2.9 AUC pts on the 1x2 decisive score, surviving a long-run-skill control;
pair *pace* is worth +1.1…+1.7 pts on totals, surviving a player-pace
control). This league is unusually H2H-rich: 94 players, median 91 prior
meetings at kickoff, median rematch gap ~1 hour.

Two things live here:

- `H2HIndex` — per-pair history addressable by any time cutoff, the
  FormIndex contract: a meeting is visible only from its publish time
  (kickoff + lag), so a match can never see its own result. With ~1h rematch
  gaps the lag rule is load-bearing, not hygiene.
- the stacker — a logistic layer mapping (raw decisive score, H2H
  features) to an adjusted home-vs-away split. It reshapes only
  s = p_home/(p_home+p_away); p_draw is preserved exactly (the draw head was
  not part of the measurement and its calibration is fine). Fit and apply
  share one input: the pre-stack s off the row's λs, never a stored
  post-stack probability.

Feature perspectives: everything is from the HOME player's side. Win-edge
and goal-diff features are winner-signed (they cancel in a total); the pace
features are the totals-shaped pair signal (prior-meeting mean total vs the
league's running mean, both under the same visibility rule).

The totals serving path consumes `pace_decay` through the recal map, not a
stacker of its own: recal.fit_line_maps(..., h2h_idx=...) extends the
per-line Platt maps with a pace term (docs/TOTALS_H2H.md — the
no-second-layer decision). Nothing new lives here for it; `TOTALS_FEATURES`
above is that serving set.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from core.config import OFFSET_TOL_MIN
from core.markets import x12_probs
from model.blend import PMF_MAX_GOALS

# 7d matches the GLM's recency scale; the 1x2 signal turned out to live in
# the LIFETIME record and the totals signal in the DECAYED pace (measured —
# see docs/H2H_FEATURE.md), so both timescales are first-class.
H2H_HALF_LIFE_DAYS = 7.0
SHRINK_LIFE = 10.0   # pseudo-meetings toward 0 for lifetime features
SHRINK_DECAY = 2.0   # same, in decayed-count units

FEATURES = ("edge_life", "edge_decay", "gd_decay", "pace_life", "pace_decay")
# Serving sets, settled by the recorded 90-day sweep (model_runs kind h2h,
# 2026-07-13): the full winner triple beats the edge_life+gd_decay prune by
# ~0.2 pts at EVERY half-life — edge_decay's negative joint coefficient is a
# stable contrast against the lifetime edge, not noise. pace_decay alone
# matches the pace pair; pace_life carries nothing on totals.
X12_FEATURES = ("edge_life", "edge_decay", "gd_decay")
TOTALS_FEATURES = ("pace_decay",)

_PROB_EPS = 1e-6


def _logit(prob) -> np.ndarray:
    p = np.clip(prob, _PROB_EPS, 1 - _PROB_EPS)
    return np.log(p) - np.log1p(-p)


_ZERO = {"h2h_n": 0.0, **dict.fromkeys(FEATURES, 0.0)}


class H2HIndex:
    """Per-pair meeting history addressable by any time cutoff.

    Built once per cycle/eval-day from the canonical match frame
    (data.load_matches order). `features(home, away, cutoff)` sees only
    meetings published (kickoff + lag) at or before the cutoff; an unseen
    pair gets all-zero features — the degradation path, mirror of the
    unseen club.
    """

    def __init__(self, df: pd.DataFrame, lag_min: float = 20.0,
                 half_life: float = H2H_HALF_LIFE_DAYS,
                 shrink_life: float = SHRINK_LIFE,
                 shrink_decay: float = SHRINK_DECAY):
        self.half_life = half_life
        self.shrink_life = shrink_life
        self.shrink_decay = shrink_decay
        # UTC-naive datetime64 so lookups compare against Timestamp.to_datetime64()
        kick = df["kickoff_ts"].dt.tz_convert("UTC").dt.tz_localize(None)
        publish = (kick + pd.Timedelta(minutes=lag_min)).to_numpy()
        kick = kick.to_numpy()

        p1 = df[["home_player", "away_player"]].min(axis=1)
        home_is_p1 = (df["home_player"] == p1).to_numpy()
        flip = np.where(home_is_p1, 1.0, -1.0)
        gd = (df["home_ft"] - df["away_ft"]).to_numpy(dtype=float)
        sign_p1 = np.sign(gd) * flip
        total = (df["home_ft"] + df["away_ft"]).to_numpy(dtype=float)

        pair_key = p1 + "|" + df[["home_player", "away_player"]].max(axis=1)
        self._pairs: dict[str, tuple] = {}
        for key, idx in pair_key.groupby(pair_key).indices.items():
            # df is canonically kickoff-sorted, so slices stay sorted
            self._pairs[key] = (publish[idx], kick[idx], sign_p1[idx],
                                gd[idx] * flip[idx], total[idx])
        # league running mean total under the same visibility rule (df is
        # kickoff-sorted and the lag is constant, so publish is sorted too)
        self._league_publish = publish
        self._league_cum_total = np.cumsum(total)

    def league_mean_total(self, cutoff: np.datetime64) -> float:
        j = int(np.searchsorted(self._league_publish, cutoff, side="right"))
        return float(self._league_cum_total[j - 1] / j) if j > 0 else 0.0

    def features(self, home: str, away: str, cutoff) -> dict[str, float]:
        """H2H features from `home`'s perspective at `cutoff` (datetime64)."""
        key = f"{min(home, away)}|{max(home, away)}"
        e = self._pairs.get(key)
        if e is None:
            return dict(_ZERO)
        publish, kick, sign_p1, gd_p1, total = e
        k = int(np.searchsorted(publish, cutoff, side="right"))
        if k == 0:
            return dict(_ZERO)
        age_days = (cutoff - kick[:k]) / np.timedelta64(86400, "s")
        wgt = np.power(0.5, age_days / self.half_life)
        flip = 1.0 if home == min(home, away) else -1.0

        n, n_d = float(k), float(wgt.sum())
        diff = flip * float(sign_p1[:k].sum())
        diff_d = flip * float((wgt * sign_p1[:k]).sum())
        gd_d = flip * float((wgt * gd_p1[:k]).sum())
        m = self.league_mean_total(cutoff)
        return {
            "h2h_n": n,
            "edge_life": diff / (n + self.shrink_life),
            "edge_decay": diff_d / (n_d + self.shrink_decay),
            "gd_decay": gd_d / (n_d + self.shrink_decay),
            "pace_life": (float(total[:k].sum()) - n * m) / (n + self.shrink_life),
            "pace_decay": (float((wgt * total[:k]).sum()) - n_d * m)
                          / (n_d + self.shrink_decay),
        }

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Features for every row of a match frame, each at its own kickoff.
        A match never sees itself: its publish time is strictly after its
        kickoff (lag > 0), so searchsorted excludes it."""
        kick = (df["kickoff_ts"].dt.tz_convert("UTC").dt.tz_localize(None)
                .to_numpy())
        rows = [self.features(m.home_player, m.away_player, kick[i])
                for i, m in enumerate(df.itertuples())]
        out = pd.DataFrame(rows)
        out.insert(0, "match_id", df["match_id"].to_numpy())
        return out


@dataclass
class Stack:
    """Fitted 1x2 stacker: s' = sigmoid(b0 + b1·logit(s) + feats·b)."""
    features: tuple[str, ...]
    coef: np.ndarray  # [b0, b1, *b_feats]
    n_fit: int

    def apply(self, s, feats: np.ndarray):
        z = self.coef[0] + self.coef[1] * _logit(s) + feats @ self.coef[2:]
        return 1.0 / (1.0 + np.exp(-z))

    def apply_one(self, s: float, feats: dict[str, float]) -> float:
        """Serving convenience: one decisive share, features by name."""
        x = np.array([[feats[k] for k in self.features]])
        return float(self.apply(np.array([s]), x)[0])


def fit_stack(s: np.ndarray, feats: np.ndarray, y_home: np.ndarray,
              feature_names: tuple[str, ...]) -> Stack:
    """Fit on decisive outcomes only (caller filters): y_home in {0,1},
    s = the RAW (pre-stack) decisive share p_home/(p_home+p_away), feats
    the matching feature columns. With all-zero features this reduces to
    a Platt map of s."""
    x = np.column_stack([_logit(s), feats])
    lr = LogisticRegression(C=1e6, max_iter=1000).fit(x, y_home)
    coef = np.concatenate([[float(lr.intercept_[0])], lr.coef_[0]])
    return Stack(features=feature_names, coef=coef, n_fit=len(y_home))


def restack_x12(s_new: float, p_draw: float) -> tuple[float, float, float]:
    """(p_home, p_draw, p_away) with the decisive mass re-split by s_new.
    p_draw is preserved exactly; the triple still sums to 1."""
    return s_new * (1.0 - p_draw), p_draw, (1.0 - s_new) * (1.0 - p_draw)


# The served batch is the last one before kickoff — the definition settlement
# grades against (settle._served_x12). Populations are never pooled
# (docs/POPULATION_SPLIT.md): the priced query's fixtures join IS the
# population filter (gtl: event ids are not in fixtures); the schedule query
# routes through settlements_x12.matched_match_id. Player names on the priced
# side resolve through player_aliases exactly as the cycle's _alias does, so
# the features are keyed by the same canonical names the H2HIndex holds.
#
# The queries select the stored λs, NOT p_home/p_away: once the stacker
# serves, stored probs are post-stack ('-h2h' rows), and fitting on them
# would feed the stacker its own output — the exact closed loop the recal
# maps hit (docs/RECAL_SERVING.md, 2026-07-13). The raw decisive share is
# recomputed per row from the λs the row was served with.
_FIT_QUERY = """
    SELECT COALESCE(ah.player, f.home_player) AS home_player,
           COALESCE(aa.player, f.away_player) AS away_player,
           f.start_time_utc AS kickoff, s.result,
           p.lambda_home, p.lambda_away
    FROM settlements_x12 s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions_x12 p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions_x12
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    LEFT JOIN player_aliases ah ON ah.book_name = f.home_player
    LEFT JOIN player_aliases aa ON aa.book_name = f.away_player
    WHERE s.result != 'draw' AND s.settled_at >= ?
"""

_FIT_QUERY_SCHEDULE = """
    SELECT m.home_player, m.away_player, m.kickoff_ts AS kickoff,
           s.result, p.lambda_home, p.lambda_away
    FROM settlements_x12 s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions_x12 p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions_x12
                           WHERE event_id = s.event_id
                           AND predicted_at < m.kickoff_ts)
    WHERE s.event_id LIKE 'gtl:%' AND s.result != 'draw' AND s.settled_at >= ?
"""

_FIT_QUERIES = {"priced": _FIT_QUERY, "schedule": _FIT_QUERY_SCHEDULE}


def fit_stacker(conn: sqlite3.Connection, days: int, min_n: int,
                index: H2HIndex, population: str = "priced",
                now: datetime | None = None) -> Stack | None:
    """The serving stacker for one population, or None (identity) below
    engagement — the fit_line_maps shape (docs/RECAL_SERVING.md precedent).

    Fit on decisive settled 1x2 predictions in the window: s is the RAW
    decisive share recomputed from the served row's stored λs — the same
    quantity serving stacks — never the stored (post-stack on '-h2h' rows)
    probabilities, or the fit consumes its own output the way the recal
    maps did (docs/RECAL_SERVING.md, 2026-07-13). H2H features are
    re-derived at each row's kickoff − OFFSET_TOL_MIN — the same cutoff the
    last pre-kickoff batch (the graded one) priced with and the same one
    regen reconstructs, so fit, serve, and regen see one feature
    definition. Meetings only accrue, so a historical cutoff query against
    today's index reproduces what serving saw.

    Engagement: >= min_n decisive graded rows with both outcomes present.
    """
    if population not in _FIT_QUERIES:
        raise ValueError(f"unknown population: {population!r}")
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(_FIT_QUERIES[population], (since,)).fetchall()
    if len(rows) < min_n:
        return None
    raw = [x12_probs(r["lambda_home"], r["lambda_away"], PMF_MAX_GOALS)
           for r in rows]
    s = np.array([ph / max(ph + pa, _PROB_EPS) for ph, _pd, pa in raw])
    y = np.array([int(r["result"] == "home") for r in rows])
    if not 0 < y.sum() < len(y):  # degenerate window: no fit
        return None
    cuts = (pd.to_datetime([r["kickoff"] for r in rows], utc=True, format="ISO8601")
            .tz_localize(None) - pd.Timedelta(minutes=OFFSET_TOL_MIN))
    feats = np.array([
        [f[k] for k in X12_FEATURES]
        for f in (index.features(r["home_player"], r["away_player"],
                                 cuts[i].to_datetime64())
                  for i, r in enumerate(rows))])
    return fit_stack(s, feats, y, X12_FEATURES)
