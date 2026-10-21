from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_small_rho_diagnostic_runs_every_policy_with_budget_receipts(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    work_dir = tmp_path / "work"
    script = Path(__file__).resolve().parents[1] / "scripts" / "day6_rho_diagnostic.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--output-dir",
            str(output_dir),
            "--work-dir",
            str(work_dir),
            "--seed",
            "20261018",
            "--count",
            "10",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads((output_dir / "day6_rho_diagnostic.json").read_text())

    profiles = result["profiles"]
    policies = {"fifo", "lru", "decay", "salience", "consolidate_then_forget"}
    assert len(profiles) == 3 * len(policies)
    assert {(row["rho"], row["policy"]) for row in profiles} == {
        (rho, policy) for rho in (0.0, 0.3, 0.6) for policy in policies
    }
    for profile in profiles:
        assert profile["budget_satisfied"] is True
        assert profile["physical_after_bytes"] <= result["budget_bytes"]
        assert sum(profile["physical_after_breakdown"].values()) == profile["physical_after_bytes"]
    assert (output_dir / "day6_rho_diagnostic.json").is_file()
    assert (output_dir / "day6_rho_diagnostic_checkpoint.json").is_file()
