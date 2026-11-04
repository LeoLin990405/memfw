from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import day11_baseline_comparison as day11


def _locomo_file(path: Path) -> Path:
    samples = []
    for sample_number in range(2):
        turns = [
            {"dia_id": f"D1:{i}", "speaker": "A", "text": f"Public turn {i}."} for i in range(1, 6)
        ]
        samples.append(
            {
                "sample_id": f"sample-{sample_number}",
                "conversation": {"session_1": turns},
                "qa": [
                    {
                        "question": f"Question for category {category}",
                        "evidence": [f"D1:{category}"],
                        "category": category,
                    }
                    for category in range(1, 6)
                ],
            }
        )
    path.write_text(json.dumps(samples), encoding="utf-8")
    return path


def test_manifest_is_seeded_and_stores_question_hash_only(tmp_path: Path) -> None:
    data = _locomo_file(tmp_path / "locomo.json")
    first = day11.build_manifest(data)
    second = day11.build_manifest(data)

    assert first == second
    assert first["seed"] == 20261021
    assert first["scope"]["selected_question_count"] == 5
    assert first["questions"][0]["question_sha256"]
    assert "question" not in first["questions"][0]
    assert "sample_id" in first["source"]


def test_blocked_report_has_aligned_configuration_and_null_metrics(tmp_path: Path) -> None:
    manifest = day11.build_manifest(_locomo_file(tmp_path / "locomo.json"))
    report = day11.build_report(
        manifest,
        model_dir=tmp_path / "missing-model",
        embedding_model=None,
        answer_model="local-answer-model",
        top_k=7,
        context_token_budget=1024,
    )

    assert report["status"] == "blocked"
    assert report["execution_performed"] is False
    assert report["alignment"]["validation"]["validated"] is False
    assert "answer_model" in report["alignment"]["validation"]["missing_fields"]
    assert report["alignment"]["proposed_shared_settings"]["top_k"] == 7
    assert report["alignment"]["proposed_shared_settings"]["context_token_budget"] == 1024
    assert report["preflight"]["expected_mem0ai_version"] == "2.2.1"
    assert report["preflight"]["installation_attempt"]["installed"] is None
    for system in ("mem0", "memfw"):
        for retention in ("retention_5pct", "retention_100pct"):
            cell = report["conditions"][system][retention]
            assert cell["status"] == "blocked"
            assert all(value is None for value in cell["metrics"].values())


def test_execute_flag_reports_not_implemented_and_no_metrics(tmp_path: Path) -> None:
    manifest = day11.build_manifest(_locomo_file(tmp_path / "locomo.json"))
    report = day11.build_report(
        manifest,
        model_dir=tmp_path,
        embedding_model="local-embedding",
        answer_model="local-answer",
        top_k=5,
        context_token_budget=2048,
        execution_requested=True,
    )

    assert report["status"] == "not_implemented"
    assert report["execution_performed"] is False
    assert "runner is not implemented" in " ".join(report["preflight"]["reasons"])
    assert report["metrics"]["evidence_recall"] is None
    assert report["metrics"]["answer_f1"] is None


def test_alignment_validator_requires_matching_values_and_digests() -> None:
    aligned = {
        "answer_model": "local-a",
        "answer_model_digest": "sha256:a",
        "embedding_model": "local-e",
        "embedding_model_digest": "sha256:e",
        "top_k": 5,
        "context_token_budget": 128,
    }
    assert day11.validate_alignment(aligned, aligned)["validated"] is True

    changed = {**aligned, "embedding_model_digest": "sha256:other"}
    mismatch = day11.validate_alignment(aligned, changed)
    assert mismatch["validated"] is False
    assert "embedding_model_digest" in mismatch["differences"]
    unset = {**aligned, "answer_model_digest": None, "top_k": 0}
    invalid = day11.validate_alignment(unset, unset)
    assert invalid["validated"] is False
    assert "answer_model_digest" in invalid["missing_fields"]
    assert "top_k" in invalid["missing_fields"]


def test_blocked_cli_returns_nonzero_and_records_null_metrics(tmp_path: Path) -> None:
    data = _locomo_file(tmp_path / "locomo.json")
    output_dir = tmp_path / "results"
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/day11_baseline_comparison.py"),
            "--data",
            str(data),
            "--model-dir",
            str(tmp_path / "missing-model"),
            "--output-dir",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 2
    assert '"status": "blocked"' in process.stdout
    report = json.loads((output_dir / "day11_baseline_comparison.json").read_text())
    assert report["execution_performed"] is False
    assert report["metrics"]["evidence_recall"] is None
