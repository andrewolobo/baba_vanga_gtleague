"""Market math, pure functions only: any O/U line priced off a total-goals pmf,
push-band logic, margin-free implied probabilities.
"""

import math


def ou_probs(pmf: list[float], line: float) -> tuple[float, float, float]:
    """(p_over, p_push, p_under) for a goals line off a total-goals pmf.

    Half lines (3.5) have p_push == 0; integer lines (4.0) push on total == line.
    pmf[k] = P(total goals == k); a tail shortfall is treated as Over mass
    (the pmf should be built long enough that this is negligible).
    """
    if line < 0:
        raise ValueError(f"negative line {line}")
    is_int = float(line).is_integer()
    push_k = int(line) if is_int else None
    p_under = sum(p for k, p in enumerate(pmf) if k < line and k != push_k)
    p_push = pmf[push_k] if push_k is not None and push_k < len(pmf) else 0.0
    p_over = max(0.0, 1.0 - p_under - p_push)
    return p_over, p_push, p_under


def poisson_pmf(lam: float, max_goals: int) -> list[float]:
    """P(total == k) for k in 0..max_goals under Poisson(lam)."""
    return [math.exp(-lam) * lam**k / math.factorial(k) for k in range(max_goals + 1)]


def x12_probs(lam_home: float, lam_away: float,
              max_goals: int) -> tuple[float, float, float]:
    """(p_home, p_draw, p_away) from independent Poisson side scores.

    P(draw) = Σ_k p_h(k)·p_a(k);  P(home) = Σ_i p_h(i)·P(A < i);  away is the
    remainder, which also absorbs the negligible tail beyond max_goals.
    Independence is the same assumption the O/U pricing already makes by
    summing λs; whether it underprices draws HERE is an empirical question
    (docs/CLUB_FEATURE.md 1x2 track) — do not bolt on a Dixon-Coles diagonal
    until the walk-forward says the draw mass is actually short.
    """
    ph = poisson_pmf(lam_home, max_goals)
    pa = poisson_pmf(lam_away, max_goals)
    cdf_a = 0.0
    p_home = p_draw = 0.0
    for k in range(max_goals + 1):
        p_home += ph[k] * cdf_a  # P(away < k)
        p_draw += ph[k] * pa[k]
        cdf_a += pa[k]
    return p_home, p_draw, max(0.0, 1.0 - p_home - p_draw)


def margin_free(odds: list[float]) -> list[float]:
    """De-vig by proportional normalization: 1/odds scaled to sum to 1.

    The betPawa feed already carries margin-free probs (PRICE.10.1); this is
    the fallback for any market where they're missing.
    """
    raw = [1.0 / o for o in odds]
    s = sum(raw)
    return [r / s for r in raw]
