from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.agents.models import JsonValue, Proposal, canonical_payload_hash, mock_signature
from memfw.budget import BudgetLimits
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget, run_forget
from memfw.curation.review import apply_approved
from memfw.policies import PolicyName


def make_repo(path: Path, ids: list[str] | None = None) -> Repository:
    sequence = iter(ids or (f"event-{index}" for index in range(1000)))
    return Repository(
        path,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def append_access(repository: Repository) -> None:
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access"},
    )


def test_warm_capture_event_uses_cursor_and_appends_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)
    cursor_path = repository.root / "events.state.json"
    first_cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert set(first_cursor) == {
        "schema_version",
        "catalog_hash",
        "offset",
        "event_count",
        "last_hash",
    }
    assert first_cursor["schema_version"] == 2
    assert not (repository.root / "events.head").exists()
    assert not (repository.root / "events.cursor.json").exists()

    original_events = repository.events
    original_atomic_write = repository._atomic_write

    def reject_whole_log_rewrite(path: Path, data: bytes) -> None:
        assert path != repository.events_path, "append path must not replace the full JSONL log"
        original_atomic_write(path, data)

    monkeypatch.setattr(
        repository,
        "events",
        lambda: pytest.fail("warm metadata append must not replay the full event log"),
    )
    monkeypatch.setattr(repository, "_atomic_write", reject_whole_log_rewrite)
    for _ in range(20):
        append_access(repository)
    assert cursor_path.exists()
    monkeypatch.setattr(repository, "events", original_events)
    assert len(repository.events()) == 21


