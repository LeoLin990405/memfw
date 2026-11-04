#!/usr/bin/env python3
"""Recompute only local LoCoMo BM25/mock baselines using true oldest-first eviction."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_locomo


def _mock_facts(turns: list[dict[str, Any]]) -> dict[str, str]:
    return {
        row["dia_id"]: row["text"].split(".", 1)[0].split("。", 1)[0].strip() or row["text"].strip()
        for row in turns
    }


def _memfw_projection(sample_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "memfw_retained_count",
        "memfw_retained_body_bytes",
        "event_count",
        "memory_access_event_count",
        "budget_satisfied",
        "memfw_forget_policy",
        "simulated_consolidation_reviews",
    )
    projection: list[dict[str, Any]] = []
    for sample in sample_results:
        row: dict[str, Any] = {
            key: sample.get(key)
            for key in ("sample_index", "sample_id", "turn_count", "question_count")
        }
        row.update({key: sample.get(key) for key in fields})
        for phase in ("before_budget", "after_budget"):
            if phase in sample:
                row[phase] = sample[phase].get("memfw")
        row["budget_results"] = {}
        for budget, policies in sample.get("budget_results", {}).items():
            row["budget_results"][budget] = {}
            for policy, result in policies.items():
                row["budget_results"][budget][policy] = {
                    **{key: result.get(key) for key in fields},
                    **{
                        phase: result[phase].get("memfw")
                        for phase in ("before_budget", "after_budget")
                    },
                }
        projection.append(row)
    return projection


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_output_path(data_path: Path, raw_path: Path, output_path: Path) -> None:
    resolved = output_path.resolve()
    if resolved in {data_path.resolve(), raw_path.resolve()}:
        raise ValueError("output must not overwrite the dataset or raw evaluation")


def correct_baselines(data_path: Path, raw_path: Path, *, query_budget: int) -> dict[str, Any]:
    dataset_sha = hashlib.sha256(data_path.read_bytes()).hexdigest()
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if raw.get("query_context_budget_tokens") != query_budget:
        raise ValueError("query budget must match the raw evaluation")
    source_matches = any(row.get("sha256") == dataset_sha for row in raw.get("dataset_sources", []))
    if not source_matches:
        raise ValueError("raw evaluation source hash does not match local dataset")
    samples = json.loads(data_path.read_text(encoding="utf-8"))
    raw_rows = raw.get("results", [])
    if len(raw_rows) != 1 or raw_rows[0].get("sample_count") != len(samples):
        raise ValueError("raw evaluation does not contain the same top-level sample list")
    raw_samples = raw_rows[0].get("sample_results", [])
    if len(raw_samples) != len(samples):
        raise ValueError("raw evaluation sample count mismatch")
    original_memfw_hash = _canonical_hash(_memfw_projection(raw_samples))

    corrected_report = copy.deepcopy(raw)
    corrected_samples = corrected_report["results"][0]["sample_results"]
    ratios = (0.10, 0.25, 0.50)
    for index, payload in enumerate(samples):
        turns, questions, _ = eval_locomo.adapt_with_stats(payload)
        raw_sample = raw_samples[index]
        if (
            raw_sample.get("sample_index") != index
            or raw_sample.get("sample_id") != payload.get("sample_id")
            or raw_sample.get("turn_count") != len(turns)
            or raw_sample.get("question_count") != len(questions)
            or raw_sample.get("budget_tokens_per_query") != query_budget
        ):
            raise ValueError(f"raw sample metadata mismatch at index {index}")
        facts = _mock_facts(turns)
        original_bodies = {row["dia_id"]: row["text"] for row in turns}
        original_context = {
            row["dia_id"]: f"speaker {row['speaker']} {row['text']}" for row in turns
        }
        fact_context = {
            row["dia_id"]: f"speaker {row['speaker']} {facts[row['dia_id']]}" for row in turns
        }
        mock_before = eval_locomo.OfflineMem0StyleStore()
        for row in turns:
            identifier = row["dia_id"]
            mock_before.add(identifier, facts[identifier], row["speaker"])
            mock_before.update(identifier, facts[identifier])
        before_orders = {
            "bm25": [eval_locomo._bm25_order(row["question"], turns) for row in questions],
            "offline_mock_crud": [mock_before.retrieve(row["question"]) for row in questions],
        }
        by_budget: dict[str, Any] = {}
        raw_budget_keys = raw_sample.get("budget_results", {})
        for ratio in ratios:
            budget_key = f"{ratio:.0%}"
            raw_policy_names = set(raw_budget_keys.get(budget_key, {}))
            if raw_policy_names != set(raw_sample.get("policies_evaluated", [])):
                raise ValueError(f"raw policy results missing for sample {index} at {budget_key}")
            _, budget_bytes = eval_locomo._retention_budget_bytes(turns, ratio)
            bm25_kept = set(eval_locomo._fifo_fit_ids(turns, original_bodies, budget_bytes))
            bm25_turns = [row for row in turns if row["dia_id"] in bm25_kept]
            after_mock = eval_locomo.OfflineMem0StyleStore()
            for row in turns:
                identifier = row["dia_id"]
                after_mock.add(identifier, facts[identifier], row["speaker"])
                after_mock.update(identifier, facts[identifier])
            mock_kept = set(eval_locomo._fifo_fit_ids(turns, facts, budget_bytes))
            for identifier in tuple(after_mock.rows):
                if identifier not in mock_kept:
                    after_mock.delete(identifier)
            after_orders = {
                "bm25": [eval_locomo._bm25_order(row["question"], bm25_turns) for row in questions],
                "offline_mock_crud": [after_mock.retrieve(row["question"]) for row in questions],
            }
            score = eval_locomo._score_phase(
                questions,
                {"bm25": original_context, "offline_mock_crud": fact_context},
                query_budget,
                after_orders,
            )
            original_mock_body_bytes = sum(len(value.encode("utf-8")) for value in facts.values())
            retained_mock_body_bytes = sum(
                len(facts[identifier].encode("utf-8")) for identifier in mock_kept
            )
            baseline_metrics = {
                "bm25_retained_count": len(bm25_kept),
                "bm25_retained_body_bytes": sum(
                    len(original_bodies[identifier].encode("utf-8")) for identifier in bm25_kept
                ),
                "mock_retained_count": len(mock_kept),
                "mock_initial_body_bytes": original_mock_body_bytes,
                "mock_retained_body_bytes": retained_mock_body_bytes,
                "mock_budget_satisfied": retained_mock_body_bytes <= budget_bytes,
            }
            by_budget[budget_key] = {
                "body_utf8_budget_bytes": budget_bytes,
                **baseline_metrics,
                "before_budget": eval_locomo._score_phase(
                    questions,
                    {"bm25": original_context, "offline_mock_crud": fact_context},
                    query_budget,
                    before_orders,
                ),
                "after_budget": score,
            }
            for policy_name in raw_policy_names:
                policy_result = corrected_samples[index]["budget_results"][budget_key][policy_name]
                policy_result["bm25_retained_count"] = len(bm25_kept)
                policy_result["bm25_retained_body_bytes"] = baseline_metrics[
                    "bm25_retained_body_bytes"
                ]
                policy_result["baseline_retained_count"] = len(mock_kept)
                policy_result["baseline_initial_body_bytes"] = original_mock_body_bytes
                policy_result["baseline_retained_body_bytes"] = retained_mock_body_bytes
                policy_result["baseline_budget_satisfied"] = (
                    retained_mock_body_bytes <= budget_bytes
                )
                for phase in ("before_budget", "after_budget"):
                    system_scores = policy_result[phase]
                    system_scores["bm25"] = by_budget[budget_key][phase]["bm25"]
                    system_scores["offline_mock_crud"] = by_budget[budget_key][phase][
                        "offline_mock_crud"
                    ]
                    system_scores["offline_mem0_style_crud"] = system_scores["offline_mock_crud"]
                policy_result["bm25_description"] = (
                    "local deterministic Okapi BM25 over retained full turn text; "
                    "FIFO evicts oldest turns until byte limit"
                )
                policy_result["offline_mock_extraction_crud_description"] = (
                    "local first-sentence mock extractor with idempotent add/update/delete; "
                    "oldest turns evicted until the same UTF-8 body-byte capacity fits; "
                    "not official Mem0"
                )
        primary_ratio = f"{ratios[0]:.0%}"
        primary_policy = raw_sample["primary_policy_alias"]
        primary = corrected_samples[index]["budget_results"][primary_ratio][primary_policy]
        for key in (
            "before_budget",
            "after_budget",
            "baseline_retained_count",
            "baseline_initial_body_bytes",
            "baseline_retained_body_bytes",
            "baseline_budget_satisfied",
            "bm25_retained_count",
            "bm25_retained_body_bytes",
            "bm25_description",
            "offline_mock_extraction_crud_description",
        ):
            corrected_samples[index][key] = copy.deepcopy(primary[key])
    corrected_report["baseline_correction"] = {
        "description": (
            "oldest-first eviction; evict complete oldest turns until UTF-8 body bytes fit"
        ),
        "raw_report_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "corrector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dataset_sha256": dataset_sha,
        "query_context_budget_tokens": query_budget,
        "systems": {
            "bm25": "full-text BM25 over original session turn text with oldest-first eviction",
            "offline_mock_crud": (
                "first-sentence mock extractor, idempotent update, oldest-first deletion; "
                "not official Mem0"
            ),
        },
        "raw_memfw_projection_sha256": original_memfw_hash,
        "corrected_memfw_projection_sha256": _canonical_hash(_memfw_projection(corrected_samples)),
        "raw_memfw_metrics_unchanged": original_memfw_hash
        == _canonical_hash(_memfw_projection(corrected_samples)),
    }
    if not corrected_report["baseline_correction"]["raw_memfw_metrics_unchanged"]:
        raise AssertionError("baseline correction altered MemFW result metrics")
    return cast(dict[str, Any], corrected_report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--raw-evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--query-budget", type=int, default=128)
    args = parser.parse_args()
    if args.query_budget < 1:
        parser.error("--query-budget must be positive")
    try:
        _validate_output_path(args.data, args.raw_evaluation, args.output)
    except ValueError as exc:
        parser.error(str(exc))
    result = correct_baselines(args.data, args.raw_evaluation, query_budget=args.query_budget)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sample_count = result["results"][0].get("sample_count", 0)
    print(json.dumps({"output": str(args.output), "samples": sample_count}))


if __name__ == "__main__":
    main()
