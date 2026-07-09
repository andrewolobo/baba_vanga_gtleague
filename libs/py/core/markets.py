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


def margin_free(odds: list[float]) -> list[float]:
    """De-vig by proportional normalization: 1/odds scaled to sum to 1.

    The betPawa feed already carries margin-free probs (PRICE.10.1); this is
    the fallback for any market where they're missing.
    """
    raw = [1.0 / o for o in odds]
    s = sum(raw)
    return [r / s for r in raw]
