from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day7_eval
import eval_locomo

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import tokenize

FIXTURE = Path(__file__).parent / "fixtures" / "day4_locomo" / "fixture_1.json"


def _fixture_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_day7_all_policies_share_budgets_access_flow_and_oracle_bound() -> None:
    payload = _fixture_payload()
    turns, questions, stats = eval_locomo.adapt_with_stats(payload)
    turns[0]["text"] += "\n"
    result = day7_eval._evaluate_sample(
        turns,
        questions,
        stats,
        sample_index=0,
        sample_id="local-fixture",
        seed=20261018,
        token_budget=128,
        ratios=(0.10, 0.25),
    )
    assert result["keyness_configuration"]["enabled"] is True
    assert result["keyness_configuration"]["window_size"] == 20
    assert result["raw_turn_text_utf8_bytes"] == result["initial_body_utf8_bytes"] + 1
    assert result["scopes"]["keyness_only"]["access_flow_count"] == 0
    assert result["scopes"]["keyness_plus_shared_access"]["access_flow_count"] > 0
    assert (
        result["scopes"]["keyness_plus_shared_access"]["access_flow_sha256"]
        == result["shared_access_flow_sha256"]
    )
    for scope in result["scopes"].values():
        for profile in scope["profiles"].values():
            systems = profile["systems"]
            assert len(systems) == 13
            for policy in day7_eval.POLICIES:
                assert (
                    systems[f"{policy}_fts"]["retained_ids"]
                    == systems[f"{policy}_bm25"]["retained_ids"]
                )
            for name, metrics in systems.items():
                if name != "oracle_question_conditioned":
                    assert metrics["retention_budget_satisfied"] is True
                    assert metrics["retained_body_utf8_bytes"] <= profile["capacity_bytes"]
            oracle_hits = [
                row["hits"]["oracle_question_conditioned"] for row in profile["per_question"]
            ]
            for row in profile["per_question"]:
                for _name, hits in row["hits"].items():
                    assert hits <= row["hits"]["oracle_question_conditioned"]
            assert len(oracle_hits) == sum(bool(question["evidence"]) for question in questions)


def test_day7_fts_count_and_bm25_match_index_rankers(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("speaker Maya", "The cabinet installer arrives October 22.", memory_id="d1-1")
    repository.add("speaker Alex", "The spare key is in the blue drawer.", memory_id="d1-2")
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    query = "cabinet installer date"
    questions = [{"question": query, "evidence": ["D1:1"]}]
    mapping = {"d1-1": "D1:1", "d1-2": "D1:2"}
    all_ids = set(mapping)
    hits = index.search_many(day7_eval._query_terms(questions), limit=10)
    count_order = day7_eval._fts_orders(index, questions, mapping, all_ids, all_ids, hits)[0]
    ranked_count = [
        identifier
        for identifier, _ in index.ranked_search(query, ranking="count", candidate_ids=all_ids)
    ]
    assert count_order == [mapping[identifier] for identifier in ranked_count]

    docs = repository.list()
    scorer = BM25Scorer(
        {doc.id: " ".join((doc.title, doc.body, doc.summary, " ".join(doc.tags))) for doc in docs}
    )
    ranked_bm25 = [
        identifier
        for identifier, _ in index.ranked_search(query, ranking="bm25", candidate_ids=all_ids)
    ]
    assert scorer.rank(query) == ranked_bm25
    assert tokenize(query)


def test_day7_entrypoint_writes_final_single_run_result(tmp_path: Path, monkeypatch: Any) -> None:
    data = tmp_path / "fixture.json"
    output = tmp_path / "result.json"
    data.write_text(json.dumps([_fixture_payload()]), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_locomo.py",
            "--day7",
            "--data",
            str(data),
            "--output",
            str(output),
            "--quick",
            "--gating-count",
            "20",
        ],
    )
    eval_locomo.main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["command_mode"] == "quick_all_samples_all_questions"
    assert result["sample_count"] == 1
    assert (
        len(result["samples"][0]["scopes"]["keyness_only"]["profiles"]["10%"]["per_question"]) == 2
    )
    assert result["bootstrap"]["keyness_only/10%"]["repeats"] == 1000
    assert len(result["bootstrap"]["keyness_only/10%"]["scopes"]["overall"]["pairwise"]) == 10
    assert result["capture_gating"]["count"] == 20
    assert result["end_to_end_runtime_seconds"] > 0
