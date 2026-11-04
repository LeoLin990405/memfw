"""Evaluation-only precision comparisons using exact finite-population sampling."""

from __future__ import annotations

import math


def _log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def hypergeometric_random_retention(
    *, population_size: int, important_count: int, retained_count: int, observed_important: int
) -> dict[str, int | float | list[float]]:
    """Return expectation, exact central 95% random-retention interval, and upper tail.

    The interval describes the distribution over uniformly random subsets of size k
    from a known finite population (N,K); it is not a confidence interval for an
    unknown population prevalence.
    """
    n, k_true, k = population_size, important_count, retained_count
    if n < 0 or not 0 <= k_true <= n or not 0 <= k <= n:
        raise ValueError("require 0 <= important_count, retained_count <= population_size")
    if observed_important < 0 or observed_important > k:
        raise ValueError("observed_important must be between 0 and retained_count")
    lower = max(0, k - (n - k_true))
    upper = min(k, k_true)
    if lower == upper:
        distribution = [(lower, 1.0)]
    else:
        logs = [
            _log_choose(k_true, x) + _log_choose(n - k_true, k - x) - _log_choose(n, k)
            for x in range(lower, upper + 1)
        ]
        max_log = max(logs)
        weights = [math.exp(value - max_log) for value in logs]
        total = sum(weights)
        distribution = [
            (x, weight / total) for x, weight in zip(range(lower, upper + 1), weights, strict=True)
        ]
    cumulative = 0.0
    q025 = lower
    q975 = upper
    found_lower = False
    for x, probability in distribution:
        cumulative += probability
        if cumulative >= 0.025 and not found_lower:
            q025 = x
            found_lower = True
        if cumulative >= 0.975:
            q975 = x
            break
    upper_tail = sum(probability for x, probability in distribution if x >= observed_important)
    precision_interval = [q025 / k, q975 / k] if k else [0.0, 0.0]
    return {
        "population_size": n,
        "important_count": k_true,
        "retained_count": k,
        "observed_important": observed_important,
        "expected_precision": k_true / n if n else 0.0,
        "expected_important_retained": k * k_true / n if n else 0.0,
        "random_retention_interval_95_precision": precision_interval,
        "random_retention_interval_95_count": [q025, q975],
        "randomization_upper_tail_probability": upper_tail,
    }
