"""Budget enforcement with ``forget_action: hard`` never deletes a live supersede
chain (a record whose ``superseded_by`` names a stored record, plus that
successor). It picks other records or reports ``history_floor``. Plain expiry
(``valid_to`` alone) is not protected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.budget import BudgetLimits
from memfw.core.models import Layer, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget, plan_forget, run_forget
from memfw.policies import PolicyName, live_chain_ids, make_policy
from memfw.stats import measure_runtime, proposal_stats

NOW = datetime(2026, 2, 1, tzinfo=UTC)
FORGETTING = (PolicyName.FIFO, PolicyName.LRU, PolicyName.DECAY, PolicyName.SALIENCE)
CHAIN = ("a-old", "z-new")


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


def _hard(repo: Repository, policy: PolicyName, limits: BudgetLimits, **kw: Any) -> Any:
    kw.setdefault("dry_run", False)
    kw.setdefault("scope", "all")
    return run_forget(repo, policy, limits, now=NOW, action="hard", **kw)


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


# --- the live chain is kept whole -----------------------------------------


@pytest.mark.parametrize("policy", FORGETTING)
def test_hard_forget_deletes_other_records_and_keeps_chain(
    tmp_path: Path, policy: PolicyName
) -> None:
    repo = _chain_repo(tmp_path / policy.value)

    result = _hard(repo, policy, BudgetLimits(max_count=2))

    assert result.applied_ids == ("o1",)
    assert result.satisfied_after_apply is True
    assert repo.get("a-old").superseded_by == "z-new"
    reasons = {row.memory_id: row.reason for row in result.plan.decisions}
    assert reasons["a-old"] == reasons["z-new"] == "protected_history"


@pytest.mark.parametrize("policy", FORGETTING)
def test_head_is_never_deleted_while_predecessor_is_kept(
    tmp_path: Path, policy: PolicyName
) -> None:
    repo = _chain_repo(tmp_path / policy.value)

    result = _hard(repo, policy, BudgetLimits(max_count=1))

    assert "z-new" not in result.applied_ids and "a-old" not in result.applied_ids
    assert {doc.id for doc in repo.list()} == set(CHAIN)
    assert repo.get("a-old").superseded_by == "z-new"
    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason == "history_floor"
    assert result.blocking_history_ids == CHAIN
    assert result.as_dict()["blocking_history_ids"] == list(CHAIN)
    assert any(
        event.details.get("operation") == "budget_unsatisfiable"
        and event.details.get("reason") == "history_floor"
        for event in repo.events()
    )


@pytest.mark.parametrize("policy", FORGETTING)
def test_enforce_budget_body_mode_keeps_chain(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    _configure(
        repo, max_count=2, forget_policy=policy.value, forget_action="hard", budget_scope="all"
    )

    result = enforce_budget(repo, now=NOW)

    assert result.applied_ids == ("o1",)
    assert result.satisfied_after_apply is True
    assert {doc.id for doc in repo.list()} == set(CHAIN)


@pytest.mark.parametrize("policy", FORGETTING)
def test_enforce_budget_total_mode_keeps_chain(tmp_path: Path, policy: PolicyName) -> None:
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

    assert result.applied_ids == ("o1",)
    assert {doc.id for doc in repo.list()} == set(CHAIN)
    assert repo.get("a-old").superseded_by == "z-new"
    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason_code == "history_floor"
    assert result.blocking_history_ids == CHAIN


def test_total_mode_hot_scope_reason_uses_in_scope_records(tmp_path: Path) -> None:
    repo = _chain_repo(tmp_path / "v")
    repo.update("o1", actor="human", tier=Tier.COLD)
    _configure(
        repo,
        budget_mode="total",
        max_bytes=10**9,
        max_count=0,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="hot",
    )

    result = enforce_budget(repo, now=NOW)

    # The cold o1 is out of scope: it neither turns the floor into
    # policy_exhausted nor appears among the blocking ids.
    assert result.applied_ids == ()
    assert result.unsatisfied_reason_code == "history_floor"
    assert result.blocking_history_ids == CHAIN
    assert "o1" in {doc.id for doc in repo.list()}


@pytest.mark.parametrize("policy", FORGETTING)
def test_chain_with_cold_stub_successor_is_still_protected(
    tmp_path: Path, policy: PolicyName
) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    repo.update("z-new", actor="human", tier=Tier.COLD)
    repo.cold_archive("cool-head", actor="mechanism", memory_ids={"z-new"})
    assert repo.get("z-new").is_stub

    result = _hard(repo, policy, BudgetLimits(max_count=0), scope="hot")

    assert result.applied_ids == ("o1",)
    assert {doc.id for doc in repo.list()} == set(CHAIN)
    assert result.unsatisfied_reason == "history_floor"
    assert result.blocking_history_ids == ("a-old",)


@pytest.mark.parametrize("action", ["hard", "soft"])
@pytest.mark.parametrize("with_anchor", [True, False], ids=["hot_anchor", "no_anchor"])
def test_total_mode_hot_scope_other_floors_match_base(
    tmp_path: Path, action: str, with_anchor: bool
) -> None:
    """Without chains the hot-scope floor stays as before this fix.

    A big cold record outside the hot scope is still a non-anchor memory, so
    the result is ``policy_exhausted`` (base 8429235), never ``anchor_floor``
    or ``metadata_floor``.
    """
    repo = _repo(tmp_path / f"{action}{with_anchor}")
    repo.add("Big", "b" * 6000, memory_id="big-cold", tier=Tier.COLD)
    if with_anchor:
        repo.add("Pinned", "keep me", memory_id="pinned", layer=Layer.ANCHOR)
    _configure(
        repo,
        budget_mode="total",
        max_bytes=1000,
        forget_policy="fifo",
        forget_action=action,
        budget_scope="hot",
    )

    result = enforce_budget(repo, now=NOW)

    assert result.applied_ids == ()
    assert result.unsatisfied_reason_code == "policy_exhausted"
    assert result.blocking_anchor_ids == ()
    assert result.blocking_history_ids == ()


def test_total_mode_still_converges_on_other_records(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "v")
    repo.add("Plan", "p" * 600, memory_id="a-old")
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

    assert result.applied_ids == ("big",)
    assert {doc.id for doc in repo.list()} == set(CHAIN)
    if result.satisfied_after_apply is False:
        assert result.unsatisfied_reason_code in {"history_floor", "metadata_floor"}


# --- plain expiry is not protected ----------------------------------------


@pytest.mark.parametrize("policy", FORGETTING)
@pytest.mark.parametrize("offset_days", [3, -3], ids=["future_valid_to", "expired"])
def test_valid_to_without_live_successor_is_evictable(
    tmp_path: Path, policy: PolicyName, offset_days: int
) -> None:
    repo = _repo(tmp_path / f"{policy.value}{offset_days}")
    repo.add("Ending", "ending body", memory_id="e1")
    repo.update("e1", actor="human", valid_to=NOW + timedelta(days=offset_days))

    result = _hard(repo, policy, BudgetLimits(max_count=0))

    assert result.applied_ids == ("e1",)
    assert result.satisfied_after_apply is True
    assert result.blocking_history_ids == ()


def test_live_chain_ids() -> None:
    documents = [
        _doc("a", 3, superseded_by="b", valid_to=NOW),
        _doc("b", 2, superseded_by="c", valid_to=NOW),
        _doc("c", 1),
        _doc("dangling", 1, superseded_by="gone", valid_to=NOW),
        _doc("expired", 1, valid_to=NOW - timedelta(days=1)),
        _doc("future", 1, valid_to=NOW + timedelta(days=1)),
        _doc("plain", 1),
    ]
    assert live_chain_ids(documents) == {"a", "b", "c"}
    assert live_chain_ids(MemoryDocument(**row) for row in documents) == {"a", "b", "c"}


# --- dry runs and reporting -----------------------------------------------


@pytest.mark.parametrize("policy", FORGETTING)
def test_dry_run_reports_history_floor(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)

    result = _hard(repo, policy, BudgetLimits(max_count=1), dry_run=True)

    assert result.plan.forgotten_ids == ("o1",)
    assert result.unsatisfied_reason == "history_floor"
    assert result.blocking_history_ids == CHAIN
    assert {doc.id for doc in repo.list()} == {*CHAIN, "o1"}


@pytest.mark.parametrize("policy", FORGETTING)
def test_dry_run_that_fits_reports_no_floor(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    result = _hard(repo, policy, BudgetLimits(max_count=2), dry_run=True)
    assert result.unsatisfied_reason is None
    assert result.blocking_history_ids == ()


def test_total_dry_run_reports_history_floor(tmp_path: Path) -> None:
    repo = _chain_repo(tmp_path / "v")
    _configure(
        repo,
        budget_mode="total",
        max_bytes=10**9,
        max_count=0,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="all",
    )

    result = enforce_budget(repo, now=NOW, dry_run=True)

    assert result.unsatisfied_reason_code == "history_floor"
    assert result.blocking_history_ids == CHAIN
    assert {doc.id for doc in repo.list()} == {*CHAIN, "o1"}


def test_proposal_stats_counts_history_floor(tmp_path: Path) -> None:
    repo = _chain_repo(tmp_path / "v")
    plan = plan_forget(
        repo, "fifo", BudgetLimits(max_count=1), now=NOW, scope="all", protect_live_chains=True
    )
    stats = proposal_stats(plan)
    assert stats["history_retained"] == 2
    assert stats["history_floor_unmet"] == 1


# --- unchanged behaviour ----------------------------------------------------


@pytest.mark.parametrize("policy", FORGETTING)
def test_soft_plan_may_still_cool_history(tmp_path: Path, policy: PolicyName) -> None:
    repo = _chain_repo(tmp_path / policy.value)
    plan = plan_forget(repo, policy, BudgetLimits(max_count=0), now=NOW, scope="all")
    # The soft plan is unchanged: cooling archives the original bytes.
    assert "a-old" in plan.forgotten_ids


@pytest.mark.parametrize("policy", FORGETTING)
def test_plan_without_protected_ids_is_unchanged(policy: PolicyName) -> None:
    documents = [_doc(f"m{index}", index, strength=0.1 * (index + 1)) for index in range(6)]
    for limits in (
        BudgetLimits(max_count=3),
        BudgetLimits(max_bytes=20),
        BudgetLimits(max_count=0),
    ):
        plain = make_policy(policy).plan(documents, limits, now=NOW)
        assert make_policy(policy).plan(documents, limits, now=NOW, protected_history_ids=()) == (
            plain
        )
        assert live_chain_ids(documents) == frozenset()


def test_explicit_human_delete_of_chain_member_still_works(tmp_path: Path) -> None:
    repo = _chain_repo(tmp_path / "v")
    repo.delete("a-old", actor="human")
    assert {doc.id for doc in repo.list()} == {"z-new", "o1"}
