#!/usr/bin/env python3
"""Run the complete bounded Day 4 capture, storage, and local retrieval experiment."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from day4_experiment import run_experiment
from day4_storage_matrix import run_matrix
from eval_locomo import evaluate_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "day4_locomo"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--ring-intervals", type=float, default=2.0)
    args = parser.parse_args()
    if args.count < 1 or args.ring_intervals <= 0:
        parser.error("--count and --ring-intervals must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    capture = run_experiment(
        args.output_dir,
        seed=args.seed,
        count=args.count,
        ring_intervals=args.ring_intervals,
    )
    capture["status"] = "complete for bounded synthetic capture and scoring; see matrix and P2"
    (args.output_dir / "day4_experiment.json").write_text(
        json.dumps(capture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    matrix, matrix_receipt = run_matrix(args.output_dir, count=args.count, seed=args.seed)
    (args.output_dir / "day4_storage_matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "day4_storage_runtime_receipt.json").write_text(
        json.dumps(matrix_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    locomo = [evaluate_file(path, budget_tokens=128) for path in sorted(FIXTURES.glob("*.json"))]
    (args.output_dir / "day4_locomo_evaluation.json").write_text(
        json.dumps(locomo, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    elapsed = round(time.perf_counter() - started, 6)
    receipt = {
        "command": "python scripts/day4_quick.py --count 200 --seed 20261018",
        "record_count": args.count,
        "ring_intervals": args.ring_intervals,
        "locomo_fixture_count": len(locomo),
        "locomo_question_count": sum(row["question_count"] for row in locomo),
        "storage_matrix_seconds": matrix_receipt["runtime_seconds"],
        "runtime_seconds": elapsed,
        "under_300_seconds": elapsed < 300,
    }
    (args.output_dir / "day4_quick_runtime_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
