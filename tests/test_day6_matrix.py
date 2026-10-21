from __future__ import annotations

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from compare_day6_matrices import compare  # noqa: E402
from day5_storage_matrix import _probe, _write_fixture, semantic_result_hash  # noqa: E402


@pytest.mark.parametrize("timeout, expected", [(0, None), (0.25, 0.25), (None, None)])
def test_probe_timeout_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float | None, expected: float | None
) -> None:
    captured: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> CompletedProcess[str]:
        captured.update(kwargs)
        return CompletedProcess(args[0], 0, "{}", "")

    monkeypatch.setattr("day5_storage_matrix.subprocess.run", run)
    result = _probe(
        source_root=tmp_path / "src",
        repo_root=tmp_path / "vault",
        mode="build",
        timeout_seconds=timeout,
    )
    assert result == {}
    assert captured["timeout"] == expected


def test_semantic_hash_ignores_runtime_but_keeps_result_fields() -> None:
    first = {
        "runtime_seconds": 10.5,
        "checkpoint_file": "run-a/checkpoint.json",
        "work_directory_persisted": True,
        "policy_profiles": {"1MB": {"fifo": {"retained_count": 81, "elapsed_seconds": 9.0}}},
    }
    second: dict[str, Any] = {
        "runtime_seconds": 99.0,
        "checkpoint_file": "run-b/checkpoint.json",
        "work_directory_persisted": False,
        "policy_profiles": {"1MB": {"fifo": {"retained_count": 81, "elapsed_seconds": 80.0}}},
    }
    assert semantic_result_hash(first) == semantic_result_hash(second)
    second["policy_profiles"]["1MB"]["fifo"]["retained_count"] = 82
    assert semantic_result_hash(first) != semantic_result_hash(second)


def test_compare_cli_payload_accepts_separate_run_reports(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"runtime_seconds": 1.0, "records": [1, 2]}))
    second.write_text(json.dumps({"runtime_seconds": 3.0, "records": [1, 2]}))
    result = compare(first, second)
    assert result["semantic_match"] is True


def test_fixture_can_make_ten_megabyte_budget_active_without_more_records(tmp_path: Path) -> None:
    docs, _accesses, _labels, metadata = _write_fixture(
        tmp_path, count=1_000, seed=20261018, target_body_bytes=15_000
    )
    rows = [json.loads(line) for line in docs.read_text().splitlines()]
    logical_bytes = sum(len(row["body"].encode()) for row in rows)
    assert len(rows) == 1_000
    assert metadata["body_bytes_min"] >= 15_000
    assert logical_bytes > 10_000_000
