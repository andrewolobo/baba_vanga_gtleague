"""Per-line Platt recalibration of served totals probabilities.

Walk-forward validated 2026-07-09 (docs/FEATURE_IDEAS.md §combiner option 4,
`model_runs` kind='recal'): the blend's Poisson probability map is
underconfident in the tails (deciles −7.1/+4.9 at line 3.5); a per-line
monotone map p' = sigmoid(a·logit(p) + b) fit on settled predictions closes
it to ±2.9 with Brier improving at every line and AUC untouched. Fitted maps
were stable at a ≈ 1.21–1.27, |b| ≤ 0.2.

Serving contract (rollback notes in docs/RECAL_SERVING.md):
- maps are fit per half-line on the last `days` of settled predictions,
  only lines with ≥ min_n graded samples; anything else passes through
  untouched. Zero settled data -> empty maps -> byte-identical old behavior.
- hierarchical tier (2026-07-11): lines with min_n_line ≤ n < min_n borrow
  a pooled slope and fit only their own intercept (fit_platt_shared), so
  thin tail lines engage early. RECAL_MIN_N_LINE=0 disables just this tier.
- RECAL_ENABLED=false in the environment disables fitting entirely.
- rows priced through a map carry a '-recal2' model_version suffix
  ('-recal' = the 2026-07-11..07-13 fit generation, see below).
- population split (2026-07-11, docs/POPULATION_SPLIT.md): maps are fit per
  prediction population (priced vs schedule-only gtl:) and never pooled —
  the populations do not share a calibration curve.
- raw-probability fit basis (2026-07-13): maps are fit on the RAW pre-map
  p_over recomputed from each settled row's stored λs, never on the stored
  p_over — stored p_over is the post-map served value, and fitting on it
  makes the map consume its own output (fit windows fill with recal-tagged
  rows and the fitted slope decays toward identity; measured 2026-07-13,
  the priced shared slope had gone negative and was inverting picks).
  Fit and application now share one target: raw → truth.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression

from model.blend import totals_probs

_PROB_EPS = 1e-6  # logit clip; model probs never actually reach 0/1


def _logit(prob) -> np.ndarray:
    p = np.clip(prob, _PROB_EPS, 1 - _PROB_EPS)
    return np.log(p) - np.log1p(-p)


def fit_platt(prob: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Platt scaling: p' = sigmoid(a·logit(p) + b). a > 1 sharpens
    (the underconfident case); a = 1, b = 0 is the identity."""
    lr = LogisticRegression(C=1e6).fit(_logit(prob).reshape(-1, 1), y)
    return float(lr.coef_[0][0]), float(lr.intercept_[0])


def fit_platt_shared(by_line: dict[float, tuple[np.ndarray, np.ndarray]],
                     ) -> tuple[float, dict[float, float]]:
    """Joint fit of p'_L = sigmoid(a·logit(p) + b_L): ONE slope pooled across
    lines, one intercept per line.

    Measured 2026-07-11: the fitted per-line slope is nearly constant
    (a ≈ 1.21–1.32 at 2.5..5.5) while the intercept climbs with the line
    (−0.20 → +0.32) — the compression is shared, the bias is line-specific.
    Pooling pins `a` on the whole window, so a thin line only has to estimate
    its own 1-parameter intercept and becomes mappable at far smaller n than
    a standalone 2-parameter Platt fit."""
    lines = sorted(by_line)
    s = np.concatenate([_logit(by_line[li][0]) for li in lines])
    y = np.concatenate([by_line[li][1] for li in lines])
    grp = np.concatenate([np.full(len(by_line[li][1]), i)
                          for i, li in enumerate(lines)])
    x = np.column_stack([s] + [(grp == i).astype(float)
                               for i in range(len(lines))])
    lr = LogisticRegression(C=1e6, fit_intercept=False).fit(x, y)
    return (float(lr.coef_[0][0]),
            {li: float(lr.coef_[0][1 + i]) for i, li in enumerate(lines)})


def apply_platt(ab: tuple[float, float], prob):
    return 1.0 / (1.0 + np.exp(-(ab[0] * _logit(prob) + ab[1])))


