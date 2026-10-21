#!/usr/bin/env python3
"""Compare actual Day4 1MB/10MB hard-forget budgets before and after log compaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gen_day4 import SEED, generate_dataset  # noqa: E402

TARGETS = {"1MB": 1_000_000, "10MB": 10_000_000}
BASELINE_ARCHIVE = ROOT / "reports" / "day4_baseline_snapshot.zip"
BASELINE = Path("/private/tmp/memfw-day4-baseline-20261018/src")


def _probe(
    source_root: Path, repo_root: Path, dataset: Path, budget: int, compact: bool
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "day4_storage_probe.py"),
        "--source-root",
        str(source_root),
        "--repo-root",
        str(repo_root),
        "--dataset",
        str(dataset),
        "--budget-bytes",
        str(budget),
    ]
    if compact:
        command.append("--compact")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, env=env, timeout=240
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def _validate_archive_members(archive: zipfile.ZipFile) -> None:
    for member in archive.infolist():
        member_path = PurePosixPath(member.filename)
        windows_path = PureWindowsPath(member.filename)
        if (
            member_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in member_path.parts
            or "\\" in member.filename
        ):
            raise ValueError("baseline archive contains an unsafe member path")


def run_matrix(
    output_dir: Path,
    *,
    baseline_source: Path | None = None,
    baseline_archive: Path = BASELINE_ARCHIVE,
    count: int = 200,
    seed: int = SEED,
) -> tuple[dict[str, Any], dict[str, Any]]:
    records, _labels = generate_dataset(seed=seed, count=count)
    dataset_path = output_dir / "day4_storage_fixture.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(
        "".join(
            json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in records
        ),
        encoding="utf-8",
    )
    source_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    runtime_rows: dict[str, Any] = {}
    started = time.perf_counter()
    with TemporaryDirectory(prefix="memfw-day4-storage-matrix-") as temp:
        work = Path(temp)
        if baseline_source is None:
            with zipfile.ZipFile(baseline_archive) as archive:
                _validate_archive_members(archive)
                archive.extractall(work / "baseline")
            baseline_source = work / "baseline" / "src"
        for profile, budget in TARGETS.items():
            baseline_result = _probe(
                baseline_source, work / f"{profile}-baseline", dataset_path, budget, compact=False
            )
            runtime_result = _probe(
                ROOT / "src", work / f"{profile}-runtime", dataset_path, budget, compact=True
            )
            for result in (baseline_result, runtime_result):
                if result["budget_bytes"] != budget or result["body_bytes_before_forget"] <= budget:
                    raise RuntimeError(f"{profile} scenario did not exercise an over-budget vault")
                if not result["budget_satisfied"] or result["retained_count"] == 0:
                    raise RuntimeError(
                        f"{profile} scenario failed to retain a valid non-empty vault"
                    )
                if result["anchors_retained"] != 0:
                    raise RuntimeError("storage-size fixture unexpectedly created an anchor")
            if baseline_result["retained_count"] != runtime_result["retained_count"]:
                raise RuntimeError(f"old/new survivor counts differ for {profile}")
            runtime_rows[profile] = {
                "baseline_snapshot": baseline_result,
                "new_runtime_compacted": runtime_result,
                "physical_delta_bytes": (
                    runtime_result["physical_total_after_compaction"]
                    - baseline_result["physical_total_after_compaction"]
                ),
                "physical_savings_fraction": (
                    baseline_result["physical_total_after_compaction"]
                    - runtime_result["physical_total_after_compaction"]
                )
                / baseline_result["physical_total_after_compaction"]
                if baseline_result["physical_total_after_compaction"]
                else 0.0,
                "same_survivor_count": True,
            }
    wall = time.perf_counter() - started
    receipt = {
        "runtime_seconds": round(wall, 6),
        "record_count": count,
        "quick_run_under_5m": wall < 300,
        "profiles": list(TARGETS),
    }
    return {
        "dataset_seed": seed,
        "memory_count": count,
        "quick_count": count < 1_000,
        "dataset": dataset_path.name,
        "dataset_sha256": source_hash,
        "labels_used": False,
        "source_session_timestamps_and_content_match_between_runs": True,
        "configuration": (
            "Same FIFO hard-forget body budget, no anchors, one metadata summary update, "
            "log_keep_recent=100."
        ),
        "budget_semantics": (
            "The 1MB and 10MB values cap logical UTF-8 memory-body bytes only. They are not "
            "physical vault quotas; physical allocated bytes are reported separately."
        ),
        "body_fixture_method": (
            "The storage fixture repeats one readable follow-up sentence to exceed each requested "
            "body budget. It is storage-only and excluded from semantic scoring."
        ),
        "baseline_source": "frozen baseline from archive; module path checked by probe",
        "baseline_inventory": "BASELINE_SHA256.txt inside baseline archive",
        "baseline_archive": baseline_archive.name,
        "comparison": (
            "Frozen old source after FIFO forget versus current source after the same FIFO "
            "forget and log compaction."
        ),
        "physical_units": "allocated bytes (st_blocks * 512 where supported)",
        "profiles": runtime_rows,
    }, receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument(
        "--baseline-source",
        type=Path,
        default=None,
        help="Optional already-extracted frozen source tree; archive is default.",
    )
    parser.add_argument("--baseline-archive", type=Path, default=BASELINE_ARCHIVE)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    report, receipt = run_matrix(
        args.output_dir,
        baseline_source=args.baseline_source,
        baseline_archive=args.baseline_archive,
        count=args.count,
        seed=args.seed,
    )
    (args.output_dir / "day4_storage_matrix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "day4_storage_runtime_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"report": str(args.output_dir / "day4_storage_matrix.json"), **receipt}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
