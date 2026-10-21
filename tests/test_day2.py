from __future__ import annotations

import hashlib
import io
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import zstandard

from memfw.budget import BudgetLimits
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import EventKind, Layer, Tier
from memfw.core.repository import Repository
from memfw.curation.engine import (
    _with_archived_body,
    access_projection,
    candidates_for,
    enforce_budget,
    plan_forget,
    run_forget,
)
from memfw.curation.review import apply_approved, decide_proposal
from memfw.index import MemoryIndex
from memfw.layers import set_layer
from memfw.policies import PolicyName
from memfw.retrieval import VaultRetriever
from memfw.stats import measure_runtime
from memfw.tiers import BudgetAwareTierPolicy

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def make_repo(root: Path) -> Repository:
    counter = iter(f"event-{index}" for index in range(1000))
    repo = Repository(root, clock=lambda: NOW, id_factory=lambda: next(counter))
    repo.init()
    return repo


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("policy", tuple(PolicyName))
def test_all_policies_protect_anchor_at_runtime(policy: PolicyName, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / policy.value)
    repo.add("Pinned", "pinned payload", memory_id="pinned", layer=Layer.ANCHOR)
    repo.add("Older", "ordinary payload", memory_id="ordinary")

    plan = plan_forget(repo, policy, BudgetLimits(max_count=0), now=NOW)

    assert "pinned" in plan.retained_ids
    assert "pinned" not in plan.forgotten_ids
    assert not plan.satisfied


def test_anchor_layer_transition_requires_human_or_repository_approval(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "anchor-layer")
    repo.add("Future anchor", "keep this", memory_id="future-anchor")

    with pytest.raises(IntegrityError):
        set_layer(
            repo,
            "future-anchor",
            Layer.ANCHOR,
            actor="agent:full",
            agent_id="full",
            signature="opaque-signature",
        )

    anchored = set_layer(repo, "future-anchor", Layer.ANCHOR, actor="human")
    assert anchored.layer == Layer.ANCHOR


def test_soft_forget_cools_and_archives_without_reducing_total_count(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "soft")
    repo.add("Old", "old body", memory_id="a-old")
    repo.add("New", "new body", memory_id="z-new")

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="hot",
        action="soft",
        dry_run=False,
    )

    assert result.applied_ids == ("a-old",)
    assert result.satisfied_after_apply is True
    assert result.actual_usage_count == 1
    assert len(repo.list()) == 2
    archived = repo.get("a-old")
    assert archived.tier == Tier.COLD and archived.is_stub
    forget = [event for event in repo.events() if event.kind == EventKind.FORGET]
    assert len(forget) == 1
    assert forget[0].actor == "mechanism"
    assert forget[0].details["reason"].startswith("budget_pressure;")


def test_agent_cannot_apply_soft_forget_even_when_permissions_are_none(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "agent-forget")
    repo.add("Old", "old body", memory_id="old")
    config = repo.config_path.read_text(encoding="utf-8")
    repo._atomic_write(
        repo.config_path,
        config.replace("permissions: propose_then_confirm", "permissions: none").encode(),
    )
    before = tree_hashes(repo.root)

    proposal = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        now=NOW,
        action="soft",
        dry_run=True,
        actor="agent:untrusted",
        agent_id="untrusted",
        signature="unsigned-proposal",
    )
    assert proposal.applied_ids == ()
    assert tree_hashes(repo.root) == before

    with pytest.raises(IntegrityError, match="cannot apply"):
        run_forget(
            repo,
            PolicyName.FIFO,
            BudgetLimits(max_count=0),
            now=NOW,
            action="soft",
            dry_run=False,
            actor="agent:untrusted",
            agent_id="untrusted",
            signature="unsigned-proposal",
        )
    assert tree_hashes(repo.root) == before


def test_body_budget_anchor_floor_is_recorded_in_auditable_event(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "body-anchor-floor")
    repo.add("Pinned", "protected body", memory_id="pinned", layer=Layer.ANCHOR)

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )

    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason == "anchor_floor"
    assert result.blocking_anchor_ids == ("pinned",)
    event = next(event for event in repo.events() if event.kind == EventKind.BUDGET_UNSATISFIABLE)
    assert event.details["budget_mode"] == "body"
    assert event.details["anchor_ids"] == "pinned"