def test_new_repository_validates_cursor_and_cross_instance_appends(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    first = make_repo(root)
    first.init()
    append_access(first)

    second = make_repo(root)
    append_access(second)
    append_access(first)
    assert [event.event_hash for event in first.events()] == [
        event.event_hash for event in second.events()
    ]
    assert len(second.events()) == 3


def test_unlogged_external_tail_change_invalidates_cursor(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)
    with repository.events_path.open("ab") as stream:
        stream.write(b"not-a-canonical-event\n")
        stream.flush()
        os.fsync(stream.fileno())

    with pytest.raises(IntegrityError):
        append_access(repository)


def test_corrupt_cursor_count_is_rebuilt_before_append(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)
    cursor_path = repository.root / "events.state.json"
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    cursor["event_count"] += 7
    cursor_path.write_text(json.dumps(cursor), encoding="utf-8")

    append_access(repository)

    repaired = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert repaired["event_count"] == 2
    assert len(repository.events()) == 2


def test_compaction_invalidates_cursor_before_next_append(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    for _ in range(5):
        append_access(repository)
    assert (repository.root / "events.state.json").exists()
    repository.compact_log(keep_recent=1)
    state = json.loads((repository.root / "events.state.json").read_text(encoding="utf-8"))
    assert state["event_count"] == 5
    append_access(repository)
    assert len(repository.events()) == 6


def test_partial_jsonl_append_recovers_only_from_known_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)

    def partial_write(event: object, line: bytes, previous: str, event_count: int) -> None:
        cursor = repository._validated_event_cursor()
        intent = {
            "schema_version": 1,
            "offset": cursor["offset"],
            "previous_hash": previous,
            "event_hash": event.event_hash,  # type: ignore[attr-defined]
            "line": line.decode("utf-8"),
            "event_count": event_count,
        }
        repository._atomic_write(
            repository.root / ".event-append.pending.json",
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        with repository.events_path.open("ab") as stream:
            stream.write(line[: max(1, len(line) // 2)])
            stream.flush()
            os.fsync(stream.fileno())
        raise OSError("injected partial append")

    monkeypatch.setattr(repository, "_append_event_with_intent", partial_write)
    with pytest.raises(OSError, match="partial append"):
        append_access(repository)
    monkeypatch.undo()

    assert repository.recover_pending() == ["events.jsonl"]
    events = repository.events()
    assert len(events) == 2
    assert events[-1].prev_event_hash == events[0].event_hash


def test_compaction_is_blocked_while_complete_append_intent_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)

    def complete_line_but_leave_intent(
        event: object, line: bytes, previous: str, event_count: int
    ) -> None:
        cursor = repository._validated_event_cursor()
        intent = {
            "schema_version": 1,
            "offset": cursor["offset"],
            "previous_hash": previous,
            "event_hash": event.event_hash,  # type: ignore[attr-defined]
            "line": line.decode("utf-8"),
            "event_count": event_count,
        }
        repository._atomic_write(
            repository.root / ".event-append.pending.json",
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        with repository.events_path.open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        raise OSError("injected crash after complete line")

    monkeypatch.setattr(repository, "_append_event_with_intent", complete_line_but_leave_intent)
    with pytest.raises(OSError, match="complete line"):
        append_access(repository)
    monkeypatch.undo()

    with pytest.raises(IntegrityError, match="pending event append"):
        repository.compact_log(keep_recent=1)
    assert repository.recover_pending() == ["events.jsonl"]
    assert len(repository.events()) == 2


def make_forget_proposal(repository: Repository, memory_id: str) -> Proposal:
    document = repository.get(memory_id)
    proposal_id = f"sim-forget-{memory_id}"
    output: dict[str, JsonValue] = {"action": "hard"}
    payload_hash = canonical_payload_hash(output)
    timestamp = repository._now()
    input_hash = canonical_payload_hash([memory_id, document.model_dump(mode="json")])
    proposal = Proposal(
        proposal_id=proposal_id,
        kind="forget",
        target_ids=(memory_id,),
        target_hashes={memory_id: canonical_payload_hash(document.model_dump(mode="json"))},
        agent_name="test-agent",
        model_name="test-model",
        input_hash=input_hash,
        output=output,
        reason="Test simulated deletion path",
        payload_hash=payload_hash,
        actor="agent:test-agent",
        signature=mock_signature(proposal_id, input_hash, payload_hash, timestamp),
        created_at=timestamp,
        updated_at=timestamp,
    )
    repository.record_proposal(proposal)
    return proposal


def test_knowledge_source_delete_requires_human_proposal_approval(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("Source", "evidence", memory_id="source")
    repository.create_knowledge_card("card", "insight", "Derived statement", ["source"])

    with pytest.raises(IntegrityError, match="human-approved forget proposal"):
        repository.delete("source")
    assert repository.recover_pending() == []
    assert repository.get("source").body == "evidence"

    proposal = repository.propose_source_deletion("source")
    assert proposal.actor == "agent:source-protection"
    with pytest.raises(IntegrityError, match="knowledge-card source"):
        repository.simulate_proposal_decision(proposal.proposal_id)
    repository.decide_proposal(proposal.proposal_id, payload_hash=proposal.payload_hash)
    apply_approved(repository, proposal.proposal_id)

    assert repository.get_knowledge_card("card").missing_source_refs == ("source",)
    assert repository.knowledge_source_counts() == {}


def test_simulated_approval_is_separate_and_cannot_delete_anchor(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("Ordinary", "body", memory_id="ordinary")
    proposal = make_forget_proposal(repository, "ordinary")
    repository.simulate_proposal_decision(proposal.proposal_id)
    applied = apply_approved(
        repository,
        proposal.proposal_id,
        actor="simulated_approver",
        enforce_fallback=False,
    )
    assert applied is None
    events = repository.events()
    assert any(
        event.kind == EventKind.SIMULATED_DECISION
        and event.actor == "simulated_approver"
        and event.details.get("decision") == "applied"
        for event in events
    )
    assert not any(
        event.kind == EventKind.HUMAN_DECISION
        and event.details.get("proposal_id") == proposal.proposal_id
        for event in events
    )

    repository.add("Anchor", "protected", memory_id="anchor")
    repository.update("anchor", actor="human", layer="anchor", anchor="pinned")
    anchored = make_forget_proposal(repository, "anchor")
    with pytest.raises(IntegrityError, match="anchored targets"):
        repository.simulate_proposal_decision(anchored.proposal_id)


def test_simulated_approval_applies_ordinary_merge_with_simulated_audit_actor(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    representative = repository.add("Representative", "primary body", memory_id="rep")
    duplicate = repository.add("Duplicate", "secondary body", memory_id="dup")
    proposal_id = "sim-merge-ordinary"
    output: dict[str, JsonValue] = {
        "representative_id": "rep",
        "duplicate_ids": ["dup"],
        "match_basis": "test fixture pair",
        "disposition": "delete",
        "merged_body": "primary body with reviewed duplicate provenance",
    }
    payload_hash = canonical_payload_hash(output)
    timestamp = repository._now()
    input_hash = canonical_payload_hash(["rep", "dup"])
    proposal = Proposal(
        proposal_id=proposal_id,
        kind="merge",
        target_ids=("rep", "dup"),
        target_hashes={
            "rep": canonical_payload_hash(representative.model_dump(mode="json")),
            "dup": canonical_payload_hash(duplicate.model_dump(mode="json")),
        },
        agent_name="test-agent",
        model_name="test-model",
        input_hash=input_hash,
        output=output,
        reason="Regression for explicitly simulated ordinary-source merge",
        payload_hash=payload_hash,
        actor="agent:test-agent",
        signature=mock_signature(proposal_id, input_hash, payload_hash, timestamp),
        created_at=timestamp,
        updated_at=timestamp,
    )
    repository.record_proposal(proposal)

    repository.simulate_proposal_decision(proposal_id)
    merged = apply_approved(
        repository,
        proposal_id,
        actor="simulated_approver",
        enforce_fallback=False,
    )

    assert merged.id == "rep"
    assert merged.body == "primary body with reviewed duplicate provenance"
    provenance = json.loads(merged.provenance)
    assert any(item.startswith("memory:rep@") for item in provenance)
    assert any(item.startswith("memory:dup@") for item in provenance)
    assert not (repository.memories_dir / "dup.md").exists()

    events = repository.events()
    proposal_events = [
        event
        for event in events
        if event.details.get("proposal_id") == proposal_id
        and event.kind in {EventKind.UPDATE, EventKind.DELETE, EventKind.SIMULATED_DECISION}
    ]
    assert any(
        event.kind == EventKind.SIMULATED_DECISION
        and event.actor == "simulated_approver"
        and event.details.get("decision") == "approve"
        for event in proposal_events
    )
    assert any(
        event.kind == EventKind.UPDATE and event.actor == "simulated_approver"
        for event in proposal_events
    )
    assert any(
        event.kind == EventKind.DELETE and event.actor == "simulated_approver"
        for event in proposal_events
    )
    assert any(
        event.kind == EventKind.SIMULATED_DECISION
        and event.actor == "simulated_approver"
        and event.details.get("decision") == "applied"
        for event in proposal_events
    )
    assert not any(
        event.kind == EventKind.HUMAN_DECISION and event.details.get("proposal_id") == proposal_id
        for event in events
    )


def test_budget_forget_skips_cited_source_and_reuses_pending_proposal(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("Source", "evidence", memory_id="source")
    repository.create_knowledge_card("card", "insight", "Derived statement", ["source"])

    first = run_forget(
        repository,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        action="hard",
        dry_run=False,
        scope="all",
    )
    second = run_forget(
        repository,
        PolicyName.FIFO,
        BudgetLimits(max_count=0),
        action="hard",
        dry_run=False,
        scope="all",
    )

    assert first.satisfied_after_apply is False
    assert first.unsatisfied_reason == "approval_required"
    assert len(first.pending_proposal_ids) == 1
    assert second.pending_proposal_ids == first.pending_proposal_ids
    assert repository.get("source").body == "evidence"


def test_max_count_one_never_removes_all_cited_sources_without_approval(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("First source", "first", memory_id="source-1")
    repository.add("Second source", "second", memory_id="source-2")
    repository.create_knowledge_card("card-1", "insight", "First", ["source-1"])
    repository.create_knowledge_card("card-2", "insight", "Second", ["source-2"])

    result = run_forget(
        repository,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        action="hard",
        dry_run=False,
        scope="all",
    )
    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason == "approval_required"
    assert len(repository.list()) == 2
    assert repository.recover_pending() == []
    assert len(repository.list()) == 2

    proposal_id = result.pending_proposal_ids[0]
    proposal = next(
        item
        for item in repository.list_proposals(status="pending")
        if item.proposal_id == proposal_id
    )
    repository.decide_proposal(proposal_id, payload_hash=proposal.payload_hash)
    apply_approved(repository, proposal_id)
    assert len(repository.list()) == 1


def _configure_total_hard_budget(repository: Repository, *, max_count: int) -> None:
    config = repository.read_config()
    config.update(
        budget_mode="total",
        max_bytes=10_000_000,
        max_count=max_count,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="all",
        knowledge_protection_boost=0.0,
    )
    repository.configure(config)


def test_total_budget_skips_cited_oldest_source_and_deletes_ordinary_candidate(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("Old source", "evidence", memory_id="source")
    repository.create_knowledge_card("card", "insight", "Derived statement", ["source"])
    repository.clock = lambda: datetime(2026, 10, 19, tzinfo=UTC)
    repository.add("New ordinary", "disposable", memory_id="ordinary")
    _configure_total_hard_budget(repository, max_count=1)

    result = enforce_budget(repository, now=repository._now())

    assert result.satisfied_after_apply is True
    assert result.applied_ids == ("ordinary",)
    assert result.pending_proposal_ids
    assert repository.get("source").body == "evidence"
    assert repository.get_knowledge_card("card").source_refs == ("source",)
    assert not (repository.memories_dir / "ordinary.md").exists()
    proposal = next(
        proposal
        for proposal in repository.list_proposals(status="pending")
        if proposal.proposal_id in result.pending_proposal_ids
    )
    assert proposal.actor == "agent:source-protection"
    assert repository.recover_pending() == []
    assert repository.fsck() == []
    assert repository.get("source").body == "evidence"


def test_total_budget_waits_for_human_approval_when_all_sources_are_cited(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("First source", "first", memory_id="source-1")
    repository.add("Second source", "second", memory_id="source-2")
    repository.create_knowledge_card("card-1", "insight", "First", ["source-1"])
    repository.create_knowledge_card("card-2", "insight", "Second", ["source-2"])
    _configure_total_hard_budget(repository, max_count=1)

    blocked = enforce_budget(repository, now=repository._now())

    assert blocked.satisfied_after_apply is False
    assert blocked.unsatisfied_reason_code == "approval_required"
    assert len(blocked.pending_proposal_ids) == 2
    assert {document.id for document in repository.list()} == {"source-1", "source-2"}
    assert repository.recover_pending() == []
    assert repository.fsck() == []
    for memory_id in ("source-1", "source-2"):
        assert repository.get(memory_id).body in {"first", "second"}

    proposal_id = blocked.pending_proposal_ids[0]
    proposal = next(
        item
        for item in repository.list_proposals(status="pending")
        if item.proposal_id == proposal_id
    )
    deleted_source = proposal.target_ids[0]
    deleted_card = "card-1" if deleted_source == "source-1" else "card-2"
    remaining_source = "source-2" if deleted_source == "source-1" else "source-1"
    remaining_card = "card-2" if deleted_source == "source-1" else "card-1"
    repository.decide_proposal(proposal_id, payload_hash=proposal.payload_hash)
    apply_approved(repository, proposal_id)
    assert len(repository.list()) == 1
    assert repository.get_knowledge_card(deleted_card).status == "missing_sources"
    assert repository.get_knowledge_card(deleted_card).missing_source_refs == (deleted_source,)
    assert repository.get(remaining_source).body in {"first", "second"}
    assert repository.get_knowledge_card(remaining_card).status == "complete"

    resolved = enforce_budget(repository, now=repository._now())
    assert resolved.satisfied_after_apply is True
    assert len(repository.list()) == 1
    with pytest.raises(NotFoundError):
        repository.get(deleted_source)


def test_multi_target_forget_with_cited_source_fails_before_any_deletion(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    ordinary = repository.add("Ordinary", "ordinary", memory_id="ordinary")
    source = repository.add("Source", "evidence", memory_id="source")
    repository.create_knowledge_card("card", "insight", "Derived statement", ["source"])
    proposal_id = "multi-forget-with-source"
    output: dict[str, JsonValue] = {"action": "hard"}
    payload_hash = canonical_payload_hash(output)
    timestamp = repository._now()
    input_hash = canonical_payload_hash([ordinary.id, source.id])
    proposal = Proposal(
        proposal_id=proposal_id,
        kind="forget",
        target_ids=(ordinary.id, source.id),
        target_hashes={
            ordinary.id: canonical_payload_hash(ordinary.model_dump(mode="json")),
            source.id: canonical_payload_hash(source.model_dump(mode="json")),
        },
        agent_name="test-agent",
        model_name="test-model",
        input_hash=input_hash,
        output=output,
        reason="Regression for protected multi-target deletion ordering",
        payload_hash=payload_hash,
        actor="agent:test-agent",
        signature=mock_signature(proposal_id, input_hash, payload_hash, timestamp),
        created_at=timestamp,
        updated_at=timestamp,
    )
    repository.record_proposal(proposal)
    repository.decide_proposal(proposal_id, payload_hash=payload_hash)

    with pytest.raises(IntegrityError, match="Multi-target forget proposals"):
        apply_approved(repository, proposal_id)

    assert repository.get("ordinary").body == "ordinary"
    assert repository.get("source").body == "evidence"
    assert not any(
        event.kind == EventKind.DELETE and event.memory_id in {"ordinary", "source"}
        for event in repository.events()
    )


def test_budget_forget_can_remove_ordinary_memory_around_cited_source(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    repository.add("Ordinary", "ordinary", memory_id="ordinary")
    repository.add("Source", "evidence", memory_id="source")
    repository.create_knowledge_card("card", "insight", "Derived statement", ["source"])

    result = run_forget(
        repository,
        PolicyName.FIFO,
        BudgetLimits(max_count=1),
        action="hard",
        dry_run=False,
        scope="all",
    )

    assert result.satisfied_after_apply is True
    assert repository.get("source").body == "evidence"
    with pytest.raises(NotFoundError):
        repository.get("ordinary")


def test_partial_append_recovery_refuses_a_tampered_middle_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.init()
    append_access(repository)
    append_access(repository)

    def partial_write(event: object, line: bytes, previous: str, event_count: int) -> None:
        cursor = repository._validated_event_cursor()
        intent = {
            "schema_version": 1,
            "offset": cursor["offset"],
            "previous_hash": previous,
            "event_hash": event.event_hash,  # type: ignore[attr-defined]
            "line": line.decode("utf-8"),
            "event_count": event_count,
        }
        repository._atomic_write(
            repository.root / ".event-append.pending.json",
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        with repository.events_path.open("ab") as stream:
            stream.write(line[: max(1, len(line) // 2)])
            stream.flush()
            os.fsync(stream.fileno())
        raise OSError("injected partial append")

    monkeypatch.setattr(repository, "_append_event_with_intent", partial_write)
    with pytest.raises(OSError, match="partial append"):
        append_access(repository)
    monkeypatch.undo()
    rows = repository.events_path.read_bytes().splitlines(keepends=True)
    rows[0] = rows[0].replace(b'"actor":"human"', b'"actor":"changed"')
    repository._atomic_write(repository.events_path, b"".join(rows))

    with pytest.raises(IntegrityError):
        repository.recover_pending()
    assert (repository.root / ".event-append.pending.json").exists()
