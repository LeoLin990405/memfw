"""Read-only evaluation of approved pilot retrieval queries under byte budgets."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from memfw.agents.models import JsonValue, canonical_payload_hash
from memfw.budget import BudgetLimits
from memfw.core.models import MemoryDocument
from memfw.policies import PolicyCandidate, make_policy
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import KeynessScorer

BUDGET_PERCENTS = (10, 25, 50)
ARMS = ("fifo", "keyness_salience", "approved_mlx_salience", "no_forget_full_corpus_reference")


@dataclass(frozen=True, slots=True)
class ApprovedQuery:
    """One target-bound query from the privately approved query file."""

    target_id: str
    query: str


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    arm: str
    budget_percent: int
    budget_bytes: int
    query_count: int
    hit_at_5: float
    mrr: float
    retained_count: int
    retained_body_bytes: int
    budget_satisfied: bool
    score_signal: str


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    corpus_fingerprint: str
    metrics: tuple[ArmMetrics, ...]


def corpus_fingerprint(documents: Sequence[MemoryDocument]) -> str:
    """Hash every stored model field that can affect policy or retrieval behavior."""
    rows = [item.model_dump(mode="json") for item in sorted(documents, key=lambda value: value.id)]
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_queries(
    queries: Sequence[ApprovedQuery], candidate_ids: set[str]
) -> tuple[ApprovedQuery, ...]:
    if not queries:
        raise ValueError("approved query set must not be empty")
    for query in queries:
        if not query.target_id or query.target_id not in candidate_ids:
            raise ValueError("every query target must identify a candidate memory")
        if not query.query.strip():
            raise ValueError("approved query text must not be empty")
    return tuple(queries)


def _approved_scores(
    candidate_ids: set[str],
    scores: Mapping[str, float] | None,
    proposal_ids: Mapping[str, str] | None,
    approved_corpus_fingerprint: str | None,
    current_corpus_fingerprint: str,
    approved_mlx_payload_hashes: Mapping[str, str] | None,
    approved_mlx_payloads: Mapping[str, Mapping[str, JsonValue]] | None,
) -> dict[str, float]:
    if (
        scores is None
        or proposal_ids is None
        or approved_corpus_fingerprint is None
        or approved_mlx_payload_hashes is None
        or approved_mlx_payloads is None
    ):
        raise ValueError("approved MLX scores and their approval bindings are required")
    if approved_corpus_fingerprint != current_corpus_fingerprint:
        raise ValueError("approved MLX score snapshot is stale for the current corpus")
    if (
        set(scores) != candidate_ids
        or set(proposal_ids) != candidate_ids
        or set(approved_mlx_payload_hashes) != candidate_ids
        or set(approved_mlx_payloads) != candidate_ids
    ):
        raise ValueError("approved MLX scores and bindings must cover every candidate")
    output: dict[str, float] = {}
    for identifier, score in scores.items():
        payload = approved_mlx_payloads[identifier]
        payload_score = payload.get("score")
        if (
            not proposal_ids[identifier]
            or not approved_mlx_payload_hashes[identifier]
            or canonical_payload_hash(payload) != approved_mlx_payload_hashes[identifier]
        ):
            raise ValueError("every MLX score must bind to an approved proposal payload")
        if isinstance(score, bool) or not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("approved MLX scores must be finite values from 0 through 1")
        if (
            isinstance(payload_score, bool)
            or not isinstance(payload_score, (int, float))
            or payload_score != score
        ):
            raise ValueError("approved MLX score must match its approved proposal payload")
        output[identifier] = score
    return output


def evaluate_queries(
    documents: Sequence[MemoryDocument],
    queries: Sequence[ApprovedQuery],
    *,
    now: datetime,
    approved_mlx_scores: Mapping[str, float] | None,
    approved_mlx_proposal_ids: Mapping[str, str] | None,
    approved_mlx_corpus_fingerprint: str | None,
    approved_mlx_payload_hashes: Mapping[str, str] | None,
    approved_mlx_payloads: Mapping[str, Mapping[str, JsonValue]] | None,
    approved_query_binding_hash: str | None,
    current_query_binding_hash: str | None,
    approved_query_target_ids: Sequence[str] | None,
) -> EvaluationResult:
    """Evaluate four retention arms without writing to the repository or source data.

    The caller must load queries from the approved private artifact and score/proposal
    bindings from approved proposals. Snapshot and coverage checks reject stale or
    incomplete MLX data. The production salience policy consumes the selected signal
    through its existing ``keyness`` field (weighted by its production 2x coefficient).
    Query text is consumed in memory and omitted from results.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    if not documents:
        raise ValueError("candidate corpus must not be empty")
    identifiers = [document.id for document in documents]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("candidate IDs must be unique")
    candidates_by_id = {item.id: item for item in documents}
    candidate_ids = set(candidates_by_id)
    approved_queries = _validated_queries(queries, candidate_ids)
    if not approved_query_binding_hash or approved_query_binding_hash != current_query_binding_hash:
        raise ValueError("approved query binding is missing or stale")
    if approved_query_target_ids is None or sorted(set(approved_query_target_ids)) != sorted(
        {query.target_id for query in approved_queries}
    ):
        raise ValueError("approved query target set is missing or stale")
    fingerprint = corpus_fingerprint(documents)
    mlx_scores = _approved_scores(
        candidate_ids,
        approved_mlx_scores,
        approved_mlx_proposal_ids,
        approved_mlx_corpus_fingerprint,
        fingerprint,
        approved_mlx_payload_hashes,
        approved_mlx_payloads,
    )

    base_candidates = [PolicyCandidate.from_document(item) for item in documents]
    keyness_scores = KeynessScorer().score_documents({item.id: item.body for item in documents})
    keyness_candidates = [
        replace(candidate, keyness=keyness_scores[candidate.id].score)
        for candidate in base_candidates
    ]
    mlx_candidates = [
        replace(candidate, keyness=mlx_scores[candidate.id]) for candidate in base_candidates
    ]
    by_arm: dict[str, Sequence[PolicyCandidate]] = {
        "fifo": base_candidates,
        "keyness_salience": keyness_candidates,
        "approved_mlx_salience": mlx_candidates,
        "no_forget_full_corpus_reference": base_candidates,
    }

    total_body_bytes = sum(len(item.body.encode("utf-8")) for item in documents)
    metrics: list[ArmMetrics] = []
    for budget_percent in BUDGET_PERCENTS:
        budget = total_body_bytes * budget_percent // 100
        limits = BudgetLimits(max_bytes=budget)
        for arm in ARMS:
            candidates = by_arm[arm]
            if arm == "no_forget_full_corpus_reference":
                retained_ids = tuple(sorted(candidate_ids))
                budget_satisfied = (
                    sum(len(candidates_by_id[item].body.encode("utf-8")) for item in retained_ids)
                    <= budget
                )
            else:
                policy_name = "fifo" if arm == "fifo" else "salience"
                plan = make_policy(policy_name).plan(candidates, limits, now=now)
                retained_ids = plan.retained_ids
                budget_satisfied = plan.satisfied
            scorer = BM25Scorer(
                {
                    identifier: " ".join(
                        (
                            candidates_by_id[identifier].title,
                            candidates_by_id[identifier].body,
                            candidates_by_id[identifier].summary,
                            " ".join(candidates_by_id[identifier].tags),
                        )
                    )
                    for identifier in retained_ids
                }
            )
            hits = 0
            reciprocal_ranks = 0.0
            for query in approved_queries:
                ranking = scorer.rank(query.query, candidate_ids=retained_ids)
                if query.target_id in ranking[:5]:
                    hits += 1
                try:
                    rank = ranking.index(query.target_id) + 1
                except ValueError:
                    rank = 0
                if rank:
                    reciprocal_ranks += 1.0 / rank
            count = len(approved_queries)
            metrics.append(
                ArmMetrics(
                    arm=arm,
                    budget_percent=budget_percent,
                    budget_bytes=budget,
                    query_count=count,
                    hit_at_5=hits / count,
                    mrr=reciprocal_ranks / count,
                    retained_count=len(retained_ids),
                    retained_body_bytes=sum(
                        len(candidates_by_id[item].body.encode("utf-8")) for item in retained_ids
                    ),
                    budget_satisfied=budget_satisfied,
                    score_signal=(
                        "measured_keyness_via_policy_keyness_slot_2x"
                        if arm == "keyness_salience"
                        else "approved_mlx_score_via_policy_keyness_slot_2x"
                        if arm == "approved_mlx_salience"
                        else "none"
                    ),
                )
            )
    return EvaluationResult(corpus_fingerprint=fingerprint, metrics=tuple(metrics))