def test_soft_archive_contains_only_selected_targets_and_skips_cold_anchor(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "selected-archive")
    config = repo.read_config()
    config["anchor_always_hot"] = False
    repo.configure(config)
    repo.add("Cold anchor", "anchor body", memory_id="anchor", layer=Layer.ANCHOR, tier=Tier.COLD)
    repo.add("Cool me", "old body", memory_id="a-cool")
    repo.add("Keep hot", "new body", memory_id="z-hot")

    run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="hot",
        action="soft",
        dry_run=False,
    )

    anchor = repo.get("anchor")
    cooled = repo.get("a-cool")
    assert not anchor.is_stub and anchor.tier == Tier.COLD
    archive_bytes = zstandard.ZstdDecompressor().decompress(
        (repo.archives_dir / f"{cooled.archive_ref}.tar.zst").read_bytes()
    )
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        assert archive.getnames() == ["memories/a-cool.md"]


def test_repeated_soft_forget_after_autowarm_uses_a_fresh_archive_id(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repeat-soft")
    repo.add("Cool me", "old body", memory_id="a-cool")
    first = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        now=NOW,
        scope="hot",
        action="soft",
        dry_run=False,
    )
    first_archive = repo.get("a-cool").archive_ref

    repo.autowarm("a-cool", actor="mechanism")
    second = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        now=NOW,
        scope="hot",
        action="soft",
        dry_run=False,
    )

    assert first.applied_ids == second.applied_ids == ("a-cool",)
    assert repo.get("a-cool").archive_ref != first_archive
    assert not (repo.archives_dir / f"{first_archive}.tar.zst").exists()
    second_archive = repo.archives_dir / f"{repo.get('a-cool').archive_ref}.tar.zst"
    assert second_archive.exists()
    archive_bytes = zstandard.ZstdDecompressor().decompress(second_archive.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        assert archive.getnames() == ["memories/a-cool.md"]


def test_soft_all_scope_does_not_claim_total_count_budget_was_met(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "soft-all")
    repo.add("Old", "old body", memory_id="a-old")
    repo.add("New", "new body", memory_id="z-new")

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="soft",
        dry_run=False,
    )

    assert len(repo.list()) == 2
    assert result.actual_usage_count == 2
    assert result.satisfied_after_apply is False
    assert result.as_dict()["satisfied"] is False


def test_scope_all_counts_archived_cold_body_without_restoring_or_writing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "scope-all")
    repo.add("Archived", "archived body", memory_id="a-cold", tier=Tier.COLD)
    repo.add("Hot", "hot body", memory_id="z-hot")
    repo.cold_archive("existing-archive")
    before = tree_hashes(repo.root)

    plan = plan_forget(repo, PolicyName.FIFO, BudgetLimits(max_count=1), now=NOW, scope="all")

    assert len(plan.decisions) == 2
    assert plan.usage.count == 1
    assert plan.usage.bytes == len(b"hot body")
    assert "a-cold" in plan.forgotten_ids
    assert repo.get("a-cold").is_stub
    assert tree_hashes(repo.root) == before


def test_archived_policy_candidate_uses_current_stub_metadata(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "archive-metadata")
    repo.add("Cold", "old body", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("metadata-archive")
    repo.update(
        "cold",
        strength=0.12,
        reference_count=4,
        last_access_interval_days=3.5,
        manually_marked=True,
        group_key="current-group",
    )

    candidate = candidates_for(repo)[0]

    assert candidate.strength == 0.12
    assert candidate.reference_count == 4
    assert candidate.last_access_interval_days == 3.5
    assert candidate.manually_marked
    assert candidate.group_key == "current-group"


@pytest.mark.parametrize("archived_hash", [None, "0" * 64])
def test_archived_policy_candidate_rejects_missing_or_wrong_body_hash(
    archived_hash: str | None, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / f"archive-hash-{archived_hash is None}")
    repo.add("Cold", "original body", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("hash-check")
    stub = repo.get("cold").model_copy(update={"archived_hash": archived_hash})

    with pytest.raises(IntegrityError, match="Archived content hash mismatch"):
        _with_archived_body(repo, stub)


def test_soft_forget_all_does_not_count_an_already_cold_stub_as_applied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "soft-cold")
    repo.add("Archived", "archived body", memory_id="a-cold", tier=Tier.COLD)
    repo.add("Hot", "hot body", memory_id="z-hot")
    repo.cold_archive("existing-archive")

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="soft",
        dry_run=False,
    )

    assert result.plan.forgotten_ids == ("a-cold",)
    assert result.applied_ids == ()
    assert result.actual_usage_count == 2
    assert result.satisfied_after_apply is False