# The served batch is the last prediction batch before kickoff — the same
# definition settlement grades against (settle._served_prediction).
#
# Population routing (docs/POPULATION_SPLIT.md): the two prediction
# populations do NOT share a calibration curve (measured 2026-07-11: priced
# a ≈ 0.14 vs schedule a ≈ 1.30, interaction CI clear of zero), so maps are
# fit per population and never pooled. The priced query joins through
# fixtures, which schedule (gtl:) events are not in — that join IS the
# population filter. The schedule query selects gtl events explicitly and
# routes through settlements.matched_match_id (indexed PK join).
#
# The queries select the stored λs, NOT p_over: the raw probability is
# recomputed per row (totals_probs is deterministic given λ and line, so
# the reconstruction is exact up to the 4dp λ storage rounding, ~2.5e-5
# in p). Reading p_over back would feed the fit its own output — see the
# module docstring.
_FIT_QUERY = """
    SELECT p.line, p.lambda_home, p.lambda_away, s.result_total
    FROM settlements s
    JOIN fixtures f ON f.event_id = s.event_id
    JOIN predictions p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                           WHERE event_id = s.event_id
                           AND predicted_at < f.start_time_utc)
    WHERE s.result_total IS NOT NULL AND s.leak_risk = 0
      AND s.settled_at >= ? AND p.lambda_home IS NOT NULL
"""

_FIT_QUERY_SCHEDULE = """
    SELECT p.line, p.lambda_home, p.lambda_away, s.result_total
    FROM settlements s
    JOIN matches m ON m.id = s.matched_match_id
    JOIN predictions p ON p.event_id = s.event_id
     AND p.predicted_at = (SELECT MAX(predicted_at) FROM predictions
                           WHERE event_id = s.event_id
                           AND predicted_at < m.kickoff_ts)
    WHERE s.event_id LIKE 'gtl:%' AND s.result_total IS NOT NULL
      AND s.leak_risk = 0 AND s.settled_at >= ? AND p.lambda_home IS NOT NULL
"""

_FIT_QUERIES = {"priced": _FIT_QUERY, "schedule": _FIT_QUERY_SCHEDULE}


def fit_line_maps(conn: sqlite3.Connection, days: int, min_n: int,
                  min_n_line: int = 0, now: datetime | None = None,
                  population: str = "priced",
                  ) -> dict[float, tuple[float, float]]:
    """{line: (a, b)} per half-line with enough graded, model-covered,
    leak-clean settled predictions in the window. Empty dict = identity.

    The fit's x is the RAW p_over recomputed from each row's stored λs —
    the same quantity serving applies the map to — never the stored
    (post-map) p_over. See the module docstring for the 2026-07-13 loop
    this closes.

    `population` selects which prediction population feeds the fit —
    'priced' (book fixtures, the original behavior) or 'schedule' (gtl:
    rows, settled since the population split Phase 1). The hierarchical
    tier below pools slope across *lines*, which stays valid within a
    population; pooling across populations is what the measurement forbids.

    Two engagement tiers:
    - n >= min_n: the line gets its own 2-parameter Platt fit (the original,
      validated behavior — unchanged).
    - min_n_line <= n < min_n (only when min_n_line > 0): the line borrows
      the slope from a joint shared-a/per-line-b fit across every line with
      >= min_n_line samples, provided that pool itself has >= min_n samples.
      This is how thin tail lines (4.5/5.5, which accrue settlements slowly)
      engage without waiting weeks; min_n_line = 0 restores per-line-only
      engagement exactly.
    """
    if population not in _FIT_QUERIES:
        raise ValueError(f"unknown population: {population!r}")
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(_FIT_QUERIES[population], (since,)).fetchall()
    by_line: dict[float, list] = {}
    for r in rows:
        line = float(r["line"])
        if line % 1.0 == 0.5:  # push-free lines only; integer lines pass through
            raw = totals_probs(r["lambda_home"], r["lambda_away"], line)[0]
            by_line.setdefault(line, []).append((raw, r["result_total"]))
    usable: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    for line, obs in by_line.items():
        prob = np.array([o[0] for o in obs])
        y = (np.array([o[1] for o in obs]) > line).astype(int)
        if 0 < y.sum() < len(y):  # both outcomes seen, else the fit is degenerate
            usable[line] = (prob, y)
    maps = {line: fit_platt(prob, y) for line, (prob, y) in usable.items()
            if len(y) >= min_n}
    if min_n_line > 0:
        pool = {line: v for line, v in usable.items() if len(v[1]) >= min_n_line}
        thin = [line for line in pool if line not in maps]
        if thin and sum(len(v[1]) for v in pool.values()) >= min_n:
            a, b = fit_platt_shared(pool)
            for line in thin:
                maps[line] = (a, b[line])
    return maps


def apply_to_line(maps: dict[float, tuple[float, float]], line: float,
                  p_over: float, p_push: float) -> tuple[float, float, float]:
    """Recalibrated (p_over, p_push, p_under); identity for unmapped lines.
    Maps only ever contain push-free half-lines, so p_push is preserved."""
    ab = maps.get(float(line))
    if ab is None:
        return p_over, p_push, max(0.0, 1.0 - p_over - p_push)
    p = float(apply_platt(ab, p_over))
    return p, p_push, max(0.0, 1.0 - p - p_push)
