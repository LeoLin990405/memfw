from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import correct_locomo_baselines
import eval_locomo


def test_official_shape_adapter_skips_and_counts_malformed_evidence() -> None:
    payload = {
        "conversation": {
            "speaker_a": "A",
            "session_1_date_time": "1:00 pm on 1 January, 2024",
            "session_1": [
                {"dia_id": "D1:1", "speaker": "A", "text": "We met in Oslo."},
                {"dia_id": "D1:2", "speaker": "B", "text": "The train left at noon."},
                {"dia_id": "D11:26", "speaker": "A", "text": "We kept the address."},
            ],
        },
        "qa": [
            {
                "question": "Where did they meet?",
                "evidence": ["D1:1; D 1 : 2; D:11:26; malformed", "D9:3"],
                "category": 1,
            }
        ],
    }
    turns, questions, counts = eval_locomo.adapt_with_stats(payload)
    assert [row["dia_id"] for row in turns] == ["D1:1", "D1:2", "D11:26"]
    assert questions[0]["evidence"] == ["D1:1", "D1:2", "D11:26"]
    assert counts["invalid_evidence_fragments"] == 1
    assert counts["unknown_evidence_references"] == 1
    assert counts["questions_without_valid_evidence"] == 0
    assert counts["category_counts"] == {"1": 1, "2": 0, "3": 0, "4": 0, "5": 0}
    assert counts["timestamp_fallback_turns"] == 0
    assert turns[0]["created_at"].startswith("2024-01-01T13:00:00")
    assert counts["category_evidence_counts"]["1"]["valid_evidence_questions"] == 1


def test_top_level_sample_list_aggregates_categories_and_invalid_counts() -> None:
    samples = [
        {
            "conversation": {"session_1": [{"dia_id": "D1:1", "text": "One"}]},
            "qa": [{"question": "Q1", "evidence": "D1:1", "category": 5}],
        },
        {
            "conversation": {"session_1": [{"dia_id": "D1:1", "text": "Two"}]},
            "qa": [{"question": "Q2", "evidence": "bad-ref; D:11:26", "category": 2}],
        },
    ]
    turns, questions, counts = eval_locomo.adapt_with_stats(samples)
    assert len(turns) == 2
    assert len({row["dia_id"] for row in turns}) == 2
    assert len(questions) == 2
    assert counts["samples"] == 2
    assert counts["invalid_evidence_fragments"] == 1
    assert counts["questions_without_valid_evidence"] == 1
    assert counts["category_counts"]["5"] == 1
    assert counts["category_counts"]["2"] == 1


def test_bm25_is_deterministic_and_orders_rare_term_match_first() -> None:
    turns = [
        {"dia_id": "a", "text": "train train schedule"},
        {"dia_id": "b", "text": "train route"},
        {"dia_id": "c", "text": "museum gallery"},
    ]
    first = eval_locomo._bm25_order("train schedule", turns)
    assert first == eval_locomo._bm25_order("train schedule", turns)
    assert first[0] == "a"


def test_baseline_fifo_eviction_keeps_newest_suffix_without_skipping() -> None:
    turns = [
        {"dia_id": "old", "text": "old"},
        {"dia_id": "middle", "text": "middle"},
        {"dia_id": "new", "text": "new"},
    ]
    bodies = {"old": "123456", "middle": "1234", "new": "5678"}

    assert eval_locomo._fifo_fit_ids(turns, bodies, budget_bytes=8) == ["middle", "new"]


def test_baseline_correction_preserves_memfw_and_updates_aliases(tmp_path: Path) -> None:
    payload = {
        "conversation": {
            "session_1": [
                {"dia_id": "D1:1", "speaker": "A", "text": "Old fact about amber."},
                {"dia_id": "D1:2", "speaker": "B", "text": "New fact about cobalt."},
            ]
        },
        "qa": [{"question": "What is the new fact about?", "evidence": "D1:2", "category": 1}],
    }
    data = tmp_path / "data.json"
    raw_path = tmp_path / "raw.json"
    data.write_text(json.dumps([payload]), encoding="utf-8")
    raw_result = eval_locomo.evaluate_file(
        data,
        budget_tokens=128,
        policies=("fifo", "lru", "decay", "salience", "consolidate_then_forget"),
    )
    raw = {
        "query_context_budget_tokens": 128,
        "dataset_sources": [
            {"sha256": hashlib.sha256(data.read_bytes()).hexdigest(), "path": str(data)}
        ],
        "results": [raw_result],
    }
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    memfw_before = {
        (budget, policy): sample["after_budget"]["memfw"]
        for sample in raw_result["sample_results"]
        for budget, policies in sample["budget_results"].items()
        for policy, sample in policies.items()
    }

    corrected = correct_locomo_baselines.correct_baselines(data, raw_path, query_budget=128)
    corrected_sample = corrected["results"][0]["sample_results"][0]

    assert corrected["baseline_correction"]["raw_memfw_metrics_unchanged"] is True
    assert (
        corrected["baseline_correction"]["raw_memfw_projection_sha256"]
        == corrected["baseline_correction"]["corrected_memfw_projection_sha256"]
    )
    assert (
        corrected["baseline_correction"]["dataset_sha256"]
        == hashlib.sha256(data.read_bytes()).hexdigest()
    )
    for budget, policies in corrected_sample["budget_results"].items():
        for policy, sample in policies.items():
            assert sample["after_budget"]["memfw"] == memfw_before[(budget, policy)]
            assert (
                sample["after_budget"]["offline_mock_crud"]
                == sample["after_budget"]["offline_mem0_style_crud"]
            )
            assert (
                sample["before_budget"]["offline_mock_crud"]
                == sample["before_budget"]["offline_mem0_style_crud"]
            )

    try:
        correct_locomo_baselines.correct_baselines(data, raw_path, query_budget=64)
    except ValueError as exc:
        assert "query budget" in str(exc)
    else:
        raise AssertionError("baseline correction accepted a mismatched query budget")

    try:
        correct_locomo_baselines._validate_output_path(data, raw_path, raw_path)
    except ValueError as exc:
        assert "must not overwrite" in str(exc)
    else:
        raise AssertionError("baseline correction accepted an output path that overwrites raw data")

    try:
        correct_locomo_baselines._validate_output_path(data, raw_path, data)
    except ValueError as exc:
        assert "must not overwrite" in str(exc)
    else:
        raise AssertionError(
            "baseline correction accepted an output path that overwrites the dataset"
        )


