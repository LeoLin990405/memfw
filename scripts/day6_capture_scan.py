#!/usr/bin/env python3
"""Measure end-of-session packed capture staging at configurable prevalence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from day5_experiment import _capture  # noqa: E402
from gen_day5 import RHOS, SEED, Day5Label, Day5Record, generate_day6_dataset  # noqa: E402

from memfw.capture import pack_capture_candidates  # noqa: E402
from memfw.core.models import CaptureState, EventKind  # noqa: E402
from memfw.core.repository import Repository  # noqa: E402
from memfw.stats.runtime import measure_physical_breakdown, measure_vault  # noqa: E402

DEFAULT_THRESHOLDS = (0.35, 0.5, 0.65, 0.8)


def _allocated(path: Path) -> int:
    if not path.exists():
        return 0
    stat = path.stat()
    return stat.st_blocks * 512 if hasattr(stat, "st_blocks") else stat.st_size


def _capture_cell(
    records: tuple[Day5Record, ...],
    labels: tuple[Day5Label, ...],
    root: Path,
    *,
    threshold: float | None,
    run_id: str,
    always_on_stage_bytes: int,
    always_on_all_in_bytes: int,
) -> dict[str, Any]:
    truth = {row.memory_id: row.importance for row in labels}
    repository, session = _capture(records, root, threshold=threshold, run_id=run_id)
    captured = {item.source_id for item in session.items() if item.source_id}
    important_total = sum(label.importance == "important" for label in labels)
    important_captured = sum(truth.get(memory_id) == "important" for memory_id in captured)
    nonimportant_total = len(labels) - important_total
    nonimportant_captured = sum(truth.get(memory_id) != "important" for memory_id in captured)
    recall = important_captured / important_total if important_total else 0.0
    wakes = [event for event in session.events if event.kind == EventKind.CAPTURE_WAKE]
    record_by_time = {datetime.fromisoformat(row.timestamp): row for row in records}
    labeled_wakes = [
        truth[record_by_time[event.timestamp].memory_id]
        for event in wakes
        if event.timestamp in record_by_time
    ]
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
    staging = repository.root / ".staging" / "capture-candidates.jsonl"
    raw_logical_bytes = staging.stat().st_size if staging.exists() else 0
    raw_allocated_bytes = _allocated(staging)
    pack_result = pack_capture_candidates(repository)
    packed_path = staging.with_name("capture-candidates.jsonl.zst")
    packed_logical_bytes = packed_path.stat().st_size if packed_path.exists() else 0
    packed_allocated_bytes = _allocated(packed_path)
    physical = measure_physical_breakdown(repository)
    all_in = measure_vault(repository).physical_vault_bytes
    if sum(physical.values()) != all_in:
        raise RuntimeError("allocated physical categories do not sum to vault bytes")
    packed_ratio = packed_allocated_bytes / always_on_stage_bytes if always_on_stage_bytes else 0.0
    all_in_ratio = all_in / always_on_all_in_bytes if always_on_all_in_bytes else 0.0
    eligible = recall >= 0.70
    return {
        "threshold": threshold,
        "candidate_count": len(captured),
        "important_captured": important_captured,
        "important_total": important_total,
        "important_recall": recall,
        "nonimportant_captured": nonimportant_captured,
        "nonimportant_total": nonimportant_total,
        "nonimportant_retention": (
            nonimportant_captured / nonimportant_total if nonimportant_total else None
        ),
        "noise_retention": None,
        "false_wake_count": None,
        "false_wake_rate": None,
        "nonimportant_wake_count": sum(value != "important" for value in labeled_wakes),
        "nonimportant_wake_rate": (
            sum(value != "important" for value in labeled_wakes) / len(wakes) if wakes else 0.0
        ),
        "wake_count": len(wakes),
        "elapsed_seconds": elapsed,
        "full_duty_seconds": full_seconds,
        "full_duty_fraction": full_seconds / elapsed if elapsed else 0.0,
        "h6_eligible": eligible,
        "h6_packed_staging_ratio": packed_ratio,
        "h6_packed_staging_pass": eligible and packed_ratio <= 0.20,
        "all_in_allocated_ratio": all_in_ratio,
        "empty_vault_allocated_bytes": None,
        "event_log_baseline_allocated_bytes": None,
        "event_log_growth_allocated_bytes": None,
        "raw_staging_logical_bytes": raw_logical_bytes,
        "raw_staging_allocated_bytes": raw_allocated_bytes,
        "packed_staging_logical_bytes": packed_logical_bytes,
        "packed_staging_allocated_bytes": packed_allocated_bytes,
        "all_in_allocated_bytes": all_in,
        "allocated_breakdown": physical,
        "pack_receipt": pack_result,
    }


def run_scan(
    output_dir: Path,
    *,
    count_records: int = 1_000,
    seed: int = SEED,
    important_prevalence: float = 0.10,
    rho_captures: tuple[float, ...] = RHOS,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    datasets: dict[float, tuple[tuple[Day5Record, ...], tuple[Day5Label, ...], dict[str, Any]]] = {}
    for rho_capture in rho_captures:
        records, labels, _accesses, generation = generate_day6_dataset(
            count=count_records,
            seed=seed,
            rho_capture=rho_capture,
            rho_access=0.3,
            important_prevalence=important_prevalence,
        )
        datasets[rho_capture] = records, labels, generation
    first_records, _first_labels, _first_generation = datasets[rho_captures[0]]
    with TemporaryDirectory(prefix="memfw-day6-capture-") as temporary:
        root = Path(temporary)
        empty = Repository(root / "empty-vault")
        empty.init()
        empty_breakdown = measure_physical_breakdown(empty)
        empty_all_in = measure_vault(empty).physical_vault_bytes
        baseline = _capture(
            first_records, root / "always-on", threshold=None, run_id="day6-always-on"
        )[0]
        baseline_stage = baseline.root / ".staging" / "capture-candidates.jsonl"
        baseline_raw_logical_bytes = baseline_stage.stat().st_size if baseline_stage.exists() else 0
        pack_capture_candidates(baseline)
        baseline_pack = baseline_stage.with_name("capture-candidates.jsonl.zst")
        always_on_stage_bytes = _allocated(baseline_pack)
        always_on_all_in_bytes = measure_vault(baseline).physical_vault_bytes
        baseline_breakdown = measure_physical_breakdown(baseline)
        cells = []
        generation_metadata: dict[str, Any] = {}
        for rho_capture, (records, labels, generation) in datasets.items():
            generation_metadata[str(rho_capture)] = generation
            for threshold in thresholds:
                cell = _capture_cell(
                    records,
                    labels,
                    root / f"rho-{rho_capture:.1f}-threshold-{threshold:.2f}",
                    threshold=threshold,
                    run_id=f"day6-r{int(rho_capture * 10)}-t{int(threshold * 100):02d}",
                    always_on_stage_bytes=always_on_stage_bytes,
                    always_on_all_in_bytes=always_on_all_in_bytes,
                )
                cell["rho_capture_requested"] = rho_capture
                cell["rho_access_requested"] = generation["rho_access_requested"]
                cell["rho_access_empirical"] = generation["rho_access_empirical"]
                cell["rho_access_attainable_min"] = generation["rho_access_attainable_min"]
                cell["rho_access_attainable_max"] = generation["rho_access_attainable_max"]
                cells.append(cell)
    empty_event_bytes = int(empty_breakdown.get("events", 0))
    for cell in cells:
        breakdown = cell["allocated_breakdown"]
        cell["empty_vault_allocated_bytes"] = empty_all_in
        cell["event_log_baseline_allocated_bytes"] = empty_event_bytes
        cell["event_log_growth_allocated_bytes"] = max(
            0, int(breakdown.get("events", 0)) - empty_event_bytes
        )
    return {
        "seed": seed,
        "record_count": count_records,
        "important_prevalence_requested": important_prevalence,
        "rho_capture_values": list(rho_captures),
        "generation_by_rho_capture": generation_metadata,
        "rho_access_generation_control": generation_metadata[str(rho_captures[0])],
        "labels_are_evaluation_only": True,
        "thresholds": list(thresholds),
        "ring_seconds": 1800,
        "record_interval_seconds": 60,
        "always_on_stage_allocated_bytes": always_on_stage_bytes,
        "always_on_all_in_allocated_bytes": always_on_all_in_bytes,
        "always_on_allocated_breakdown": baseline_breakdown,
        "always_on_raw_logical_staging_bytes": baseline_raw_logical_bytes,
        "empty_vault_allocated_bytes": empty_all_in,
        "empty_vault_breakdown": empty_breakdown,
        "staging_metric": (
            "allocated bytes of one verified zstd pack after removing raw JSONL WAL"
        ),
        "h6_definition": (
            "eligible only when important recall >= 0.70; packed staging ratio <= 0.20"
        ),
        "h6_eligible_cells": sum(bool(cell["h6_eligible"]) for cell in cells),
        "h6_passing_eligible_cells": sum(bool(cell["h6_packed_staging_pass"]) for cell in cells),
        "cell_count": len(cells),
        "cells": cells,
        "limitations": [
            (
                "The pack is an explicit end-of-session state; append reopens the "
                "authenticated JSONL WAL."
            ),
            (
                "Allocated bytes depend on filesystem block size; logical pack bytes are "
                "reported separately."
            ),
            (
                "Packed-stage ratio and all-in allocated ratio have different denominators "
                "and are reported separately."
            ),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--important-prevalence", type=float, default=0.10)
    args = parser.parse_args()
    report = run_scan(
        args.output_dir,
        count_records=args.count,
        seed=args.seed,
        important_prevalence=args.important_prevalence,
    )
    path = args.output_dir / "day6_capture_scan.json"
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "report": path.name,
                "h6": [report["h6_passing_eligible_cells"], report["h6_eligible_cells"]],
            }
        )
    )


if __name__ == "__main__":
    main()
