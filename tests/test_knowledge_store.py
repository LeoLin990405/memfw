from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.curation.review import apply_approved
from memfw.knowledge import KnowledgeKind


def repo_at(path: Path) -> Repository:
    sequence = iter(f"event-{index}" for index in range(100))
    return Repository(
        path,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def test_persist_and_reload_all_knowledge_card_kinds(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Evidence", "source text", memory_id="evidence")
    for index, kind in enumerate(KnowledgeKind):
        card_id = f"card-{index}"
        created = repo.create_knowledge_card(
            card_id, kind, f"Statement for {kind.value}", ["evidence"]
        )
        loaded = repo.get_knowledge_card(card_id)
        assert loaded == created
        assert loaded.status == "complete"
        path = repo.root / "knowledge" / "cards" / f"{card_id}.md"
        raw = path.read_text(encoding="utf-8")
        metadata = yaml.safe_load(raw.split("---\n", 2)[1])
        assert metadata["kind"] == kind.value
        assert metadata["source_refs"] == ["evidence"]
        assert any(
            event.kind == EventKind.KNOWLEDGE_CARD_CREATE and event.memory_id == card_id
            for event in repo.events()
        )


def test_card_rejects_nonexistent_sources_without_writing(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    with pytest.raises(ValueError, match="source does not exist"):
        repo.create_knowledge_card("broken", "insight", "No source", ["missing"])
    assert not (repo.root / "knowledge" / "cards" / "broken.md").exists()


def test_source_deletion_marks_card_missing_and_logs_event(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Evidence", "source text", memory_id="evidence")
    repo.create_knowledge_card("card", "methodology", "How to test", ["evidence"])
    proposal = repo.propose_source_deletion("evidence")
    repo.decide_proposal(proposal.proposal_id, payload_hash=proposal.payload_hash)
    apply_approved(repo, proposal.proposal_id)

    card = repo.get_knowledge_card("card")
    assert card.status == "missing_sources"
    assert card.missing_source_refs == ("evidence",)
    assert repo.knowledge_source_counts() == {}
    assert any(
        event.kind == EventKind.KNOWLEDGE_SOURCE_MISSING and event.memory_id == "card"
        for event in repo.events()
    )
    with pytest.raises(NotFoundError):
        repo.get("evidence")


def test_card_read_detects_external_edits(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Evidence", "source text", memory_id="evidence")
    repo.create_knowledge_card("card", "pattern", "Known statement", ["evidence"])
    path = repo.root / "knowledge" / "cards" / "card.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Known statement", "Changed"))
    with pytest.raises(IntegrityError, match="hash mismatch"):
        repo.get_knowledge_card("card")


def test_knowledge_refs_protect_and_repoint_before_merge_delete(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Old source", "old content", memory_id="old-source")
    repo.add("Survivor", "kept content", memory_id="survivor")
    repo.create_knowledge_card(
        "card", "insight", "Combined evidence remains available", ["old-source", "survivor"]
    )

    assert repo.knowledge_source_counts() == {"old-source": 1, "survivor": 1}
    assert repo.repoint_knowledge_sources("old-source", "survivor") == 1
    repo.delete("old-source")

    card = repo.get_knowledge_card("card")
    assert card.source_refs == ("survivor",)
    assert card.missing_source_refs == ()
    assert card.source_repoint_history == (("old-source", "survivor"),)
    assert repo.knowledge_source_counts() == {"survivor": 1}
    assert repo.repoint_knowledge_sources("survivor", "survivor") == 0
    assert any(
        event.kind == EventKind.KNOWLEDGE_SOURCE_REPOINT and event.memory_id == "card"
        for event in repo.events()
    )


def test_agent_cannot_repoint_sources_without_approval(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Old source", "old content", memory_id="old-source")
    repo.add("Survivor", "kept content", memory_id="survivor")
    repo.create_knowledge_card("card", "insight", "Keep evidence", ["old-source"])
    with pytest.raises(IntegrityError, match="not authorized"):
        repo.repoint_knowledge_sources("old-source", "survivor", actor="agent:helper")


def test_legacy_card_without_repoint_history_gets_correct_before_hash(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "vault")
    repo.init()
    repo.add("Evidence", "source text", memory_id="evidence")
    metadata = {
        "id": "legacy-card",
        "kind": "insight",
        "source_refs": ["evidence"],
        "missing_source_refs": [],
        "status": "complete",
    }
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    raw = f"---\n{frontmatter}\n---\n\nOld card format.\n".encode()
    path = repo.root / "knowledge" / "cards" / "legacy-card.md"
    repo._atomic_write(path, raw)
    digest = hashlib.sha256(raw).hexdigest()
    repo._append_event(
        EventKind.KNOWLEDGE_CARD_CREATE,
        "legacy-card",
        actor="human",
        after_hash=digest,
        details={"kind": "insight", "source_refs": ["evidence"]},
    )

    proposal = repo.propose_source_deletion("evidence")
    repo.decide_proposal(proposal.proposal_id, payload_hash=proposal.payload_hash)
    apply_approved(repo, proposal.proposal_id)
    missing_event = next(
        event
        for event in reversed(repo.events())
        if event.kind == EventKind.KNOWLEDGE_SOURCE_MISSING and event.memory_id == "legacy-card"
    )
    assert missing_event.before_hash == digest
