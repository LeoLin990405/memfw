"""Budget enforcement with ``forget_action: hard`` never deletes a historical
version (``valid_to`` / ``superseded_by`` set); it picks current records instead
or reports that the budget cannot be met."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.budget import BudgetLimits
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget, plan_forget, run_forget
from memfw.policies import PolicyName, make_policy
from memfw.stats import measure_runtime

NOW = datetime(2026, 2, 1, tzinfo=UTC)
FORGETTING = (PolicyName.FIFO, PolicyName.LRU, PolicyName.DECAY, PolicyName.SALIENCE)


def _repo(root: Path) -> Repository:
    counter = iter(f"event-{index}" for index in range(10_000))
    repo = Repository(root, clock=lambda: NOW, id_factory=lambda: next(counter))
    repo.init()
    return repo


def _chain_repo(root: Path) -> Repository:
    """``a-old -> z-new`` plus one unrelated current record ``o1``."""
    repo = _repo(root)
    repo.add("Plan", "first plan body", memory_id="a-old")
    repo.supersede("a-old", "Plan", "second plan body", memory_id_new="z-new")
    repo.add("Other", "other body", memory_id="o1")
    return repo


def _configure(repo: Repository, **values: Any) -> None:
    config = repo.read_config()
    config.update(values)
    repo.configure(config)


def _doc(identifier: str, days: int, **fields: Any) -> dict[str, Any]:
    stamp = NOW - timedelta(days=days)
    return MemoryDocument(
        id=identifier,
        title="t",
        body="body " + identifier,
        created_at=stamp,
        updated_at=stamp,
        **fields,
    ).model_dump()


@pytest.mark.parametrize("policy", FORGETTING)
def test_hard_run_forget_keeps_superseded_version(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)

    result = run_forget(
        repo,
        policy,
        BudgetLimits(max_count=2),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )

    assert "a-old" not in result.applied_ids
    assert len(result.applied_ids) == 1
    assert result.satisfied_after_apply is True
    old = repo.get("a-old")
    assert old.superseded_by == "z-new" and old.valid_to is not None
    reasons = {row.memory_id: row.reason for row in result.plan.decisions}
    assert reasons["a-old"] == "protected_history"


@pytest.mark.parametrize("policy", FORGETTING)
def test_enforce_budget_body_mode_keeps_history(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    _configure(
        repo, max_count=2, forget_policy=policy.value, forget_action="hard", budget_scope="all"
    )

    result = enforce_budget(repo, now=NOW)

    assert "a-old" not in result.applied_ids
    assert result.satisfied_after_apply is True
    assert {doc.id for doc in repo.list()} >= {"a-old"}
    assert len(repo.list()) == 2


@pytest.mark.parametrize("policy", FORGETTING)
def test_enforce_budget_total_mode_keeps_history(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    _configure(
        repo,
        budget_mode="total",
        max_bytes=10**9,
        max_count=0,
        forget_policy=policy.value,
        forget_action="hard",
        budget_scope="all",
    )

    result = enforce_budget(repo, now=NOW)

    assert "a-old" not in result.applied_ids
    # Deleting the successor unlinks ``superseded_by``; ``valid_to`` stays.
    assert repo.get("a-old").valid_to is not None
    # Only history is left: the budget is honestly reported as unmet.
    assert {doc.id for doc in repo.list()} == {"a-old"}
    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason_code == "history_floor"
    assert result.blocking_history_ids == ("a-old",)


@pytest.mark.parametrize("policy", FORGETTING)
def test_unmet_budget_reports_history_floor(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)

    result = run_forget(
        repo,
        policy,
        BudgetLimits(max_count=0),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )

    assert set(result.applied_ids) == {"z-new", "o1"}
    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason == "history_floor"
    assert result.blocking_history_ids == ("a-old",)
    assert result.as_dict()["blocking_history_ids"] == ["a-old"]
    assert [doc.id for doc in repo.list()] == ["a-old"]
    assert any(
        event.details.get("operation") == "budget_unsatisfiable"
        and event.details.get("reason") == "history_floor"
        for event in repo.events()
    )


@pytest.mark.parametrize("policy", FORGETTING)
def test_soft_forget_may_still_cool_history(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    plan = plan_forget(repo, policy, BudgetLimits(max_count=0), now=NOW, scope="all")
    # The soft plan is unchanged: cooling archives the original bytes.
    assert "a-old" in plan.forgotten_ids


@pytest.mark.parametrize("policy", FORGETTING)
def test_current_records_plan_is_unchanged(policy: PolicyName) -> None:
    documents = [_doc(f"m{index}", index, strength=0.1 * (index + 1)) for index in range(6)]
    for limits in (
        BudgetLimits(max_count=3),
        BudgetLimits(max_bytes=20),
        BudgetLimits(max_count=0),
    ):
        plain = make_policy(policy).plan(documents, limits, now=NOW)
        protected = make_policy(policy).plan(documents, limits, now=NOW, protect_historical=True)
        assert protected == plain


@pytest.mark.parametrize("policy", FORGETTING)
def test_protected_plan_picks_next_current_candidate(policy: PolicyName) -> None:
    documents = [
        _doc("h", 30, valid_to=NOW - timedelta(days=1), superseded_by="c1"),
        _doc("c1", 1),
        _doc("c2", 20),
        _doc("c3", 10),
    ]
    plain = make_policy(policy).plan(documents, BudgetLimits(max_count=2), now=NOW)
    protected = make_policy(policy).plan(
        documents, BudgetLimits(max_count=2), now=NOW, protect_historical=True
    )
    assert "h" not in protected.forgotten_ids
    assert len(protected.forgotten_ids) == len(plain.forgotten_ids) == 2
    assert protected.satisfied


def test_explicit_human_delete_of_history_still_works(tmp_path: Path) -> None:
    repo = _chain_repo(tmp_path / "v")
    repo.delete("a-old", actor="human")
    assert {doc.id for doc in repo.list()} == {"z-new", "o1"}


def test_total_mode_still_converges_on_current_records(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "v")
    repo.add("Plan", "p" * 6000, memory_id="a-old")
    repo.supersede("a-old", "Plan", "q" * 10, memory_id_new="z-new")
    repo.add("Big", "b" * 6000, memory_id="big")
    before = measure_runtime(repo, now=NOW).physical_disk_bytes
    _configure(
        repo,
        budget_mode="total",
        max_bytes=before - 2500,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="all",
    )

    result = enforce_budget(repo, now=NOW)

    assert "a-old" not in result.applied_ids
    assert result.applied_ids
    assert repo.get("a-old").superseded_by == "z-new"
    if result.satisfied_after_apply is False:
        assert result.unsatisfied_reason_code in {"history_floor", "metadata_floor"}
