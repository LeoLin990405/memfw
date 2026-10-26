"""Unit tests for the Step G runner (no mem0 / no model server required)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_metrics import (  # noqa: E402
    aggregate_system,
    exact_paired_permutation,
    exact_wilcoxon,
    judge_is_correct,
    paired_comparison,
)
from eval.stepG_provenance import (  # noqa: E402
    classify_memory,
    classify_system,
    summarize_frame,
    write_rubric,
)
from eval.stepG_runner import (  # noqa: E402
    build_subset,
    fit_context,
    load_config,
    run_system,
    token_f1,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeChat:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> tuple[str, dict[str, int]]:
        self.calls += 1
        return "unknown", {"prompt_tokens": 10, "completion_tokens": 1}


class FakeSystem:
    name = "fake"

    def __init__(self) -> None:
        self.added: list[dict[str, str]] = []
        self.storage = 0

    def add(self, turn: dict[str, str]) -> None:
        self.added.append(turn)
        self.storage += len(turn["text"].encode("utf-8"))

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        return [
            {"id": f"m{index}", "memory": f"memory {index}", "dia_ids": [], "score": 1.0}
            for index in range(min(top_k, 2))
        ]

    def export_memories(self) -> list[dict[str, Any]]:
        return [{"memory_id": "m1", "memory": "body", "dia_ids": ["D1:1"]}]

    def memory_count(self) -> int:
        return len(self.added)

    def storage_bytes(self) -> int:
        return self.storage

    def token_usage(self) -> dict[str, int]:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def _turns(count: int = 3) -> list[dict[str, str]]:
    return [
        {
            "dia_id": f"D1:{index}",
            "speaker": "Sam",
            "text": f"turn number {index} about the greenhouse sensor",
            "created_at": f"2023-05-18T13:4{index}:00+00:00",
        }
        for index in range(count)
    ]


def _questions(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "question_id": _sha(f"question {index}"),
            "qa_index": index,
            "category": 1,
            "question": f"question {index}?",
            "answer": f"answer {index}",
            "evidence_turn_ids": ["D1:1"],
        }
        for index in range(count)
    ]


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "seed": 20261021,
        "llm": {"model": "fake", "transport": "ollama", "max_tokens": 8},
        "embedding": {"model": "fake-embed", "dims": 4},
        "retrieval": {"top_k": 2, "context_token_budget": 128},
        "runs": {"systems": ["memfw"], "repeats": 1},
    }


def test_load_config_requires_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_rejects_unknown_transport(tmp_path: Path) -> None:
    payload = {
        "schema_version": "stepg-config-v1",
        "seed": 1,
        "paths": {
            "results_dir": str(tmp_path),
            "data": "d.json",
            "subset": "s.json",
            "subset_manifest": "m.json",
        },
        "data": {"url": "http://example.invalid", "sha256": "0" * 64},
        "llm": {"transport": "carrier-pigeon", "base_url": "http://127.0.0.1:1"},
        "embedding": {"transport": "ollama"},
        "retrieval": {"top_k": 5, "context_token_budget": 128},
        "runs": {"systems": ["memfw"]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_build_subset_reconstructs_manifest_subset(tmp_path: Path) -> None:
    data = [
        {
            "sample_id": "conv-x",
            "conversation": {
                "session_1_date_time": "1:00 pm on 18 May, 2023",
                "session_1": [
                    {"dia_id": "D1:1", "speaker": "Sam", "text": "I like tea."},
                    {"dia_id": "D1:2", "speaker": "Evan", "text": "Tea is fine."},
                ],
            },
            "qa": [
                {
                    "question": "What does Sam like?",
                    "answer": "tea",
                    "category": 1,
                    "evidence": ["D1:1"],
                },
                {"question": "What is fine?", "answer": "tea", "category": 1, "evidence": ["D1:2"]},
            ],
        }
    ]
    manifest = {
        "source": {"sample_index": 0, "sample_id": "conv-x"},
        "scope": {
            "input_turn_ids": ["D1:1", "D1:2"],
            "input_turn_count": 2,
            "selected_question_count": 2,
        },
        "questions": [
            {
                "qa_index": 0,
                "category": 1,
                "question_sha256": _sha("What does Sam like?"),
                "evidence_turn_ids": ["D1:1"],
            },
            {
                "qa_index": 1,
                "category": 1,
                "question_sha256": _sha("What is fine?"),
                "evidence_turn_ids": ["D1:2"],
            },
        ],
    }
    data_path = tmp_path / "locomo.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = {
        "seed": 20261021,
        "paths": {
            "data": str(data_path),
            "subset": str(tmp_path / "subset.json"),
            "subset_manifest": str(manifest_path),
            "results_dir": str(tmp_path),
        },
        "data": {"sha256": hashlib.sha256(data_path.read_bytes()).hexdigest()},
    }
    subset = build_subset(config)
    assert len(subset["turns"]) == 2
    assert len(subset["questions"]) == 2
    assert subset["questions"][0]["answer"] == "tea"
    assert (tmp_path / "subset.json").exists()


def test_build_subset_rejects_question_hash_mismatch(tmp_path: Path) -> None:
    data = [
        {
            "sample_id": "conv-x",
            "conversation": {"session_1": [{"dia_id": "D1:1", "speaker": "Sam", "text": "hi"}]},
            "qa": [{"question": "q?", "answer": "a", "category": 1, "evidence": ["D1:1"]}],
        }
    ]
    data_path = tmp_path / "locomo.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    manifest = {
        "source": {"sample_index": 0, "sample_id": "conv-x"},
        "scope": {"input_turn_ids": ["D1:1"], "input_turn_count": 1, "selected_question_count": 1},
        "questions": [
            {
                "qa_index": 0,
                "category": 1,
                "question_sha256": "f" * 64,
                "evidence_turn_ids": ["D1:1"],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = {
        "seed": 1,
        "paths": {
            "data": str(data_path),
            "subset": str(tmp_path / "subset.json"),
            "subset_manifest": str(manifest_path),
            "results_dir": str(tmp_path),
        },
        "data": {"sha256": hashlib.sha256(data_path.read_bytes()).hexdigest()},
    }
    with pytest.raises(ValueError):
        build_subset(config)


def test_token_f1_and_category_five_abstention() -> None:
    assert token_f1("Rockies, Jasper", "Rockies, Jasper") == pytest.approx(1.0)
    assert token_f1("unknown", "Rockies") == 0.0
    from eval.stepG_runner import answer_score

    assert answer_score("unknown", None, 5) == 1.0
    assert answer_score("two", "two", 1) == pytest.approx(1.0)


def test_fit_context_respects_token_budget() -> None:
    items = [{"memory": "one two three"}, {"memory": "four five six"}]
    selected = fit_context(items, 3)
    assert selected == [{"memory": "one two three"}]


def test_run_system_writes_rows_and_resumes(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "rep1" / "fake"
    chat = FakeChat()
    system = FakeSystem()
    config = _config(tmp_path)
    summary = run_system(
        system=system,
        system_name="fake",
        run_id="rep1",
        turns=_turns(),
        questions=_questions(),
        config=config,
        run_dir=run_dir,
        chat=chat,
    )
    assert summary["question_rows"] == 3
    assert summary["memory_count"] == 3
    rows = [json.loads(line) for line in (run_dir / "questions.jsonl").read_text().splitlines()]
    assert {row["question_id"] for row in rows} == {
        _sha("question 0"),
        _sha("question 1"),
        _sha("question 2"),
    }
    assert all(row["retrieved"] for row in rows)
    # Resume: second invocation must not duplicate rows or re-ingest.
    calls_before = chat.calls
    add_count_before = len(system.added)
    run_system(
        system=system,
        system_name="fake",
        run_id="rep1",
        turns=_turns(),
        questions=_questions(),
        config=config,
        run_dir=run_dir,
        chat=chat,
    )
    assert len((run_dir / "questions.jsonl").read_text().splitlines()) == 3
    assert chat.calls == calls_before
    assert len(system.added) == add_count_before


def test_run_system_dry_run_limits_questions(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "dry-run" / "fake"
    summary = run_system(
        system=FakeSystem(),
        system_name="fake",
        run_id="dry-run",
        turns=_turns(),
        questions=_questions(5),
        config=_config(tmp_path),
        run_dir=run_dir,
        chat=FakeChat(),
        dry_run=True,
    )
    assert summary["question_rows"] == 2


def _metric_rows(scores: list[float]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {"rep1": []}
    for index, score in enumerate(scores):
        rows["rep1"].append(
            {
                "question_id": f"q{index}",
                "category": 1,
                "answer_score": score,
                "token_f1": score,
                "retrieval_seconds": 0.01,
                "answer_seconds": 0.02,
                "storage_bytes": 100,
                "memory_count": 10,
            }
        )
    return rows


def test_aggregate_system_uses_question_means() -> None:
    aggregate = aggregate_system(_metric_rows([1.0, 0.0]))
    assert aggregate["mean_answer_score"] == pytest.approx(0.5)
    assert aggregate["question_count"] == 2


def test_exact_paired_tests_on_all_positive_differences() -> None:
    diffs = [0.1, 0.2, 0.3, 0.4, 0.5]
    permutation = exact_paired_permutation(diffs)
    assert permutation["p_value"] == pytest.approx(2 / 32)
    wilcoxon = exact_wilcoxon(diffs)
    assert wilcoxon["p_value"] == pytest.approx(2 / 32)


def test_paired_comparison_matches_by_question() -> None:
    memfw = {"q1": 1.0, "q2": 0.5}
    mem0 = {"q1": 0.5, "q2": 0.5}
    result = paired_comparison(memfw, mem0)
    assert result["n_pairs"] == 2
    assert result["mean_difference"] == pytest.approx(0.25)


def test_judge_is_correct_handles_adversarial() -> None:
    assert judge_is_correct({"category": 5, "answer": "unknown"}, "incorrect")
    assert not judge_is_correct({"category": 5, "answer": "Rockies"}, "incorrect")
    assert judge_is_correct({"category": 1, "answer": "tea"}, "correct")


def test_provenance_classification_and_sampling() -> None:
    verdict = classify_memory(
        "I like tea",
        "I like tea very much.",
        "Sam",
        {},
        attributed_to_user=True,
    )
    assert verdict["label"] == "verbatim_source_text"
    assert not verdict["flagged"]
    derived = classify_memory(
        "Sam is a tea enthusiast",
        "I like tea very much.",
        "Sam",
        {},
        attributed_to_user=True,
    )
    assert derived["label"] == "derived_attributed_to_user_unlabelled"
    assert derived["flagged"]
    labelled = classify_memory(
        "Sam is a tea enthusiast",
        "I like tea very much.",
        "Sam",
        {"inferred": True},
        attributed_to_user=True,
    )
    assert labelled["label"] == "derived_labelled"
    assert not labelled["flagged"]


def test_classify_system_and_frame_summary(tmp_path: Path) -> None:
    turn_index = {"D1:1": {"dia_id": "D1:1", "speaker": "Sam", "text": "I like tea very much."}}
    memories = [
        {
            "memory_id": "m1",
            "memory": "I like tea very much.",
            "dia_ids": ["D1:1"],
            "provenance": "verbatim",
        },
        {
            "memory_id": "m2",
            "memory": "Sam enjoys tea",
            "dia_ids": ["D1:1"],
            "provenance": "verbatim",
        },
    ]
    rows = classify_system("memfw", memories, turn_index)
    summary = summarize_frame(rows)
    assert summary["sampled"] == 2
    assert summary["derived"] == 1
    rubric = tmp_path / "rubric.md"
    write_rubric(rubric)
    assert "R3" in rubric.read_text(encoding="utf-8")


def test_aggregate_recall_pre_post_fit_and_cat5_f1() -> None:
    from eval.stepG_metrics import aggregate_system

    rows = {
        "rep1": [
            {
                "question_id": "q1",
                "category": 1,
                "answer_score": 1.0,
                "token_f1": 1.0,
                "retrieval_seconds": 0.01,
                "answer_seconds": 0.02,
                "evidence_turn_ids": ["D1:1"],
                "retrieved": [
                    {"id": "m1", "dia_ids": ["D1:1"], "memory": "a"},
                    {"id": "m2", "dia_ids": ["D1:2"], "memory": "b"},
                ],
                "context_memories": ["b"],
            },
            {
                "question_id": "q2",
                "category": 5,
                "answer_score": 1.0,
                "token_f1": 0.0,
                "retrieval_seconds": 0.01,
                "answer_seconds": 0.02,
                "evidence_turn_ids": ["D1:2"],
                "retrieved": [{"id": "m2", "dia_ids": ["D1:2"], "memory": "b"}],
                "context_memories": ["b"],
            },
        ]
    }
    aggregate = aggregate_system(rows)
    assert aggregate["micro_evidence_recall_prefit"] == pytest.approx(1.0)
    assert aggregate["micro_evidence_recall_postfit"] == pytest.approx(0.5)
    assert aggregate["mean_evidence_recall_postfit"] == pytest.approx(0.5)
    # category-5 adversarial token F1 is excluded from the aggregate
    assert aggregate["scored_question_count_non_adversarial"] == 1
    assert aggregate["mean_token_f1"] == pytest.approx(1.0)


def test_judge_abstention_markers_match_primary_metric() -> None:
    # "cannot" is not an abstention marker in answer_score, so it must not count here either.
    assert not judge_is_correct({"category": 5, "answer": "cannot tell"}, "incorrect")
    assert judge_is_correct({"category": 5, "answer": "unknown"}, "incorrect")


def test_provenance_day11_attribution_rule_differs_from_stepg_rule() -> None:
    turn_index = {"D1:1": {"dia_id": "D1:1", "speaker": "Sam", "text": "I like tea very much."}}
    memory = {
        "memory_id": "m1",
        "memory": "Sam enjoys tea",
        "dia_ids": ["D1:1"],
        "metadata": {},
    }
    stepg = classify_system("mem0", [memory], turn_index)
    day11 = classify_system("mem0", [memory], turn_index, day11_attribution=True)
    assert stepg[0]["flagged"] is True  # mem0 user namespace by default
    assert day11[0]["flagged"] is False  # no explicit attributed_to == "user"


def test_report_tables_have_matching_header_and_row_width() -> None:
    from eval.stepG_report import _comparison_table, _provenance_table, _quality_and_efficiency

    def _aggregate(score: float) -> dict[str, Any]:
        return {
            "mean_answer_score": score,
            "mean_token_f1": 0.3,
            "mean_evidence_recall_postfit": 0.7,
            "micro_evidence_recall_postfit": 0.6,
            "mean_evidence_recall_prefit": 0.7,
            "question_count": 18,
            "repeats": 3,
            "ingest_seconds_mean": 2.0,
            "write_latency": {"mean_seconds": 0.02},
            "write_latency_turns": {"p50_seconds": 0.019, "p95_seconds": 0.03},
            "retrieval_latency": {"p50_seconds": 0.4, "p95_seconds": 0.5},
            "answer_latency": {"p50_seconds": 1.2},
            "total_token_usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "memory_count_mean": 100,
            "storage_bytes_mean": 1000,
        }

    metrics = {
        "arms": ["memfw", "mem0_stock"],
        "systems": {"memfw": _aggregate(0.4), "mem0_stock": _aggregate(0.5)},
        "judge": {
            "models": [
                {
                    "label": "primary",
                    "model": "qwen3.5:4b",
                    "accuracy": {
                        "memfw": {"accuracy": 0.4},
                        "mem0_stock": {"accuracy": 0.5},
                    },
                }
            ]
        },
        "comparisons": [],
    }
    text = _quality_and_efficiency(metrics)
    quality_block = text.split("### 效率")[0]
    widths = {len(line.split("|")) for line in quality_block.splitlines() if line.startswith("|")}
    assert len(widths) == 1, widths

    pair = {
        "mean_memfw": 0.4,
        "mean_mem0": 0.5,
        "mean_difference": -0.1,
        "difference_ci95": [-0.3, 0.1],
        "exact_sign_flip": {"p_value": 0.5},
        "exact_wilcoxon": {"p_value": 0.6},
        "cohen_dz": -0.1,
        "minimum_detectable_difference": 0.2,
    }
    comparison = {
        "label": "memfw vs mem0_stock",
        "a": "memfw",
        "b": "mem0_stock",
        "answer_score": pair,
        "token_f1": pair,
        "evidence_recall": pair,
        "judge_primary": pair,
    }
    comparison_text = _comparison_table(comparison)
    comparison_widths = {
        len(line.split("|")) for line in comparison_text.splitlines() if line.startswith("|")
    }
    assert len(comparison_widths) == 1, comparison_widths

    provenance = {
        "systems": {
            "memfw": {
                "memories_total": 100,
                "derived_total": 0,
                "derived_frame": {"sampled": 0, "derived": 0, "flagged": 0},
                "all_frame": {"sampled": 30, "derived": 0, "flagged": 0},
                "day11_frame": {"sampled": 30, "derived": 0, "flagged": 0},
                "derived_shortfall": 30,
            },
            "mem0_stock": {
                "memories_total": 94,
                "derived_total": 94,
                "derived_frame": {"sampled": 30, "derived": 30, "flagged": 21},
                "all_frame": {"sampled": 30, "derived": 30, "flagged": 21},
                "day11_frame": {"sampled": 30, "derived": 30, "flagged": 21},
                "derived_shortfall": 0,
            },
        }
    }
    prov_text = _provenance_table(provenance)
    prov_widths = {len(line.split("|")) for line in prov_text.splitlines() if line.startswith("|")}
    assert len(prov_widths) == 1, prov_widths


def test_arm_mapping_includes_stock_and_custom_mem0() -> None:
    from eval.stepG_runner import SYSTEMS

    assert SYSTEMS["memfw"] == "memfw"
    assert SYSTEMS["mem0_stock"] == "mem0_stock"
    assert SYSTEMS["mem0_custom"] == "mem0_custom"
    assert SYSTEMS["mem0_stock_day11ctx"] == "mem0_stock_day11ctx"
    assert SYSTEMS["mem0"] == "mem0_custom"  # backwards-compatible alias


def test_report_loads_preliminary_numbers_from_committed_json() -> None:
    from eval.stepG_report import _load_preliminary

    preliminary = _load_preliminary()
    assert preliminary is not None
    assert preliminary["answer_memfw"] == pytest.approx(0.45906084656084656)
    assert preliminary["recall_micro_memfw"] == pytest.approx(2 / 3)
    assert preliminary["prov_flagged_mem0"] == 21


def test_expanded_subset_selects_all_eligible_questions(tmp_path: Path) -> None:
    from eval.stepG_expanded import build_expanded_subset

    data = [
        {
            "sample_id": "conv-x",
            "conversation": {
                "session_1": [
                    {"dia_id": "D1:1", "speaker": "Sam", "text": "I like tea."},
                    {"dia_id": "D1:2", "speaker": "Evan", "text": "Tea is fine."},
                ],
                "session_2": [{"dia_id": "D9:1", "speaker": "Sam", "text": "Later."}],
            },
            "qa": [
                {"question": "q1?", "answer": "a1", "category": 1, "evidence": ["D1:1"]},
                {"question": "q2?", "answer": "a2", "category": 1, "evidence": ["D1:2"]},
                {"question": "q3?", "answer": "a3", "category": 1, "evidence": ["D9:1"]},
            ],
        }
    ]
    data_path = tmp_path / "locomo.json"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    manifest = {
        "source": {"sample_index": 0, "sample_id": "conv-x"},
        "scope": {
            "input_turn_ids": ["D1:1", "D1:2"],
            "input_turn_count": 2,
            "eligible_question_counts_by_category": {"1": 2},
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = {
        "seed": 20261021,
        "paths": {
            "data": str(data_path),
            "subset": str(tmp_path / "subset.json"),
            "subset_manifest": str(manifest_path),
            "results_dir": str(tmp_path),
        },
        "data": {"sha256": hashlib.sha256(data_path.read_bytes()).hexdigest()},
    }
    subset = build_expanded_subset(config)
    assert subset["scope"]["question_count"] == 2
    assert subset["scope"]["eligible_counts_by_category"] == {
        "1": 2,
        "2": 0,
        "3": 0,
        "4": 0,
        "5": 0,
    }
    assert (tmp_path / "subset_expanded.json").exists()


def test_judge_prediction_hash_changes_with_answer() -> None:
    from eval.stepG_metrics import _prediction_hash

    row_a = {"answer": "tea", "gold_answer": "tea"}
    row_b = {"answer": "coffee", "gold_answer": "tea"}
    assert _prediction_hash(row_a) != _prediction_hash(row_b)
    assert _prediction_hash(row_a) == _prediction_hash(dict(row_a))


def test_mem0_add_kwargs_only_passes_prompt_for_custom_arm() -> None:
    from eval.stepG_runner import mem0_add_kwargs

    turn = {"dia_id": "D1:1"}
    stock = mem0_add_kwargs(turn, "u", None)
    custom = mem0_add_kwargs(turn, "u", "Return JSON only.")
    assert "prompt" not in stock
    assert custom["prompt"] == "Return JSON only."
    assert stock["metadata"] == {"dia_id": "D1:1"}
    assert stock["infer"] is True


def test_metrics_rows_normalize_legacy_arm_name(tmp_path: Path) -> None:
    from eval.stepG_metrics import load_rows

    run_dir = tmp_path / "runs" / "rep1" / "mem0_custom"
    run_dir.mkdir(parents=True)
    (run_dir / "questions.jsonl").write_text(
        json.dumps({"run_id": "rep1", "system": "mem0", "question_id": "q1"}) + "\n"
    )
    rows = load_rows(tmp_path)
    assert list(rows) == ["mem0_custom"]
    assert rows["mem0_custom"]["rep1"][0]["system"] == "mem0_custom"


def test_redaction_hides_machine_paths() -> None:
    from pathlib import Path

    from eval.stepG_runner import REPO_ROOT, redact_text

    assert redact_text(f"{REPO_ROOT}/results/stepG") == "$PROJECT_ROOT/results/stepG"
    assert "python-keyness" not in redact_text(f"{REPO_ROOT}/src/memfw/__init__.py")
    assert str(Path.home()) not in redact_text(f"{Path.home()}/Documents/private.txt")


def test_day11ctx_factory_uses_day11_num_ctx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import eval.stepG_runner as runner

    captured: dict[str, object] = {}

    class FakeMem0:
        def __init__(
            self,
            root: Path,
            config: dict[str, object],
            *,
            custom_instructions: bool = True,
            num_ctx: int | None = None,
        ) -> None:
            captured.update(
                root=root,
                custom_instructions=custom_instructions,
                num_ctx=num_ctx,
            )

    monkeypatch.setattr(runner, "Mem0System", FakeMem0)
    config = {
        "paths": {"results_dir": str(tmp_path)},
        "mem0": {"day11_num_ctx": 8192},
        "runs": {"mem0": {"collection_name": "x"}},
    }
    runner._make_system("mem0_stock_day11ctx", config, "rep1", object())  # type: ignore[arg-type]
    assert captured["num_ctx"] == 8192
    assert captured["custom_instructions"] is False
