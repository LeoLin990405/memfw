from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.agents.models import (
    JsonValue,
    Proposal,
    ProposalKind,
    canonical_payload_hash,
    mock_signature,
)
from memfw.capture import CaptureSession, TimedSignal
from memfw.capture.confirm import confirm_candidates
from memfw.cli import app as main_app
from memfw.core.errors import IntegrityError
from memfw.core.models import CaptureState, EventKind, Layer
from memfw.core.repository import Repository
from memfw.curation.review import (
    apply_approved,
    approve_batch,
    decide_proposal,
    list_pending,
)

NOW = "2026-10-18T00:00:00+00:00"


def make_repo(root: Path) -> Repository:
    from datetime import datetime

    repo = Repository(root, clock=lambda: datetime.fromisoformat(NOW))
    repo.init()
    return repo


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def stage_candidate(repo: Repository, *, source_id: str) -> str:
    session = CaptureSession(
        repository=repo,
        session_id="review-test",
        initial_state=CaptureState.DORMANT,
    )
    session.feed(
        TimedSignal(
            timestamp=repo._now(),
            source="human",
            action="remember",
            text="[research] Keep this candidate.\n保留这条候选。",
            source_id=source_id,
        )
    )
    return session.items()[0].candidate_id


def test_capture_confirmation_keep_creates_formal_memory_after_staging(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "capture-keep")
    candidate_id = stage_candidate(repo, source_id="captured-keep")
    assert repo.list() == []

    result = confirm_candidates(repo, [f"{candidate_id}=keep"])

    document = repo.get("captured-keep")
    assert document.body == "[research] Keep this candidate.\n保留这条候选。"
    assert "source_sha256:" in document.provenance
    assert result["formal_memory_ids_created"] == ["captured-keep"]
    stage_path = repo.root / ".staging" / "capture-candidates.jsonl"
    assert not stage_path.read_text(encoding="utf-8").strip()
    assert any(event.kind == EventKind.HUMAN_DECISION for event in repo.events())


def test_capture_anchor_confirmation_creates_human_anchor_hot(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "capture-anchor")
    candidate_id = stage_candidate(repo, source_id="captured-anchor")

    confirm_candidates(repo, [f"{candidate_id}=anchor"])

    document = repo.get("captured-anchor")
    assert document.layer == Layer.ANCHOR
    assert document.anchor == f"capture:{candidate_id}"
    assert document.tier.value == "hot"


def test_capture_discard_removes_candidate_body_without_creating_memory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "capture-discard")
    candidate_id = stage_candidate(repo, source_id="captured-discard")
    staged_file = repo.root / ".staging" / "capture-candidates.jsonl"
    from memfw.capture import _read_stage_records

    staged_records = _read_stage_records(staged_file)
    staged_bytes = next(
        raw for candidate, raw in staged_records if candidate.candidate_id == candidate_id
    )
    assert "保留这条候选" in staged_bytes.decode("utf-8")

    result = confirm_candidates(repo, [f"{candidate_id}=discard"])

    assert repo.list() == []
    assert not staged_file.read_text(encoding="utf-8").strip()
    assert result["formal_memory_ids_created"] == []
    event = next(event for event in repo.events() if event.kind == EventKind.HUMAN_DECISION)
    assert event.details["source_hash"] == hashlib.sha256(staged_bytes).hexdigest()
    assert "保留这条候选" not in repo.events_path.read_text(encoding="utf-8")


def test_capture_dry_run_keeps_hashes_and_rejects_unknown_decision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "capture-dry")
    candidate_id = stage_candidate(repo, source_id="captured-dry")
    before = tree_hashes(repo.root)

    result = confirm_candidates(repo, [f"{candidate_id}=keep"], dry_run=True)

    assert result["formal_memory_ids_created"] == []
    assert tree_hashes(repo.root) == before
    with pytest.raises(IntegrityError):
        confirm_candidates(repo, [f"{candidate_id}=no"])


