from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from day4_experiment import run_experiment
from day4_storage_matrix import run_matrix
from eval_locomo import evaluate_file

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(
    not (ROOT / "reports" / "day4_baseline_snapshot.zip").exists(),
    reason="day4_baseline_snapshot.zip is a release asset; see reports/LARGE_FILES.md",
)
def test_complete_semantic_quick_reports_repeat_across_fresh_roots(tmp_path: Path) -> None:
    snapshots: list[bytes] = []
    for run_number in range(2):
        output = tmp_path / f"run-{run_number}"
        capture = run_experiment(output, seed=20261018, count=40, ring_intervals=2.0)
        storage, _runtime_receipt = run_matrix(output, seed=20261018, count=40)
        locomo = [
            evaluate_file(path, budget_tokens=128)
            for path in sorted((ROOT / "tests" / "fixtures" / "day4_locomo").glob("*.json"))
        ]
        semantic_report = {"capture": capture, "storage": storage, "locomo": locomo}
        snapshots.append(
            (json.dumps(semantic_report, ensure_ascii=False, sort_keys=True) + "\n").encode()
        )
    assert snapshots[0] == snapshots[1]
