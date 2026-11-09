"""Sample size for a 5-point paired gap under a Holm gate.

The procedure is a paired percentile bootstrap. Each class comparison uses the
strictest Holm threshold, alpha / number of classes, which is Holm step 1.
The item model is a shared uniform latent: the alternative adds hits and does
not remove baseline hits. That keeps the gap variance small, so the resulting
n is a lower bound for noisier paired gaps. Observed intervals in later phases
are computed from the measured differences, not from this model.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass


def holm_alpha(n_hypotheses: int, alpha: float = 0.05) -> float:
    """Return Holm's first critical alpha for a family of ``n_hypotheses``."""

    if n_hypotheses < 1:
        raise ValueError("n_hypotheses must be positive")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    return alpha / n_hypotheses


def paired_differences(
    n: int,
    gap: float,
    rng: random.Random,
    *,
    baseline_rate: float = 0.5,
) -> list[float]:
    """Draw n paired score differences whose expectation equals ``gap``.

    A shared uniform latent makes the treatment hit whenever the baseline hits.
    Extra hits fall only in the band of width ``gap``.
    """

    if n < 1:
        raise ValueError("n must be positive")
    if not 0 < gap < 1:
        raise ValueError("gap must be between 0 and 1")
    if baseline_rate < 0 or baseline_rate + gap > 1:
        raise ValueError("baseline_rate + gap must stay inside [0, 1]")
    upper = baseline_rate + gap
    differences: list[float] = []
    for _ in range(n):
        latent = rng.random()
        baseline = 1.0 if latent < baseline_rate else 0.0
        alternative = 1.0 if latent < upper else 0.0
        differences.append(alternative - baseline)
    return differences


def bootstrap_mean_interval(
    values: list[float],
    *,
    alpha: float,
    n_bootstrap: int,
    rng: random.Random,
) -> tuple[float, float]:
    """Two-sided percentile bootstrap interval for the mean."""

    if not values:
        raise ValueError("values must be non-empty")
    if n_bootstrap < 20:
        raise ValueError("n_bootstrap must be at least 20")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    count = len(values)
    means = [sum(rng.choices(values, k=count)) / count for _ in range(n_bootstrap)]
    means.sort()
    low_index = int(math.floor((alpha / 2) * (n_bootstrap - 1)))
    high_index = int(math.ceil((1 - alpha / 2) * (n_bootstrap - 1)))
    high_index = min(n_bootstrap - 1, high_index)
    return means[low_index], means[high_index]


def estimate_power(
    n: int,
    *,
    gap: float,
    alpha: float,
    n_sim: int,
    n_bootstrap: int,
    seed: int,
    baseline_rate: float = 0.5,
) -> float:
    """Fraction of simulations whose interval lies entirely above 0."""

    if n_sim < 1:
        raise ValueError("n_sim must be positive")
    successes = 0
    for offset in range(n_sim):
        rng = random.Random(seed + offset * 1_000_003 + n)
        differences = paired_differences(n, gap, rng, baseline_rate=baseline_rate)
        lower, _upper = bootstrap_mean_interval(
            differences, alpha=alpha, n_bootstrap=n_bootstrap, rng=rng
        )
        if lower > 0:
            successes += 1
    return successes / n_sim


def select_sample_size(
    power_by_n: dict[int, float],
    *,
    power_target: float,
    minimum: int,
) -> tuple[int, bool]:
    """Pick the smallest n that meets the target. Never return below ``minimum``.

    The boolean is false when every simulated n is underpowered. The returned
    size is then the larger of ``minimum`` and the biggest simulated n.
    """

    if minimum < 1:
        raise ValueError("minimum must be positive")
    if not power_by_n:
        raise ValueError("power_by_n must be non-empty")
    if not 0 < power_target <= 1:
        raise ValueError("power_target must be in (0, 1]")
    meeting = sorted(n for n, power in power_by_n.items() if power >= power_target)
    if meeting:
        return max(minimum, meeting[0]), True
    return max(minimum, max(power_by_n)), False


@dataclass(frozen=True, slots=True)
class PowerResult:
    gap: float
    alpha: float
    n_hypotheses: int
    holm_alpha: float
    power_target: float
    baseline_rate: float
    n_bootstrap: int
    n_sim: int
    seed: int
    per_class: int
    estimated_power: float
    meets_power_target: bool
    power_by_n: dict[int, float]
    method: str


def required_per_class(
    *,
    gap: float = 0.05,
    alpha: float = 0.05,
    power_target: float = 0.80,
    n_hypotheses: int = 6,
    baseline_rate: float = 0.5,
    n_bootstrap: int = 400,
    n_sim: int = 200,
    minimum: int = 50,
    candidates: tuple[int, ...] | None = None,
    seed: int = 20261108,
    verbose: bool = False,
) -> PowerResult:
    """Return the per-class question count for a gap of ``gap``."""

    family_alpha = holm_alpha(n_hypotheses, alpha)
    sizes = candidates or (
        50,
        80,
        120,
        160,
        200,
        240,
        320,
        400,
        520,
        680,
        880,
    )
    power_by_n: dict[int, float] = {}
    for size in sizes:
        if size < 1:
            raise ValueError("candidate n must be positive")
        power_by_n[size] = estimate_power(
            size,
            gap=gap,
            alpha=family_alpha,
            n_sim=n_sim,
            n_bootstrap=n_bootstrap,
            seed=seed,
            baseline_rate=baseline_rate,
        )
        if verbose:
            print(
                f"power n={size} estimate={power_by_n[size]:.3f}",
                file=sys.stderr,
                flush=True,
            )
        if power_by_n[size] >= power_target and size >= minimum:
            break
    per_class, meets = select_sample_size(power_by_n, power_target=power_target, minimum=minimum)
    return PowerResult(
        gap=gap,
        alpha=alpha,
        n_hypotheses=n_hypotheses,
        holm_alpha=family_alpha,
        power_target=power_target,
        baseline_rate=baseline_rate,
        n_bootstrap=n_bootstrap,
        n_sim=n_sim,
        seed=seed,
        per_class=per_class,
        estimated_power=power_by_n.get(per_class, power_by_n[max(power_by_n)]),
        meets_power_target=meets and power_by_n.get(per_class, 0.0) >= power_target,
        power_by_n=power_by_n,
        method=(
            "paired percentile bootstrap of a monotone latent binary gap; "
            "each class is sized at Holm step-1 alpha"
        ),
    )


__all__ = [
    "PowerResult",
    "bootstrap_mean_interval",
    "estimate_power",
    "holm_alpha",
    "paired_differences",
    "required_per_class",
    "select_sample_size",
]