def proposal(
    repo: Repository,
    *,
    proposal_id: str,
    kind: ProposalKind,
    target_ids: tuple[str, ...],
    output: dict[str, JsonValue],
) -> Proposal:
    timestamp = repo._now()
    payload_hash = canonical_payload_hash(output)
    input_hash = hashlib.sha256(b"day3-review-test").hexdigest()
    return Proposal(
        proposal_id=proposal_id,
        kind=kind,
        target_ids=target_ids,
        agent_name="test-agent",
        model_name="test-model",
        input_hash=input_hash,
        output=output,
        reason="test proposal",
        payload_hash=payload_hash,
        actor="agent:test-agent",
        signature=mock_signature(proposal_id, input_hash, payload_hash, timestamp),
        created_at=timestamp,
        updated_at=timestamp,
    )


def add_document(repo: Repository, memory_id: str, body: str, *, anchor: bool = False) -> None:
    repo.add(
        memory_id,
        body,
        memory_id=memory_id,
        layer=Layer.ANCHOR if anchor else Layer.CURRENT,
        anchor=f"anchor:{memory_id}" if anchor else None,
        actor="human",
    )


def approve(repo: Repository, value: Proposal) -> None:
    repo.record_proposal(value)
    decide_proposal(repo, value.proposal_id, "approve", payload_hash=value.payload_hash)


def test_unapproved_proposal_does_not_change_memory_and_approval_is_one_time(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "review-unapproved")
    add_document(repo, "layer-target", "before")
    item = proposal(
        repo,
        proposal_id="review-layer-01",
        kind="layer",
        target_ids=("layer-target",),
        output={"layer": "consolidated"},
    )
    repo.record_proposal(item)

    assert list_pending(repo)[0]["reason"] == "test proposal"
    assert repo.get("layer-target").layer == Layer.CURRENT
    with pytest.raises(IntegrityError):
        apply_approved(repo, item.proposal_id)
    decide_proposal(repo, item.proposal_id, "approve", payload_hash=item.payload_hash)
    apply_approved(repo, item.proposal_id)
    assert repo.get("layer-target").layer == Layer.CONSOLIDATED
    with pytest.raises(IntegrityError):
        decide_proposal(repo, item.proposal_id, "approve", payload_hash=item.payload_hash)
    with pytest.raises(IntegrityError):
        apply_approved(repo, item.proposal_id)