def test_scope_all_soft_forget_archives_selected_existing_cold_memory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "cold-soft")
    repo.add("Hot", "hot body", memory_id="hot")
    repo.add("Cold", "cold body", memory_id="cold", tier=Tier.COLD)

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        now=NOW,
        scope="all",
        action="soft",
        dry_run=False,
    )

    assert set(result.applied_ids) == {"hot", "cold"}
    assert result.satisfied_after_apply is False
    assert result.actual_usage_count == 2
    assert all(repo.get(memory_id).is_stub for memory_id in ("hot", "cold"))
    assert not result.as_dict()["satisfied"]


def test_configured_budget_enforcement_allows_mechanism_hard_delete(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "configured-budget")
    repo.add("Old", "old body", memory_id="old")
    repo._atomic_write(
        repo.config_path,
        b"budget_level: 1MB\nmax_count: 0\nbudget_scope: hot\n"
        b"forget_policy: fifo\nforget_action: hard\n",
    )

    result = enforce_budget(repo, now=NOW, dry_run=False)

    assert result.action == "hard"
    assert result.applied_ids == ("old",)
    assert any(
        event.kind == EventKind.DELETE and event.memory_id == "old" for event in repo.events()
    )
    with pytest.raises(NotFoundError):
        repo.get("old")


def test_enforce_budget_dry_run_reads_config_without_writing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "configured-dry-run")
    repo.add("Old", "old body", memory_id="old")
    repo._atomic_write(
        repo.config_path,
        b"budget_level: 10MB\nmax_count: 0\nforget_policy: fifo\nforget_action: soft\n",
    )
    before = tree_hashes(repo.root)

    result = enforce_budget(repo, now=NOW, dry_run=True)

    assert result.plan.limits.max_bytes == 10_000_000
    assert result.plan.limits.max_count == 0
    assert result.applied_ids == ()
    assert tree_hashes(repo.root) == before


def test_hard_forget_erases_managed_original(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "hard")
    repo.add("Old", "sensitive payload", memory_id="a-old")
    repo.add("New", "ordinary payload", memory_id="z-new")

    result = run_forget(
        repo,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
        actor="human",
    )

    assert result.applied_ids == ("a-old",)
    assert result.satisfied_after_apply is True
    assert "a-old" not in {doc.id for doc in repo.list()}
    assert not (repo.history_dir / "a-old").exists()
    assert "sensitive payload" not in repo.events_path.read_text(encoding="utf-8")


def test_consolidation_writes_representative_link_before_forgetting_duplicate(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "consolidation")
    shared_body = (
        "The migration checklist records service ownership, deployment readiness, "
        "rollback steps, and health checks for the release. "
    ) * 3
    repo.add("Representative migration checklist", shared_body, memory_id="a-rep")
    repo.add("Duplicate migration checklist", shared_body, memory_id="z-dup")

    result = run_forget(
        repo,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )

    assert result.applied_ids == ()
    assert len(result.pending_proposal_ids) == 1
    assert result.unsatisfied_reason == "approval_required"
    proposal = repo.list_proposals(status="pending")[0]
    assert proposal.output["representative_id"] == "a-rep"
    duplicates = proposal.output["duplicate_ids"]
    assert isinstance(duplicates, list)
    assert "z-dup" in duplicates
    assert "a-rep" in {doc.id for doc in repo.list()}
    assert "z-dup" in {doc.id for doc in repo.list()}

    decide_proposal(repo, proposal.proposal_id, "approve", payload_hash=proposal.payload_hash)
    apply_approved(repo, proposal.proposal_id)

    merged = repo.get("a-rep")
    assert "memory:z-dup@" in merged.provenance
    assert "a-rep" in {doc.id for doc in repo.list()}
    assert "z-dup" not in {doc.id for doc in repo.list()}


def test_consolidation_judge_finds_near_text_without_exact_group_key(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "consolidation-near")
    repo.add(
        "Phoenix rollout checklist",
        (
            "The Phoenix rollout checklist covers deployment readiness, owner review, "
            "rollback steps, and service health checks."
        ),
        memory_id="a-rep",
    )
    repo.add(
        "Phoenix deployment notes",
        (
            "The Phoenix rollout checklist covers deployment readiness, owner review, "
            "rollback steps, and service health checks for release day."
        ),
        memory_id="z-near",
    )

    result = run_forget(
        repo,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )

    assert result.pending_proposal_ids
    assert result.applied_ids == ()
    assert {document.id for document in repo.list()} == {"a-rep", "z-near"}
    proposal = repo.list_proposals(status="pending")[0]
    assert proposal.output["representative_id"] == "a-rep"
    assert proposal.output["duplicate_ids"] == ["z-near"]


