from __future__ import annotations

import math
import random

from memfw.stats.tail_fit import _hurwitz_zeta, _sample_powerlaw_tail, fit_discrete_tail


def test_hurwitz_zeta_matches_known_value_and_normalizes_discrete_tail() -> None:
    zeta_two = _hurwitz_zeta(2.0, 1)
    assert math.isclose(zeta_two, math.pi**2 / 6.0, rel_tol=1e-9)
    finite_mass = sum(value**-2.0 for value in range(1, 100_001))
    residual_mass = _hurwitz_zeta(2.0, 100_001)
    assert math.isclose((finite_mass + residual_mass) / zeta_two, 1.0, rel_tol=1e-10)
    large_alpha = _hurwitz_zeta(12.0, 1)
    assert math.isclose(large_alpha, sum(value**-12.0 for value in range(1, 100)), rel_tol=1e-12)
    reference = _hurwitz_zeta(3.0, 7)
    direct = sum(value**-3.0 for value in range(7, 200_001))
    direct += 200_001**-2 / 2.0 + 0.5 * 200_001**-3
    assert math.isclose(reference, direct, rel_tol=1e-10)


def test_discrete_powerlaw_bootstrap_is_deterministic_and_refits() -> None:
    generated, clamped = _sample_powerlaw_tail(random.Random(41), 800, 5, 2.4)
    assert clamped == 0
    result = fit_discrete_tail(
        generated, xmin=5, minimum_sample=100, bootstrap_samples=40, seed=812
    )
    assert result.status == "descriptive_approximation"
    assert result.sample_size == len(generated)
    assert result.alpha is not None and abs(result.alpha - 2.4) < 0.35
    assert result.gof_pvalue is not None
    assert result.gof_pvalue > 0.05
    assert result.comparison == "inconclusive"
    assert result.bootstrap_successes == 40
    assert result == fit_discrete_tail(
        generated, xmin=5, minimum_sample=100, bootstrap_samples=40, seed=812
    )
    assert result.estimator == "golden_section_discrete_zeta_mle"


def test_tail_fit_supports_fixed_and_automatically_selected_xmin() -> None:
    generated, clamped = _sample_powerlaw_tail(random.Random(72), 120, 4, 2.6)
    assert clamped == 0
    fixed = fit_discrete_tail(generated, xmin=1, minimum_sample=30, bootstrap_samples=0)
    automatic = fit_discrete_tail(generated, minimum_sample=30, bootstrap_samples=0)
    assert fixed.xmin == 1
    assert fixed.alpha is not None
    assert automatic.xmin >= 1
    assert automatic.alpha is not None
    assert fixed.estimator == automatic.estimator == "golden_section_discrete_zeta_mle"


def test_discrete_lognormal_synthetic_can_favor_lognormal_without_forcing() -> None:
    rng = random.Random(90210)
    values = [max(1, int(round(math.exp(rng.gauss(1.0, 1.0))))) for _ in range(1200)]
    result = fit_discrete_tail(values, xmin=2, minimum_sample=100, bootstrap_samples=20, seed=731)
    assert result.status == "descriptive_approximation"
    assert result.vuong_pvalue is not None
    assert result.gof_pvalue is not None and result.gof_pvalue < 0.05
    assert result.comparison == "lognormal"


def test_extreme_count_scale_returns_explicit_numeric_status() -> None:
    result = fit_discrete_tail([1_000_000, 1_000_001] * 20, bootstrap_samples=0)
    assert result.status == "numerical_unavailable"
    assert result.reason is not None
