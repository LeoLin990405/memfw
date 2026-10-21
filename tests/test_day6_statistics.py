from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from day6_statistics import hypergeometric_random_retention  # noqa: E402
from gen_day5 import generate_day6_dataset  # noqa: E402


def test_hypergeometric_random_retention_matches_small_exact_case() -> None:
    result = hypergeometric_random_retention(
        population_size=10, important_count=2, retained_count=2, observed_important=2
    )
    assert result["expected_precision"] == pytest.approx(0.2)
    assert result["expected_important_retained"] == pytest.approx(0.4)
    assert result["randomization_upper_tail_probability"] == pytest.approx(1 / 45)
    assert result["random_retention_interval_95_count"] == [0, 1]


def test_hypergeometric_degenerate_and_invalid_cases() -> None:
    result = hypergeometric_random_retention(
        population_size=10, important_count=10, retained_count=3, observed_important=3
    )
    assert result["random_retention_interval_95_count"] == [3, 3]
    with pytest.raises(ValueError):
        hypergeometric_random_retention(
            population_size=10, important_count=11, retained_count=3, observed_important=0
        )
    with pytest.raises(ValueError, match="strictly between"):
        generate_day6_dataset(count=20, important_prevalence=0.0)


@pytest.mark.parametrize("rho", [0.0, 0.3, 0.6])
def test_day6_generator_preserves_heavy_tail_marginal_and_reports_realized_rho(rho: float) -> None:
    records, labels, accesses, metadata = generate_day6_dataset(
        count=200, seed=20261018, rho_access=rho, important_prevalence=0.10
    )
    assert len(records) == 200
    assert len(accesses) == sum(row.access_count for row in records)
    assert metadata["access_count_marginal_preserved"] == 1
    assert metadata["important_prevalence_empirical"] == pytest.approx(0.1)
    assert metadata["distinct_last_access_times"] > 1
    assert metadata["accesses_after_creation"] is True
    actual = float(metadata["rho_access_empirical"])
    maximum = float(metadata["rho_access_attainable_max"])
    assert actual == pytest.approx(min(rho, maximum), abs=0.03)
    assert {row.importance for row in labels} <= {"important", "normal"}
    baseline, _, _, _ = generate_day6_dataset(
        count=200, seed=20261018, rho_access=0.0, important_prevalence=0.10
    )
    assert sorted(row.access_count for row in records) == sorted(
        row.access_count for row in baseline
    )
    again, _, _, again_metadata = generate_day6_dataset(
        count=200, seed=20261018, rho_access=rho, important_prevalence=0.10
    )
    assert [row.access_count for row in records] == [row.access_count for row in again]
    assert metadata == again_metadata