def test_consolidation_within_budget_is_noop_and_dry_run_reports_review(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "consolidation-noop")
    body = "A stable deployment checklist with rollback and health checks. " * 3
    repo.add("Deployment checklist A", body, memory_id="a")
    repo.add("Deployment checklist B", body, memory_id="b")
    before = tree_hashes(repo.root)

    noop = run_forget(
        repo,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        BudgetLimits(max_count=2),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=False,
    )
    assert noop.satisfied_after_apply is True
    assert noop.pending_proposal_ids == ()
    assert tree_hashes(repo.root) == before

    dry_run = run_forget(
        repo,
        PolicyName.CONSOLIDATE_THEN_FORGET,
        BudgetLimits(max_count=1),
        now=NOW,
        scope="all",
        action="hard",
        dry_run=True,
    )
    assert dry_run.would_propose
    assert dry_run.plan.forgotten_ids == ()
    assert tree_hashes(repo.root) == before


def test_total_consolidation_budget_creates_review_proposal_without_deleting(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "total-consolidation")
    body = "A stable deployment checklist with rollback and health checks. " * 10
    repo.add("Deployment checklist A", body, memory_id="a")
    repo.add("Deployment checklist B", body, memory_id="b")
    physical = measure_runtime(repo, now=NOW).physical_disk_bytes
    config = repo.read_config()
    config.update(
        {
            "budget_mode": "total",
            "max_bytes": physical - 1,
            "budget_scope": "all",
            "forget_action": "hard",
            "forget_policy": PolicyName.CONSOLIDATE_THEN_FORGET.value,
        }
    )
    repo.configure(config)

    result = enforce_budget(repo, now=NOW)

    assert result.budget_mode == "total"
    assert result.pending_proposal_ids
    assert result.applied_ids == ()
    assert result.unsatisfied_reason_code == "approval_required"
    assert "allocated bytes" in (result.unsatisfied_reason or "")
    assert {document.id for document in repo.list()} == {"a", "b"}


def test_dry_run_keeps_every_vault_file_hash_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "dry")
    repo.add("Old", "old body", memory_id="a-old")
    repo.add("New", "new body", memory_id="z-new")
    before = tree_hashes(repo.root)

    result = run_forget(
        repo,
        PolicyName.LRU,
        BudgetLimits(max_count=1),
        now=NOW,
        action="soft",
        dry_run=True,
    )

    assert result.dry_run
    assert result.applied_ids == ()
    assert tree_hashes(repo.root) == before


def test_policy_candidate_reads_persisted_strength_reference_and_access_spacing(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "metadata")
    doc = repo.add("Measured", "body", memory_id="measured")
    repo.update(
        doc.id,
        strength=0.17,
        reference_count=3,
        last_access_interval_days=12.5,
        manually_marked=True,
        group_key="group-a",
    )

    candidate = candidates_for(repo)[0]

    assert candidate.strength == 0.17
    assert candidate.reference_count == 3
    assert candidate.last_access_interval_days == 12.5
    assert candidate.manually_marked
    assert candidate.group_key == "group-a"


def test_tier_policy_uses_idle_strength_and_hot_budget(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "tiers")
    repo.add("Idle weak", "x" * 20, memory_id="idle")
    repo.add("Recent strong", "y" * 20, memory_id="recent")
    repo.capture_event(EventKind.MEMORY_ACCESS, actor="human", details={"memory_id": "recent"})
    policy = BudgetAwareTierPolicy(idle_days=10, strength_threshold=0.3)

    plan = policy.plan(repo, now=NOW, limits=BudgetLimits(max_count=1), strengths={"recent": 0.9})

    assert plan.cooled_ids == ("idle",)
    assert plan.hot_usage_after[0] == 1
    policy.apply(repo, plan, dry_run=False)
    assert repo.get("idle").tier == Tier.COLD
    assert repo.get("recent").tier == Tier.HOT


def test_tier_policy_idle_or_low_strength_and_explicit_now() -> None:
    policy = BudgetAwareTierPolicy(idle_days=10, strength_threshold=0.25)
    recent = NOW - timedelta(days=1)
    idle = NOW - timedelta(days=11)

    assert policy.choose_tier(last_access=recent, token_cost=1, strength=0.1, now=NOW) == Tier.COLD
    assert policy.choose_tier(last_access=idle, token_cost=1, strength=0.9, now=NOW) == Tier.COLD
    with pytest.raises(TypeError):
        policy.choose_tier(last_access=recent, token_cost=1, strength=0.9)  # type: ignore[call-arg]


