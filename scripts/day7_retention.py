"""Retention projection helpers shared by the Day 7 evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from memfw.agents import DeterministicJudge
from memfw.budget import BudgetLimits, evaluate_budget
from memfw.core.models import MemoryDocument
from memfw.policies import PolicyName


@dataclass(frozen=True, slots=True)
class RetentionProjection:
    """Body-budget view of production consolidation proposals."""

    retained_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    proposal_count: int
    projected_merge_count: int
    policy: PolicyName
    satisfied: bool
    unsatisfied_reason: str | None


def configured_policy(
    config: dict[str, Any], override: str | PolicyName | None = None
) -> PolicyName:
    """Resolve an explicit override or MemFW's configured FIFO fallback."""
    if override is not None:
        return PolicyName(override)
    return PolicyName(config.get("forget_policy", PolicyName.FIFO.value))


def project_consolidation_retention(
    documents: list[MemoryDocument],
    limits: BudgetLimits,
    *,
    now: datetime,
    policy: str | PolicyName = PolicyName.CONSOLIDATE_THEN_FORGET,
    proposals: list[Any] | None = None,
) -> RetentionProjection:
    """Project real mock-judge merge proposals without mutating a sample vault.

    Proposal discovery uses the same deterministic judge as ``run_forget``. The
    production review path changes a proposal's disposition to ``delete`` before
    recording it. Applying that disposition keeps the representative body unless
    the signed output explicitly supplies ``merged_body``. Profiles simulate
    approval of each disjoint proposal returned in one review batch; proposals
    sharing any target are skipped because the first application would stale the
    later proposal. A persistent equivalence test covers the actual proposal,
    simulated-decision, and apply path.
    """
    selected_policy = PolicyName(policy)
    if selected_policy != PolicyName.CONSOLIDATE_THEN_FORGET:
        raise ValueError("project_consolidation_retention requires the consolidation policy")
    if len({document.id for document in documents}) != len(documents):
        raise ValueError("documents must have unique IDs")
    current = {document.id: document for document in documents}
    before = evaluate_budget(documents, limits)
    if before.within_budget:
        return RetentionProjection(
            retained_ids=tuple(sorted(current)),
            removed_ids=(),
            proposal_count=0,
            projected_merge_count=0,
            policy=selected_policy,
            satisfied=True,
            unsatisfied_reason=None,
        )
    if proposals is None:
        eligible = [
            document
            for document in documents
            if not document.anchor and document.layer.value != "anchor"
        ]
        proposals = DeterministicJudge(clock=lambda: now).find_near_duplicates(eligible)
    projected: list[tuple[str, tuple[str, ...], str | None]] = []
    for candidate in proposals:
        proposal = candidate.to_proposal()
        output = proposal.output
        representative_id = output.get("representative_id")
        duplicate_ids = output.get("duplicate_ids")
        if (
            not isinstance(representative_id, str)
            or not isinstance(duplicate_ids, list)
            or not duplicate_ids
            or not all(isinstance(memory_id, str) for memory_id in duplicate_ids)
            or representative_id not in current
            or any(memory_id not in current for memory_id in duplicate_ids)
        ):
            continue
        projected_body = output.get("merged_body")
        projected.append(
            (
                representative_id,
                tuple(cast(list[str], duplicate_ids)),
                projected_body if isinstance(projected_body, str) else None,
            )
        )
    removed: set[str] = set()
    consumed: set[str] = set()
    applied = 0
    for representative_id, duplicate_ids, projected_body in projected:
        targets = {representative_id, *duplicate_ids}
        if targets & consumed:
            continue
        representative = current[representative_id]
        if representative.anchor or representative.layer.value == "anchor":
            continue
        if any(
            current[memory_id].anchor or current[memory_id].layer.value == "anchor"
            for memory_id in duplicate_ids
        ):
            continue
        if projected_body is not None:
            current[representative_id] = representative.model_copy(update={"body": projected_body})
        for memory_id in duplicate_ids:
            current.pop(memory_id, None)
            removed.add(memory_id)
        consumed.update(targets)
        applied += 1
    after = evaluate_budget(list(current.values()), limits)
    satisfied = after.within_budget
    return RetentionProjection(
        retained_ids=tuple(sorted(current)),
        removed_ids=tuple(sorted(removed)),
        proposal_count=len(projected),
        projected_merge_count=applied,
        policy=selected_policy,
        satisfied=satisfied,
        unsatisfied_reason=None
        if satisfied
        else ("approval_required" if projected else "policy_exhausted"),
    )
