"""Binomial statistics remain accurate at full benchmark sample sizes."""

from decimal import Decimal, localcontext
from fractions import Fraction
from math import comb

import pytest

from opencollab_eval.experiment.cell_report_statistics import binom_cdf, clopper_pearson


def _decimal_cdf(k, n, p):
    with localcontext() as ctx:
        ctx.prec = 60
        probability = Decimal(str(p))
        complement = 1 - probability
        term = complement ** n
        total = term
        for i in range(k):
            term *= Decimal(n - i) / Decimal(i + 1) * probability / complement
            total += term
        return float(total)


@pytest.mark.xfail(strict=True, reason="P2-09 binomial coefficients overflow float conversion")
@pytest.mark.parametrize("n", [1100, 1782])
def test_large_balanced_interval_matches_independent_decimal_tails(n):
    k = n // 2
    expected = 0.5 + float(Fraction(comb(n, k), 2 ** n)) / 2
    assert binom_cdf(k, n, 0.5) == pytest.approx(expected, abs=1e-11)
    low, high = clopper_pearson(k, n)
    assert 0 < low < 0.5 < high < 1
    assert low + high == pytest.approx(1, abs=1e-11)
    assert _decimal_cdf(k - 1, n, low) == pytest.approx(0.975, abs=1e-10)
    assert _decimal_cdf(k, n, high) == pytest.approx(0.025, abs=1e-10)


@pytest.mark.parametrize("n", [20, pytest.param(1782, marks=pytest.mark.xfail(
    strict=True, reason="P2-09 large boundary coefficients also overflow"
))])
def test_binomial_boundary_probabilities(n):
    assert binom_cdf(0, n, 0.0) == 1.0
    assert binom_cdf(n - 1, n, 1.0) == 0.0
    assert binom_cdf(n, n, 1.0) == 1.0
    assert binom_cdf(0, n, 0.001) == pytest.approx(0.999 ** n, rel=1e-12)
