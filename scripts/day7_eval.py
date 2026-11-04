#!/usr/bin/env python3
"""Fast, offline Day 7 evidence-retrieval evaluation for local LoCoMo JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from memfw.agents import DeterministicJudge
from memfw.budget import BudgetLimits
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.curation.engine import candidates_for
from memfw.index import MemoryIndex
from memfw.policies import PolicyName, make_policy
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import tokenize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_locomo
from day7_bootstrap import paired_stratified_bootstrap
from day7_gating import run_gating
from day7_retention import configured_policy, project_consolidation_retention

POLICIES = tuple(policy.value for policy in PolicyName)
DEFAULT_RATIOS = (0.10, 0.25)
DEFAULT_CONDITIONS = ("keyness_only", "keyness_plus_shared_access")


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def _event_ids(start: int = 0) -> Generator[str, None, None]:
    value = start
    while True:
        yield f"day7-event-{value:09d}"
        value += 1


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _make_shared_access_flow(
    turns: list[dict[str, str]], *, seed: int, sample_index: int, evaluation_now: datetime
) -> list[tuple[datetime, str]]:
    """Generate a fixed-seed stream independent of question text and labels."""
    rng = random.Random(seed ^ (sample_index * 0x9E3779B1))
    rows: list[tuple[datetime, str]] = []
    for row in turns:
        created = datetime.fromisoformat(row["created_at"]).astimezone(UTC)
        count = rng.choices((0, 1, 2, 3), weights=(0.45, 0.35, 0.15, 0.05), k=1)[0]
        for ordinal in range(count):
            available = max(1, int((evaluation_now - created).total_seconds()) - 1)
            offset = min(1 + rng.randrange(1, 60 * 60 * 24 * 45), max(1, available - ordinal))
            rows.append((created + timedelta(seconds=offset + ordinal), row["dia_id"]))
    return sorted(rows, key=lambda item: (item[0], item[1]))


def _new_sample_repo(
    turns: list[dict[str, str]], *, keyness: bool, temp_root: Path | None = None
) -> tuple[Repository, dict[str, str], datetime, TemporaryDirectory[str]]:
    temp = TemporaryDirectory(prefix="memfw-day7-sample-", dir=temp_root)
    first_moment = min(datetime.fromisoformat(row["created_at"]).astimezone(UTC) for row in turns)
    repo = Repository(
        Path(temp.name) / "vault",
        clock=lambda: first_moment,
        id_factory=iter(_event_ids()).__next__,
    )
    repo.init()
    config = repo.read_config()
    config.update({"keyness_enabled": keyness, "keyness_top_k": 5, "keyness_window": 20})
    repo.configure(config)
    created: dict[str, str] = {}
    times: list[datetime] = []
    for row in turns:
        moment = datetime.fromisoformat(row["created_at"]).astimezone(UTC)
        repo.clock = cast(Callable[[], datetime], lambda moment=moment: moment)
        identifier = _safe_id(row["dia_id"])
        repo.add(
            f"speaker {row['speaker']}",
            row["text"],
            memory_id=identifier,
            source="local-locomo-adapter",
            provenance="day7-offline-evaluation",
        )
        created[row["dia_id"]] = identifier
        times.append(moment)
    now = max(times) + timedelta(days=2)
    repo.clock = lambda: now
    return repo, created, now, temp


def _query_terms(questions: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term for row in questions for term in tokenize(row["question"]) if len(term) >= 2
        )
    )


def _fts_orders(
    index: MemoryIndex,
    questions: list[dict[str, Any]],
    repo_to_dia: dict[str, str],
    retained: set[str],
    all_ids: set[str],
    hits: dict[str, list[tuple[str, str]]],
) -> list[list[str]]:
    del index, all_ids
    result: list[list[str]] = []
    for row in questions:
        counts: Counter[str] = Counter()
        for term in dict.fromkeys(tokenize(row["question"])):
            if len(term) >= 2:
                counts.update(
                    identifier for identifier, _ in hits.get(term, []) if identifier in retained
                )
        ranked = sorted(counts, key=lambda identifier: (-counts[identifier], identifier))
        result.append(
            [repo_to_dia[identifier] for identifier in ranked if identifier in repo_to_dia]
        )
    return result


def _score_orders(
    questions: list[dict[str, Any]],
    orders: dict[str, list[list[str]]],
    bodies: dict[str, dict[str, str]],
    token_budget: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    systems = tuple(orders)
    token_costs = {
        name: {identifier: len(tokenize(text)) for identifier, text in bodies[name].items()}
        for name in systems
    }
    sums = {name: [0, 0] for name in systems}
    cats = {name: {str(c): [0, 0] for c in range(1, 6)} for name in systems}
    for qindex, question in enumerate(questions):
        evidence = set(question["evidence"])
        if not evidence:
            continue
        per_system: dict[str, int] = {}
        for system in systems:
            selected: set[str] = set()
            used_tokens = 0
            for identifier in orders[system][qindex]:
                cost = token_costs[system][identifier]
                if used_tokens + cost <= token_budget:
                    selected.add(identifier)
                    used_tokens += cost
            hits = len(selected & evidence)
            per_system[system] = hits
            sums[system][0] += hits
            sums[system][1] += len(evidence)
            category = question.get("category")
            if isinstance(category, int) and 1 <= category <= 5:
                cats[system][str(category)][0] += hits
                cats[system][str(category)][1] += len(evidence)
        records.append(
            {
                "question_index": qindex,
                "category": question.get("category"),
                "evidence_total": len(evidence),
                "hits": per_system,
            }
        )
    summary: dict[str, Any] = {}
    for system in systems:
        hit_count, evidence_count = sums[system]
        summary[system] = {
            "evidence_retrieved": hit_count,
            "evidence_total": evidence_count,
            "micro_evidence_recall": hit_count / evidence_count if evidence_count else None,
            "by_category": {
                category: {
                    "retrieved": counts[0],
                    "total": counts[1],
                    "micro_recall": counts[0] / counts[1] if counts[1] else None,
                }
                for category, counts in cats[system].items()
            },
        }
    return summary, records


def _oracle_orders(
    questions: list[dict[str, Any]],
    bodies: dict[str, str],
    retrieval_contexts: dict[str, str],
    budget_bytes: int,
    token_budget: int,
) -> list[list[str]]:
    """Exact evidence-only 2D knapsack; this arm is query-conditioned cheating."""
    orders: list[list[str]] = []
    for row in questions:
        evidence = [identifier for identifier in row["evidence"] if identifier in bodies]
        # (bytes, tokens) -> selected ID tuple; retain maximum cardinality per state.
        states: dict[tuple[int, int], tuple[str, ...]] = {(0, 0): ()}
        for identifier in evidence:
            byte_cost = len(bodies[identifier].encode("utf-8"))
            token_cost = len(tokenize(retrieval_contexts[identifier]))
            additions = dict(states)
            for (used_bytes, used_tokens), selected in states.items():
                state = (used_bytes + byte_cost, used_tokens + token_cost)
                if state[0] <= budget_bytes and state[1] <= token_budget:
                    candidate = (*selected, identifier)
                    previous = additions.get(state)
                    if previous is None or len(candidate) > len(previous):
                        additions[state] = candidate
            states = additions
        winner = max(states.values(), key=lambda values: (len(values), tuple(values)))
        orders.append(list(winner))
    return orders


def evaluate_file(
    data_path: Path,
    *,
    seed: int = 20261018,
    token_budget: int = 128,
    ratios: tuple[float, ...] = DEFAULT_RATIOS,
    bootstrap_repeats: int = 1000,
) -> dict[str, Any]:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Day7 adapter expects a sample object or top-level list")
    results: list[dict[str, Any]] = []
    for sample_index, sample in enumerate(payload):
        turns, questions, adapter_stats = eval_locomo.adapt_with_stats(sample)
        if not turns:
            raise ValueError(f"sample {sample_index} has no session_N turns")
        result = _evaluate_sample(
            turns,
            questions,
            adapter_stats,
            sample_index=sample_index,
            sample_id=sample.get("sample_id"),
            seed=seed,
            token_budget=token_budget,
            ratios=ratios,
        )
        results.append(result)
        print(
            f"day7 sample {sample_index + 1}/{len(payload)} complete: "
            f"{sample.get('sample_id', sample_index)}, {len(turns)} turns, {len(questions)} QA",
            file=sys.stderr,
            flush=True,
        )
    digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
    return {
        "dataset": {"path": str(data_path.resolve()), "sha256": digest, "content_in_report": False},
        "seed": seed,
        "query_context_budget_tokens": token_budget,
        "retention_budget": (
            "floor(ratio * sum UTF-8 bytes of original turn.text); whole turns only"
        ),
        "signal_conditions": list(DEFAULT_CONDITIONS),
        "strategies": [
            *[f"{name}_{rank}" for name in POLICIES for rank in ("fts", "bm25")],
            "bm25_fifo",
            "mock_crud_fifo",
            "oracle_question_conditioned",
        ],
        "sample_count": len(results),
        "samples": results,
        "bootstrap_repeats": bootstrap_repeats,
        "limitations": [
            "Logical retention evaluation using production policy plans; "
            "not physical vault capacity.",
            "The oracle sees gold evidence and is query-conditioned, not deployable.",
            "Access flow is synthetic, fixed-seed, and shared across systems in condition B.",
            "No LLM, answer generation, image understanding, or external network access.",
        ],
    }


def _evaluate_sample(
    turns: list[dict[str, str]],
    questions: list[dict[str, Any]],
    adapter_stats: dict[str, Any],
    *,
    sample_index: int,
    sample_id: Any,
    seed: int,
    token_budget: int,
    ratios: tuple[float, ...],
) -> dict[str, Any]:
    repo, repo_ids, now, temp = _new_sample_repo(turns, keyness=True)
    try:
        index = MemoryIndex(repo.index_path)
        index.rebuild(repo)
        repo_to_dia = {repo_id: dia_id for dia_id, repo_id in repo_ids.items()}
        all_repo_ids = set(repo_to_dia)
        indexed_docs = repo.list()
        original_bodies = {row["dia_id"]: row["text"] for row in turns}
        stored_bodies = {
            repo_to_dia[doc.id]: doc.body for doc in indexed_docs if doc.id in repo_to_dia
        }
        raw_initial_bytes = sum(len(value.encode("utf-8")) for value in original_bodies.values())
        initial_bytes = sum(len(value.encode("utf-8")) for value in stored_bodies.values())
        retrieval_contexts = {
            row["dia_id"]: f"speaker {row['speaker']} {stored_bodies[row['dia_id']]}"
            for row in turns
        }
        reviewable_docs = [
            doc for doc in indexed_docs if not doc.anchor and doc.layer.value != "anchor"
        ]
        judge_proposals = DeterministicJudge(clock=lambda: now).find_near_duplicates(
            reviewable_docs
        )
        full_bm25 = BM25Scorer(
            {
                row.id: " ".join((row.title, row.body, row.summary, " ".join(row.tags)))
                for row in indexed_docs
            }
        )
        query_terms = _query_terms(questions)
        fts_hits = index.search_many(query_terms, limit=max(10, len(all_repo_ids)))
        access_flow = _make_shared_access_flow(
            turns, seed=seed, sample_index=sample_index, evaluation_now=now
        )
        access_digest = _canonical_sha(
            [(moment.isoformat(), repo_ids[dia_id]) for moment, dia_id in access_flow]
        )
        scopes: dict[str, Any] = {}
        for condition in DEFAULT_CONDITIONS:
            if condition == "keyness_plus_shared_access":
                for moment, dia_id in access_flow:
                    repo.capture_event(
                        EventKind.MEMORY_ACCESS,
                        actor="human",
                        details={
                            "memory_id": repo_ids[dia_id],
                            "operation": "access",
                            "access_count": 1,
                        },
                        timestamp=moment,
                    )
            candidates = candidates_for(repo)
            candidate_by_id = {item.id: item for item in candidates}
            # Exact keyness parity assertion: production adapter supplies a score for each row.
            if set(candidate_by_id) != all_repo_ids:
                raise AssertionError("candidate adapter did not return the complete sample corpus")
            scope_label = condition
            scope_result: dict[str, Any] = {
                "access_flow_count": len(access_flow) if condition.endswith("access") else 0,
                "access_flow_sha256": access_digest if condition.endswith("access") else None,
                "profiles": {},
            }
            before_fts = _fts_orders(
                index, questions, repo_to_dia, all_repo_ids, all_repo_ids, fts_hits
            )
            before_bm25 = [
                [repo_to_dia[item] for item in full_bm25.rank(row["question"])] for row in questions
            ]
            all_facts = {
                row["dia_id"]: stored_bodies[row["dia_id"]]
                .split(".", 1)[0]
                .split("。", 1)[0]
                .strip()
                or stored_bodies[row["dia_id"]].strip()
                for row in turns
            }
            mock_before = eval_locomo.OfflineMem0StyleStore()
            for row in turns:
                identifier = row["dia_id"]
                mock_before.add(identifier, all_facts[identifier], row["speaker"])
                mock_before.update(identifier, all_facts[identifier])
            before_mock = [mock_before.retrieve(row["question"]) for row in questions]
            before_summary, before_per_question = _score_orders(
                questions,
                {"memfw_fts": before_fts, "memfw_bm25": before_bm25, "mock_crud": before_mock},
                {
                    "memfw_fts": retrieval_contexts,
                    "memfw_bm25": retrieval_contexts,
                    "mock_crud": {
                        row["dia_id"]: f"speaker {row['speaker']} {all_facts[row['dia_id']]}"
                        for row in turns
                    },
                },
                token_budget,
            )
            scope_result["before_budget"] = {
                "systems": before_summary,
                "per_question": before_per_question,
            }
            configured = configured_policy(repo.read_config())
            if configured != PolicyName.FIFO:
                raise AssertionError("official evaluation baseline must default to FIFO")
            for ratio in ratios:
                budget_bytes = max(1, int(initial_bytes * ratio))
                budget = BudgetLimits(max_bytes=budget_bytes)
                profile_key = f"{ratio:.0%}"
                policy_kept: dict[str, set[str]] = {}
                policy_notes: dict[str, dict[str, Any]] = {}
                for policy_name in POLICIES:
                    if policy_name == PolicyName.CONSOLIDATE_THEN_FORGET.value:
                        projection = project_consolidation_retention(
                            indexed_docs,
                            budget,
                            now=now,
                            proposals=judge_proposals,
                        )
                        retained_repo = set(projection.retained_ids)
                        fallback_used = False
                        if not projection.satisfied:
                            fallback = make_policy(PolicyName.FIFO).plan(
                                [item for item in candidates if item.id in retained_repo],
                                budget,
                                now=now,
                            )
                            retained_repo = set(fallback.retained_ids)
                            fallback_used = True
                        policy_kept[policy_name] = {
                            repo_to_dia[identifier] for identifier in retained_repo
                        }
                        policy_notes[policy_name] = {
                            "simulated_merge_proposals": projection.proposal_count,
                            "simulated_merges": projection.projected_merge_count,
                            "fifo_fallback": fallback_used,
                            "retention_budget_satisfied": sum(
                                len(original_bodies[item].encode("utf-8"))
                                for item in policy_kept[policy_name]
                            )
                            <= budget_bytes,
                        }
                    else:
                        plan = make_policy(PolicyName(policy_name)).plan(
                            candidates, budget, now=now
                        )
                        policy_kept[policy_name] = {
                            repo_to_dia[identifier] for identifier in plan.retained_ids
                        }
                        policy_notes[policy_name] = {
                            "retention_budget_satisfied": plan.satisfied,
                            "fifo_fallback": False,
                        }
                # Baseline retention uses true FIFO eviction under the same absolute
                # body-byte capacity.
                bm25_kept = set(eval_locomo._fifo_fit_ids(turns, stored_bodies, budget_bytes))
                facts = {
                    row["dia_id"]: stored_bodies[row["dia_id"]]
                    .split(".", 1)[0]
                    .split("。", 1)[0]
                    .strip()
                    or stored_bodies[row["dia_id"]].strip()
                    for row in turns
                }
                mock_kept = set(eval_locomo._fifo_fit_ids(turns, facts, budget_bytes))
                profile_orders: dict[str, list[list[str]]] = {}
                profile_contexts: dict[str, dict[str, str]] = {}
                for policy_name, retained_dia in policy_kept.items():
                    retained_repo = {repo_ids[item] for item in retained_dia}
                    fts = _fts_orders(
                        index, questions, repo_to_dia, retained_repo, all_repo_ids, fts_hits
                    )
                    subset_bm25 = full_bm25.for_candidates(retained_repo)
                    bm25 = [
                        [repo_to_dia[item] for item in subset_bm25.rank(row["question"])]
                        for row in questions
                    ]
                    profile_orders[f"{policy_name}_fts"] = fts
                    profile_orders[f"{policy_name}_bm25"] = bm25
                    profile_contexts[f"{policy_name}_fts"] = retrieval_contexts
                    profile_contexts[f"{policy_name}_bm25"] = retrieval_contexts
                bm25_repo = {repo_ids[item] for item in bm25_kept}
                subset_bm25 = full_bm25.for_candidates(bm25_repo)
                bm25_orders = [
                    [repo_to_dia[item] for item in subset_bm25.rank(row["question"])]
                    for row in questions
                ]
                mock_store = eval_locomo.OfflineMem0StyleStore()
                for row in turns:
                    identifier = row["dia_id"]
                    mock_store.add(identifier, facts[identifier], row["speaker"])
                    mock_store.update(identifier, facts[identifier])
                for identifier in tuple(mock_store.rows):
                    if identifier not in mock_kept:
                        mock_store.delete(identifier)
                mock_orders = [mock_store.retrieve(row["question"]) for row in questions]
                profile_orders["bm25_fifo"] = bm25_orders
                profile_orders["mock_crud_fifo"] = mock_orders
                profile_contexts["bm25_fifo"] = retrieval_contexts
                profile_contexts["mock_crud_fifo"] = {
                    row["dia_id"]: f"speaker {row['speaker']} {facts[row['dia_id']]}"
                    for row in turns
                }
                oracle = _oracle_orders(
                    questions,
                    stored_bodies,
                    retrieval_contexts,
                    budget_bytes,
                    token_budget,
                )
                profile_orders["oracle_question_conditioned"] = oracle
                profile_contexts["oracle_question_conditioned"] = retrieval_contexts
                summary, per_question = _score_orders(
                    questions, profile_orders, profile_contexts, token_budget
                )
                # Profile snapshots make the logical result reproducible without storing text.
                profile_data: dict[str, Any] = {}
                for system in summary:
                    if system.endswith("_fts"):
                        policy = system[: -len("_fts")]
                        ids = policy_kept[policy]
                    elif system.endswith("_bm25"):
                        policy = system[: -len("_bm25")]
                        ids = policy_kept[policy]
                    elif system == "bm25_fifo":
                        ids = bm25_kept
                    elif system == "mock_crud_fifo":
                        ids = mock_kept
                    else:
                        ids = set()
                    bytes_kept = (
                        sum(len(stored_bodies[item].encode("utf-8")) for item in ids)
                        if system != "mock_crud_fifo"
                        else sum(len(facts[item].encode("utf-8")) for item in ids)
                    )
                    profile_data[system] = {
                        **summary[system],
                        "retained_count": len(ids)
                        if system != "oracle_question_conditioned"
                        else None,
                        "retained_body_utf8_bytes": bytes_kept
                        if system != "oracle_question_conditioned"
                        else None,
                        "retained_ids_sha256": _canonical_sha(
                            sorted(repo_ids[item] for item in ids)
                        )
                        if system != "oracle_question_conditioned"
                        else None,
                        "retained_ids": sorted(repo_ids[item] for item in ids)
                        if system != "oracle_question_conditioned"
                        else None,
                    }
                    policy_key = (
                        system[: -len("_fts")]
                        if system.endswith("_fts")
                        else system[: -len("_bm25")]
                        if system.endswith("_bm25")
                        else None
                    )
                    if policy_key is not None:
                        profile_data[system].update(policy_notes[policy_key])
                    if system != "oracle_question_conditioned":
                        if bytes_kept > budget_bytes:
                            raise AssertionError(
                                f"{system} retained {bytes_kept} body bytes over {budget_bytes}"
                            )
                        profile_data[system]["retention_budget_satisfied"] = True
                scope_result["profiles"][profile_key] = {
                    "capacity_bytes": budget_bytes,
                    "initial_turns": len(turns),
                    "systems": profile_data,
                    "per_question": per_question,
                    "per_question_systems": tuple(summary),
                    "before_budget": before_summary,
                    "before_per_question": before_per_question,
                }
            scopes[scope_label] = scopes.get(scope_label, {})
            scopes[scope_label] = {
                "access_flow_count": scope_result["access_flow_count"],
                "access_flow_sha256": scope_result["access_flow_sha256"],
                "before_budget": scope_result["before_budget"],
                "profiles": scope_result["profiles"],
            }
        return {
            "sample_index": sample_index,
            "sample_id": sample_id if isinstance(sample_id, str) else None,
            "turn_count": len(turns),
            "question_count": len(questions),
            "adapter_counts": adapter_stats,
            "initial_body_utf8_bytes": initial_bytes,
            "raw_turn_text_utf8_bytes": raw_initial_bytes,
            "body_byte_definition": (
                "sum UTF-8 canonical MemoryDocument.body after Markdown roundtrip; "
                "budgets and retained full/fact bodies use the same stored text"
            ),
            "keyness_configuration": {
                "enabled": True,
                "window_size": 20,
                "scoring": "production candidates_for rolling prior-only keyness",
            },
            "shared_access_flow_sha256": access_digest,
            "shared_access_flow_event_count": len(access_flow),
            "scopes": scopes,
        }
    finally:
        temp.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--budget-tokens", type=int, default=128)
    parser.add_argument("--bootstrap-repeats", type=int, default=1000)
    parser.add_argument(
        "--gating-count",
        type=int,
        default=None,
        help="Synthetic capture-gate records (default: 200 in quick mode, 1000 otherwise)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run the full dataset and all questions with the standard bootstrap count",
    )
    args = parser.parse_args()
    gating_count = (
        args.gating_count if args.gating_count is not None else (200 if args.quick else 1000)
    )
    run_started = time.perf_counter()
    evaluation_started = time.perf_counter()
    result = evaluate_file(
        args.data,
        seed=args.seed,
        token_budget=args.budget_tokens,
        bootstrap_repeats=args.bootstrap_repeats,
    )
    evaluation_seconds = time.perf_counter() - evaluation_started
    primary_pairwise = [f"{name}_bm25" for name in POLICIES]
    result["bootstrap"] = {}
    for condition in DEFAULT_CONDITIONS:
        for ratio in DEFAULT_RATIOS:
            scope = f"{condition}/{ratio:.0%}"
            records: list[dict[str, Any]] = []
            for sample in result["samples"]:
                rows = sample["scopes"][condition]["profiles"][f"{ratio:.0%}"]["per_question"]
                records.extend({**row, "sample_index": sample["sample_index"]} for row in rows)
            result["bootstrap"][scope] = paired_stratified_bootstrap(
                records,
                strategies=result["strategies"],
                pairwise_strategies=primary_pairwise,
                repeats=args.bootstrap_repeats,
                seed=args.seed ^ int(ratio * 1000) ^ (1 if condition.endswith("access") else 0),
            )
    bootstrap_seconds = time.perf_counter() - evaluation_started - evaluation_seconds
    gating_started = time.perf_counter()
    result["capture_gating"] = run_gating(
        count=gating_count,
        seed=args.seed,
        rho_capture=0.6,
        rho_access=0.0,
        prevalence=0.10,
    )
    result["quick_scope"] = {
        "official_samples": "all",
        "questions": "all",
        "bootstrap_repeats": args.bootstrap_repeats,
        "capture_gate_records": gating_count,
    }
    gating_seconds = time.perf_counter() - gating_started
    for sample in result["samples"]:
        for _condition, value in sample["scopes"].items():
            for profile in value["profiles"].values():
                profile.pop("per_question_systems", None)
    result["command_mode"] = "quick_all_samples_all_questions" if args.quick else "full"
    result["end_to_end_runtime_seconds"] = time.perf_counter() - run_started
    result["stage_runtime_seconds"] = {
        "official_evaluation": evaluation_seconds,
        "bootstrap": bootstrap_seconds,
        "synthetic_capture_gating": gating_seconds,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(args.output), "samples": result["sample_count"], "quick": args.quick},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
