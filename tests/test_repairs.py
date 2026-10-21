from __future__ import annotations

import concurrent.futures
import hashlib
import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard

from memfw.agents.models import JsonValue, Proposal, canonical_payload_hash, mock_signature
from memfw.core.errors import DeletedContentUnavailable, IntegrityError
from memfw.core.models import Event, EventKind, Layer, Tier
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.index import MemoryIndex
from memfw.policies import PolicyName
from memfw.stats import measure_physical_disk_bytes, measure_runtime


def make_repo(path: Path) -> Repository:
    sequence = iter(f"event-{number}" for number in range(100))
    return Repository(
        path,
        clock=lambda: datetime(2026, 2, 3, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def test_log_detects_last_event_tamper_and_head_truncation(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Title", "body", memory_id="note")
    original = repo.events_path.read_bytes()
    tampered = original.replace(b'"actor":"human"', b'"actor":"agent:x"')
    assert tampered != original
    repo.events_path.write_bytes(tampered)
    with pytest.raises(IntegrityError, match="canonical|hash mismatch"):
        repo.events()

    repo.events_path.write_bytes(original)
    (repo.root / "events.head").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(IntegrityError, match="head"):
        repo.events()


@pytest.mark.parametrize("tamper_index", [0, 1, 2])
def test_log_detects_canonical_tampering_at_any_position(tmp_path: Path, tamper_index: int) -> None:
    repo = make_repo(tmp_path / f"vault-{tamper_index}")
    repo.init()
    first = repo.add("One", "body", memory_id="one")
    repo.update(first.id, title="One updated")
    repo.add("Two", "body", memory_id="two")
    events = repo.events()
    altered = events[tamper_index].model_copy(update={"actor": "mechanism"})
    lines = [event.model_dump_json() for event in events]
    lines[tamper_index] = altered.model_dump_json()
    repo.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        repo.events()


def test_failed_add_leaves_pending_then_explicit_recovery_rolls_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    append = repo._append_event

    def fail_before_log(kind: EventKind, memory_id: str, **kwargs: Any) -> Event:
        raise OSError("simulated failure before event commit")

    monkeypatch.setattr(repo, "_append_event", fail_before_log)
    with pytest.raises(OSError, match="simulated"):
        repo.add("Partial", "body", memory_id="partial")
    assert repo.pending_path.exists()
    with pytest.raises(IntegrityError, match="pending transaction"):
        repo.list()

    monkeypatch.setattr(repo, "_append_event", append)
    repo.recover_pending()
    assert not repo.pending_path.exists()
    assert not (repo.memories_dir / "partial.md").exists()
    assert not (repo.history_dir / "partial").exists()
    assert "partial" not in repo.replay()


def test_config_change_is_hash_only_and_failed_update_recovers_previous_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    original = repo.config_path.read_bytes()
    config = repo.read_config()
    config["max_count"] = 42
    repo.configure(config)
    event = repo.events()[-1]
    assert event.kind == EventKind.CONFIG_CHANGE
    assert event.details["before_config_hash"] == hashlib.sha256(original).hexdigest()
    assert (
        event.details["after_config_hash"]
        == hashlib.sha256(repo.config_path.read_bytes()).hexdigest()
    )
    assert set(event.details) == {"before_config_hash", "after_config_hash"}
    assert "config" not in repo.replay()

    committed = repo.config_path.read_bytes()
    changed = repo.read_config()
    changed["max_count"] = 9
    append = repo._append_event

    def fail_event(kind: EventKind, memory_id: str, **kwargs: Any) -> Event:
        raise OSError("simulated config event failure")

    monkeypatch.setattr(repo, "_append_event", fail_event)
    with pytest.raises(OSError, match="config event"):
        repo.configure(changed)
    assert repo.pending_path.exists()
    monkeypatch.setattr(repo, "_append_event", append)
    repo.recover_pending()
    assert repo.config_path.read_bytes() == committed
    assert repo.read_config()["max_count"] == 42
    assert not repo.pending_path.exists()


def test_invalid_external_markdown_is_quarantined_once_and_replays_as_absent(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Original", "body", memory_id="note")
    current = repo.memories_dir / "note.md"
    current.write_bytes(b"---\nid: note\nid: duplicate\n---\n\ninvalid\n")

    issues = repo.fsck()
    assert issues[0]["kind"] == "invalid_file_quarantined"
    assert repo.replay()["note"] is None
    first_count = len(repo.events())
    assert repo.fsck() == []
    assert len(repo.events()) == first_count
    assert not current.exists()
    quarantined = list((repo.root / ".quarantine").glob("note-*.md"))
    assert len(quarantined) == 1
    assert hashlib.sha256(quarantined[0].read_bytes()).hexdigest() in quarantined[0].name


def test_none_mode_allows_bound_proposal_but_rejects_agent_mutation(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    document = repo.add("Before", "body", memory_id="note")
    config = repo.config_path.read_text(encoding="utf-8").replace(
        "permissions: propose_then_confirm", "permissions: none"
    )
    repo.config_path.write_text(config, encoding="utf-8")

    proposal = repo.record_proposal(
        memory_id="note",
        operation="update",
        changes={"title": "After"},
        rationale="title correction",
        agent_id="writer",
        signature="opaque",
    )
    with pytest.raises(IntegrityError, match="permission"):
        repo.update(
            document.id, actor="agent:writer", agent_id="writer", signature="opaque", title="After"
        )

    # `none` still permits proposals, but a proposal cannot be applied in that mode.
    config = config.replace("permissions: none", "permissions: propose_then_confirm")
    repo.config_path.write_text(config, encoding="utf-8")
    payload = {"memory_id": "note", "operation": "update", "changes": {"title": "After"}}
    changed = repo.decide_proposal(proposal.event_id, payload=payload)
    assert changed is not None
    assert not isinstance(changed, Proposal)
    assert changed.title == "After"
    assert repo.events()[-2].kind == EventKind.HUMAN_DECISION
    assert repo.events()[-1].actor == "agent:writer"


@pytest.mark.parametrize(
    ("agent_writes", "expected"),
    [("none", "none"), ("propose_then_confirm", "propose_then_confirm"), ("full", "full")],
)
def test_agent_writes_string_is_a_permission_mode_when_permissions_absent(
    tmp_path: Path, agent_writes: str, expected: str
) -> None:
    repo = make_repo(tmp_path / agent_writes)
    repo.init()
    config = repo.read_config()
    config.pop("permissions")
    config["agent_writes"] = agent_writes
    repo.configure(config)
    assert repo._permission_mode() == expected


def test_invalid_agent_writes_fails_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "invalid-mode")
    repo.init()
    config = repo.read_config()
    config.pop("permissions")
    config["agent_writes"] = "sometimes"
    repo.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(IntegrityError, match="permissions or budget_level"):
        repo.read_config()


def test_index_search_dry_run_never_warms_accesses_or_rebuilds(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repo = make_repo(tmp_path / "cli-vault")
    repo.init()
    repo.add("Searchable", "needle in body", memory_id="searchable")
    MemoryIndex(repo.index_path).rebuild(repo)
    runner = CliRunner()
    before = {
        path.relative_to(repo.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repo.root.rglob("*"))
        if path.is_file()
    }
    result = runner.invoke(
        app,
        ["index", "search", "needle", "--root", str(repo.root), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert '"writes": false' in result.output
    assert repo.events()[-1].kind == EventKind.CREATE
    after = {
        path.relative_to(repo.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repo.root.rglob("*"))
        if path.is_file()
    }
    assert before == after

    MemoryIndex(repo.index_path).remove("searchable")
    result = runner.invoke(
        app,
        ["index", "search", "needle", "--root", str(repo.root), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert '"would_rebuild": true' in result.output
    assert not repo.index_path.exists()


def test_stale_proposal_cannot_overwrite_newer_human_edit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Before", "body", memory_id="note")
    proposal = repo.record_proposal(
        memory_id="note",
        operation="update",
        changes={"title": "Agent title"},
        rationale="proposed title",
        agent_id="writer",
        signature="opaque",
    )
    repo.update("note", title="Human title")
    with pytest.raises(IntegrityError, match="stale"):
        repo.decide_proposal(
            proposal.event_id,
            payload={
                "memory_id": "note",
                "operation": "update",
                "changes": {"title": "Agent title"},
            },
        )
    assert repo.get("note").title == "Human title"


def test_cold_restore_is_exact_original_bytes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Cold", "line one\nline two", memory_id="cold", tier=Tier.COLD)
    expected = (repo.memories_dir / "cold.md").read_bytes()
    repo.cold_archive("exact")
    assert repo.get("cold").is_stub
    repo.restore_cold("cold")
    assert (repo.memories_dir / "cold.md").read_bytes() == expected
    assert repo.events()[-1].kind == EventKind.MEMORY_RESTORE


def test_failed_restore_event_recovery_preserves_only_archive_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Cold", "the only archived copy", memory_id="cold", tier=Tier.COLD)
    expected = (repo.memories_dir / "cold.md").read_bytes()
    archive = repo.cold_archive("only-copy")
    append = repo._append_event

    def fail_restore_event(kind: EventKind, memory_id: str, **kwargs: Any) -> Event:
        if kind == EventKind.MEMORY_RESTORE:
            raise OSError("simulated restore event failure")
        return append(kind, memory_id, **kwargs)

    monkeypatch.setattr(repo, "_append_event", fail_restore_event)
    with pytest.raises(OSError, match="restore event"):
        repo.restore_cold("cold")
    assert repo.pending_path.exists()

    monkeypatch.setattr(repo, "_append_event", append)
    repo.recover_pending()
    assert repo.get("cold").is_stub
    assert archive.exists()
    repo.restore_cold("cold")
    assert (repo.memories_dir / "cold.md").read_bytes() == expected


def test_restore_rejects_valid_but_tampered_archive_without_overwriting_stub(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Cold", "original bytes", memory_id="cold", tier=Tier.COLD)
    archive_path = repo.cold_archive("tamper-check")
    stub_bytes = (repo.memories_dir / "cold.md").read_bytes()

    compressed = archive_path.read_bytes()
    tar_bytes = zstandard.ZstdDecompressor().decompress(compressed)
    rewritten = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as source:
        with tarfile.open(fileobj=rewritten, mode="w") as destination:
            for member in source.getmembers():
                stream = source.extractfile(member)
                assert stream is not None
                raw = stream.read()
                if member.name == "memories/cold.md":
                    changed = repo.parse(raw.decode("utf-8"), expected_id="cold").model_copy(
                        update={"body": "different but valid Markdown"}
                    )
                    raw = repo.render(changed).encode("utf-8")
                replacement = tarfile.TarInfo(member.name)
                replacement.size = len(raw)
                replacement.mtime = 0
                destination.addfile(replacement, io.BytesIO(raw))
    archive_path.write_bytes(
        zstandard.ZstdCompressor(level=8, threads=0).compress(rewritten.getvalue())
    )

    with pytest.raises(IntegrityError, match="hash mismatch"):
        repo.restore_cold("cold")
    assert (repo.memories_dir / "cold.md").read_bytes() == stub_bytes
    assert repo.get("cold").is_stub


def test_retrieval_warms_anchor_stub_without_losing_human_metadata(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    config = repo.read_config()
    config["anchor_always_hot"] = False
    repo.configure(config)
    repo.add("Anchor note", "original archived body", memory_id="anchor-note", tier=Tier.COLD)
    repo.cold_archive("anchor-metadata")
    repo.update(
        "anchor-note",
        layer=Layer.ANCHOR,
        anchor="human-pinned",
        tags=["human-tag", "needle"],
        strength=0.95,
    )
    MemoryIndex(repo.index_path).rebuild(repo)

    from memfw.retrieval import VaultRetriever

    retriever = VaultRetriever(repo)
    assert retriever.retrieve("needle", limit=3) == ["anchor-note"]
    warmed = repo.get("anchor-note")
    assert warmed.body == "original archived body"
    assert warmed.tier.value == "hot"
    assert warmed.layer.value == "anchor"
    assert warmed.anchor == "human-pinned"
    assert warmed.tags == ["human-tag", "needle"]
    assert warmed.strength == 0.95
    assert not warmed.is_stub


def test_restore_then_rearchive_keeps_one_authoritative_cold_copy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Cold", "body retained once", memory_id="cold", tier=Tier.COLD)
    first = repo.cold_archive("first")
    original = (repo.memories_dir / "cold.md").read_bytes()
    repo.restore_cold("cold")
    assert not first.exists()
    repo.cold_archive("second")
    archives = list(repo.archives_dir.glob("*.tar.zst"))
    assert [path.name for path in archives] == ["second.tar.zst"]
    assert (repo.memories_dir / "cold.md").read_bytes() != original
    assert repo.get("cold").archive_ref == "second"


def test_mechanism_archive_skips_unselected_anchor(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    config = repo.read_config()
    config["anchor_always_hot"] = False
    repo.configure(config)
    repo.add("Protected", "anchor body", memory_id="anchor", layer=Layer.ANCHOR, tier=Tier.COLD)
    repo.add("Ordinary", "cold body", memory_id="ordinary", tier=Tier.COLD)
    archive = repo.cold_archive("ordinary-only", actor="mechanism", memory_ids={"ordinary"})
    assert archive.exists()
    assert repo.get("anchor").body == "anchor body"
    assert not repo.get("anchor").is_stub
    assert repo.get("ordinary").is_stub


def _allocated_bytes(root: Path) -> int:
    return sum(
        getattr(path.stat(), "st_blocks", (path.stat().st_size + 511) // 512) * 512
        for path in root.rglob("*")
        if path.is_file()
    )


def test_cold_archive_reduces_managed_footprint_and_drops_raw_index(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    body = "memfw-cold-body\n" * 100_000
    repo.add("Large cold note", body, memory_id="large", tier=Tier.COLD)
    MemoryIndex(repo.index_path).rebuild(repo)
    before_size = sum(path.stat().st_size for path in repo.root.rglob("*") if path.is_file())
    before_allocated = _allocated_bytes(repo.root)

    archive = repo.cold_archive("compressed")
    after_size = sum(path.stat().st_size for path in repo.root.rglob("*") if path.is_file())
    after_allocated = _allocated_bytes(repo.root)
    assert archive.exists()
    assert not repo.index_path.exists()
    assert after_size < before_size
    assert after_allocated < before_allocated
    history_files = list((repo.history_dir / "large").glob("*.meta.json"))
    # Archive history keeps metadata-only refs; the compressed archive is the body source.
    assert len(history_files) == 2
    for history_file in history_files:
        digest = history_file.name.removesuffix(".meta.json")
        assert repo._read_version("large", digest)
    assert repo.get("large").is_stub


@pytest.mark.parametrize("keep", [0, 1, 3])
def test_archive_history_keep_exactly_retains_configured_versions(
    tmp_path: Path, keep: int
) -> None:
    repo = make_repo(tmp_path / f"vault-{keep}")
    repo.init()
    config = yaml.safe_load(repo.config_path.read_text(encoding="utf-8"))
    config["archive_keep_history"] = keep
    repo.configure(config)
    document = repo.add("History", "v0", memory_id="history", tier=Tier.COLD)
    for index in range(3):
        document = repo.update(document.id, body=f"v{index + 1}")
    pre_archive = [
        event.after_hash
        for event in repo.events()
        if event.memory_id == "history" and event.after_hash is not None
    ]

    repo.cold_archive(f"archive-{keep}")

    retained = {
        path.name.removesuffix(".meta.json")
        for path in (repo.history_dir / "history").glob("*.meta.json")
    }
    assert retained == set(pre_archive[-keep:] if keep else []) | {
        hashlib.sha256((repo.memories_dir / "history.md").read_bytes()).hexdigest()
    }


def test_history_keep_prunes_old_versions(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    config = repo.config_path.read_text(encoding="utf-8").replace(
        "history_keep: 10", "history_keep: 2"
    )
    repo.config_path.write_text(config, encoding="utf-8")
    document = repo.add("History", "v0", memory_id="history")
    for index in range(4):
        document = repo.update(document.id, body=f"v{index + 1}")
    assert document.version == 5
    retained = {
        path.name.removesuffix(".meta.json")
        for path in (repo.history_dir / "history").glob("*.meta.json")
    }
    current_hash = hashlib.sha256((repo.memories_dir / "history.md").read_bytes()).hexdigest()
    assert len(retained) == 2
    assert current_hash in retained


def test_typed_proposals_are_hash_bound_human_decided_and_erasable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Target", "private current body", memory_id="target")
    now = datetime(2026, 2, 3, tzinfo=UTC)
    output: dict[str, JsonValue] = {"body": "proposed private body"}
    payload_hash = canonical_payload_hash(output)
    target_hashes = {"target": canonical_payload_hash(repo.get("target").model_dump(mode="json"))}
    proposal = Proposal(
        proposal_id="proposal-1",
        kind="layer",
        target_ids=("target",),
        target_hashes=target_hashes,
        agent_name="mock",
        model_name="mock-v1",
        input_hash="a" * 64,
        output=output,
        reason="test proposal",
        payload_hash=payload_hash,
        actor="agent:mock",
        signature=mock_signature("proposal-1", "a" * 64, payload_hash, now, target_hashes),
        created_at=now,
        updated_at=now,
    )

    event = repo.record_proposal(proposal)
    assert event.kind == EventKind.AGENT_PROPOSAL
    assert repo.events_path.read_bytes().find(b"proposed private body") == -1
    assert repo.list_proposals() == [proposal]
    approved = repo.decide_proposal(
        proposal.proposal_id, decision="approve", payload_hash=payload_hash
    )
    assert isinstance(approved, Proposal)
    assert approved.status == "approved"
    with pytest.raises(IntegrityError, match="Only a human"):
        repo.mark_proposal_applied(
            proposal.proposal_id, payload_hash=payload_hash, actor="mechanism"
        )
    applied = repo.mark_proposal_applied(proposal.proposal_id, payload_hash=payload_hash)
    assert applied.kind == EventKind.HUMAN_DECISION
    assert not (repo.proposals_dir / "proposal-1.md").exists()
    with pytest.raises(IntegrityError, match="already has a human decision"):
        repo.decide_proposal(proposal.proposal_id, decision="approve", payload_hash=payload_hash)

    repo.delete("target")
    assert not (repo.proposals_dir / "proposal-1.md").exists()
    assert repo.list_proposals() == []
    assert b"private current body" not in repo.events_path.read_bytes()


def test_typed_proposal_rejects_stale_target_and_recovery_removes_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    original = repo.add("Target", "before", memory_id="target")
    now = datetime(2026, 2, 3, tzinfo=UTC)
    target_hashes = {"target": canonical_payload_hash(original.model_dump(mode="json"))}
    output: dict[str, JsonValue] = {"layer": "consolidated"}
    payload_hash = canonical_payload_hash(output)
    proposal = Proposal(
        proposal_id="stale-proposal",
        kind="layer",
        target_ids=("target",),
        target_hashes=target_hashes,
        agent_name="mock",
        model_name="mock-v1",
        input_hash="b" * 64,
        output=output,
        reason="test stale check",
        payload_hash=payload_hash,
        actor="agent:mock",
        signature=mock_signature("stale-proposal", "b" * 64, payload_hash, now, target_hashes),
        created_at=now,
        updated_at=now,
    )
    repo.record_proposal(proposal)
    repo.update("target", body="changed after judge")
    with pytest.raises(IntegrityError, match="stale"):
        repo.decide_proposal(proposal.proposal_id, decision="approve", payload_hash=payload_hash)

    changed_target_hashes = {
        "target": canonical_payload_hash(repo.get("target").model_dump(mode="json"))
    }
    orphan = proposal.model_copy(
        update={"proposal_id": "orphan-proposal", "target_hashes": changed_target_hashes}
    )
    original_append = repo._append_event

    def fail_proposal_append(*args: Any, **kwargs: Any) -> Event:
        kind = args[0] if args else kwargs.get("kind")
        if kind == EventKind.AGENT_PROPOSAL:
            raise OSError("injected append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(repo, "_append_event", fail_proposal_append)
    with pytest.raises(OSError, match="injected"):
        repo.record_proposal(orphan)
    assert repo.pending_path.exists()
    recovered = make_repo(repo.root)
    recovered.recover_pending()
    assert not (recovered.proposals_dir / "orphan-proposal.md").exists()
    assert recovered.list_proposals(status=None) == [proposal]


def test_fsck_quarantines_bad_file_and_continues_external_delete_and_untracked(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.add("Bad", "old", memory_id="bad")
    repo.add("Missing", "gone", memory_id="missing")
    repo.add("New", "new body", memory_id="new")
    repo.add("External", "external body", memory_id="external-template")
    untracked = repo.get("external-template").model_copy(update={"id": "untracked"})
    external = repo.render(untracked)
    repo.delete("external-template")

    (repo.memories_dir / "bad.md").write_text("not markdown", encoding="utf-8")
    (repo.memories_dir / "missing.md").unlink()
    (repo.memories_dir / "untracked.md").write_text(external, encoding="utf-8")
    assert any("missing" in item for item in repo.list_warnings())

    issues = repo.fsck()
    kinds = {issue["kind"] for issue in issues}
    assert kinds >= {
        "invalid_file_quarantined",
        "external_delete_registered",
        "untracked_file_registered",
    }
    assert repo.replay()["bad"] is None
    assert repo.replay()["missing"] is None
    assert repo.get("untracked").body == "external body"
    assert repo.fsck() == []


def test_full_signed_update_and_anchor_requires_human_approval(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "vault")
    repo.init()
    repo.config_path.write_text(
        repo.config_path.read_text(encoding="utf-8").replace(
            "permissions: propose_then_confirm", "permissions: full"
        ),
        encoding="utf-8",
    )
    repo.add("Agent editable", "before", memory_id="editable")
    changed = repo.update(
        "editable", actor="agent:writer", agent_id="writer", signature="sig-v1", body="after"
    )
    assert changed.body == "after"
    assert repo.events()[-1].details["signature"] == "sig-v1"

    anchor_payload = {
        "memory_id": "editable",
        "operation": "layer_change",
        "changes": {"layer": "anchor"},
    }
    proposal = repo.record_proposal(
        memory_id="editable",
        operation="layer_change",
        changes={"layer": "anchor"},
        rationale="human-approved anchor",
        agent_id="writer",
        signature="sig-anchor",
    )
    anchored = repo.decide_proposal(proposal.event_id, payload=anchor_payload)
    assert anchored is None
    assert repo.get("editable").layer == Layer.CURRENT
    anchored = repo.apply_proposal(proposal.event_id, payload=anchor_payload)
    assert anchored is not None and anchored.layer == Layer.ANCHOR
    assert repo.events()[-2].kind == EventKind.HUMAN_DECISION
    assert repo.events()[-1].kind == EventKind.LAYER_CHANGE
    assert repo.events()[-1].actor == "human"


def test_concurrent_repository_writes_are_serialized(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repo_a = make_repo(root)
    sequence = iter(f"second-{number}" for number in range(100))
    repo_b = Repository(
        root,
        clock=lambda: datetime(2026, 2, 3, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )
    repo_a.init()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda index: (repo_a if index % 2 else repo_b).add(
                    f"Note {index}", f"body {index}", memory_id=f"n-{index}"
                ),
                range(20),
            )
        )
    assert len(results) == 20
    events = repo_a.events()
    assert len(repo_a.list()) == 20
    assert len(events) == 20
    assert len({event.event_hash for event in events}) == 20


@pytest.mark.parametrize("policy", tuple(PolicyName))
def test_total_budget_anchor_floor_is_reported_for_every_policy(
    tmp_path: Path, policy: PolicyName
) -> None:
    repository = make_repo(tmp_path / f"anchor-floor-{policy.value}")
    repository.init()
    repository.add("Pinned", "protected body", memory_id="pinned", layer=Layer.ANCHOR)
    config = repository.read_config()
    config.update(
        {
            "budget_mode": "total",
            "max_bytes": 0,
            "max_count": 0,
            "budget_scope": "all",
            "forget_policy": policy.value,
            "forget_action": "hard",
        }
    )
    repository.configure(config)

    result = enforce_budget(repository, now=datetime(2026, 2, 3, tzinfo=UTC))

    assert result.satisfied_after_apply is False
    assert result.unsatisfied_reason_code == "anchor_floor"
    assert "exceeds the configured budget by" in (result.unsatisfied_reason or "")
    assert result.blocking_anchor_ids == ("pinned",)
    events = [
        event for event in repository.events() if event.kind == EventKind.BUDGET_UNSATISFIABLE
    ]
    assert len(events) == 1
    assert events[0].details["reason"] == "anchor_floor"
    assert events[0].details["anchor_ids"] == "pinned"


def test_fast_physical_byte_walk_matches_full_runtime_stats(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "physical-stats")
    repo.init()
    repo.add("Body", "x" * 8192, memory_id="body")
    repo.update("body", tier=Tier.COLD)
    repo.cold_archive("physical-archive", memory_ids={"body"})

    assert (
        measure_physical_disk_bytes(repo.root)
        == measure_runtime(repo, now=datetime(2026, 2, 3, tzinfo=UTC)).physical_disk_bytes
    )


def test_rollback_preserves_valid_cold_stub_archive_hash(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "rollback-valid-cold")
    repo.init()
    repo.add("Cold", "archive-backed body", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("rollback-valid")
    stub = repo.get("cold")
    assert stub.is_stub and stub.archived_hash

    repo.update("cold", summary="edited stub summary")
    rollback = repo.rollback(repo.events()[-1].event_id)

    assert rollback.is_stub
    assert rollback.archived_hash == stub.archived_hash
    assert rollback.archive_ref == stub.archive_ref


def test_rollback_refuses_cold_stub_when_archive_member_was_restored_away(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "rollback-missing-cold")
    repo.init()
    repo.add("Cold", "archive-backed body", memory_id="cold", tier=Tier.COLD)
    repo.cold_archive("rollback-missing")
    repo.restore_cold("cold")
    restore_event = next(
        event for event in reversed(repo.events()) if event.kind == EventKind.MEMORY_RESTORE
    )
    before = repo._tree_fingerprint()

    with pytest.raises(DeletedContentUnavailable, match="verified archived body"):
        repo.rollback(restore_event.event_id)

    assert repo._tree_fingerprint() == before
