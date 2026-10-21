from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from day7_gating import deterministic_judge_scores, run_gating  # noqa: E402
from gen_day5 import Day5Record  # noqa: E402


def test_text_judge_is_deterministic_and_uses_past_only() -> None:
    first = Day5Record("a", "2026-10-01T00:00:00+00:00", "repeat repeat", 0.5, 0, "")
    second = Day5Record("b", "2026-10-02T00:00:00+00:00", "repeat deadline 2026", 0.5, 0, "")
    scores = deterministic_judge_scores((first, second))
    assert scores == deterministic_judge_scores((first, second))
    assert scores["a"] == 0.0
    assert scores["b"] > 0.0


def test_gating_run_reports_oracle_and_real_storage_boundaries(tmp_path: Path) -> None:
    output = tmp_path / "gate.json"
    script = SCRIPTS / "day7_gating.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--count",
            "20",
            "--seed",
            "20261018",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(output.read_text())

    assert report["evaluation_only"] is True
    assert "labels" in report["label_boundary"]
    assert "text only" in report["label_boundary"]
    assert report["count"] == 20
    by_strategy = {row["strategy"]: row for row in report["strategies"]}
    assert set(by_strategy) == {
        "oracle_upper_bound",
        "deterministic_keyness_text",
        "existing_intensity",
        "always_on",
    }
    oracle = by_strategy["oracle_upper_bound"]
    assert oracle["important_recall"] == pytest.approx(1.0)
    assert oracle["candidate_count"] == oracle["important_total"]
    for result in by_strategy.values():
        assert result["recall_eligible"] == (result["important_recall"] >= 0.70)
        assert 0.0 <= result["candidate_fraction"] <= 1.0
        assert 0.0 <= result["persistent_memory_fraction"] <= 1.0
        assert result["pack_raw_coexistence_observed"] is True
        assert result["atomic_temp_peak_all_in_allocated_bytes"] is not None
        assert result["atomic_temp_peak_paths"]
        assert result["peak_all_in_allocated_bytes"] >= result["empty_vault_allocated_bytes"]
        assert result["packed_stage_logical_bytes"] >= 0
        assert result["packed_stage_allocated_bytes"] >= 0
        assert result["simulated_eval_keep_confirmations"] == result["candidate_count"]
        assert 0.0 <= result["final_all_in_allocated_fraction_vs_always_on"]


def test_gating_rejects_invalid_prevalence() -> None:
    script = SCRIPTS / "day7_gating.py"
    result = subprocess.run(
        [sys.executable, str(script), "--count", "10", "--important-prevalence", "1"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_complete_small_gating_run_is_byte_semantically_deterministic() -> None:
    first = run_gating(
        count=10,
        seed=20261018,
        rho_capture=0.3,
        rho_access=0.3,
        prevalence=0.2,
    )
    second = run_gating(
        count=10,
        seed=20261018,
        rho_capture=0.3,
        rho_access=0.3,
        prevalence=0.2,
    )
    assert first == second
    assert first["oracle_ideal_candidate_count"] == len(first["oracle_ideal_candidate_ids"])
    assert first["oracle_realized_overcapture_count"] == (
        first["oracle_realized_candidate_count"] - first["oracle_realized_important_count"]
    )
    assert first["oracle_realized_missed_important_count"] == (
        first["oracle_ideal_candidate_count"] - first["oracle_realized_important_count"]
    )
    assert first["oracle_realized_matches_ideal"] == (
        first["oracle_realized_candidate_count"] == first["oracle_ideal_candidate_count"]
    )
    for row in first["strategies"]:
        assert row["peak_acquisition_and_packing_allocated_bytes"] >= 0
        assert row["peak_after_simulated_confirmation_allocated_bytes"] >= 0
        assert row["h6_recall_eligible"] is row["recall_eligible"]
        assert row["h6_allocated_pass"] == (
            row["h6_recall_eligible"] and row["h6_stage_allocated_fraction"] <= 0.20
        )
