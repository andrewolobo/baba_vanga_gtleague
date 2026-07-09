"""λ-blend of the day-frozen Poisson with the recent-form heuristic — the
served totals source (passed the Phase-4 gate: beats pure Poisson on AUC and
Brier at every line, though the margin is thin, ~+0.4 AUC pts).

Weight is the POISSON share. GT Leagues sweep optimum 0.7 (robust 0.6–0.8);
the parent's 0.5 favored form more because its results feed was staler —
here the blend edge is freshness-insensitive (lag 20 ≈ lag 60).
"""

from core.markets import ou_probs, poisson_pmf

TOTALS_BLEND_WEIGHT = 0.7
PMF_MAX_GOALS = 20  # pmf length for pricing; tail mass beyond is ~0 at these λ


def blend_sides(
    pois: tuple[float, float], form: tuple[float, float], w: float = TOTALS_BLEND_WEIGHT
) -> tuple[float, float]:
    return (w * pois[0] + (1 - w) * form[0], w * pois[1] + (1 - w) * form[1])


def totals_probs(lam_home: float, lam_away: float, line: float) -> tuple[float, float, float]:
    """(p_over, p_push, p_under) for any line off the one total-goals pmf."""
    pmf = poisson_pmf(lam_home + lam_away, PMF_MAX_GOALS)
    return ou_probs(pmf, line)
