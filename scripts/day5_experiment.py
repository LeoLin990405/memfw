#!/usr/bin/env python3
"""Run deterministic rho × wake-threshold capture diagnostics offline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gen_day5 import RHOS, SEED, Day5Label, Day5Record, _corr, generate_dataset, write_dataset  # noqa: E402

from memfw.budget import BudgetLimits  # noqa: E402
from memfw.capture import CaptureSession, TimedSignal  # noqa: E402
from memfw.core.models import CaptureState, EventKind  # noqa: E402
from memfw.core.repository import Repository  # noqa: E402
from memfw.policies import PolicyCandidate, PolicyName, make_policy  # noqa: E402
from memfw.stats.keyness import KeynessScorer, observable_strength  # noqa: E402
from memfw.stats.runtime import measure_physical_breakdown, measure_vault  # noqa: E402
from memfw.stats.tail_fit import fit_discrete_tail  # noqa: E402

THRESHOLDS = (0.35, 0.5, 0.65, 0.8)
RING_SECONDS = 1800.0
LOW_THRESHOLD_FRACTION = 0.8
STANDBY_SECONDS = 180.0
DORMANT_SECONDS = 3600.0


def _id_factory(prefix: str) -> Callable[[], str]:
    sequence = count(1)
    return lambda: f"{prefix}-{next(sequence):08d}"


def _capture(
    records: tuple[Day5Record, ...],
    root: Path,
    *,
    threshold: float | None,
    run_id: str,
) -> tuple[Repository, CaptureSession]:
    repo = Repository(
        root, clock=lambda: datetime(2026, 10, 18, tzinfo=UTC), id_factory=_id_factory(run_id)
    )
    repo.init()
    session = CaptureSession(
        repository=repo,
        session_id=run_id,
        initial_state=CaptureState.DORMANT,
        high_threshold=threshold if threshold is not None else 0.8,
        low_threshold=(threshold if threshold is not None else 0.8) * LOW_THRESHOLD_FRACTION,
        standby_seconds=STANDBY_SECONDS,
        dormant_seconds=DORMANT_SECONDS,
        ring_seconds=RING_SECONDS,
    )
    if threshold is None:
        session.feed(
            TimedSignal(
                timestamp=datetime.fromisoformat(records[0].timestamp),
                source="human",
                action="remember",
                intensity=1.0,
            )
        )
    for row in records:
        session.feed(
            TimedSignal(
                timestamp=datetime.fromisoformat(row.timestamp),
                source="model",
                action="content",
                intensity=row.capture_intensity,
                text=row.text,
                source_id=row.memory_id,
            )
        )
    end = datetime.fromisoformat(records[-1].timestamp) + timedelta(seconds=60)
    session.advance_to(end)
    return repo, session


def _cell(
    records: tuple[Day5Record, ...],
    labels: tuple[Day5Label, ...],
    root: Path,
    *,
    rho: float,
    threshold: float,
    always_on_bytes: int,
) -> dict[str, Any]:
    truth = {row.memory_id: row.importance for row in labels}
    important_total = sum(value == "important" for value in truth.values())
    noise_total = sum(value == "noise" for value in truth.values())
    repo, session = _capture(
        records, root, threshold=threshold, run_id=f"d5-r{int(rho * 10)}-t{int(threshold * 100)}"
    )
    captured = {item.source_id for item in session.items() if item.source_id}
    captured_important = sum(truth.get(identifier) == "important" for identifier in captured)
    captured_noise = sum(truth.get(identifier) == "noise" for identifier in captured)
    wakes = [event for event in session.events if event.kind == EventKind.CAPTURE_WAKE]
    record_by_time = {datetime.fromisoformat(row.timestamp): row for row in records}
    labeled_wakes = [
        truth[record_by_time[event.timestamp].memory_id]
        for event in wakes
        if event.timestamp in record_by_time
    ]
    event_rows = repo.events()
    candidates = {item.candidate_id: item for item in session.items(include_decided=True)}
    backfilled_ids = {
        candidate.source_id
        for event in event_rows
        if event.kind == EventKind.CAPTURE_RECORD
        and isinstance(event.details.get("candidate_id"), str)
        and (candidate := candidates.get(str(event.details["candidate_id"]))) is not None
        and candidate.source_id is not None
        and event.timestamp > candidate.timestamp
        and (event.timestamp - candidate.timestamp).total_seconds() <= RING_SECONDS
    }
    physical = measure_physical_breakdown(repo)
    physical_total = sum(physical.values())
    measured_total = measure_vault(repo).physical_vault_bytes
    if physical_total != measured_total:
        raise RuntimeError("allocated physical categories do not sum to physical vault total")
    elapsed = (
        datetime.fromisoformat(records[-1].timestamp)
        + timedelta(seconds=60)
        - datetime.fromisoformat(records[0].timestamp)
    ).total_seconds()
    full_seconds = 0.0
    last_time = datetime.fromisoformat(records[0].timestamp)
    prior_state = CaptureState.DORMANT
    for event in session.events:
        if prior_state == CaptureState.FULL:
            full_seconds += max(0.0, (event.timestamp - last_time).total_seconds())
        last_time, prior_state = event.timestamp, event.state
    end = datetime.fromisoformat(records[-1].timestamp) + timedelta(seconds=60)
    if prior_state == CaptureState.FULL:
        full_seconds += max(0.0, (end - last_time).total_seconds())
    ratio = physical_total / always_on_bytes if always_on_bytes else 0.0
    recall = captured_important / important_total if important_total else 0.0
    return {
        "rho_requested": rho,
        "rho_empirical": _corr(
            [row.capture_intensity for row in records],
            [float(truth[row.memory_id] == "important") for row in records],
        ),
        "threshold": threshold,
        "ring_seconds": RING_SECONDS,
        "low_threshold_fraction_of_high": LOW_THRESHOLD_FRACTION,
        "standby_seconds": STANDBY_SECONDS,
        "record_interval_seconds": 60,
        "important_captured": captured_important,
        "important_total": important_total,
        "important_recall": recall,
        "noise_captured": captured_noise,
        "noise_total": noise_total,
        "noise_retention": captured_noise / noise_total if noise_total else None,
        "candidate_count": len(captured),
        "backfilled_candidate_count": len(backfilled_ids),
        "wake_count": len(wakes),
        "false_wake_count": sum(value == "noise" for value in labeled_wakes),
        "false_wake_rate": sum(value == "noise" for value in labeled_wakes) / len(wakes)
        if wakes
        else 0.0,
        "physical_vault_bytes": physical_total,
        "always_on_physical_vault_bytes": always_on_bytes,
        "physical_ratio": ratio,
        "physical_breakdown": physical,
        "elapsed_seconds": elapsed,
        "full_duty_seconds": full_seconds,
        "full_duty_fraction": full_seconds / elapsed if elapsed else 0.0,
        "h6_meets_both": ratio <= 0.20 and recall >= 0.70,
        "h6_conditions": {
            "physical_ratio_at_most_0_20": ratio <= 0.20,
            "important_recall_at_least_0_70": recall >= 0.70,
        },
    }


def _access_policy_diagnostics(
    records: tuple[Day5Record, ...], labels: tuple[Day5Label, ...], access_events: tuple[Any, ...]
) -> dict[str, Any]:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    count_by_id: Counter[str] = Counter()
    last_by_id: dict[str, datetime] = {}
    for event in access_events:
        timestamp = datetime.fromisoformat(event.timestamp)
        count_by_id[event.memory_id] += 1
        last_by_id[event.memory_id] = max(timestamp, last_by_id.get(event.memory_id, timestamp))
    rolling = KeynessScorer().score_rolling_documents(
        [(row.memory_id, datetime.fromisoformat(row.timestamp), row.text) for row in records],
        window_size=20,
    )
    candidates = []
    for row in records:
        created = datetime.fromisoformat(row.timestamp)
        last_access = last_by_id.get(row.memory_id)
        keyness = rolling[row.memory_id].score
        strength = observable_strength(
            count_by_id[row.memory_id], last_access, now, keyness, created_at=created
        )
        candidates.append(
            PolicyCandidate(
                id=row.memory_id,
                body=row.text,
                created_at=created,
                last_accessed_at=last_access,
                access_count=count_by_id[row.memory_id],
                strength=strength,
                keyness=keyness,
            )
        )
    limits = BudgetLimits(max_count=max(1, len(candidates) * 4 // 5))
    plans = {
        policy.value: make_policy(policy).plan(candidates, limits, now=now)
        for policy in (PolicyName.FIFO, PolicyName.LRU, PolicyName.DECAY, PolicyName.SALIENCE)
    }
    truth = {row.memory_id: row.importance for row in labels}
    results: dict[str, Any] = {}
    for name, plan in plans.items():
        retained = set(plan.retained_ids)
        important = sum(truth[identifier] == "important" for identifier in retained)
        important_total = sum(label.importance == "important" for label in labels)
        results[name] = {
            "retained_count": len(retained),
            "forgotten_count": len(plan.forgotten_ids),
            "important_retained": important,
            "important_total": important_total,
            "important_recall": important / important_total if important_total else 0.0,
        }
    access_counts = [count_by_id[row.memory_id] for row in records]
    recency_days = [
        (now - last_by_id[row.memory_id]).total_seconds() / 86400
        for row in records
        if row.memory_id in last_by_id
    ]
    keyness_order = sorted(records, key=lambda row: (-rolling[row.memory_id].score, row.memory_id))
    top_k = max(1, len(records) // 10)
    top_ids = {row.memory_id for row in keyness_order[:top_k]}
    important_total = sum(value == "important" for value in truth.values())
    access_tail_fit = asdict(
        fit_discrete_tail(access_counts, minimum_sample=20, bootstrap_samples=50, seed=SEED)
    )
    return {
        "access_events": len(access_events),
        "documents_with_access": len(last_by_id),
        "access_count_min_median_max": [
            min(access_counts),
            sorted(access_counts)[len(access_counts) // 2],
            max(access_counts),
        ],
        "access_count_mean": sum(access_counts) / len(access_counts),
        "last_access_recency_days_min_max": [min(recency_days), max(recency_days)],
        "distinct_last_access_times": len(set(last_by_id.values())),
        "access_tail_fit": access_tail_fit,
        "policy_budget": {"type": "max_count", "value": limits.max_count, "same_candidates": True},
        "policies": results,
        "keyness_top_decile": {
            "count": top_k,
            "important_retained": sum(truth[identifier] == "important" for identifier in top_ids),
            "important_total": important_total,
            "important_precision": sum(truth[identifier] == "important" for identifier in top_ids)
            / top_k,
            "rolling_reference_window": 20,
        },
        "observable_policy_inputs": (
            "per-record access count, exact last-access timestamp, recency-derived strength, "
            "rolling keyness; no labels"
        ),
        "tail_fit_note": (
            "discrete diagnostic with seeded bootstrap; synthetic access schedule only"
        ),
    }


def run_scan(output_dir: Path, *, count_records: int = 200, seed: int = SEED) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_rows: dict[
        float, tuple[tuple[Day5Record, ...], tuple[Day5Label, ...], tuple[Any, ...]]
    ] = {}
    generator_receipts: dict[str, Any] = {}
    shared_labels: tuple[Day5Label, ...] | None = None
    shared_accesses: tuple[Any, ...] | None = None
    for rho in RHOS:
        rows, labels, access_events = generate_dataset(count=count_records, seed=seed, rho=rho)
        all_rows[rho] = rows, labels, access_events
        if shared_labels is None:
            shared_labels, shared_accesses = labels, access_events
        if labels != shared_labels or access_events != shared_accesses:
            raise RuntimeError("rho scan changed evaluation labels or access history")
        generator_receipts[str(rho)] = write_dataset(
            output_dir / f"day5_rho_{rho:.1f}.jsonl",
            output_dir / "day5_labels.jsonl",
            output_dir / "day5_accesses.jsonl",
            count=count_records,
            seed=seed,
            rho=rho,
        )
    assert shared_labels is not None
    access_diagnostics = _access_policy_diagnostics(
        all_rows[0.0][0], shared_labels, shared_accesses or ()
    )
    rows_zero = all_rows[0.0][0]
    with TemporaryDirectory(prefix="memfw-day5-scan-") as temp:
        baseline_repo, baseline_session = _capture(
            rows_zero, Path(temp) / "always-on", threshold=None, run_id="d5-always-on"
        )
        if (
            len({item.source_id for item in baseline_session.items() if item.source_id})
            != count_records
        ):
            raise RuntimeError("always-on comparator did not capture the full input")
        always_on_bytes = measure_vault(baseline_repo).physical_vault_bytes
        cells = [
            _cell(
                rows,
                labels,
                Path(temp) / f"rho-{int(rho * 10)}-threshold-{int(threshold * 100)}",
                rho=rho,
                threshold=threshold,
                always_on_bytes=always_on_bytes,
            )
            for rho in RHOS
            for threshold in THRESHOLDS
            for rows, labels, _accesses in (all_rows[rho],)
        ]
    return {
        "seed": seed,
        "record_count": count_records,
        "rho_values": list(RHOS),
        "thresholds": list(THRESHOLDS),
        "cell_count": len(cells),
        "ring_seconds": RING_SECONDS,
        "record_interval_seconds": 60,
        "labels_accesses_text_timestamps_fixed_across_rho": True,
        "labels_are_evaluation_only": True,
        "physical_accounting": (
            "allocated bytes across all categories; same always-on input and measurement code"
        ),
        "h6_definition": ("physical ratio <= 0.20 and important capture recall >= 0.70"),
        "h6_success_cells": sum(cell["h6_meets_both"] for cell in cells),
        "generation": generator_receipts,
        "access_policy_diagnostics": access_diagnostics,
        "cells": cells,
        "limitations": [
            "Controlled rho is synthetic and not an observed human signal relationship."
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be at least 2")
    started = time.perf_counter()
    report = run_scan(args.output_dir, count_records=args.count, seed=args.seed)
    runtime = time.perf_counter() - started
    path = args.output or args.output_dir / "day5_capture_scan.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    receipt = {
        "runtime_seconds": round(runtime, 6),
        "quick_under_5_minutes": runtime < 300,
        "cells": report["cell_count"],
    }
    (args.output_dir / "day5_runtime_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"report": path.name, **receipt}, sort_keys=True))


if __name__ == "__main__":
    main()
