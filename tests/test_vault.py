from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.errors import DeletedContentUnavailable, IntegrityError
from memfw.core.models import CaptureState, EventKind, Layer, MemoryDocument, Tier
from memfw.core.repository import Repository, sha256_text
from memfw.index import MemoryIndex


def fixed_repo(root: Path) -> Repository:
    sequence = iter(f"event-{n}" for n in range(100))
    return Repository(
        root,
        clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def tree_hash(root: Path, *, skip_index: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (skip_index and path.name == "index.sqlite3"):
            continue
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_rebuild_replay_and_cold_stub_search(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.add("Hot note", "findable body", memory_id="hot-note")
    repo.add("Cold note", "cold contents uniquephrase", memory_id="cold-note", tier=Tier.COLD)
    archive = repo.cold_archive("archive-1")
    assert archive.exists()
    assert repo.get("cold-note").is_stub
    assert repo.get("cold-note").body == ""
    assert repo.get("cold-note").summary == "cold contents uniquephrase"

    committed = repo.replay()
    for doc in repo.list():
        current = (repo.memories_dir / f"{doc.id}.md").read_text(encoding="utf-8")
        assert committed[doc.id] == sha256_text(current)

    index = MemoryIndex(repo.index_path)
    assert index.rebuild(repo) == 2
    first_rows = index.rows()
    assert [(row[0], row[1], row[2], row[3], row[4], row[5]) for row in first_rows] == [
        ("cold-note", "Cold note", "", "current", "cold", "manual"),
        ("hot-note", "Hot note", "findable body", "current", "hot", "manual"),
    ]
    first_results = index.search("findable")
    assert first_results == [("hot-note", "Hot note")]
    assert index.search("uniquephrase") == [("cold-note", "Cold note")]
    repo.index_path.unlink()
    assert index.rebuild(repo) == 2
    rebuilt_rows = index.rows()
    assert rebuilt_rows == first_rows
    assert index.search("findable") == first_results
    restored = repo.restore_cold("cold-note")
    assert not restored.is_stub
    assert "uniquephrase" in restored.body


def test_external_edit_is_only_committed_by_fsck(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    original = repo.add("Title", "original body", memory_id="memo")
    original_hash = repo.replay()[original.id]
    path = repo.memories_dir / "memo.md"
    edited = path.read_text(encoding="utf-8").replace("original body", "edited body")
    path.write_text(edited, encoding="utf-8")

    assert repo.replay()["memo"] == original_hash
    issues = repo.fsck()
    assert issues[0]["kind"] == "external_edit_registered"
    assert repo.replay()["memo"] == sha256_text(edited)
    assert repo.events()[-1].kind == EventKind.EXTERNAL_EDIT
    assert repo.get("memo").body == "edited body"


def test_crlf_byte_change_is_registered_as_external_edit(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.add("Line endings", "keep bytes", memory_id="crlf")
    path = repo.memories_dir / "crlf.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    issues = repo.fsck()
    assert issues[0]["kind"] == "external_edit_registered"
    assert repo.events()[-1].kind == EventKind.EXTERNAL_EDIT
    assert repo.replay()["crlf"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_frontmatter_is_obsidian_yaml_with_required_fields(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    doc = repo.add(
        "Obsidian",
        "body",
        memory_id="obsidian",
        layer=Layer.CONSOLIDATED,
        tier=Tier.HOT,
        source="chat:turn-1",
        provenance="direct-quote",
        anchor="project-x",
        tags=["work"],
    )
    content = (repo.memories_dir / "obsidian.md").read_bytes().decode("utf-8")
    parsed = repo.parse(content, expected_id="obsidian")
    assert parsed == doc
    for key in (
        "id:",
        "created_at:",
        "updated_at:",
        "layer:",
        "tier:",
        "source:",
        "provenance:",
        "anchor:",
        "tags:",
        "version:",
    ):
        assert key in content


def test_hard_delete_erases_managed_content_and_cannot_rollback(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    doc = repo.add("Secret title", "ultra-secret-phrase", memory_id="secret", tier=Tier.COLD)
    repo.update(doc.id, body="changed ultra-secret-phrase")
    MemoryIndex(repo.index_path).rebuild(repo)
    archive = repo.cold_archive("secret-archive")
    assert archive.exists()
    repo.delete(doc.id)
    deleted_event_id = repo.events()[-1].event_id

    assert repo.replay()[doc.id] is None
    assert not (repo.memories_dir / "secret.md").exists()
    assert not (repo.history_dir / "secret").exists()
    assert not repo.index_path.exists()
    assert not list(repo.archives_dir.glob("*.tar.zst"))
    assert "ultra-secret-phrase" not in repo.events_path.read_text(encoding="utf-8")
    assert all(
        "ultra-secret-phrase" not in p.read_text(encoding="utf-8")
        for p in repo.events_path.parent.rglob("*.md")
    )
    with pytest.raises(DeletedContentUnavailable):
        repo.rollback(deleted_event_id)


def test_two_full_vault_flows_are_deterministic(tmp_path: Path) -> None:
    snapshots = []
    for name in ("one", "two"):
        repo = fixed_repo(tmp_path / name)
        repo.init()
        config = repo.read_config()
        config["anchor_always_hot"] = False
        repo.configure(config)
        first = repo.add(
            "Same", "same body", layer=Layer.ANCHOR, tier=Tier.COLD, memory_id="same-id"
        )
        repo.update(first.id, body="revised body", tags=["tag-a"])
        repo.cold_archive("same-archive")
        repo.capture_event(
            EventKind.CAPTURE_STATE_CHANGE,
            actor="human",
            details={"state": CaptureState.STANDBY.value},
        )
        snapshots.append(tree_hash(repo.root, skip_index=True))
    assert snapshots[0] == snapshots[1]


def test_id_path_frontmatter_duplicates_and_agent_write_are_rejected(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    with pytest.raises(IntegrityError):
        repo.add("Escape", "body", memory_id="../escape")
    with pytest.raises(IntegrityError):
        repo.add("Agent", "body", memory_id="agent", actor="agent")
    with pytest.raises(IntegrityError, match="Duplicate frontmatter key"):
        repo.parse("---\nid: x\nid: y\n---\n\nbody\n")


def test_replay_and_index_rebuild_do_not_repair_external_files(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.add("Title", "before", memory_id="m")
    path = repo.memories_dir / "m.md"
    path.write_text(path.read_text(encoding="utf-8").replace("before", "after"), encoding="utf-8")
    before_events = repo.events_path.read_bytes()
    expected = repo.replay()["m"]
    MemoryIndex(repo.index_path).rebuild(repo)
    assert repo.events_path.read_bytes() == before_events
    assert repo.replay()["m"] == expected
    assert repo.get("m").body == "after"


def test_corrupt_event_is_reported_without_rewrite(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.events_path.write_text("{bad json}\n", encoding="utf-8")
    before = repo.events_path.read_bytes()
    with pytest.raises(IntegrityError, match="Invalid event"):
        repo.replay()
    assert repo.events_path.read_bytes() == before


def test_delete_from_archive_preserves_other_memory_and_untracked_fsck(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.add("Delete me", "private alpha", memory_id="alpha", tier=Tier.COLD)
    repo.add("Keep me", "private beta", memory_id="beta", tier=Tier.COLD)
    repo.cold_archive("combined")
    repo.delete("alpha")
    assert repo.restore_cold("beta").body == "private beta"
    with pytest.raises(DeletedContentUnavailable):
        repo.restore_cold("alpha")

    now = repo._now()
    untracked_doc = MemoryDocument(
        id="untracked", title="New", body="body", created_at=now, updated_at=now
    )
    untracked = repo.render(untracked_doc)
    (repo.memories_dir / "untracked.md").write_text(untracked, encoding="utf-8")
    issue = repo.fsck()[-1]
    assert issue["kind"] == "untracked_file_registered"
    assert repo.get("untracked").body == "body"


def test_cold_archive_id_is_unique_and_stub_points_to_exact_archive(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    repo.add("First", "archive-one-body", memory_id="first", tier=Tier.COLD)
    first_archive = repo.cold_archive("archive-one")
    with pytest.raises(FileExistsError):
        repo.cold_archive("archive-one")

    repo.add("Second", "archive-two-body", memory_id="second", tier=Tier.COLD)
    second_archive = repo.cold_archive("archive-two")
    assert repo.get("first").archive_ref == first_archive.name.removesuffix(".tar.zst")
    assert repo.get("second").archive_ref == second_archive.name.removesuffix(".tar.zst")
    assert repo.restore_cold("second").body == "archive-two-body"
    assert repo.restore_cold("first").body == "archive-one-body"


def test_rollback_creates_new_version_and_event_has_metadata_only(tmp_path: Path) -> None:
    repo = fixed_repo(tmp_path / "vault")
    repo.init()
    first = repo.add("Title", "safe original", memory_id="memo")
    update = repo.update(first.id, body="new body")
    update_event = repo.events()[-1]
    restored = repo.rollback(update_event.event_id)
    assert restored.body == "safe original"
    assert restored.version == update.version + 1
    proposal = repo.capture_event(
        EventKind.AGENT_PROPOSAL,
        actor="agent:test",
        details={
            "memory_id": "memo",
            "operation": "review",
            "proposal_hash": "a" * 64,
            "target_hash": "b" * 64,
            "agent_id": "test",
            "signature": "opaque-signature",
        },
    )
    assert proposal.details["operation"] == "review"
    with pytest.raises(IntegrityError):
        repo.capture_event(
            EventKind.AGENT_PROPOSAL,
            actor="agent:test",
            details={"memory_id": "memo", "body": "must not log"},
        )
    with pytest.raises(IntegrityError):
        repo.capture_event(
            EventKind.CAPTURE_STATE_CHANGE, actor="human", details={"state": "paused"}
        )


def test_cli_quickstart_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    root = str(tmp_path / "cli-vault")
    commands = [
        ["init", root],
        ["add", "--root", root, "--id", "cli-note", "--title", "CLI note", "--body", "searchable"],
        ["list", "--root", root],
        ["show", "cli-note", "--root", root],
        ["index", "rebuild", "--root", root],
        ["index", "search", "searchable", "--root", root],
        ["log", "replay", "--root", root],
        ["fsck", "--root", root],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, f"{command}: {result.output}"
