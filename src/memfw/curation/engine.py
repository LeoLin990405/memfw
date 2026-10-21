"""Deterministic runtime orchestration for policy plans and memory access."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import zstandard

from memfw.budget import BYTE_BUDGETS, BudgetLimits, BudgetProfile, evaluate_budget
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind, Layer, Tier
from memfw.policies import (
    DecisionAction,
    PolicyCandidate,
    PolicyName,
    PolicyPlan,
    make_policy,
)


def access_projection(repository: Any) -> dict[str, tuple[datetime | None, int]]:
    """Project append-only access events into last-access and count metadata."""

    result: dict[str, tuple[datetime | None, int]] = {}
    for event in repository.events():
        if event.kind != EventKind.MEMORY_ACCESS:
            continue
        _, count = result.get(event.memory_id, (None, 0))
        result[event.memory_id] = (event.timestamp, count + 1)
    return result


def _latest_access_intervals(repository: Any) -> dict[str, float]:
    timestamps: dict[str, list[datetime]] = {}
    for event in repository.events():
        if event.kind == EventKind.MEMORY_ACCESS:
            timestamps.setdefault(event.memory_id, []).append(event.timestamp)
    intervals: dict[str, float] = {}
    for memory_id, values in timestamps.items():
        if len(values) >= 2:
            intervals[memory_id] = max(0.0, (values[-1] - values[-2]).total_seconds() / 86400.0)
    return intervals


def _with_archived_body(repository: Any, document: Any) -> Any:
    """Read a cold stub's original document from its archive without restoring it."""

    if not document.is_stub:
        return document
    if not document.archive_ref:
        raise IntegrityError(f"Cold stub has no archive reference: {document.id}")
    archive_path = repository.archives_dir / f"{document.archive_ref}.tar.zst"
    try:
        compressed = archive_path.read_bytes()
        tar_bytes = zstandard.ZstdDecompressor().decompress(compressed)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            member = archive.extractfile(f"memories/{document.id}.md")
            if member is None:
                raise IntegrityError(f"Cold archive does not contain {document.id}")
            raw = member.read()
        archived_hash = document.archived_hash
        if not archived_hash or sha256(raw).hexdigest() != archived_hash:
            raise IntegrityError(f"Archived content hash mismatch for {document.id}")
        original = repository.parse(raw.decode("utf-8"), expected_id=document.id)
    except (
        OSError,
        UnicodeDecodeError,
        zstandard.ZstdError,
        tarfile.TarError,
        KeyError,
    ) as exc:
        raise IntegrityError(f"Unable to read archived body for {document.id}") from exc
    return document.model_copy(update={"body": original.body, "is_stub": False})


