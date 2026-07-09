import math

import pytest

from core.markets import margin_free, ou_probs, poisson_pmf


def test_poisson_pmf_sums_to_one():
    pmf = poisson_pmf(3.9, 30)
    assert math.isclose(sum(pmf), 1.0, abs_tol=1e-9)


def test_half_line_has_no_push():
    pmf = poisson_pmf(3.9, 30)
    over, push, under = ou_probs(pmf, 3.5)
    assert push == 0.0
    assert math.isclose(over + under, 1.0, abs_tol=1e-9)
    assert math.isclose(under, sum(pmf[:4]), abs_tol=1e-12)


def test_integer_line_pushes_on_exact_total():
    pmf = poisson_pmf(3.9, 30)
    over, push, under = ou_probs(pmf, 4.0)
    assert math.isclose(push, pmf[4], abs_tol=1e-12)
    assert math.isclose(over + push + under, 1.0, abs_tol=1e-9)


def test_probabilities_monotone_in_line():
    pmf = poisson_pmf(3.9, 30)
    overs = [ou_probs(pmf, line)[0] for line in (2.5, 3.5, 4.5, 5.5, 6.5)]
    assert overs == sorted(overs, reverse=True)


def test_negative_line_rejected():
    with pytest.raises(ValueError):
        ou_probs([1.0], -0.5)


def test_margin_free_normalizes():
    probs = margin_free([2.21, 4.9, 2.04])
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-12)
    assert probs[2] > probs[0] > probs[1]  # shorter odds -> higher prob
