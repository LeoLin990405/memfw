"""Discrete heavy-tail diagnostics using Clauset-style xmin and bootstrap GOF.

Power-law alpha is an exact numerical maximum-likelihood estimate under the
discrete zeta model. The Hurwitz-zeta normalizer is approximated by
Euler-Maclaurin summation. Lognormal comparison uses probability mass integrated
over integer bins and conditioned on ``x >= xmin``. Bootstrap samples combine
resampled empirical body values with simulated fitted discrete-tail values, and
each replicate reselects xmin (unless a fixed xmin was requested). Results are
diagnostics, not proof that a model is true; Vuong p-values are approximate for
this estimated, non-nested comparison.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class TailFit:
    sample_size: int
    excluded_nonpositive: int
    xmin: int
    status: str
    alpha: float | None
    ks_distance: float | None
    powerlaw_log_likelihood: float | None
    lognormal_log_likelihood: float | None
    log_likelihood_ratio: float | None
    gof_pvalue: float | None = None
    bootstrap_successes: int = 0
    vuong_z: float | None = None
    vuong_pvalue: float | None = None
    comparison: str = "insufficient_sample"
    bootstrap_clamped: int = 0
    estimator: str = "golden_section_discrete_zeta_mle"
    reason: str | None = None


@lru_cache(maxsize=100_000)
def _log_hurwitz_zeta(alpha: float, lower: int) -> float:
    """Log Euler-Maclaurin approximation, scaled to avoid tail underflow."""
    if alpha <= 1.0 or lower < 1:
        return math.inf
    n = 32
    end = float(lower + n)
    lower_float = float(lower)
    scaled_end = end / lower_float
    total = sum((1.0 + offset / lower_float) ** -alpha for offset in range(n))
    total += lower_float * scaled_end ** (1.0 - alpha) / (alpha - 1.0) + 0.5 * scaled_end**-alpha
    # B_2/(2!) and B_4/(4!), B_6/(6!), B_8/(8!), B_10/(10!).
    terms = (
        (1.0 / 12.0, 1),
        (-1.0 / 720.0, 3),
        (1.0 / 30240.0, 5),
        (-1.0 / 1209600.0, 7),
        (1.0 / 47900160.0, 9),
    )
    for coefficient, power in terms:
        rising = math.prod(alpha + value for value in range(power))
        total += coefficient * rising * lower_float**-power * scaled_end ** (-alpha - power)
    return -alpha * math.log(lower_float) + math.log(total)


def _hurwitz_zeta(alpha: float, lower: int) -> float:
    """Return the positive-support normalizer when representable as a float."""
    log_value = _log_hurwitz_zeta(alpha, lower)
    if log_value > math.log(float.fromhex("0x1.fffffffffffffp+1023")):
        return math.inf
    if log_value < math.log(float.fromhex("0x0.0000000000001p-1022")):
        return 0.0
    return math.exp(log_value)


def _alpha_mle(sample: list[int], xmin: int) -> float:
    sum_logs = sum(math.log(value) for value in sample)

    def likelihood(alpha: float) -> float:
        return -alpha * sum_logs - len(sample) * _log_hurwitz_zeta(alpha, xmin)

    left, right = 1.000001, 16.0
    while likelihood(right) > likelihood(right - 1.0) and right < 4096.0:
        right *= 2.0
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    first = right - ratio * (right - left)
    second = left + ratio * (right - left)
    first_ll, second_ll = likelihood(first), likelihood(second)
    for _ in range(96):
        if first_ll > second_ll:
            right, second, second_ll = second, first, first_ll
            first = right - ratio * (right - left)
            first_ll = likelihood(first)
        else:
            left, first, first_ll = first, second, second_ll
            second = left + ratio * (right - left)
            second_ll = likelihood(second)
    return (left + right) / 2.0


def _discrete_ks(sample: list[int], xmin: int, alpha: float) -> float:
    log_normalizer = _log_hurwitz_zeta(alpha, xmin)
    n = len(sample)
    statistic = 0.0
    index = 0
    while index < n:
        value = sample[index]
        end = index + 1
        while end < n and sample[end] == value:
            end += 1
        empirical_before = index / n
        empirical_at = end / n
        model_before = 1.0 - math.exp(_log_hurwitz_zeta(alpha, value) - log_normalizer)
        model_at = 1.0 - math.exp(_log_hurwitz_zeta(alpha, value + 1) - log_normalizer)
        statistic = max(
            statistic,
            abs(empirical_before - model_before),
            abs(empirical_at - model_at),
        )
        index = end
    return statistic


def _select_xmin(
    positive: list[int], minimum_sample: int, fixed_xmin: int | None = None
) -> tuple[int, list[int], float, float] | None:
    candidates = [fixed_xmin] if fixed_xmin is not None else sorted(set(positive))
    viable: list[tuple[int, list[int], float, float]] = []
    for threshold in candidates:
        if threshold is None:
            continue
        sample = [value for value in positive if value >= threshold]
        if len(sample) < minimum_sample or len(set(sample)) < 2:
            continue
        alpha = _alpha_mle(sample, threshold)
        if not math.isfinite(alpha) or alpha <= 1.0:
            continue
        viable.append((threshold, sample, alpha, _discrete_ks(sample, threshold, alpha)))
    return min(viable, key=lambda item: (item[3], item[0])) if viable else None


def _normal_cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def _lognormal_log_likelihood(sample: list[int], xmin: int) -> tuple[float, float, float]:
    log_values = [math.log(value) for value in sample]
    mu = sum(log_values) / len(log_values)
    sigma = max(
        math.sqrt(sum((value - mu) ** 2 for value in log_values) / len(log_values)),
        0.05,
    )

    def log_likelihood(candidate_mu: float, candidate_sigma: float) -> float:
        if candidate_sigma <= 0:
            return -math.inf
        cutoff_z = (math.log(xmin - 0.5) - candidate_mu) / candidate_sigma
        tail_probability = max(0.5 * math.erfc(cutoff_z / math.sqrt(2.0)), 1e-300)
        total = 0.0
        for value in sample:
            upper = (math.log(value + 0.5) - candidate_mu) / candidate_sigma
            lower = (math.log(value - 0.5) - candidate_mu) / candidate_sigma
            mass = max(_normal_cdf(upper) - _normal_cdf(lower), 1e-300)
            total += math.log(mass) - math.log(tail_probability)
        return total

    best = log_likelihood(mu, sigma)
    mu_step, log_sigma_step = max(sigma / 2.0, 0.1), 0.5
    log_sigma = math.log(sigma)
    for _ in range(120):
        improved = False
        for mu_delta, sigma_delta in (
            (mu_step, 0.0),
            (-mu_step, 0.0),
            (0.0, log_sigma_step),
            (0.0, -log_sigma_step),
        ):
            candidate_mu = mu + mu_delta
            candidate_log_sigma = log_sigma + sigma_delta
            candidate_sigma = math.exp(candidate_log_sigma)
            candidate_ll = log_likelihood(candidate_mu, candidate_sigma)
            if candidate_ll > best:
                mu, log_sigma, sigma, best, improved = (
                    candidate_mu,
                    candidate_log_sigma,
                    candidate_sigma,
                    candidate_ll,
                    True,
                )
        if not improved:
            mu_step *= 0.5
            log_sigma_step *= 0.5
        if mu_step < 1e-6 and log_sigma_step < 1e-6:
            break
    return best, mu, sigma


def _powerlaw_log_likelihood(sample: list[int], xmin: int, alpha: float) -> float:
    log_normalizer = _log_hurwitz_zeta(alpha, xmin)
    return sum(-alpha * math.log(value) - log_normalizer for value in sample)


def _vuong(
    sample: list[int], xmin: int, alpha: float, mu: float, sigma: float
) -> tuple[float | None, float | None, str]:
    z_cutoff = (math.log(xmin - 0.5) - mu) / sigma
    log_tail = math.log(max(0.5 * math.erfc(z_cutoff / math.sqrt(2.0)), 1e-300))
    zeta = _log_hurwitz_zeta(alpha, xmin)
    differences: list[float] = []
    for value in sample:
        power = -alpha * math.log(value) - zeta
        upper = (math.log(value + 0.5) - mu) / sigma
        lower = (math.log(value - 0.5) - mu) / sigma
        mass = max(_normal_cdf(upper) - _normal_cdf(lower), 1e-300)
        lognormal = math.log(mass) - log_tail
        differences.append(power - lognormal)
    mean = sum(differences) / len(differences)
    variance = sum((value - mean) ** 2 for value in differences) / (len(differences) - 1)
    if variance <= 1e-18:
        return None, None, "inconclusive"
    z_score = mean / math.sqrt(variance / len(differences))
    pvalue = math.erfc(abs(z_score) / math.sqrt(2.0))
    if pvalue >= 0.05:
        comparison = "inconclusive"
    else:
        comparison = "power_law" if z_score > 0 else "lognormal"
    return z_score, pvalue, comparison


def _sample_powerlaw_tail(
    rng: random.Random, count: int, xmin: int, alpha: float
) -> tuple[list[int], int]:
    if count <= 0:
        return [], 0
    log_normalizer = _log_hurwitz_zeta(alpha, xmin)
    maximum = 10**300
    draws: list[int] = []
    clamped = 0
    for _ in range(count):
        log_target_survival = math.log1p(-rng.random()) + log_normalizer
        low, high = xmin, xmin
        while high < maximum and _log_hurwitz_zeta(alpha, high + 1) > log_target_survival:
            low = high + 1
            high = min(maximum, high * 2)
        if _log_hurwitz_zeta(alpha, high + 1) > log_target_survival:
            draws.append(maximum)
            clamped += 1
            continue
        while low < high:
            middle = (low + high) // 2
            if _log_hurwitz_zeta(alpha, middle + 1) <= log_target_survival:
                high = middle
            else:
                low = middle + 1
        draws.append(low)
    return draws, clamped


def fit_discrete_tail(
    values: list[int],
    *,
    xmin: int | None = None,
    minimum_sample: int = 20,
    bootstrap_samples: int = 200,
    seed: int = 20261018,
) -> TailFit:
    """Fit a discrete tail, choose xmin by KS, and compare discrete alternatives.

    ``xmin`` fixes the cutoff when supplied. Otherwise each replicate and the
    observed sample select the cutoff with the smallest discrete KS distance,
    subject to ``minimum_sample``. Bootstrap resamples the empirical body and
    refits alpha and xmin for each parametric-tail replicate.
    """
    if xmin is not None and xmin < 1:
        raise ValueError("xmin must be positive")
    if minimum_sample < 2 or bootstrap_samples < 0:
        raise ValueError("minimum_sample must be at least 2 and bootstrap_samples non-negative")
    positive = sorted(value for value in values if value > 0)
    excluded = len(values) - len(positive)
    selected = _select_xmin(positive, minimum_sample, xmin)
    if selected is None:
        threshold = xmin if xmin is not None else (positive[0] if positive else 1)
        count = sum(value >= threshold for value in positive)
        return TailFit(
            count, excluded, threshold, "insufficient_sample", None, None, None, None, None
        )
    threshold, sample, alpha, ks = selected
    if alpha >= 4095.0:
        return TailFit(
            len(sample),
            excluded,
            threshold,
            "numerical_unavailable",
            None,
            None,
            None,
            None,
            None,
            reason="discrete exponent optimizer reached its numeric search boundary",
        )
    ll_power = _powerlaw_log_likelihood(sample, threshold, alpha)
    ll_lognormal, mu, sigma = _lognormal_log_likelihood(sample, threshold)
    llr = ll_power - ll_lognormal
    vuong_z, vuong_pvalue, comparison = _vuong(sample, threshold, alpha, mu, sigma)

    gof_pvalue: float | None = None
    successes = 0
    bootstrap_clamped = 0
    if bootstrap_samples:
        rng = random.Random(seed)
        body = [value for value in positive if value < threshold]
        tail_probability = len(sample) / len(positive)
        exceedances = 0
        for _ in range(bootstrap_samples):
            synthetic: list[int] = []
            tail_count = 0
            for _ in positive:
                if not body or rng.random() < tail_probability:
                    tail_count += 1
                else:
                    synthetic.append(rng.choice(body))
            tail_values, clamped = _sample_powerlaw_tail(rng, tail_count, threshold, alpha)
            bootstrap_clamped += clamped
            synthetic.extend(tail_values)
            synthetic.sort()
            replicate = _select_xmin(synthetic, minimum_sample, xmin)
            if replicate is None:
                continue
            successes += 1
            if replicate[3] >= ks:
                exceedances += 1
        if successes:
            gof_pvalue = (exceedances + 1) / (successes + 1)
    return TailFit(
        len(sample),
        excluded,
        threshold,
        "descriptive_approximation",
        alpha,
        ks,
        ll_power,
        ll_lognormal,
        llr,
        gof_pvalue,
        successes,
        vuong_z,
        vuong_pvalue,
        comparison,
        bootstrap_clamped,
    )
