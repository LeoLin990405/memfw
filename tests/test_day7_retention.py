from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from memfw.budget import BudgetLimits
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.curation.engine import run_forget
from memfw.curation.review import apply_approved
from memfw.policies import PolicyName

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from day7_retention import configured_policy, project_consolidation_retention


def _repo(path: Path) -> Repository:
    sequence = iter(f"retention-event-{index}" for index in range(500))
    return Repository(
        path,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def test_consolidation_projection_matches_persistent_simulated_merge(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "persistent-vault")
    repository.init()
    body = "shared duplicate body repeated phrase alpha beta gamma " * 3
    representative = repository.add("Alpha", body, memory_id="alpha")
    repository.add("Beta", body, memory_id="beta")
    now = datetime(2026, 10, 18, tzinfo=UTC)
    limits = BudgetLimits(max_bytes=len(representative.body.encode("utf-8")))
    projection = project_consolidation_retention(
        repository.list(),
        limits,
        now=now,
    )
    assert projection.retained_ids == ("alpha",)
    assert projection.removed_ids == ("beta",)
    assert projection.projected_merge_count == 1

    result = run_forget(
        repository,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        limits,
        now=now,
        scope="all",
        action="hard",
        dry_run=False,
        actor="mechanism",
    )
    assert len(result.pending_proposal_ids) == 1
    proposal_id = result.pending_proposal_ids[0]
    repository.simulate_proposal_decision(proposal_id)
    applied = apply_approved(
        repository,
        proposal_id,
        actor="simulated_approver",
        enforce_fallback=False,
    )
    assert applied.id == "alpha"

    actual = repository.list()
    assert tuple(sorted(document.id for document in actual)) == projection.retained_ids
    assert sum(len(document.body.encode("utf-8")) for document in actual) == len(
        representative.body.encode("utf-8")
    )
    provenance = json.loads(repository.get("alpha").provenance)
    assert any(item.startswith("memory:alpha@") for item in provenance)
    assert any(item.startswith("memory:beta@") for item in provenance)
    events = repository.events()
    assert any(
        event.kind == EventKind.SIMULATED_DECISION
        and event.actor == "simulated_approver"
        and event.details.get("decision") == "applied"
        for event in events
    )
    assert any(
        event.kind == EventKind.DELETE
        and event.memory_id == "beta"
        and event.actor == "simulated_approver"
        for event in events
    )
    assert not any(event.kind == EventKind.HUMAN_DECISION for event in events)


def test_configured_policy_uses_fifo_fallback_and_explicit_override() -> None:
    assert configured_policy({}) == PolicyName.FIFO
    assert configured_policy({"forget_policy": "lru"}) == PolicyName.LRU
    assert configured_policy({"forget_policy": "lru"}, PolicyName.SALIENCE) == PolicyName.SALIENCE
