from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import Event, EventKind, Tier
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.retrieval import VaultRetriever
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import tokenize


def fixed_repo(root: Path) -> Repository:
    sequence = iter(f"day7-event-{number}" for number in range(1000))
    return Repository(
        root,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def test_bm25_uses_shared_tokens_and_stable_ranking() -> None:
    documents = {
        "a": "speaker user rareterm common common 中文记忆",
        "b": "speaker assistant common 中文记忆",
        "c": "speaker user unrelated",
    }
    scorer = BM25Scorer(documents)
    assert tokenize("rareterm 中文记忆")
    assert scorer.rank("rareterm common") == ["a", "b"]
    assert scorer.rank("common", candidate_ids={"b", "a"}) == ["a", "b"]
    assert scorer.score("rareterm", "a") > scorer.score("rareterm", "b")
    subset = scorer.for_candidates({"a", "b"})
    assert subset.rank("common") == BM25Scorer({key: documents[key] for key in ("a", "b")}).rank(
        "common"
    )


def test_cold_stub_index_searches_verified_archived_body(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "cold-index")
    repository.init()
    body = ("neutral prefix " * 24) + " archivedbodyneedle"
    repository.add("cold source", body, memory_id="cold-source", tier=Tier.COLD)
    repository.cold_archive("archive-day7")

    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    assert repository.get("cold-source").is_stub
    assert index.search("archivedbodyneedle") == [("cold-source", "cold source")]


def test_cold_stub_index_fails_closed_on_missing_archive(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "cold-index-corrupt")
    repository.init()
    repository.add("cold source", "archived body needle", memory_id="cold-source", tier=Tier.COLD)
    archive_path = repository.cold_archive("archive-corrupt")
    archive_path.unlink()
    with pytest.raises(IntegrityError, match="Unable to read cold archive|Cold archive"):
        MemoryIndex(repository.index_path).rebuild(repository)


def test_retriever_rebuilds_and_autowarms_cold_body_hits(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "cold-retrieval")
    repository.init()
    body = ("neutral prefix " * 24) + " archivedbodyneedle"
    repository.add("cold source", body, memory_id="cold-source", tier=Tier.COLD)
    repository.cold_archive("archive-retrieval")

    assert VaultRetriever(repository).retrieve("archivedbodyneedle") == ["cold-source"]
    assert not repository.get("cold-source").is_stub
    assert any(
        event.kind == EventKind.MEMORY_ACCESS and event.memory_id == "cold-source"
        for event in repository.events()
    )


def test_source_deletion_proposal_survives_cooling_but_rejects_content_edit(
    tmp_path: Path,
) -> None:
    repository = fixed_repo(tmp_path / "source-cooling")
    repository.init()
    repository.add("source", "cited source body", memory_id="source-note")
    repository.create_knowledge_card(
        "card-one", kind="insight", statement="A derived fact", source_refs=["source-note"]
    )
    proposal = repository.propose_source_deletion("source-note")
    repository.decide_proposal(
        proposal.proposal_id, decision="approve", payload_hash=proposal.payload_hash
    )

    repository.update("source-note", tier=Tier.COLD, actor="human")
    repository.cold_archive("archive-approved-source")
    repository.assert_proposal_current(proposal.proposal_id)

    repository.restore_cold("source-note")
    repository.update("source-note", body="edited source body", actor="human")
    with pytest.raises(IntegrityError, match="Proposal is stale"):
        repository.assert_proposal_current(proposal.proposal_id)


def test_human_approved_source_delete_applies_after_cooling(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "source-delete-after-cooling")
    repository.init()
    repository.add("source", "cited source body", memory_id="source-note")
    repository.create_knowledge_card(
        "card-one", kind="insight", statement="A derived fact", source_refs=["source-note"]
    )
    proposal = repository.propose_source_deletion("source-note")
    repository.decide_proposal(
        proposal.proposal_id, decision="approve", payload_hash=proposal.payload_hash
    )
    repository.update("source-note", tier=Tier.COLD, actor="human")
    repository.cold_archive("archive-approved-source")

    repository.delete("source-note", actor="human", proposal_id=proposal.proposal_id)
    with pytest.raises(NotFoundError):
        repository.get("source-note")
    card = repository.get_knowledge_card("card-one")
    assert card.status == "missing_sources"


def test_legacy_raw_hash_source_proposal_blocks_cooling_without_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = fixed_repo(tmp_path / "legacy-source-proposal")
    repository.init()
    repository.add("source", "cited source body", memory_id="source-note")
    repository.create_knowledge_card(
        "card-one", kind="insight", statement="A derived fact", source_refs=["source-note"]
    )
    proposal = repository.propose_source_deletion("source-note")
    raw_snapshot = repository._proposal_targets_hash(
        proposal.target_ids, raw_source_protection=True
    )
    original_events = repository.events

    def legacy_events() -> list[Event]:
        events = original_events()
        for index, event in enumerate(events):
            if (
                event.kind == EventKind.AGENT_PROPOSAL
                and event.details.get("proposal_id") == proposal.proposal_id
            ):
                details = dict(event.details)
                details.pop("target_snapshot_hash_mode", None)
                details["target_snapshot_hash"] = raw_snapshot
                events[index] = event.model_copy(update={"details": details})
        return events

    monkeypatch.setattr(repository, "events", legacy_events)
    with pytest.raises(IntegrityError, match="active legacy raw-hash"):
        repository.update("source-note", tier=Tier.COLD, actor="human")
    repository.assert_proposal_current(proposal.proposal_id)


def test_tail_fit_label_names_actual_estimator() -> None:
    from memfw.stats.tail_fit import fit_discrete_tail

    fit = fit_discrete_tail([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], minimum_sample=4)
    assert fit.estimator == "golden_section_discrete_zeta_mle"


def test_legacy_head_and_cursor_migrate_to_combined_state(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "legacy-cursor")
    repository.init()
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access"},
    )
    state_path = repository.event_state_path
    state = json.loads(state_path.read_text(encoding="utf-8"))
    legacy_cursor = {**state, "schema_version": 1}
    state_path.unlink()
    (repository.root / "events.head").write_text(state["last_hash"] + "\n", encoding="ascii")
    (repository.root / "events.cursor.json").write_text(json.dumps(legacy_cursor), encoding="utf-8")

    reopened = fixed_repo(repository.root)
    reopened.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access"},
    )
    migrated = json.loads(reopened.event_state_path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 2
    assert migrated["event_count"] == 2
    assert not (repository.root / "events.head").exists()
    assert not (repository.root / "events.cursor.json").exists()
    assert len(reopened.events()) == 2


def test_combined_event_state_tampering_is_detected(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "state-tamper")
    repository.init()
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access"},
    )
    state = json.loads(repository.event_state_path.read_text(encoding="utf-8"))
    state["last_hash"] = "0" * 64
    repository.event_state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(IntegrityError, match="head|cursor|state"):
        repository.events()