def candidates_for(
    repository: Any, documents: Iterable[Any] | None = None
) -> list[PolicyCandidate]:
    """Adapt current documents and access events to the pure policy API."""

    access = access_projection(repository)
    intervals = _latest_access_intervals(repository)
    docs = repository.list() if documents is None else documents
    candidates: list[PolicyCandidate] = []
    for listed_document in docs:
        document = _with_archived_body(repository, listed_document)
        accessed, count = access.get(document.id, (None, 0))
        candidate = PolicyCandidate.from_document(document)
        candidates.append(
            replace(
                candidate,
                last_accessed_at=accessed,
                access_count=count,
                last_access_interval_days=intervals.get(
                    document.id, candidate.last_access_interval_days
                ),
                manually_marked=candidate.manually_marked or "manual" in document.tags,
            )
        )
    config = repository.read_config()
    knowledge_counts = repository.knowledge_source_counts()
    protection_boost = float(config.get("knowledge_protection_boost", 1.0))
    candidates = [
        replace(
            item,
            metadata={
                **item.metadata,
                "knowledge_reference_count": knowledge_counts.get(item.id, 0),
                "knowledge_protection_boost": protection_boost,
            },
        )
        for item in candidates
    ]
    if not config.get("keyness_enabled", False) or not candidates:
        return candidates
    from memfw.stats.keyness import KeynessScorer

    top_k = int(config.get("keyness_top_k", 5))

    reference_path = config.get("keyness_reference_path")
    if reference_path:
        try:
            reference = Path(reference_path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise IntegrityError(
                f"Unable to read keyness reference text: {reference_path}"
            ) from exc
        scores = KeynessScorer([reference], top_k=top_k).score_documents(
            {item.id: item.body for item in candidates}
        )
    else:
        scores = KeynessScorer(top_k=top_k).score_rolling_documents(
            [(item.id, item.created_at, item.body) for item in candidates],
            window_size=int(config.get("keyness_window", 20)),
        )
    return [replace(item, keyness=scores[item.id].score) for item in candidates]


def plan_forget(
    repository: Any,
    policy: PolicyName | str,
    limits: BudgetLimits,
    *,
    now: datetime | None = None,
    scope: str = "hot",
    memory_ids: set[str] | None = None,
) -> PolicyPlan:
    """Create a pure plan. ``hot`` measures only active hot bodies."""

    if scope not in {"hot", "all"}:
        raise ValueError("scope must be 'hot' or 'all'")
    current = now or repository._now()
    documents = repository.list()
    if scope == "hot":
        documents = [doc for doc in documents if doc.tier == Tier.HOT and not doc.is_stub]
    if memory_ids is not None:
        documents = [doc for doc in documents if doc.id in memory_ids]
    return make_policy(policy).plan(candidates_for(repository, documents), limits, now=current)


@dataclass(frozen=True, slots=True)
class ForgetResult:
    plan: PolicyPlan
    action: str
    scope: str
    dry_run: bool
    applied_ids: tuple[str, ...]
    satisfied_after_apply: bool | None
    actual_usage_count: int | None
    actual_usage_bytes: int | None
    initial_usage_count: int
    initial_usage_bytes: int
    budget_mode: str = "body"
    physical_bytes_before: int | None = None
    physical_bytes_after: int | None = None
    unsatisfied_reason: str | None = None
    unsatisfied_reason_code: str | None = None
    blocking_anchor_ids: tuple[str, ...] = ()
    pending_proposal_ids: tuple[str, ...] = ()
    would_propose: tuple[str, ...] = ()
    physical_breakdown_before: dict[str, int] = field(default_factory=dict)
    physical_breakdown_after: dict[str, int] = field(default_factory=dict)
    physical_overage_bytes: int = 0
    overage_component: str | None = None
    overage_component_bytes: int = 0
    unsatisfied_explanation: str | None = None
    floor_components: dict[str, int] = field(default_factory=dict)
    floor_anchor_count: int = 0
    floor_remaining_memory_count: int = 0
    floor_explanation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        plan = self.plan
        if self.satisfied_after_apply is not None:
            satisfied = self.satisfied_after_apply
        elif self.action == "soft" and self.scope == "all":
            count_ok = (
                plan.limits.max_count is None or self.initial_usage_count <= plan.limits.max_count
            )
            bytes_ok = (
                plan.limits.max_bytes is None or self.initial_usage_bytes <= plan.limits.max_bytes
            )
            satisfied = count_ok and bytes_ok
        else:
            satisfied = plan.satisfied
        return {
            "policy": plan.policy.value,
            "scope": self.scope,
            "action": self.action,
            "dry_run": self.dry_run,
            "retained_ids": list(plan.retained_ids),
            "forgotten_ids": list(plan.forgotten_ids),
            "satisfied": satisfied,
            "usage": {"count": plan.usage.count, "bytes": plan.usage.bytes},
            "limits": {
                "max_count": plan.limits.max_count,
                "max_bytes": plan.limits.max_bytes,
            },
            "decisions": [
                {
                    "id": row.memory_id,
                    "action": row.action.value,
                    "reason": row.reason,
                    "score": row.score,
                }
                for row in plan.decisions
            ],
            "consolidation_proposals": [
                {
                    "representative_id": row.representative_id,
                    "duplicate_ids": list(row.duplicate_ids),
                    "match_basis": row.match_basis,
                    "reason": row.reason,
                }
                for row in plan.consolidation_proposals
            ],
            "applied_count": len(self.applied_ids),
            "applied_ids": list(self.applied_ids),
            "satisfied_after_apply": self.satisfied_after_apply,
            "actual_usage": (
                None
                if self.actual_usage_count is None
                else {"count": self.actual_usage_count, "bytes": self.actual_usage_bytes}
            ),
            "body_budget_scope": (
                "UTF-8 Markdown body bytes only; logs, indexes, archives, and other "
                "physical storage are excluded"
            ),
            "budget_mode": self.budget_mode,
            "physical_bytes_before": self.physical_bytes_before,
            "physical_bytes_after": self.physical_bytes_after,
            "physical_breakdown_before": self.physical_breakdown_before,
            "physical_breakdown_after": self.physical_breakdown_after,
            "physical_overage_bytes": self.physical_overage_bytes,
            "overage_component": self.overage_component,
            "overage_component_bytes": self.overage_component_bytes,
            "unsatisfied_explanation": self.unsatisfied_explanation,
            "floor_components": self.floor_components,
            "floor_anchor_count": self.floor_anchor_count,
            "floor_remaining_memory_count": self.floor_remaining_memory_count,
            "floor_explanation": self.floor_explanation,
            "unsatisfied_reason": self.unsatisfied_reason,
            "unsatisfied_reason_code": self.unsatisfied_reason_code,
            "blocking_anchor_ids": list(self.blocking_anchor_ids),
            "pending_proposal_ids": list(self.pending_proposal_ids),
            "would_propose": list(self.would_propose),
        }


def _physical_bytes(repository: Any) -> int:
    from memfw.stats import measure_physical_disk_bytes

    return measure_physical_disk_bytes(repository.root)


def _scoped_count(documents: Iterable[Any], scope: str) -> int:
    rows = tuple(documents)
    if scope == "all":
        return len(rows)
    return sum(doc.tier == Tier.HOT and not doc.is_stub for doc in rows)


def _record_consolidation_review(
    repository: Any, documents: list[Any], now: datetime, *, record: bool = True
) -> tuple[str, ...]:
    """Turn judge-found candidates into human-reviewed proposals.

    Candidate discovery is deliberately independent of the pure policy's exact
    duplicate/group-key heuristic. The policy chooses when consolidation is a
    useful response to pressure; the judge decides which eligible pairs merit
    review. No candidate is mutated here.
    """
    from memfw.agents import DeterministicJudge
    from memfw.agents.models import canonical_payload_hash, mock_signature

    judge = DeterministicJudge(clock=lambda: now)
    proposals: list[Any] = []
    existing = {
        proposal.proposal_id: proposal for proposal in repository.list_proposals(status=None)
    }
    for candidate in judge.find_near_duplicates(documents):
        proposal = candidate.to_proposal()
        output = dict(proposal.output)
        output["disposition"] = "delete"
        payload_hash = canonical_payload_hash(output)
        proposal_id = canonical_payload_hash(
            [proposal.proposal_id, "human-review-required", payload_hash]
        )[:32]
        signature = mock_signature(
            proposal_id,
            proposal.input_hash,
            payload_hash,
            now,
            proposal.target_hashes,
        )
        reviewed = proposal.model_copy(
            update={
                "proposal_id": proposal_id,
                "output": output,
                "payload_hash": payload_hash,
                "signature": signature,
                "created_at": now,
                "updated_at": now,
            }
        )
        proposals.append(existing.get(proposal_id, reviewed))
    if not proposals:
        return ()
    proposal_ids: list[str] = []
    for proposal in proposals:
        if record and proposal.proposal_id not in existing:
            repository.record_proposal(proposal)
        if proposal.proposal_id not in existing or proposal.status in {"pending", "approved"}:
            proposal_ids.append(proposal.proposal_id)
    return tuple(proposal_ids)


def _record_budget_unsatisfied(
    repository: Any,
    *,
    reason: str,
    budget_bytes: int,
    physical_bytes: int,
    budget_count: int | None,
    anchors: tuple[str, ...],
    scope: str,
    budget_mode: str = "total",
) -> None:
    from memfw.stats import measure_physical_breakdown

    breakdown = measure_physical_breakdown(repository)
    documents = repository.list()
    anchors_left = sum(doc.layer == Layer.ANCHOR or bool(doc.anchor) for doc in documents)
    non_anchors_left = len(documents) - anchors_left
    dominant_component = (
        max(breakdown, key=lambda name: breakdown[name]) if breakdown else "unknown"
    )
    overage = max(0, physical_bytes - budget_bytes) if budget_mode == "total" else 0
    state_payload = json.dumps(
        {
            "reason": reason,
            "budget_mode": budget_mode,
            "budget_bytes": budget_bytes,
            "budget_count": budget_count,
            "anchors": anchors,
            "scope": scope,
            "memory_hashes": [
                (doc.id, sha256(repository._current_path(doc.id).read_bytes()).hexdigest())
                for doc in repository.list()
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    state_hash = sha256(state_payload.encode("utf-8")).hexdigest()
    if any(
        event.kind == EventKind.BUDGET_UNSATISFIABLE
        and event.details.get("state_hash") == state_hash
        for event in repository.events()
    ):
        return
    repository.capture_event(
        EventKind.BUDGET_UNSATISFIABLE,
        actor="mechanism",
        details={
            "memory_id": "budget",
            "operation": "budget_unsatisfiable",
            "reason": reason,
            "reason_code": reason,
            "budget_mode": budget_mode,
            "budget_bytes": budget_bytes,
            "physical_bytes": physical_bytes,
            "max_count": budget_count,
            "anchor_ids": ",".join(anchors),
            "state_hash": state_hash,
            "scope": scope,
            "physical_breakdown": json.dumps(breakdown, sort_keys=True, separators=(",", ":")),
            "dominant_current_component": dominant_component,
            "dominant_component_bytes": breakdown.get(dominant_component, 0),
            "physical_overage_bytes": overage,
            "overage_explanation": (
                (
                    f"Vault exceeds the total budget by {overage} allocated bytes; "
                    f"the largest current component is {dominant_component} "
                    f"({breakdown.get(dominant_component, 0)} allocated bytes)."
                )
                if budget_mode == "total"
                else "This is a body-budget event; physical component totals are informational."
            ),
            "remaining_anchor_count": anchors_left,
            "remaining_non_anchor_count": non_anchors_left,
            "rebuildable_index_bytes": breakdown.get("index", 0),
            "floor_explanation": (
                "Observed physical categories after the configured policy stopped. "
                "This is not a theoretical irreducible floor; index bytes are rebuildable."
            ),
        },
    )


def _enforce_total_budget(
    repository: Any,
    policy: PolicyName | str,
    limits: BudgetLimits,
    *,
    now: datetime | None,
    scope: str,
    action: str,
    dry_run: bool,
    actor: str,
    _lock_held: bool = False,
) -> ForgetResult:
    """Enforce physical allocated-byte limits one committed policy action at a time."""
    if not _lock_held:
        with repository._locked():
            return _enforce_total_budget(
                repository,
                policy,
                limits,
                now=now,
                scope=scope,
                action=action,
                dry_run=dry_run,
                actor=actor,
                _lock_held=True,
            )
    point = now or repository._now()
    before = _physical_bytes(repository)
    if not dry_run:
        repository.compact_log()
    current_bytes_after_compaction = _physical_bytes(repository)
    documents = repository.list()
    anchors = tuple(
        sorted(doc.id for doc in documents if doc.layer == Layer.ANCHOR or bool(doc.anchor))
    )
    eligible = [
        doc
        for doc in documents
        if (scope == "all" or (doc.tier == Tier.HOT and not doc.is_stub))
        and (action == "hard" or not doc.is_stub)
    ]
    initial_body = evaluate_budget(candidates_for(repository, eligible), BudgetLimits()).usage
    initial_count = _scoped_count(documents, scope)
    budget_bytes = limits.max_bytes
    if budget_bytes is None:
        default_budget_bytes = BYTE_BUDGETS[BudgetProfile.MB_10].max_bytes
        if default_budget_bytes is None:
            raise IntegrityError("The default total budget must specify max_bytes")
        budget_bytes = default_budget_bytes
    ranked_candidates = candidates_for(repository, eligible)
    ranking_limits = BudgetLimits(max_count=0)
    ranking_plan = make_policy(policy).plan(ranked_candidates, ranking_limits, now=point)
    if (
        dry_run
        and PolicyName(policy) == PolicyName.CONSOLIDATE_THEN_FORGET
        and (
            before > budget_bytes
            or (limits.max_count is not None and initial_count > limits.max_count)
        )
    ):
        reviewed = run_forget(
            repository,
            policy,
            limits,
            now=point,
            scope=scope,
            action=action,
            dry_run=True,
            actor=actor,
            force_review=True,
        )
        proposal_needed = bool(reviewed.would_propose)
        return replace(
            reviewed,
            budget_mode="total",
            physical_bytes_before=before,
            physical_bytes_after=before,
            satisfied_after_apply=False,
            unsatisfied_reason=("approval_required" if proposal_needed else "policy_exhausted"),
        )
    if dry_run:
        dry_count_ok = limits.max_count is None or initial_count <= limits.max_count
        dry_bytes_ok = before <= budget_bytes
        return ForgetResult(
            ranking_plan,
            action,
            scope,
            True,
            (),
            dry_bytes_ok and dry_count_ok,
            None,
            None,
            initial_body.count,
            initial_body.bytes,
            budget_mode="total",
            physical_bytes_before=before,
            physical_bytes_after=before,
            unsatisfied_reason=(None if dry_bytes_ok and dry_count_ok else "would_require_actions"),
            blocking_anchor_ids=anchors,
        )

    if PolicyName(policy) == PolicyName.CONSOLIDATE_THEN_FORGET and (
        before > budget_bytes or (limits.max_count is not None and initial_count > limits.max_count)
    ):
        reviewed = run_forget(
            repository,
            policy,
            limits,
            now=point,
            scope=scope,
            action=action,
            dry_run=False,
            actor=actor,
            force_review=True,
        )
        after = _physical_bytes(repository)
        eligible_nonanchors = [
            doc for doc in eligible if doc.layer != Layer.ANCHOR and not doc.anchor
        ]
        consolidation_reason = (
            "approval_required"
            if reviewed.pending_proposal_ids
            else ("anchor_floor" if anchors and not eligible_nonanchors else "policy_exhausted")
        )
        _record_budget_unsatisfied(
            repository,
            reason=consolidation_reason,
            budget_bytes=budget_bytes,
            physical_bytes=after,
            budget_count=limits.max_count,
            anchors=anchors,
            scope=scope,
            budget_mode="total",
        )
        after = _physical_bytes(repository)
        return replace(
            reviewed,
            budget_mode="total",
            physical_bytes_before=before,
            physical_bytes_after=after,
            unsatisfied_reason=consolidation_reason,
            blocking_anchor_ids=anchors if consolidation_reason == "anchor_floor" else (),
        )

    applied: list[str] = []
    pending_source_proposals: list[str] = []
    source_counts = repository.knowledge_source_counts() if action == "hard" else {}
    current_bytes = current_bytes_after_compaction
    current_count = initial_count
    reason: str | None = None
    documents_by_id = {doc.id: doc for doc in documents}
    decision_reasons = {row.memory_id: row.reason for row in ranking_plan.decisions}
    remaining_order = list(ranking_plan.forgotten_ids)
    # Policy ranking is stable for a fixed snapshot. Reuse that order and apply only
    # selected targets; recomputing every candidate and event projection after every
    # single write made large physical-budget enforcement quadratic in vault size.
    while current_bytes > budget_bytes or (
        limits.max_count is not None and current_count > limits.max_count
    ):
        while remaining_order and remaining_order[0] in applied:
            remaining_order.pop(0)
        if not remaining_order:
            non_anchor_documents = [
                doc
                for doc in documents_by_id.values()
                if doc.layer != Layer.ANCHOR and not doc.anchor
            ]
            reason = (
                "approval_required"
                if pending_source_proposals
                else (
                    "policy_exhausted"
                    if non_anchor_documents
                    else ("anchor_floor" if anchors else "metadata_floor")
                )
            )
            break
        target = remaining_order.pop(0)
        document = documents_by_id.get(target)
        if document is None:
            reason = "policy_exhausted"
            break
        if document.layer == Layer.ANCHOR or document.anchor:
            continue
        # Match run_forget's audited operation semantics without rebuilding a plan
        # or reparsing all Markdown to select this already-ranked single target.
        if action == "hard":
            if source_counts.get(target, 0) > 0:
                proposal = repository.propose_source_deletion(target)
                pending_source_proposals.append(proposal.proposal_id)
                continue
            repository.delete(
                target,
                actor=actor,
                _forget_details={
                    "policy": PolicyName(policy).value,
                    "reason": decision_reasons.get(target, "budget_pressure"),
                },
            )
            documents_by_id.pop(target, None)
            current_count -= 1
        else:
            repository.capture_event(
                EventKind.FORGET,
                actor="mechanism",
                details={
                    "memory_id": target,
                    "operation": "cool",
                    "policy": PolicyName(policy).value,
                    "reason": decision_reasons.get(target, "budget_pressure"),
                },
            )
            if document.tier != Tier.COLD:
                repository.update(target, actor="mechanism", tier=Tier.COLD)
            current = repository.get(target)
            current_raw = repository._current_path(target).read_bytes()
            tail = repository.events()[-1].event_hash if repository.events() else ""
            archive_seed = target + "\n" + sha256(current_raw).hexdigest() + "\n" + tail
            archive_id = "forget-" + sha256(archive_seed.encode("utf-8")).hexdigest()[:16]
            repository.cold_archive(archive_id, actor="mechanism", memory_ids={target})
            documents_by_id[target] = current.model_copy(
                update={"tier": Tier.COLD, "is_stub": True, "body": ""}
            )
            if scope == "hot":
                current_count -= 1
        applied.append(target)
        if current_bytes > budget_bytes:
            config = repository.read_config()
            keep_recent = int(config.get("log_keep_recent", 100))
            live_events = (
                repository.events_path.read_bytes().count(b"\n")
                if repository.events_path.exists()
                else 0
            )
            if live_events > 2 * keep_recent:
                repository.compact_log(keep_recent)
        current_bytes = _physical_bytes(repository)
    after_docs = repository.list()
    actual_body = evaluate_budget(candidates_for(repository, after_docs), BudgetLimits()).usage
    if current_bytes > budget_bytes or (
        limits.max_count is not None and current_count > limits.max_count
    ):
        reason = reason or ("anchor_floor" if anchors and not eligible else "metadata_floor")
        _record_budget_unsatisfied(
            repository,
            reason=reason,
            budget_bytes=budget_bytes,
            physical_bytes=current_bytes,
            budget_count=limits.max_count,
            anchors=anchors,
            scope=scope,
            budget_mode="total",
        )
        current_bytes = _physical_bytes(repository)
    return ForgetResult(
        ranking_plan,
        action,
        scope,
        False,
        tuple(applied),
        current_bytes <= budget_bytes
        and (limits.max_count is None or current_count <= limits.max_count),
        actual_body.count,
        actual_body.bytes,
        initial_body.count,
        initial_body.bytes,
        budget_mode="total",
        physical_bytes_before=before,
        physical_bytes_after=current_bytes,
        unsatisfied_reason=(
            reason
            if current_bytes > budget_bytes
            or (limits.max_count is not None and current_count > limits.max_count)
            else None
        ),
        unsatisfied_reason_code=(
            reason
            if current_bytes > budget_bytes
            or (limits.max_count is not None and current_count > limits.max_count)
            else None
        ),
        blocking_anchor_ids=anchors if reason == "anchor_floor" else (),
        pending_proposal_ids=tuple(pending_source_proposals),
    )


def run_forget(
    repository: Any,
    policy: PolicyName | str,
    limits: BudgetLimits,
    *,
    now: datetime | None = None,
    scope: str = "hot",
    action: str = "soft",
    dry_run: bool = True,
    actor: str = "human",
    agent_id: str | None = None,
    signature: str | None = None,
    proposal_id: str | None = None,
    approved_by: str | None = None,
    memory_ids: set[str] | None = None,
    _lock_held: bool = False,
    force_review: bool = False,
) -> ForgetResult:
    """Plan and optionally apply soft cooling or hard managed-content deletion.

    Soft forgetting changes placement and preserves managed originals in a cold
    archive. Hard forgetting deletes every managed original; Repository permissions
    remain authoritative for mechanism and agent actors.
    """

    if action not in {"soft", "hard"}:
        raise ValueError("action must be 'soft' or 'hard'")
    if scope not in {"hot", "all"}:
        raise ValueError("scope must be 'hot' or 'all'")
    if action == "soft" and not dry_run and actor not in {"human", "mechanism"}:
        raise IntegrityError("Agents may propose soft forgetting but cannot apply it directly")
    has_approval = bool(agent_id and signature and proposal_id and approved_by == "human")
    if action == "hard" and actor not in {"human", "mechanism"} and not has_approval:
        raise IntegrityError(
            "Hard forgetting requires a human actor or a current human-approved proposal"
        )

    if not dry_run and not _lock_held:
        with repository._locked():
            return run_forget(
                repository,
                policy,
                limits,
                now=now,
                scope=scope,
                action=action,
                dry_run=False,
                actor=actor,
                agent_id=agent_id,
                signature=signature,
                proposal_id=proposal_id,
                approved_by=approved_by,
                memory_ids=memory_ids,
                _lock_held=True,
                force_review=force_review,
            )

    input_documents = repository.list()
    if scope == "hot":
        input_documents = [
            doc for doc in input_documents if doc.tier == Tier.HOT and not doc.is_stub
        ]
    initial_evaluation = evaluate_budget(candidates_for(repository, input_documents), limits)
    initial_usage = initial_evaluation.usage
    plan = plan_forget(repository, policy, limits, now=now, scope=scope, memory_ids=memory_ids)
    is_consolidation = PolicyName(policy) == PolicyName.CONSOLIDATE_THEN_FORGET
    eligible_documents = [doc for doc in repository.list() if not doc.is_stub]
    if scope == "hot":
        eligible_documents = [doc for doc in eligible_documents if doc.tier == Tier.HOT]
    if memory_ids is not None:
        eligible_documents = [doc for doc in eligible_documents if doc.id in memory_ids]
    eligible_documents = [
        doc for doc in eligible_documents if doc.layer != Layer.ANCHOR and not doc.anchor
    ]
    if is_consolidation and initial_evaluation.within_budget and not force_review:
        return ForgetResult(
            plan,
            action,
            scope,
            dry_run,
            (),
            True,
            initial_usage.count,
            initial_usage.bytes,
            initial_usage.count,
            initial_usage.bytes,
        )
    if dry_run:
        would_propose: tuple[str, ...] = ()
        if action == "hard":
            source_counts = repository.knowledge_source_counts()
            would_propose = tuple(
                memory_id for memory_id in plan.forgotten_ids if source_counts.get(memory_id, 0) > 0
            )
        if is_consolidation:
            would_propose = _record_consolidation_review(
                repository, eligible_documents, now or repository._now(), record=False
            )
            decisions = tuple(
                replace(
                    decision,
                    action=DecisionAction.RETAIN,
                    reason="awaiting human merge review",
                )
                if decision.action == DecisionAction.FORGET
                else decision
                for decision in plan.decisions
            )
            plan = replace(
                plan,
                decisions=decisions,
                retained_ids=tuple(sorted(set(plan.retained_ids) | set(plan.forgotten_ids))),
                forgotten_ids=(),
            )
        return ForgetResult(
            plan,
            action,
            scope,
            True,
            (),
            None,
            None,
            None,
            initial_usage.count,
            initial_usage.bytes,
            unsatisfied_reason=("approval_required" if would_propose else "policy_exhausted")
            if is_consolidation or would_propose
            else None,
            would_propose=would_propose,
        )

    # Consolidation is a reviewed workflow, never a policy-side deletion. The
    # selector may use exact duplicate keys as a deterministic trigger, but the
    # mock judge independently finds bounded near-duplicate candidates among
    # eligible full documents. A human must approve and explicitly apply them.
    if is_consolidation:
        pending_ids = _record_consolidation_review(
            repository, eligible_documents, now or repository._now()
        )
        if pending_ids:
            return ForgetResult(
                plan,
                action,
                scope,
                False,
                (),
                False,
                initial_usage.count,
                initial_usage.bytes,
                initial_usage.count,
                initial_usage.bytes,
                unsatisfied_reason="approval_required",
                pending_proposal_ids=pending_ids,
            )
        return ForgetResult(
            plan,
            action,
            scope,
            False,
            (),
            False,
            initial_usage.count,
            initial_usage.bytes,
            initial_usage.count,
            initial_usage.bytes,
            unsatisfied_reason="policy_exhausted",
        )

    selected = set(plan.forgotten_ids)
    # The selector protects anchors, and this second guard protects against malformed
    # or stale plans supplied by callers.
    documents = {doc.id: doc for doc in repository.list()}
    for memory_id in selected:
        doc = documents.get(memory_id)
        if doc is None:
            raise IntegrityError(f"Forget target disappeared after planning: {memory_id}")
        if doc.layer == Layer.ANCHOR or bool(doc.anchor):
            raise IntegrityError(f"Refusing to forget protected anchor: {memory_id}")
    actionable = []
    for memory_id in plan.forgotten_ids:
        document = documents[memory_id]
        if action == "hard":
            actionable.append(memory_id)
        elif not document.is_stub and (
            document.tier == Tier.HOT or (scope == "all" and document.tier == Tier.COLD)
        ):
            actionable.append(memory_id)
    selected = set(actionable)
    pending_source_proposals: list[str] = []
    if action == "hard":
        source_counts = repository.knowledge_source_counts()
        protected = [memory_id for memory_id in actionable if source_counts.get(memory_id, 0) > 0]
        for memory_id in protected:
            proposal = repository.propose_source_deletion(memory_id)
            pending_source_proposals.append(proposal.proposal_id)
        if protected:
            protected_ids = set(protected)
            actionable = [memory_id for memory_id in actionable if memory_id not in protected_ids]
            selected = set(actionable)

    # Record consolidation links before any duplicate is moved or deleted. The event
    # contains identifiers and rationale only, never body text.
    for proposal in plan.consolidation_proposals:
        if proposal.representative_id not in plan.retained_ids:
            raise IntegrityError("Consolidation representative must remain retained")
        for duplicate_id in proposal.duplicate_ids:
            if duplicate_id not in selected:
                continue
            repository.capture_event(
                EventKind.CONSOLIDATION_PROPOSAL,
                actor="mechanism" if action == "soft" else actor,
                details={
                    "memory_id": duplicate_id,
                    "operation": "consolidation_proposal",
                    "policy": plan.policy.value,
                    "reason": proposal.reason,
                    "representative_id": proposal.representative_id,
                    "duplicate_id": duplicate_id,
                    "match_basis": proposal.match_basis,
                },
            )

    decision_by_id = {row.memory_id: row for row in plan.decisions}
    applied: list[str] = []
    for memory_id in actionable:
        reason = decision_by_id[memory_id].reason
        event_actor = "mechanism" if action == "soft" else actor
        if action == "soft":
            repository.capture_event(
                EventKind.FORGET,
                actor=event_actor,
                details={
                    "memory_id": memory_id,
                    "operation": "cool",
                    "policy": plan.policy.value,
                    "reason": reason,
                },
            )
            if documents[memory_id].tier != Tier.COLD:
                repository.update(memory_id, actor="mechanism", tier=Tier.COLD)
        else:
            repository.delete(
                memory_id,
                actor=actor,
                agent_id=agent_id,
                signature=signature,
                proposal_id=proposal_id,
                approved_by=approved_by,
                _forget_details={"policy": plan.policy.value, "reason": reason},
            )
        applied.append(memory_id)

    if action == "soft" and applied:
        tail = repository.events()[-1].event_hash if repository.events() else ""
        current_hashes = "\n".join(
            sha256(repository._current_path(memory_id).read_bytes()).hexdigest()
            for memory_id in sorted(applied)
        )
        archive_seed = "\n".join(sorted(applied)) + "\n" + current_hashes + "\n" + tail
        archive_id = "forget-" + sha256(archive_seed.encode("utf-8")).hexdigest()[:16]
        repository.cold_archive(archive_id, actor="mechanism", memory_ids=set(applied))

    after_docs = repository.list()
    if scope == "hot":
        after_docs = [doc for doc in after_docs if doc.tier == Tier.HOT and not doc.is_stub]
    after_candidates = candidates_for(repository, after_docs)
    actual = evaluate_budget(after_candidates, limits)
    usage = actual.usage
    all_documents = repository.list()
    blocking_anchors = tuple(
        sorted(
            doc.id
            for doc in all_documents
            if (doc.layer == Layer.ANCHOR or bool(doc.anchor))
            and (scope == "all" or (doc.tier == Tier.HOT and not doc.is_stub))
        )
    )
    unsatisfied_reason = None
    if not actual.within_budget:
        unsatisfied_reason = (
            "approval_required"
            if pending_source_proposals
            else "anchor_floor"
            if blocking_anchors
            else "policy_exhausted"
        )
        _record_budget_unsatisfied(
            repository,
            reason=unsatisfied_reason,
            budget_bytes=limits.max_bytes or 0,
            physical_bytes=_physical_bytes(repository),
            budget_count=limits.max_count,
            anchors=blocking_anchors,
            scope=scope,
            budget_mode="body",
        )
    return ForgetResult(
        plan,
        action,
        scope,
        False,
        tuple(applied),
        actual.within_budget,
        usage.count,
        usage.bytes,
        initial_usage.count,
        initial_usage.bytes,
        budget_mode="body",
        unsatisfied_reason=unsatisfied_reason,
        blocking_anchor_ids=blocking_anchors if unsatisfied_reason == "anchor_floor" else (),
        pending_proposal_ids=tuple(pending_source_proposals),
    )


def enforce_budget(
    repository: Any,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    actor: str = "mechanism",
    agent_id: str | None = None,
    signature: str | None = None,
    proposal_id: str | None = None,
    approved_by: str | None = None,
    policy_override: PolicyName | str | None = None,
) -> ForgetResult:
    """Apply configured budget policy; callers gate this on explicit auto-enforcement.

    The vault config's default is a 10 MB hot-body budget with FIFO soft cooling.
    This function does not enable automatic enforcement itself. A CLI or caller
    should invoke it after writes only when ``auto_enforce_on_write`` is true.
    """

    config = repository.read_config()

    try:
        profile = BudgetProfile(config.get("budget_level", BudgetProfile.MB_10.value))
        policy = PolicyName(policy_override or config.get("forget_policy", PolicyName.FIFO.value))
        action = config.get("forget_action", "soft")
        if action in {"cold", "delete"}:
            action = {"cold": "soft", "delete": "hard"}[action]
        scope = config.get("budget_scope", "hot")
        budget_mode = config.get("budget_mode", "body")
        if action not in {"soft", "hard"}:
            raise ValueError("forget_action must be soft or hard")
        if scope not in {"hot", "all"}:
            raise ValueError("budget_scope must be hot or all")
        if budget_mode not in {"body", "total"}:
            raise ValueError("budget_mode must be body or total")
        max_bytes = config.get("max_bytes", BYTE_BUDGETS[profile].max_bytes)
        max_count = config.get("max_count")
        if max_bytes is not None and type(max_bytes) is not int:
            raise ValueError("max_bytes must be an integer or null")
        if max_count is not None and type(max_count) is not int:
            raise ValueError("max_count must be an integer or null")
        limits = BudgetLimits(max_bytes=max_bytes, max_count=max_count)
    except (TypeError, ValueError) as exc:
        raise IntegrityError(f"Invalid budget configuration: {exc}") from exc

    if budget_mode == "total":
        from memfw.stats import measure_physical_breakdown

        breakdown_before = measure_physical_breakdown(repository)
        result = _enforce_total_budget(
            repository,
            policy,
            limits,
            now=now,
            scope=scope,
            action=action,
            dry_run=dry_run,
            actor=actor,
        )
        breakdown_after = measure_physical_breakdown(repository)
        remaining = repository.list()
        anchor_count = sum(doc.layer == Layer.ANCHOR or bool(doc.anchor) for doc in remaining)
        unsatisfied = result.satisfied_after_apply is False
        overage_component = (
            max(breakdown_after, key=lambda name: breakdown_after[name])
            if breakdown_after
            else None
        )
        configured_budget = limits.max_bytes
        if configured_budget is None:
            configured_budget = BYTE_BUDGETS[profile].max_bytes or 0
        overage_bytes = max(0, (result.physical_bytes_after or 0) - configured_budget)
        unsatisfied_code = result.unsatisfied_reason_code or result.unsatisfied_reason
        unsatisfied_text: str | None = None
        if unsatisfied_code:
            if overage_bytes and overage_component is not None:
                unsatisfied_text = (
                    f"{unsatisfied_code}: {overage_component} is the largest current component "
                    f"at {breakdown_after[overage_component]} allocated bytes; total vault "
                    f"exceeds the configured budget by {overage_bytes} allocated bytes."
                )
            elif limits.max_count is not None:
                count_overage = max(0, len(remaining) - limits.max_count)
                unsatisfied_text = (
                    f"{unsatisfied_code}: memory count exceeds the configured limit by "
                    f"{count_overage}."
                )
            else:
                unsatisfied_text = unsatisfied_code
        return replace(
            result,
            unsatisfied_reason=unsatisfied_text,
            unsatisfied_reason_code=unsatisfied_code,
            physical_breakdown_before=breakdown_before,
            physical_breakdown_after=breakdown_after,
            physical_overage_bytes=overage_bytes,
            overage_component=overage_component,
            overage_component_bytes=(
                breakdown_after[overage_component] if overage_component is not None else 0
            ),
            unsatisfied_explanation=(
                f"The configured policy stopped {overage_bytes} allocated bytes over budget; "
                f"the largest current component is {overage_component} "
                f"({breakdown_after.get(overage_component, 0)} allocated bytes)."
                if unsatisfied and overage_component is not None
                else None
            ),
            floor_components=breakdown_after if unsatisfied else {},
            floor_anchor_count=anchor_count if unsatisfied else 0,
            floor_remaining_memory_count=len(remaining) if unsatisfied else 0,
            floor_explanation=(
                "Observed post-policy physical categories, not a theoretical irreducible floor; "
                "index bytes are rebuildable."
                if unsatisfied
                else None
            ),
        )

    return run_forget(
        repository,
        policy,
        limits,
        now=now,
        scope=scope,
        action=action,
        dry_run=dry_run,
        actor=actor,
        agent_id=agent_id,
        signature=signature,
        proposal_id=proposal_id,
        approved_by=approved_by,
    )


__all__ = [
    "ForgetResult",
    "access_projection",
    "candidates_for",
    "enforce_budget",
    "plan_forget",
    "run_forget",
]