def test_three_day5_fixtures_exercise_all_policies_and_category_metrics() -> None:
    fixtures = sorted((Path(__file__).parent / "fixtures" / "day5_locomo").glob("fixture_*.json"))
    assert len(fixtures) == 3
    policies = ("fifo", "lru", "decay", "salience", "consolidate_then_forget")
    for fixture in fixtures:
        result = eval_locomo.evaluate_file(
            fixture,
            budget_tokens=128,
            policies=policies,
            retention_ratios=(0.10, 0.25, 0.50),
        )
        assert result["policies_evaluated"] == list(policies)
        assert set(result["budget_results"]) == {"10%", "25%", "50%"}
        for ratio_result in result["budget_results"].values():
            assert set(ratio_result) == set(policies)
            for policy_result in ratio_result.values():
                assert policy_result["retention_budget"]["type"] == "body_utf8_bytes"
                assert (
                    policy_result["memfw_retained_body_bytes"]
                    <= policy_result["retention_budget"]["value"]
                )
                assert policy_result["baseline_budget_satisfied"]
                assert policy_result["memory_access_event_count"] == 0
                assert policy_result["answer_generation"] == "none; evidence retrieval only"
                assert (
                    "add/update/delete" in policy_result["offline_mock_extraction_crud_description"]
                )
                for phase in ("before_budget", "after_budget"):
                    for system in ("memfw", "bm25", "offline_mock_crud"):
                        categories = policy_result[phase][system]["by_category"]
                        assert set(categories) == {"1", "2", "3", "4", "5"}
                        assert categories["5"]["evidence_total"] in {0, 1}


def test_top_level_official_list_keeps_each_sample_in_its_own_result(tmp_path: Path) -> None:
    payloads = [
        {
            "conversation": {"session_1": [{"dia_id": "D1:1", "text": "First fact."}]},
            "qa": [{"question": "First fact?", "evidence": "D1:1", "category": 1}],
        },
        {
            "conversation": {"session_1": [{"dia_id": "D1:1", "text": "Second fact."}]},
            "qa": [{"question": "Second fact?", "evidence": "D1:1", "category": 2}],
        },
    ]
    source = tmp_path / "two_samples.json"
    checkpoint = tmp_path / "checkpoint.json"
    source.write_text(json.dumps(payloads), encoding="utf-8")
    try:
        result = eval_locomo.evaluate_file(
            source,
            policies=("fifo",),
            retention_ratios=(0.5,),
            checkpoint_path=checkpoint,
        )
    finally:
        source.unlink()
    assert result["sample_count"] == 2
    assert [row["sample_index"] for row in result["sample_results"]] == [0, 1]
    assert [row["question_count"] for row in result["sample_results"]] == [1, 1]
    checkpoint_result = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_result["status"] == "complete"
    assert checkpoint_result["completed_samples"] == checkpoint_result["expected_samples"] == 2
    assert '"text":' not in checkpoint.read_text(encoding="utf-8")


def test_long_query_workload_does_not_exhaust_old_dynamic_event_id_range(tmp_path: Path) -> None:
    turns = [
        {"dia_id": f"D1:{index}", "speaker": "A", "text": f"commonfable alpha note{index}"}
        for index in range(1, 36)
    ]
    questions = [
        {"question": "commonfable alpha", "evidence": "D1:1", "category": 1} for _ in range(100)
    ]
    source = tmp_path / "long.json"
    source.write_text(
        json.dumps({"conversation": {"session_1": turns}, "qa": questions}),
        encoding="utf-8",
    )
    result = eval_locomo.evaluate_file(source, policies=("fifo",), retention_ratios=(0.5,))
    row = result["budget_results"]["50%"]["fifo"]
    old_limit = len(turns) * 50 + 1
    assert len(questions) * 2 * len(turns) > old_limit
    assert row["memory_access_event_count"] == 0
    assert row["event_count"] < old_limit


def test_event_id_factory_is_unique_beyond_old_dynamic_range() -> None:
    turn_count = 36
    old_dynamic_limit = turn_count * 50 + 1
    next_id = eval_locomo._event_id_factory()
    ids = [next_id() for _ in range(old_dynamic_limit + 1)]

    assert len(ids) == old_dynamic_limit + 1
    assert len(set(ids)) == len(ids)
