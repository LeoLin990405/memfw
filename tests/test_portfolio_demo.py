from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import day7_eval
import eval_locomo
import portfolio_demo


def _payload() -> dict[str, Any]:
    return {
        "conversation": {
            "session_1": [
                {"dia_id": "D1:1", "speaker": "Ava", "text": "Ava saved the cedar room key."},
                {"dia_id": "D1:2", "speaker": "Bo", "text": "The blue envelope holds the key."},
                {"dia_id": "D1:3", "speaker": "Ava", "text": "The meeting starts on Friday."},
            ]
        },
        "qa": [
            {"question": "Which room key did Ava save?", "evidence": ["D1:1"], "category": 1},
            {"question": "Where is the key?", "evidence": ["D1:2"], "category": 2},
            {"question": "When does the meeting start?", "evidence": ["D1:3"], "category": 3},
            {"question": "No evidence question", "evidence": [], "category": 4},
        ],
    }


def test_selection_is_ordered_bounded_and_requires_all_evidence_in_window(tmp_path: Path) -> None:
    payload = _payload()
    payload["conversation"]["session_1"] = [
        {"dia_id": f"D1:{i}", "speaker": "A", "text": f"Turn number {i} has a unique fact."}
        for i in range(1, 104)
    ]
    payload["qa"] = [
        {"question": f"Question {i}", "evidence": [f"D1:{i}"], "category": 1} for i in range(1, 25)
    ]
    payload["qa"].append(
        {"question": "Cross-window evidence", "evidence": ["D1:1", "D1:101"], "category": 1}
    )
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    turns, questions, selection = portfolio_demo._load_first_sample(path)

    assert len(turns) == 100
    assert len(questions) == 20
    assert [row["evidence"][0] for row in questions] == [f"D1:{i}" for i in range(1, 21)]
    assert selection["questions_with_all_evidence_in_window"] == 24
    assert selection["questions_evaluated"] == 20
    turn_ids = {turn["dia_id"] for turn in turns}
    assert all(item in turn_ids for q in questions for item in q["evidence"])


def test_demo_is_deterministic_aggregate_only_and_uses_six_arms(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    first = portfolio_demo.run_demo(path)
    second = portfolio_demo.run_demo(path)

    assert first == second
    assert first["scope"]["turns_in_evaluation_window"] == 3
    assert first["scope"]["questions_evaluated"] == 3
    assert first["status"] == "completed"
    assert first["invariants"]["event_replay_stable_across_index_rebuild"] is True
    assert first["invariants"]["index_rebuild_count_stable"] is True
    assert first["invariants"]["bm25_search_stable_across_index_rebuild"] is True
    assert [item["policy"] for item in first["systems"]] == [
        "fifo",
        "lru",
        "decay",
        "salience",
        "consolidate_then_forget",
        "no_forgetting_reference",
    ]
    assert len(first["systems"]) == 6
    rendered = json.dumps(first)
    for private_content in (
        "Ava saved the cedar room key",
        "Which room key did Ava save?",
        "D1:1",
        "synthetic.json",
    ):
        assert private_content not in rendered
    for item in first["systems"][:-1]:
        assert item["retained_body_bytes"] <= item["retention_budget_bytes"]
    assert first["systems"][-1]["retention_budget_bytes"] is None


def test_cli_requires_data_and_private_pilot_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        portfolio_demo.main([])
    forbidden = tmp_path / "pilot-vault" / "input.json"
    forbidden.parent.mkdir()
    forbidden.write_text(json.dumps(_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match="restricted pilot tree"):
        portfolio_demo.run_demo(forbidden)


def test_temp_root_inside_project_is_rejected_before_vault_creation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project_root = Path(portfolio_demo.__file__).resolve().parents[1]
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(project_root))
    monkeypatch.setattr(
        day7_eval,
        "_new_sample_repo",
        lambda *_args, **_kwargs: pytest.fail("must reject unsafe temp root before writing"),
    )
    turns, _questions, _stats = eval_locomo.adapt_with_stats(_payload())

    with pytest.raises(RuntimeError, match="outside the project tree"):
        portfolio_demo._safe_temporary_repo(turns)


def test_run_demo_rejects_unsafe_temp_root_before_reading_data(
    monkeypatch: Any,
) -> None:
    project_root = Path(portfolio_demo.__file__).resolve().parents[1]
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(project_root))
    monkeypatch.setattr(
        portfolio_demo,
        "_load_first_sample",
        lambda _path: pytest.fail("must reject unsafe temp root before reading input"),
    )
    with pytest.raises(RuntimeError, match="outside the project tree"):
        portfolio_demo.run_demo(Path("missing-input.json"))


def test_cli_writes_aggregate_and_separate_runtime_receipt(tmp_path: Path, capsys: Any) -> None:
    data = tmp_path / "synthetic.json"
    output = tmp_path / "result.json"
    receipt = tmp_path / "runtime.json"
    data.write_text(json.dumps(_payload()), encoding="utf-8")

    portfolio_demo.main(
        [
            "--data",
            str(data),
            "--table",
            "--output",
            str(output),
            "--receipt",
            str(receipt),
        ]
    )

    printed = capsys.readouterr().out
    assert printed.count("\n") == 7
    assert "no_forgetting_reference" in printed
    result = json.loads(output.read_text(encoding="utf-8"))
    runtime = json.loads(receipt.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert set(runtime) == {"runtime_seconds"}
    assert runtime["runtime_seconds"] > 0
    assert "synthetic.json" not in output.read_text(encoding="utf-8")
