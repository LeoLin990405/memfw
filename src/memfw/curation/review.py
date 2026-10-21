"""Human review and application of erasable agent proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from difflib import unified_diff
from typing import Any, cast

from memfw.agents.models import (
    JsonValue,
    Proposal,
    canonical_json,
    canonical_payload_hash,
    mock_signature,
)
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind, Layer, Tier
from memfw.core.repository import Repository


def list_pending(repository: Repository) -> list[dict[str, Any]]:
    """Return pending proposals with target snapshots and human-readable diffs."""

    result: list[dict[str, Any]] = []
    for proposal in repository.list_proposals(status="pending"):
        targets = []
        for memory_id in proposal.target_ids:
            try:
                document = repository.get(memory_id)
            except Exception as exc:  # stale proposal targets are shown, not hidden
                targets.append({"memory_id": memory_id, "missing": str(exc)})
                continue
            targets.append(
                {
                    "memory_id": memory_id,
                    "title": document.title,
                    "layer": document.layer.value,
                    "tier": document.tier.value,
                    "body": document.body,
                }
            )
        before = []
        for item in targets:
            before.extend(
                [
                    f"--- {item['memory_id']} [{item.get('layer', '?')}/{item.get('tier', '?')}]",
                    *str(item.get("body", "<missing>")).splitlines(),
                ]
            )
        if proposal.kind == "merge" and isinstance(proposal.output.get("merged_body"), str):
            after = str(proposal.output["merged_body"]).splitlines()
        elif proposal.kind == "forget":
            after = ["<delete targets only if a human approves and applies>"]
        else:
            after = [canonical_json(proposal.output)]
        result.append(
            {
                "proposal_id": proposal.proposal_id,
                "kind": proposal.kind,
                "target_ids": list(proposal.target_ids),
                "reason": proposal.reason,
                "output": proposal.output,
                "payload_hash": proposal.payload_hash,
                "actor": proposal.actor,
                "created_at": proposal.created_at.isoformat(),
                "targets": targets,
                "diff": "\n".join(
                    unified_diff(before, after, fromfile="current", tofile="proposed", lineterm="")
                ),
            }
        )
    return result


def _make_edited_proposal(
    repository: Repository,
    original: Proposal,
    output: dict[str, Any],
    *,
    reason: str | None = None,
) -> Proposal:
    timestamp = repository._now()
    payload_hash = canonical_payload_hash(output)
    target_snapshots: dict[str, str] = {}
    for memory_id in original.target_ids:
        document = repository.get(memory_id)
        target_snapshots[memory_id] = canonical_payload_hash(document.model_dump(mode="json"))
    input_hash = canonical_payload_hash(target_snapshots)
    proposal_id = hashlib.sha256(
        canonical_json(
            [original.proposal_id, input_hash, payload_hash, timestamp.isoformat()]
        ).encode("utf-8")
    ).hexdigest()[:32]
    return Proposal(
        proposal_id=proposal_id,
        kind=original.kind,
        target_ids=original.target_ids,
        agent_name="human-review",
        model_name="human-edit-v1",
        input_hash=input_hash,
        output=output,
        reason=reason or f"Human edit of proposal {original.proposal_id}",
        payload_hash=payload_hash,
        actor="human",
        signature=mock_signature(
            proposal_id, input_hash, payload_hash, timestamp, target_snapshots
        ),
        target_hashes=target_snapshots,
        created_at=timestamp,
        updated_at=timestamp,
    )


def decide_proposal(
    repository: Repository,
    proposal_id: str,
    decision: str,
    *,
    payload_hash: str,
    edited_output: Mapping[str, Any] | None = None,
    reason: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    """Record a payload-bound human approve/reject/edit decision."""

    proposal = next(
        (
            item
            for item in repository.list_proposals(status=None)
            if item.proposal_id == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise IntegrityError(f"Unknown proposal: {proposal_id}")
    if decision == "edit":
        if edited_output is None:
            raise IntegrityError("An edited output is required for edit decisions")
        edited = _make_edited_proposal(
            repository,
            proposal,
            cast(dict[str, JsonValue], dict(edited_output)),
            reason=reason,
        )
        if dry_run:
            return edited
        repository.decide_proposal(
            proposal_id,
            decision="edit",
            payload_hash=payload_hash,
            edited_proposal=edited,
        )
        return edited
    if decision not in {"approve", "reject"}:
        raise IntegrityError("Decision must be approve, reject, or edit")
    if decision == "approve":
        repository.assert_proposal_current(proposal_id)
    if dry_run:
        return proposal.model_copy(
            update={"status": "approved" if decision == "approve" else "rejected"}
        )
    return cast(
        Proposal,
        repository.decide_proposal(proposal_id, decision=decision, payload_hash=payload_hash),
    )


def _source_references(repository: Repository, ids: Iterable[str]) -> list[str]:
    references: set[str] = set()
    for memory_id in ids:
        document = repository.get(memory_id)
        content_hash = hashlib.sha256(repository._current_path(memory_id).read_bytes()).hexdigest()
        references.add(f"memory:{memory_id}@{content_hash}")
        provenance = document.provenance
        try:
            parsed = json.loads(provenance)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            references.update(item for item in parsed if isinstance(item, str))
        elif provenance and provenance != "human-authored":
            references.add(f"legacy:{provenance}")
    return sorted(references)


def _apply_merge(repository: Repository, proposal: Proposal, *, actor: str = "human") -> Any:
    output = proposal.output
    representative_id = output.get("representative_id")
    raw_duplicate_ids = output.get("duplicate_ids")
    disposition = output.get("disposition", "retain")
    if (
        not isinstance(representative_id, str)
        or not isinstance(raw_duplicate_ids, list)
        or not raw_duplicate_ids
        or not all(isinstance(item, str) for item in raw_duplicate_ids)
        or representative_id in raw_duplicate_ids
        or disposition not in {"delete", "retain"}
    ):
        raise IntegrityError("Merge proposal output is malformed")
    duplicate_ids = cast(list[str], raw_duplicate_ids)
    if tuple([representative_id, *duplicate_ids]) != proposal.target_ids:
        raise IntegrityError("Merge targets differ from signed proposal targets")
    representative, documents = _validate_merge(repository, proposal)
    refs = _source_references(repository, [representative_id, *duplicate_ids])
    new_body = representative.body
    if disposition == "retain":
        sections = [f"\n\n--- Merged source {doc.id} ---\n{doc.body}" for doc in documents]
        new_body += "".join(sections)
    elif isinstance(output.get("merged_body"), str):
        new_body = str(output["merged_body"])
    updated = repository.update(
        representative_id,
        actor=actor,
        proposal_id=proposal.proposal_id,
        approved_by=actor,
        provenance=canonical_json(refs),
        body=new_body,
    )
    if disposition == "delete":
        for memory_id in duplicate_ids:
            # Relink knowledge citations while both source IDs still resolve;
            # the repository records the provenance before the DELETE event.
            repository.repoint_knowledge_sources(memory_id, representative_id, actor="mechanism")
            repository.delete(
                memory_id,
                actor=actor,
                _preserve_proposal_ids={proposal.proposal_id},
                proposal_id=proposal.proposal_id,
                approved_by=actor,
            )
    return updated


def apply_approved(
    repository: Repository,
    proposal_id: str,
    *,
    actor: str = "human",
    dry_run: bool = False,
    enforce_fallback: bool = True,
) -> Any:
    """Apply one approved proposal exactly once as an explicit human action."""

    if actor not in {"human", "simulated_approver"}:
        raise IntegrityError("Only human or explicit simulated_approver can apply proposals")
    with repository._locked():
        decision_kind = EventKind.HUMAN_DECISION
        if actor == "simulated_approver":
            decision_kind = EventKind.SIMULATED_DECISION
        proposal = next(
            (
                item
                for item in repository.list_proposals(status="approved")
                if item.proposal_id == proposal_id
                and any(
                    event.kind == decision_kind
                    and event.details.get("proposal_id") == proposal_id
                    and event.details.get("decision") == "approve"
                    for event in repository.events()
                )
            ),
            None,
        )
        if proposal is None:
            raise IntegrityError("Proposal must have a matching human approval before apply")
        repository.assert_proposal_current(proposal_id)
        if proposal.kind == "forget" and len(proposal.target_ids) > 1:
            cited_targets = [
                memory_id
                for memory_id in proposal.target_ids
                if repository.knowledge_source_counts().get(memory_id, 0) > 0
            ]
            if cited_targets:
                raise IntegrityError(
                    "Multi-target forget proposals cannot include knowledge sources; "
                    "propose each cited source separately"
                )
        if actor == "simulated_approver":
            if proposal.kind not in {"merge", "forget"}:
                raise IntegrityError("Simulation may apply merge or forget proposals only")
            for memory_id in proposal.target_ids:
                document = repository.get(memory_id)
                if document.anchor or document.layer == Layer.ANCHOR:
                    raise IntegrityError("Simulation cannot apply anchored targets")
                if repository.knowledge_source_counts().get(memory_id, 0):
                    raise IntegrityError("Simulation cannot apply to knowledge-card sources")
        if any(
            event.kind in {EventKind.HUMAN_DECISION, EventKind.SIMULATED_DECISION}
            and event.details.get("proposal_id") == proposal_id
            and event.details.get("decision") == "applied"
            for event in repository.events()
        ):
            raise IntegrityError("Proposal has already been applied")
        if dry_run:
            if proposal.kind == "merge":
                _validate_merge(repository, proposal)
            return {
                "proposal_id": proposal.proposal_id,
                "kind": proposal.kind,
                "target_ids": list(proposal.target_ids),
                "would_apply": True,
            }
        if proposal.kind == "merge":
            result = _apply_merge(repository, proposal, actor=actor)
            is_merge = True
        elif proposal.kind in {"layer", "anchor", "salience"}:
            is_merge = False
            if len(proposal.target_ids) != 1:
                raise IntegrityError("This proposal kind requires exactly one target")
            memory_id = proposal.target_ids[0]
            repository.get(memory_id)
            changes: dict[str, Any] = {}
            if proposal.kind == "salience":
                score = proposal.output.get("score")
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    raise IntegrityError("Salience proposal has no numeric score")
                changes["strength"] = float(score)
            elif proposal.kind == "layer":
                raw_layer = proposal.output.get("layer")
                if not isinstance(raw_layer, str):
                    raise IntegrityError("Layer proposal has no valid layer")
                try:
                    changes["layer"] = Layer(raw_layer)
                except ValueError as exc:
                    raise IntegrityError("Layer proposal has no valid layer") from exc
            else:
                changes["anchor"] = proposal.output.get("anchor")
                if changes["anchor"] is not None and not isinstance(changes["anchor"], str):
                    raise IntegrityError("Anchor proposal value must be a string or null")
                if changes["anchor"] is not None:
                    changes["layer"] = Layer.ANCHOR
                    changes["tier"] = Tier.HOT
                elif repository.get(memory_id).layer == Layer.ANCHOR:
                    changes["layer"] = Layer.CURRENT
            result = repository.update(memory_id, actor="human", **changes)
        elif proposal.kind == "forget":
            is_merge = False
            action = proposal.output.get("action", "hard")
            if action != "hard":
                raise IntegrityError("Only explicit hard forget proposals can be applied here")
            if any(
                document.anchor or document.layer == Layer.ANCHOR
                for document in (repository.get(memory_id) for memory_id in proposal.target_ids)
            ):
                raise IntegrityError("Forget proposal cannot delete anchored memories")
            result = None
            for memory_id in proposal.target_ids:
                repository.delete(
                    memory_id,
                    actor=actor,
                    _preserve_proposal_ids={proposal.proposal_id},
                    proposal_id=proposal.proposal_id,
                    approved_by=actor,
                )
        else:
            raise IntegrityError(f"Unsupported proposal kind: {proposal.kind}")
        repository.mark_proposal_applied(
            proposal.proposal_id,
            payload_hash=proposal.payload_hash,
            actor=actor,
        )
        if is_merge and enforce_fallback:
            _apply_merge_fallback(repository)
        return result


def approve_batch(
    repository: Repository,
    *,
    filter_type: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not filter_type.startswith("type="):
        raise IntegrityError("Batch filter must use type=<proposal-kind>")
    kind = filter_type.partition("=")[2]
    matches = [item for item in repository.list_proposals(status="pending") if item.kind == kind]
    if dry_run:
        return {
            "dry_run": True,
            "would_approve": [
                {
                    "proposal_id": item.proposal_id,
                    "kind": item.kind,
                    "payload_hash": item.payload_hash,
                }
                for item in matches
            ],
        }
    approved = []
    for proposal in matches:
        decide_proposal(
            repository,
            proposal.proposal_id,
            "approve",
            payload_hash=proposal.payload_hash,
        )
        approved.append(proposal.proposal_id)
    return {"dry_run": False, "approved": approved}


def _validate_merge(repository: Repository, proposal: Proposal) -> tuple[Any, list[Any]]:
    output = proposal.output
    representative_id = output.get("representative_id")
    raw_duplicate_ids = output.get("duplicate_ids")
    disposition = output.get("disposition", "retain")
    if (
        not isinstance(representative_id, str)
        or not isinstance(raw_duplicate_ids, list)
        or not raw_duplicate_ids
        or not all(isinstance(item, str) for item in raw_duplicate_ids)
        or representative_id in raw_duplicate_ids
        or disposition not in {"delete", "retain"}
    ):
        raise IntegrityError("Merge proposal output is malformed")
    duplicate_ids = cast(list[str], raw_duplicate_ids)
    if tuple([representative_id, *duplicate_ids]) != proposal.target_ids:
        raise IntegrityError("Merge targets differ from signed proposal targets")
    representative = repository.get(representative_id)
    documents = [repository.get(memory_id) for memory_id in duplicate_ids]
    if representative.is_stub or any(document.is_stub for document in documents):
        raise IntegrityError("Cold stubs must be restored before a reviewed merge")
    if any(document.anchor or document.layer == Layer.ANCHOR for document in documents):
        raise IntegrityError("Anchored duplicate memories cannot be removed or merged away")
    return representative, documents


def _apply_merge_fallback(repository: Repository) -> None:
    from memfw.curation.engine import enforce_budget
    from memfw.policies import PolicyName

    config = repository.read_config()
    fallback = config.get("merge_fallback_policy", PolicyName.FIFO.value)
    if PolicyName(fallback) == PolicyName.CONSOLIDATE_THEN_FORGET:
        raise IntegrityError("merge_fallback_policy cannot recurse into consolidation")
    enforce_budget(repository, policy_override=fallback, actor="mechanism")


__all__ = [
    "apply_approved",
    "approve_batch",
    "decide_proposal",
    "list_pending",
]