def test_cold_retrieval_autowarms_records_access_and_keeps_latency_ephemeral(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "retrieve")
    repo.add("Cold note", "find this cold phrase", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("cold-archive")
    index = MemoryIndex(repo.index_path)
    index.rebuild(repo)
    ticks = iter((3.0, 3.025, 3.075, 3.125))
    retriever = VaultRetriever(repo, index, timer=lambda: next(ticks))

    hits = retriever.retrieve("cold phrase", limit=5)

    assert hits == ["cold"]
    warmed = repo.get("cold")
    assert not warmed.is_stub and warmed.tier == Tier.HOT
    assert retriever.last_metrics == {
        "elapsed_seconds": 0.125,
        "result_count": 1,
        "cold_hit_count": 1,
        "cold_restore_seconds": pytest.approx(0.05),
        "cold_restore_seconds_by_memory": {"cold": pytest.approx(0.05)},
    }
    assert access_projection(repo)["cold"][1] == 1
    kinds = [event.kind for event in repo.events() if event.memory_id == "cold"]
    assert EventKind.MEMORY_RESTORE in kinds
    assert EventKind.TIER_CHANGE in kinds
    assert EventKind.MEMORY_ACCESS in kinds


def test_injected_latency_does_not_change_fixed_input_vault_bytes(tmp_path: Path) -> None:
    snapshots = []
    for name, values in (
        ("fast", (1.0, 1.02, 1.06, 1.1)),
        ("slow", (20.0, 20.2, 20.8, 20.9)),
    ):
        repo = make_repo(tmp_path / name)
        repo.add("Cold note", "same fixed phrase", memory_id="cold", tier=Tier.COLD)
        repo.cold_archive("fixed-archive")
        index = MemoryIndex(repo.index_path)
        index.rebuild(repo)
        retriever = VaultRetriever(repo, index, timer=iter(values).__next__)
        retriever.retrieve("fixed phrase", limit=1)
        snapshots.append(tree_hashes(repo.root))

    assert snapshots[0] == snapshots[1]


def test_stats_report_physical_allocation_and_keep_body_budget_separate(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "stats")
    repo.add("Hot", "雪", memory_id="hot")
    repo.add("Cold", "cold body", memory_id="cold", tier=Tier.COLD)
    repo.capture_event(
        EventKind.FORGET,
        actor="human",
        details={
            "memory_id": "hot",
            "operation": "delete",
            "policy": "fifo",
            "reason": "test",
        },
    )

    result = measure_runtime(repo, now=NOW).as_dict()
    physical_bytes = result["physical_disk_bytes"]
    body_bytes = result["body_bytes"]
    hot_ratio = result["hot_ratio"]
    cold_ratio = result["cold_ratio"]
    recent_forget_count = result["recent_forget_count"]
    body_scope = result["body_budget_scope"]
    assert isinstance(physical_bytes, int)
    assert isinstance(body_bytes, int)
    assert isinstance(hot_ratio, (int, float))
    assert isinstance(cold_ratio, (int, float))
    assert isinstance(recent_forget_count, int)
    assert isinstance(body_scope, str)

    disk_bytes = sum(
        getattr(path.stat(), "st_blocks", (path.stat().st_size + 511) // 512) * 512
        for path in repo.root.rglob("*")
        if path.is_file()
    )
    assert physical_bytes == disk_bytes
    assert body_bytes == len("雪".encode()) + len(b"cold body")
    assert hot_ratio + cold_ratio == pytest.approx(1.0)
    assert recent_forget_count == 1
    assert "logs" in body_scope and "archives" in body_scope


def test_stats_count_archived_original_body_without_restoring_stub(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "archived-stats")
    repo.add("Cold", "archived body", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("stats-archive")

    result = measure_runtime(repo, now=NOW)

    assert result.memory_count == 1
    assert result.cold_body_bytes == len(b"archived body")
    assert repo.get("cold").is_stub


def test_rebuild_replay_terminal_hashes_match_current_markdown(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "replay")
    repo.add("Cold", "rebuild phrase", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("archive")
    MemoryIndex(repo.index_path).rebuild(repo)
    repo.autowarm("cold", actor="mechanism")
    repo.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="mechanism",
        details={
            "memory_id": "cold",
            "operation": "retrieve",
        },
    )

    terminal = repo.replay()
    current = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repo.memories_dir.glob("*.md")
    }

    assert terminal == current
