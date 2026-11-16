"""Budget consolidation and agent merge proposals stay within one tenant and
never trade a current memory for a historical version of itself."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.agents import DeterministicJudge
from memfw.budget import BudgetLimits
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.curation.engine import plan_forget, run_forget
from memfw.policies import ConsolidateThenForgetPolicy, PolicyCandidate, PolicyName, make_policy

NOW = datetime(2026, 2, 1, tzinfo=UTC)


def _doc(identifier: str, body: str, days: int = 0, **fields: Any) -> MemoryDocument:
    stamp = NOW - timedelta(days=days)
    return MemoryDocument(
        id=identifier, title="note", body=body, created_at=stamp, updated_at=stamp, **fields
    )


def _plan(documents: list[MemoryDocument], limits: BudgetLimits) -> Any:
    return ConsolidateThenForgetPolicy().plan(
        [document.model_dump() for document in documents], limits, now=NOW
    )


def test_candidate_carries_tenant_and_historical_flag() -> None:
    current = PolicyCandidate.from_document(_doc("a", "x", tenant="acme"))
    old = PolicyCandidate.from_document(_doc("b", "x", superseded_by="a"))
    ended = PolicyCandidate.from_document(_doc("c", "x", valid_to=NOW))
    assert (current.tenant, current.historical) == ("acme", False)
    assert old.historical and ended.historical


def test_cross_tenant_same_body_is_not_grouped() -> None:
    documents = [_doc("a1", "same body", tenant="A"), _doc("b1", "same body", tenant="B")]
    plan = _plan(documents, BudgetLimits(max_count=1))
    assert plan.forgotten_ids == ()
    assert plan.consolidation_proposals == ()
    assert not plan.satisfied


def test_cross_tenant_same_group_key_is_not_grouped() -> None:
    documents = [
        _doc("a1", "first", tenant="A", group_key="g"),
        _doc("b1", "second", tenant="B", group_key="g"),
    ]
    plan = _plan(documents, BudgetLimits(max_count=1))
    assert plan.forgotten_ids == ()
    assert plan.consolidation_proposals == ()


def test_same_tenant_groups_still_consolidate_beside_another_tenant() -> None:
    documents = [
        _doc("a1", "same body", tenant="A"),
        _doc("a2", "same body", 1, tenant="A"),
        _doc("b1", "same body", tenant="B"),
    ]
    plan = _plan(documents, BudgetLimits(max_count=2))
    assert plan.forgotten_ids == ("a2",)
    assert [(p.representative_id, p.duplicate_ids) for p in plan.consolidation_proposals] == [
        ("a1", ("a2",))
    ]


def test_superseded_same_body_never_forgets_current_in_pure_plan() -> None:
    documents = [
        _doc("a-old", "same", 5, reference_count=3, superseded_by="z-new", valid_to=NOW),
        _doc("z-new", "same", 1),
    ]
    plan = _plan(documents, BudgetLimits(max_count=1))
    assert "z-new" in plan.retained_ids
    assert plan.forgotten_ids == ()
    assert plan.consolidation_proposals == ()


def _superseded_repo(root: Path) -> Repository:
    counter = iter(f"event-{index}" for index in range(1000))
    repo = Repository(root, clock=lambda: NOW, id_factory=lambda: next(counter))
    repo.init()
    repo.add("Plan", "the same body text", memory_id="a-old")
    repo.supersede("a-old", "Plan", "the same body text", memory_id_new="z-new")
    return repo


@pytest.mark.parametrize("action", ["soft", "hard"])
def test_superseded_same_body_never_forgets_current_in_engine(tmp_path: Path, action: str) -> None:
    repo = _superseded_repo(tmp_path / action)
    limits = BudgetLimits(max_count=1)

    plan = plan_forget(repo, PolicyName.CONSOLIDATE_THEN_FORGET, limits, now=NOW, scope="all")
    assert "z-new" not in plan.forgotten_ids

    result = run_forget(
        repo,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        limits,
        now=NOW,
        scope="all",
        action=action,
        dry_run=False,
    )
    assert "z-new" not in result.plan.forgotten_ids
    assert result.applied_ids == ()
    # Review never proposes merging the current version into its own history.
    assert result.pending_proposal_ids == ()
    assert not [
        proposal for proposal in repo.list_proposals(status=None) if "z-new" in proposal.target_ids
    ]
    current = repo.get("z-new")
    assert current.valid_to is None and not current.is_stub
    assert repo.get("a-old").superseded_by == "z-new"


def test_agent_merge_proposals_never_mix_tenants() -> None:
    documents = [
        _doc("a1", "a long shared body of text for matching"),
        _doc("a2", "a long shared body of text for matching", tenant="B"),
        _doc("a3", "a long shared body of text for matching", tenant="B"),
        _doc("a4", "a long shared body of text for matching", tenant="C"),
    ]
    tenants = {document.id: document.tenant for document in documents}
    proposals = DeterministicJudge(clock=lambda: NOW).find_near_duplicates(documents)
    assert [(p.representative_id, p.duplicate_ids) for p in proposals] == [("a2", ("a3",))]
    for proposal in proposals:
        targets = (proposal.representative_id, *proposal.duplicate_ids)
        assert len({tenants[target] for target in targets}) == 1


def test_live_agent_merge_drops_cross_tenant_pairs() -> None:
    documents = [
        _doc("a1", "body", tenant="A"),
        _doc("a2", "body", tenant="A"),
        _doc("b1", "body", tenant="B"),
    ]
    pairs = [
        {"representative_id": "a1", "duplicate_ids": ["b1"], "confidence": 0.9},
        {"representative_id": "a1", "duplicate_ids": ["a2", "b1"], "confidence": 0.9},
        {"representative_id": "a1", "duplicate_ids": ["a2"], "confidence": 0.9},
    ]

    def transport(_provider: str, _request: Any) -> dict[str, Any]:
        return {"merge_pairs": pairs}

    judge = DeterministicJudge(
        mode="live",
        transport=transport,
        model_name="test-model",
        clock=lambda: NOW,
    )
    proposals = judge.find_near_duplicates(documents)
    assert [(p.representative_id, p.duplicate_ids) for p in proposals] == [("a1", ("a2",))]


# Plan JSON for this single-tenant fixture was captured from the code at
# 99c8ad7 (before the tenant/historical scoping) by running both trees side by
# side; the fix must not change it.
_SINGLE_TENANT_PLAN_SHA256 = "88dd8eecef691a429cf7fdbe6e3d7cf0a482fa42f37d315fe303171f5de68ca1"


def _single_tenant_fixture() -> list[MemoryDocument]:
    return [
        _doc("m01", "alpha shared body", 9, tenant="team", reference_count=2),
        _doc("m02", "alpha shared body", 3, tenant="team"),
        _doc("m03", "beta one", 8, tenant="team", group_key="g"),
        _doc("m04", "beta two", 2, tenant="team", group_key="g", manually_marked=True),
        _doc("m05", "unique gamma", 7, tenant="team", layer="anchor"),
        _doc("m06", "unique gamma", 1, tenant="team"),
        _doc("m07", "delta lone", 6, tenant="team", strength=0.9),
        # Historical but with no duplicate: plans keep treating it as before.
        _doc("m08", "epsilon past", 10, tenant="team", valid_to=NOW - timedelta(days=4)),
        _doc("m09", "alpha shared bodx", 5, tenant="team"),
    ]


def test_single_tenant_plan_is_unchanged() -> None:
    documents = _single_tenant_fixture()
    plans: dict[str, Any] = {}
    for name in PolicyName:
        for limits in (
            BudgetLimits(max_count=4),
            BudgetLimits(max_bytes=40),
            BudgetLimits(max_count=0),
        ):
            plan = make_policy(name).plan(
                [document.model_dump() for document in documents], limits, now=NOW
            )
            plans[f"{name.value}:{limits.max_count}:{limits.max_bytes}"] = asdict(plan)
    encoded = json.dumps(plans, default=lambda value: value.isoformat(), sort_keys=True)
    canonical = json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == _SINGLE_TENANT_PLAN_SHA256

    merges = DeterministicJudge(clock=lambda: NOW).find_near_duplicates(documents)
    assert [(m.representative_id, m.duplicate_ids, m.confidence) for m in merges] == [
        ("m01", ("m02",), 1.0),
        ("m01", ("m09",), 0.9047619),
        ("m02", ("m09",), 0.9047619),
        ("m05", ("m06",), 1.0),
    ]