def test_edit_creates_new_human_signed_proposal_and_rejects_original(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-edit")
    add_document(repo, "edit-target", "before")
    item = proposal(
        repo,
        proposal_id="review-edit-original",
        kind="salience",
        target_ids=("edit-target",),
        output={"score": 0.4},
    )
    repo.record_proposal(item)

    edited = decide_proposal(
        repo,
        item.proposal_id,
        "edit",
        payload_hash=item.payload_hash,
        edited_output={"score": 0.9},
    )

    assert edited.proposal_id != item.proposal_id
    assert edited.actor == "human"
    assert edited.agent_name == "human-review"
    assert edited.status == "pending"
    assert repo.list_proposals(status="rejected")[0].proposal_id == item.proposal_id
    assert repo.list_proposals(status="pending")[0].proposal_id == edited.proposal_id
    assert repo.get("edit-target").strength == 0.5


def test_stale_target_is_rejected_before_approval_and_between_approval_apply(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "review-stale")
    add_document(repo, "stale-before", "original")
    before_approval = proposal(
        repo,
        proposal_id="review-stale-before",
        kind="salience",
        target_ids=("stale-before",),
        output={"score": 0.8},
    )
    repo.record_proposal(before_approval)
    repo.update("stale-before", actor="human", body="changed before approval")
    with pytest.raises(IntegrityError, match="stale|changed|snapshot|target"):
        decide_proposal(
            repo,
            before_approval.proposal_id,
            "approve",
            payload_hash=before_approval.payload_hash,
        )

    add_document(repo, "stale-after", "original")
    after_approval = proposal(
        repo,
        proposal_id="review-stale-after",
        kind="salience",
        target_ids=("stale-after",),
        output={"score": 0.8},
    )
    approve(repo, after_approval)
    repo.update("stale-after", actor="human", body="changed after approval")
    with pytest.raises(IntegrityError, match="stale|changed|snapshot|target"):
        apply_approved(repo, after_approval.proposal_id)
    assert repo.get("stale-after").body == "changed after approval"


def test_rejected_proposal_cannot_be_applied_and_repeated_decision_fails(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "review-reject")
    add_document(repo, "reject-target", "safe")
    item = proposal(
        repo,
        proposal_id="review-reject-01",
        kind="salience",
        target_ids=("reject-target",),
        output={"score": 0.1},
    )
    repo.record_proposal(item)
    decide_proposal(repo, item.proposal_id, "reject", payload_hash=item.payload_hash)
    with pytest.raises(IntegrityError):
        apply_approved(repo, item.proposal_id)
    with pytest.raises(IntegrityError):
        decide_proposal(repo, item.proposal_id, "approve", payload_hash=item.payload_hash)
    assert repo.get("reject-target").strength == 0.5


def test_payload_tamper_is_detected_before_decision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-tamper")
    add_document(repo, "tamper-target", "safe body")
    item = proposal(
        repo,
        proposal_id="review-tamper-01",
        kind="salience",
        target_ids=("tamper-target",),
        output={"score": 0.9},
    )
    repo.record_proposal(item)
    path = repo.proposals_dir / f"{item.proposal_id}.md"
    path.write_text(path.read_text().replace('"score":0.9', '"score":0.1'), encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        decide_proposal(repo, item.proposal_id, "approve", payload_hash=item.payload_hash)
    assert repo.get("tamper-target").strength == 0.5


def test_merge_requires_approval_and_retains_all_source_references(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-merge")
    add_document(repo, "merge-rep", "representative body")
    add_document(repo, "merge-dup", "duplicate body")
    repo.create_knowledge_card(
        "merge-knowledge",
        "insight",
        "The two records describe one event.",
        ["merge-dup", "merge-rep"],
    )
    value = proposal(
        repo,
        proposal_id="review-merge-01",
        kind="merge",
        target_ids=("merge-rep", "merge-dup"),
        output={
            "representative_id": "merge-rep",
            "duplicate_ids": ["merge-dup"],
            "match_basis": "synthetic exact pair",
            "disposition": "delete",
            "merged_body": "representative body",
        },
    )
    repo.record_proposal(value)
    with pytest.raises(IntegrityError):
        apply_approved(repo, value.proposal_id)
    decide_proposal(repo, value.proposal_id, "approve", payload_hash=value.payload_hash)

    merged = apply_approved(repo, value.proposal_id)

    assert merged.id == "merge-rep"
    assert not (repo.memories_dir / "merge-dup.md").exists()
    refs = json.loads(merged.provenance)
    assert any(item.startswith("memory:merge-rep@") for item in refs)
    assert any(item.startswith("memory:merge-dup@") for item in refs)
    assert "duplicate body" not in repo.events_path.read_text(encoding="utf-8")
    card = repo.get_knowledge_card("merge-knowledge")
    assert card.source_refs == ("merge-rep",)
    assert card.source_repoint_history == (("merge-dup", "merge-rep"),)
    assert card.status == "complete"
    assert any(
        event.kind == EventKind.HUMAN_DECISION
        and event.details.get("proposal_id") == value.proposal_id
        and event.details.get("decision") == "applied"
        for event in repo.events()
    )


def test_agent_cannot_apply_after_human_approval_and_anchor_is_protected(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "review-permissions")
    add_document(repo, "protected-anchor", "anchored text", anchor=True)
    value = proposal(
        repo,
        proposal_id="review-forget-anchor",
        kind="forget",
        target_ids=("protected-anchor",),
        output={"action": "hard"},
    )
    approve(repo, value)
    with pytest.raises(IntegrityError):
        apply_approved(repo, value.proposal_id, actor="agent:test-agent")
    with pytest.raises(IntegrityError):
        apply_approved(repo, value.proposal_id)
    assert repo.get("protected-anchor").anchor == "anchor:protected-anchor"


def test_layer_only_anchor_cannot_be_deleted_by_forget_proposal(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-layer-anchor")
    repo.add(
        "layer only anchor",
        "protected text",
        memory_id="layer-only-anchor",
        layer=Layer.ANCHOR,
        actor="human",
    )
    item = proposal(
        repo,
        proposal_id="review-layer-only-anchor",
        kind="forget",
        target_ids=("layer-only-anchor",),
        output={"action": "hard"},
    )
    approve(repo, item)

    with pytest.raises(IntegrityError, match="anchored"):
        apply_approved(repo, item.proposal_id)
    assert repo.get("layer-only-anchor").layer == Layer.ANCHOR


def test_batch_filter_dry_run_does_not_write(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-batch")
    add_document(repo, "batch-target", "candidate")
    add_document(repo, "batch-target-2", "possible duplicate")
    merge = proposal(
        repo,
        proposal_id="review-batch-merge",
        kind="merge",
        target_ids=("batch-target", "batch-target-2"),
        output={"representative_id": "batch-target", "duplicate_ids": ["batch-target-2"]},
    )
    repo.record_proposal(merge)
    before = tree_hashes(repo.root)

    result = approve_batch(repo, filter_type="type=merge", dry_run=True)

    assert [item["proposal_id"] for item in result["would_approve"]] == [merge.proposal_id]
    assert tree_hashes(repo.root) == before


def test_mounted_review_root_lists_and_batch_dry_run_is_read_only(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "review-mounted")
    add_document(repo, "mounted-target", "candidate")
    item = proposal(
        repo,
        proposal_id="review-mounted-01",
        kind="salience",
        target_ids=("mounted-target",),
        output={"score": 0.75},
    )
    repo.record_proposal(item)
    runner = CliRunner()
    listed = runner.invoke(main_app, ["--now", NOW, "review", "--root", str(repo.root)])
    assert listed.exit_code == 0, listed.output
    assert item.proposal_id in listed.output

    before = tree_hashes(repo.root)
    preview = runner.invoke(
        main_app,
        [
            "--now",
            NOW,
            "review",
            "--root",
            str(repo.root),
            "--approve-all",
            "--filter",
            "type=salience",
            "--dry-run",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert item.proposal_id in preview.output
    assert tree_hashes(repo.root) == before


def test_mounted_review_apply_dry_run_is_read_only_and_apply_returns_json(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "review-mounted-apply")
    add_document(repo, "mounted-apply-target", "before")
    item = proposal(
        repo,
        proposal_id="review-mounted-apply-01",
        kind="salience",
        target_ids=("mounted-apply-target",),
        output={"score": 0.9},
    )
    approve(repo, item)
    runner = CliRunner()
    before = tree_hashes(repo.root)

    preview = runner.invoke(
        main_app,
        [
            "--now",
            NOW,
            "review",
            "--root",
            str(repo.root),
            "--apply",
            "--proposal-id",
            item.proposal_id,
            "--dry-run",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["would_apply"] is True
    assert tree_hashes(repo.root) == before

    applied = runner.invoke(
        main_app,
        [
            "--now",
            NOW,
            "review",
            "--root",
            str(repo.root),
            "--apply",
            "--proposal-id",
            item.proposal_id,
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output) == {
        "applied": True,
        "memory_id": "mounted-apply-target",
        "proposal_id": item.proposal_id,
    }
    assert repo.get("mounted-apply-target").strength == 0.9
