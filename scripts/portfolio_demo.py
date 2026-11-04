#!/usr/bin/env python3
"""Bounded local LoCoMo demo using production retention policies and BM25."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import day7_eval
import day7_retention
import eval_locomo

from memfw.agents import DeterministicJudge
from memfw.budget import BudgetLimits
from memfw.core.repository import Repository
from memfw.curation.engine import candidates_for
from memfw.index import MemoryIndex
from memfw.policies import PolicyName, make_policy
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import tokenize

MAX_TURNS = 100
MAX_QUESTIONS = 20
CONTEXT_TOKENS = 128
BUDGET_RATIO = 0.25
POLICY_NAMES = (
    PolicyName.FIFO,
    PolicyName.LRU,
    PolicyName.DECAY,
    PolicyName.SALIENCE,
    PolicyName.CONSOLIDATE_THEN_FORGET,
)


def _validate_data_path(data_path: Path) -> Path:
    """Require an explicit local JSON file and reject known private pilot trees."""
    resolved = data_path.expanduser().resolve()
    private_parts = {"pilot", "pilot-vault", "pilot_vault", "private-vault"}
    if any(part.casefold() in private_parts for part in resolved.parts):
        raise ValueError("input path is inside a restricted pilot tree")
    if not resolved.is_file() or resolved.suffix.casefold() != ".json":
        raise ValueError("--data must name an existing local JSON file")
    return resolved


def _load_first_sample(
    data_path: Path,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, int]]:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    samples = payload if isinstance(payload, list) else [payload]
    if not samples or not isinstance(samples[0], dict):
        raise ValueError("input must contain a first LoCoMo sample object")
    turns, questions, _adapter_stats = eval_locomo.adapt_with_stats(samples[0])
    if not turns:
        raise ValueError("first sample has no session_N turns")

    prefix = turns[:MAX_TURNS]
    prefix_ids = {row["dia_id"] for row in prefix}
    selected_eligible: list[dict[str, Any]] = []
    valid_evidence_questions = 0
    for question in questions:
        evidence = question["evidence"]
        if not evidence:
            continue
        valid_evidence_questions += 1
        if all(identifier in prefix_ids for identifier in evidence):
            selected_eligible.append(question)
    picked = selected_eligible[:MAX_QUESTIONS]
    selection = {
        "sample_index": 0,
        "sample_turns": len(turns),
        "turns_in_evaluation_window": len(prefix),
        "sample_questions": len(questions),
        "sample_questions_with_valid_evidence": valid_evidence_questions,
        "questions_with_all_evidence_in_window": len(selected_eligible),
        "questions_evaluated": len(picked),
    }
    return prefix, picked, selection


def _require_safe_temp_root() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root.is_relative_to(project_root):
        raise RuntimeError("system temporary directory must be outside the project tree")
    return temp_root


def _safe_temporary_repo(
    turns: list[dict[str, str]],
) -> tuple[Repository, Any, TemporaryDirectory[str]]:
    temp_root = _require_safe_temp_root()
    project_root = Path(__file__).resolve().parents[1]
    repo, _ids, now, temp = day7_eval._new_sample_repo(turns, keyness=True, temp_root=temp_root)
    temp_path = Path(temp.name).resolve()
    if temp_path.is_relative_to(project_root):
        temp.cleanup()
        raise RuntimeError("temporary vault must be outside the project tree")
    return repo, now, temp


def _projection(
    repo: Repository,
    turns: list[dict[str, str]],
    selected_questions: list[dict[str, Any]],
    selection: dict[str, int],
) -> dict[str, Any]:
    index = MemoryIndex(repo.index_path)
    first_build_count = index.rebuild(repo)
    replay_before = repo.replay()
    probe_question = selected_questions[0]["question"]
    bm25_before = [
        identifier
        for identifier, _score in index.ranked_search(
            probe_question, limit=len(repo.list()), ranking="bm25"
        )
    ]
    second_build_count = index.rebuild(repo)
    replay_after = repo.replay()
    bm25_after = [
        identifier
        for identifier, _score in index.ranked_search(
            probe_question, limit=len(repo.list()), ranking="bm25"
        )
    ]
    index_rebuild_stable = first_build_count == second_build_count
    replay_stable = replay_before == replay_after
    search_stable = bm25_before == bm25_after
    if not index_rebuild_stable or not replay_stable or not search_stable:
        raise RuntimeError("temporary vault replay or index rebuild was not stable")

    docs = repo.list()
    candidates = candidates_for(repo)
    repo_ids = {doc.id for doc in docs}
    if {candidate.id for candidate in candidates} != repo_ids:
        raise RuntimeError("production candidate adapter did not return every temporary memory")
    # IDs and bodies use the production adapter's safe IDs and canonical stored text.
    repo_to_dia = {day7_eval._safe_id(row["dia_id"]): row["dia_id"] for row in turns}
    body_by_id = {repo_to_dia[doc.id]: doc.body for doc in docs}
    initial_bytes = sum(len(doc.body.encode("utf-8")) for doc in docs)
    budget_bytes = max(1, int(initial_bytes * BUDGET_RATIO))
    budget = BudgetLimits(max_bytes=budget_bytes)
    evaluation_now = repo._now()

    bm25 = BM25Scorer(
        {doc.id: " ".join((doc.title, doc.body, doc.summary, " ".join(doc.tags))) for doc in docs}
    )
    title_by_id = {repo_to_dia[doc.id]: doc.title for doc in docs}
    contexts = {
        row["dia_id"]: f"{title_by_id[row['dia_id']]} {body_by_id[row['dia_id']]}" for row in turns
    }
    token_cost = {identifier: len(tokenize(text)) for identifier, text in contexts.items()}

    retention: dict[str, tuple[set[str], dict[str, Any]]] = {}
    for policy_name in POLICY_NAMES:
        name = policy_name.value
        if policy_name == PolicyName.CONSOLIDATE_THEN_FORGET:
            eligible = [doc for doc in docs if not doc.anchor and doc.layer.value != "anchor"]
            proposals = DeterministicJudge(clock=lambda: evaluation_now).find_near_duplicates(
                eligible
            )
            projection = day7_retention.project_consolidation_retention(
                docs, budget, now=evaluation_now, proposals=proposals
            )
            kept = set(projection.retained_ids)
            fallback = False
            if not projection.satisfied:
                fallback_plan = make_policy(PolicyName.FIFO).plan(
                    [candidate for candidate in candidates if candidate.id in kept],
                    budget,
                    now=evaluation_now,
                )
                kept = set(fallback_plan.retained_ids)
                fallback = True
            note = {
                "mock_proposals": projection.proposal_count,
                "projected_merges": projection.projected_merge_count,
                "fifo_fallback": fallback,
            }
        else:
            plan = make_policy(policy_name).plan(candidates, budget, now=evaluation_now)
            kept = set(plan.retained_ids)
            note = {"fifo_fallback": False}
        retention[name] = (kept, note)

    # No-forgetting is a reference arm and intentionally has no 25% retention cap.
    retention["no_forgetting_reference"] = (repo_ids, {"fifo_fallback": False})
    systems: list[dict[str, Any]] = []
    for name, (kept_repo, note) in retention.items():
        subset = bm25.for_candidates(kept_repo)
        retrieved = 0
        evidence_total = 0
        for question in selected_questions:
            order = [
                repo_to_dia[item]
                for item in subset.rank(question["question"])
                if item in repo_to_dia
            ]
            selected: set[str] = set()
            used_tokens = 0
            for dia_id in order:
                cost = token_cost[dia_id]
                if used_tokens + cost <= CONTEXT_TOKENS:
                    selected.add(dia_id)
                    used_tokens += cost
            evidence = set(question["evidence"])
            retrieved += len(selected & evidence)
            evidence_total += len(evidence)
        kept_dia = {repo_to_dia[item] for item in kept_repo if item in repo_to_dia}
        retained_bytes = sum(len(body_by_id[item].encode("utf-8")) for item in kept_dia)
        if name != "no_forgetting_reference" and retained_bytes > budget_bytes:
            raise RuntimeError(f"production policy {name} exceeded the canonical body budget")
        systems.append(
            {
                "policy": name,
                "retained_memories": len(kept_dia),
                "retained_body_bytes": retained_bytes,
                "retention_budget_bytes": (
                    None if name == "no_forgetting_reference" else budget_bytes
                ),
                "evidence_retrieved": retrieved,
                "evidence_total": evidence_total,
                "evidence_recall": retrieved / evidence_total if evidence_total else None,
                **note,
            }
        )

    return {
        "schema_version": 1,
        "status": "completed",
        "scope": {
            "sample_index": 0,
            "turn_limit": MAX_TURNS,
            "question_limit": MAX_QUESTIONS,
            "retention_budget_ratio": BUDGET_RATIO,
            "retrieval_context_tokens": CONTEXT_TOKENS,
            "clock": "fixed from source turn timestamps by the Day 7 adapter",
            **selection,
        },
        "invariants": {
            "event_replay_stable_across_index_rebuild": replay_stable,
            "index_rebuild_count_stable": index_rebuild_stable,
            "bm25_search_stable_across_index_rebuild": search_stable,
            "production_candidate_count": len(candidates),
        },
        "systems": systems,
    }


def run_demo(data_path: Path) -> dict[str, Any]:
    _require_safe_temp_root()
    resolved = _validate_data_path(data_path)
    turns, questions, selection = _load_first_sample(resolved)
    if not questions:
        raise ValueError("first-sample prefix has no questions with all evidence in-window")
    repo, _now, temp = _safe_temporary_repo(turns)
    try:
        # Attach only bounded selection counts; no text, evidence IDs, or memory IDs enter output.
        result = _projection(repo, turns, questions, selection)
        return result
    finally:
        temp.cleanup()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Explicit local LoCoMo JSON path")
    parser.add_argument("--output", type=Path, help="Write deterministic aggregate JSON here")
    parser.add_argument("--receipt", type=Path, help="Optional separate runtime-only JSON receipt")
    parser.add_argument("--table", action="store_true", help="Print a compact six-arm summary")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    result = run_demo(args.data)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    elif not args.table:
        sys.stdout.write(encoded)
    if args.table:
        print("policy\tretained\tbytes\tevidence hits/total\trecall")
        for row in result["systems"]:
            recall = row["evidence_recall"]
            display_recall = "n/a" if recall is None else f"{recall:.1%}"
            print(
                f"{row['policy']}\t{row['retained_memories']}\t{row['retained_body_bytes']}\t"
                f"{row['evidence_retrieved']}/{row['evidence_total']}\t{display_recall}"
            )
    if args.receipt:
        args.receipt.write_text(
            json.dumps({"runtime_seconds": round(time.perf_counter() - started, 6)}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
