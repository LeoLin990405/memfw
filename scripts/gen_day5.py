#!/usr/bin/env python3
"""Generate shared Day 5 content, access history, labels, and rho-specific intensity."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gen_day4 import generate_dataset as generate_day4_dataset

SEED = 20261018
RHOS = (0.0, 0.3, 0.6, 0.9)
START = datetime(2026, 9, 18, tzinfo=UTC)
END = datetime(2026, 10, 18, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Day5Record:
    memory_id: str
    timestamp: str
    text: str
    capture_intensity: float
    access_count: int
    last_accessed_at: str


@dataclass(frozen=True, slots=True)
class Day5Label:
    memory_id: str
    theme: str
    importance: str


@dataclass(frozen=True, slots=True)
class AccessEvent:
    event_id: str
    memory_id: str
    timestamp: str


def _corr(left: list[float], right: list[float]) -> float:
    mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
    centered_left = [value - mean_left for value in left]
    centered_right = [value - mean_right for value in right]
    denominator = math.sqrt(
        sum(value * value for value in centered_left)
        * sum(value * value for value in centered_right)
    )
    return sum(a * b for a, b in zip(centered_left, centered_right, strict=True)) / denominator


def _correlated_intensities(
    labels: tuple[Day5Label, ...], rho: float, seed: int
) -> tuple[float, ...]:
    if not 0.0 <= rho <= 1.0:
        raise ValueError("rho must be in [0, 1]")
    binary = [float(row.importance == "important") for row in labels]
    if len(set(binary)) < 2:
        raise ValueError("controlled correlation requires both important and other labels")
    mean = sum(binary) / len(binary)
    sd = math.sqrt(sum((value - mean) ** 2 for value in binary) / len(binary))
    standardized = [(value - mean) / sd for value in binary]
    rng = random.Random(seed)
    noise = [rng.gauss(0.0, 1.0) for _ in labels]
    noise_mean = sum(noise) / len(noise)
    noise = [value - noise_mean for value in noise]
    projection = sum(a * b for a, b in zip(noise, standardized, strict=True)) / sum(
        value * value for value in standardized
    )
    residual = [a - projection * b for a, b in zip(noise, standardized, strict=True)]
    residual_sd = math.sqrt(sum(value * value for value in residual) / len(residual))
    residual = [value / residual_sd for value in residual]
    correlated = [
        rho * a + math.sqrt(max(0.0, 1.0 - rho * rho)) * b
        for a, b in zip(standardized, residual, strict=True)
    ]
    low, high = min(correlated), max(correlated)
    intensities = tuple(
        round(0.05 + 0.9 * (value - low) / (high - low), 12) for value in correlated
    )
    realized = _corr([float(value) for value in intensities], binary)
    if abs(realized - rho) > 1e-8:
        raise RuntimeError(
            f"intensity transform changed requested rho: requested={rho}, realized={realized}"
        )
    return intensities


def generate_dataset(
    *, count: int = 200, seed: int = SEED, rho: float = 0.6
) -> tuple[tuple[Day5Record, ...], tuple[Day5Label, ...], tuple[AccessEvent, ...]]:
    base, base_labels = generate_day4_dataset(seed=seed, count=count)
    # The imported Day 4 generator is kept separate from all rho-specific random draws.
    labels = tuple(Day5Label(row.memory_id, row.theme, row.importance) for row in base_labels)
    intensities = _correlated_intensities(labels, rho, seed ^ 0x5A17)
    access_rng = random.Random(seed ^ 0xA55A)
    records: list[Day5Record] = []
    events: list[AccessEvent] = []
    for index, (base_row, intensity) in enumerate(zip(base, intensities, strict=True)):
        # Pareto(alpha=1.35) retains a long tail with finite mean; no manual
        # salience label enters the access stream and no count cap is applied.
        access_count = max(1, int(access_rng.paretovariate(1.35)))
        created = START + timedelta(seconds=index * 60)
        remaining = max(1, int((END - created).total_seconds()))
        offsets = sorted(access_rng.randrange(0, remaining) for _ in range(access_count))
        last_access = created + timedelta(seconds=offsets[-1])
        for ordinal, offset in enumerate(offsets):
            events.append(
                AccessEvent(
                    event_id=f"access-{index:05d}-{ordinal:05d}",
                    memory_id=base_row.memory_id,
                    timestamp=(created + timedelta(seconds=offset)).isoformat(),
                )
            )
        records.append(
            Day5Record(
                memory_id=base_row.memory_id,
                timestamp=(START + timedelta(seconds=index * 60)).isoformat(),
                text=base_row.text,
                capture_intensity=intensity,
                access_count=access_count,
                last_accessed_at=last_access.isoformat(),
            )
        )
    events.sort(key=lambda row: (row.timestamp, row.event_id))
    return tuple(records), labels, tuple(events)


def generate_day6_dataset(
    *,
    count: int = 1_000,
    seed: int = SEED,
    rho_capture: float = 0.6,
    rho_access: float = 0.0,
    important_prevalence: float = 0.10,
) -> tuple[
    tuple[Day5Record, ...],
    tuple[Day5Label, ...],
    tuple[AccessEvent, ...],
    dict[str, float | int],
]:
    """Generate Day6 labels separately and reorder a fixed heavy-tail count sample.

    Only evaluation labels are used to construct the requested synthetic access
    correlation; policy inputs contain access events/counts but never label fields.
    The requested correlation is clamped to the attainable range of the fixed
    sampled marginal and the realized Pearson value is returned for reporting.
    """
    if not 0 < important_prevalence < 1:
        raise ValueError("important_prevalence must be strictly between 0 and 1")
    if not 0 <= rho_access <= 1:
        raise ValueError("rho_access must be in [0, 1]")
    records, original_labels, _old_accesses = generate_dataset(
        count=count, seed=seed, rho=rho_capture
    )
    label_rng = random.Random(seed ^ 0x6D91)
    important_n = min(count, max(0, round(count * important_prevalence)))
    important_ids = set(label_rng.sample([row.memory_id for row in records], important_n))
    labels = tuple(
        Day5Label(
            row.memory_id, row.theme, "important" if row.memory_id in important_ids else "normal"
        )
        for row in original_labels
    )
    capture_intensities = _correlated_intensities(labels, rho_capture, seed ^ 0x5A17)
    records = tuple(
        Day5Record(
            row.memory_id,
            row.timestamp,
            row.text,
            intensity,
            row.access_count,
            row.last_accessed_at,
        )
        for row, intensity in zip(records, capture_intensities, strict=True)
    )
    binary = [float(row.memory_id in important_ids) for row in records]
    sampled_counts = [row.access_count for row in records]
    assigned_counts, minimum_rho, maximum_rho = _assign_fixed_counts_at_rho(
        sampled_counts, binary, rho_access
    )
    access_rng = random.Random(seed ^ 0x8F43)
    output_records: list[Day5Record] = []
    events: list[AccessEvent] = []
    for row, access_count in zip(records, assigned_counts, strict=True):
        created = datetime.fromisoformat(row.timestamp)
        remaining = max(1, int((END - created).total_seconds()))
        offsets = sorted(access_rng.randrange(0, remaining) for _ in range(access_count))
        last_access = created + timedelta(seconds=offsets[-1])
        for ordinal, offset in enumerate(offsets):
            events.append(
                AccessEvent(
                    event_id=f"access-{row.memory_id}-{ordinal:05d}",
                    memory_id=row.memory_id,
                    timestamp=(created + timedelta(seconds=offset)).isoformat(),
                )
            )
        output_records.append(
            Day5Record(
                row.memory_id,
                row.timestamp,
                row.text,
                row.capture_intensity,
                access_count,
                last_access.isoformat(),
            )
        )
    events.sort(key=lambda item: (item.timestamp, item.event_id))
    actual_rho = _corr([float(value) for value in assigned_counts], binary)
    return (
        tuple(output_records),
        labels,
        tuple(events),
        {
            "rho_access_requested": rho_access,
            "rho_access_empirical": actual_rho,
            "rho_access_attainable_min": minimum_rho,
            "rho_access_attainable_max": maximum_rho,
            "rho_capture_requested": rho_capture,
            "rho_capture_empirical": _corr([row.capture_intensity for row in records], binary),
            "important_prevalence_requested": important_prevalence,
            "important_prevalence_empirical": important_n / count if count else 0.0,
            "access_count_marginal_preserved": 1,
            "distinct_last_access_times": len({row.last_accessed_at for row in output_records}),
            "accesses_after_creation": all(
                datetime.fromisoformat(row.timestamp)
                <= datetime.fromisoformat(row.last_accessed_at)
                for row in output_records
            ),
        },
    )


def _assign_fixed_counts_at_rho(
    values: list[int], binary: list[float], target_rho: float
) -> tuple[list[int], float, float]:
    if len(values) != len(binary) or not values:
        raise ValueError("values and binary labels must have matching non-empty lengths")
    positive_indices = [index for index, value in enumerate(binary) if value > 0.5]
    negative_indices = [index for index, value in enumerate(binary) if value <= 0.5]
    if not positive_indices or not negative_indices:
        raise ValueError("rho access requires both important and non-important records")
    sorted_values = sorted(values)
    minimum = sum(sorted_values[: len(positive_indices)])
    maximum = sum(sorted_values[len(sorted_values) - len(positive_indices) :])
    mean = sum(values) / len(values)
    positive_n = len(positive_indices)
    variance_sum = sum((value - mean) ** 2 for value in values)
    denom = math.sqrt(variance_sum * positive_n * len(negative_indices) / len(values))
    target_sum = positive_n * mean + max(-1.0, min(1.0, target_rho)) * denom
    target_sum = min(maximum, max(minimum, target_sum))
    assigned = [0] * len(values)
    low = sorted((values[index], index) for index in range(len(values)))
    selected_positive = [
        (value, index)
        for (value, _source_index), index in zip(low[:positive_n], positive_indices, strict=True)
    ]
    selected_negative = [
        (value, index)
        for (value, _source_index), index in zip(low[positive_n:], negative_indices, strict=True)
    ]
    for value, index in (*selected_positive, *selected_negative):
        assigned[index] = value
    current = minimum
    while current < target_sum:
        deficit = target_sum - current
        best_delta = 0
        best_pair: tuple[int, int, int, int] | None = None
        negatives_sorted = sorted(selected_negative)
        negative_values = [pair[0] for pair in negatives_sorted]
        for positive_value, positive_index in selected_positive:
            position = bisect.bisect_right(negative_values, positive_value + deficit) - 1
            if position < 0:
                continue
            negative_value, negative_index = negatives_sorted[position]
            delta = negative_value - positive_value
            if delta > best_delta:
                best_delta = delta
                best_pair = (positive_value, positive_index, negative_value, negative_index)
        if best_pair is None:
            break
        positive_value, positive_index, negative_value, negative_index = best_pair
        assigned[positive_index], assigned[negative_index] = negative_value, positive_value
        selected_positive.remove((positive_value, positive_index))
        selected_positive.append((negative_value, positive_index))
        selected_negative.remove((negative_value, negative_index))
        selected_negative.append((positive_value, negative_index))
        current += negative_value - positive_value
    low_rho = (minimum - positive_n * mean) / denom if denom else 0.0
    high_rho = (maximum - positive_n * mean) / denom if denom else 0.0
    return assigned, low_rho, high_rho


def write_dataset(
    data_output: Path,
    labels_output: Path,
    access_output: Path,
    *,
    count: int = 200,
    seed: int = SEED,
    rho: float = 0.6,
) -> dict[str, float | int]:
    records, labels, access_events = generate_dataset(count=count, seed=seed, rho=rho)
    for path in (data_output, labels_output, access_output):
        path.parent.mkdir(parents=True, exist_ok=True)
    for path, rows in (
        (data_output, records),
        (labels_output, labels),
        (access_output, access_events),
    ):
        path.write_text(
            "".join(
                json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows
            ),
            encoding="utf-8",
        )
    important = [float(row.importance == "important") for row in labels]
    intensities = [float(row.capture_intensity) for row in records]
    return {
        "seed": seed,
        "rho_requested": rho,
        "rho_empirical": _corr(intensities, important),
        "records": len(records),
        "important_records": int(sum(important)),
        "access_events": len(access_events),
        "access_count_max": max(row.access_count for row in records),
        "access_count_mean": sum(row.access_count for row in records) / len(records),
        "distinct_last_access_dates": len({row.last_accessed_at for row in records}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("reports/day5_synthetic.jsonl"))
    parser.add_argument("--labels", type=Path, default=Path("reports/day5_labels.jsonl"))
    parser.add_argument("--accesses", type=Path, default=Path("reports/day5_accesses.jsonl"))
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--rho", type=float, default=0.6)
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be at least 2")
    print(
        json.dumps(
            write_dataset(
                args.data,
                args.labels,
                args.accesses,
                count=args.count,
                seed=args.seed,
                rho=args.rho,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
