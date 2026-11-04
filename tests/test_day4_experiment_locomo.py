from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import eval_locomo

FIXTURES = Path(__file__).parent / "fixtures" / "day4_locomo"


def test_three_local_locomo_shaped_fixtures_have_grounded_evidence() -> None:
    paths = sorted(FIXTURES.glob("fixture_*.json"))
    assert len(paths) == 3
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        turns, questions = eval_locomo.adapt(payload)
        original_body_bytes = sum(len(turn["text"].encode("utf-8")) for turn in turns)
        result = eval_locomo.evaluate_file(path, budget_tokens=128)
        assert result["question_count"] == 2
        assert result["budget_tokens_per_query"] == 128
        assert result["answer_generation"] == "none; evidence retrieval only"
        assert result["retention_budget"]["initial_count"] == result["turn_count"]
        assert result["retention_budget"]["type"] == "body_utf8_bytes"
        assert result["retention_budget"]["initial_value"] == original_body_bytes
        assert result["retention_budget"]["value"] <= original_body_bytes
        assert result["policies_evaluated"] == ["fifo"]
        assert set(result["budget_results"]) == {"10%", "25%", "50%"}
        assert result["adapter_counts"]["questions_without_valid_evidence"] == 0
        assert all(question["evidence"] for question in questions)
        for ratio, policy_results in result["budget_results"].items():
            outcome = policy_results["fifo"]
            budget = outcome["retention_budget"]["value"]
            assert outcome["retention_budget"]["initial_value"] == original_body_bytes
            assert outcome["retention_budget"]["ratio"] == int(ratio[:-1]) / 100
            assert outcome["memfw_retained_body_bytes"] <= budget
            assert outcome["baseline_retained_body_bytes"] <= budget
            assert outcome["budget_satisfied"] is True
            assert outcome["baseline_budget_satisfied"] is True
            for system in ("memfw", "bm25", "offline_mem0_style_crud"):
                score = outcome["after_budget"][system]
                assert score["evidence_total"] == len(questions)
                assert 0 <= score["evidence_retrieved"] <= score["evidence_total"]
                assert 0 <= score["evidence_recall"] <= 1
        assert "not official Mem0" in result["offline_mem0_style_crud_description"]


def test_adapter_rejects_unknown_evidence_ids() -> None:
    payload = {
        "conversation": {"session_1": [{"dia_id": "D1:1", "text": "Example"}]},
        "qa": [{"question": "Where?", "evidence": ["D9:9"]}],
    }
    try:
        eval_locomo.adapt(payload)
    except ValueError as exc:
        assert "unknown dia_id" in str(exc)
    else:
        raise AssertionError("unknown evidence reference was accepted")


def test_adapter_accepts_actual_top_level_sample_list() -> None:
    payload = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))
    ]
    turns, questions = eval_locomo.adapt(payload)
    assert len(turns) == 13
    assert len(questions) == 6
    ids = {row["dia_id"] for row in turns}
    assert all(evidence_id in ids for row in questions for evidence_id in row["evidence"])
    assert len(ids) == len(turns)
