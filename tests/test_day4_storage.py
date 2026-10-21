from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
import zstandard

from memfw.core.errors import DeletedContentUnavailable, IntegrityError
from memfw.core.models import EventKind, Layer
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.stats import measure_physical_disk_bytes
from memfw.stats.runtime import measure_logical_breakdown, measure_physical_breakdown

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def make_repo(root: Path) -> Repository:
    ids = iter(f"day4-event-{index}" for index in range(10_000))
    repository = Repository(root, clock=lambda: NOW, id_factory=lambda: next(ids))
    repository.init()
    return repository


def test_log_compaction_preserves_cross_segment_chain_and_future_appends(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "compact")
    repository.add("one", "body one", memory_id="one")
    repository.add("two", "body two", memory_id="two")
    repository.update("one", tags=["updated"])
    before = repository.events()

    first = repository.compact_log(keep_recent=1)
    assert first["newly_archived_event_count"] == len(before) - 1
    assert len(repository.events()) == len(before)
    assert repository.replay()["one"] == before[-1].after_hash
    assert repository.events_path.read_text().count("\n") == 1

    repository.update("two", strength=0.75)
    second = repository.compact_log(keep_recent=1)
    assert second["segment_count"] == 2
    repository.add("three", "body three", memory_id="three")
    assert len(repository.events()) == len(before) + 2
    assert len(repository._log_segments()[1]) == 2


def test_log_compaction_failure_is_readable_and_explicit_recovery_trims_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "recover")
    for index in range(5):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    expected = [event.event_hash for event in repository.events()]
    atomic_write = repository._atomic_write

    def fail_tail(path: Path, data: bytes) -> None:
        if path == repository.events_path:
            raise OSError("injected interruption after catalog commit")
        atomic_write(path, data)

    monkeypatch.setattr(repository, "_atomic_write", fail_tail)
    with pytest.raises(OSError, match="injected interruption"):
        repository.compact_log(keep_recent=1)
    assert [event.event_hash for event in repository.events()] == expected

    monkeypatch.setattr(repository, "_atomic_write", atomic_write)
    assert repository.recover_pending() == ["events.jsonl"]
    assert repository.events_path.read_text().count("\n") == 1
    assert [event.event_hash for event in repository.events()] == expected


def test_add_many_interruption_rolls_back_uncommitted_created_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "add-many-recovery")
    atomic_write = repository._atomic_write

    def fail_log_commit(path: Path, data: bytes) -> None:
        if path == repository.events_path:
            raise OSError("batch log commit interrupted")
        atomic_write(path, data)

    monkeypatch.setattr(repository, "_atomic_write", fail_log_commit)
    with pytest.raises(OSError, match="batch log commit"):
        repository.add_many([{"title": "title", "body": "body", "memory_id": "one"}])
    monkeypatch.setattr(repository, "_atomic_write", atomic_write)
    repository.recover_pending()
    assert not repository._current_path("one").exists()
    assert repository.replay().get("one") is None


def test_delete_tombstone_is_durable_before_content_erasure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "delete-wal")
    repository.add("title", "body", memory_id="one")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise OSError("injected interruption after tombstone")

    monkeypatch.setattr(repository, "_erase_target_proposals", interrupt)
    with pytest.raises(OSError, match="after tombstone"):
        repository.delete("one")
    monkeypatch.setattr(repository, "_erase_target_proposals", lambda *args, **kwargs: None)
    repository.recover_pending()
    assert repository.events()[-1].kind.value == "delete"
    assert not repository._current_path("one").exists()
    assert repository.replay()["one"] is None


def test_archived_segment_tampering_fails_verification(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "tamper")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    segment = next(repository.log_segments_dir.glob("*.tar.zst"))
    segment.write_bytes(segment.read_bytes() + b"tamper")
    with pytest.raises(IntegrityError, match="segment hash mismatch"):
        repository.events()


def test_tampered_snapshot_is_rejected_after_container_hash_update(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "snapshot-tamper")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    segment = next(repository.log_segments_dir.glob("summary-container-*.tar.zst"))
    decompressed = zstandard.ZstdDecompressor().decompress(segment.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        members = {}
        for name in archive.getnames():
            extracted = archive.extractfile(name)
            assert extracted is not None
            members[name] = extracted.read()
    snapshot_name = next(name for name in members if name.endswith("/snapshot.json"))
    members[snapshot_name] = members[snapshot_name].replace(b"note-0", b"note-X")
    packed_tar = io.BytesIO()
    with tarfile.open(fileobj=packed_tar, mode="w:") as archive:
        for name, value in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(value))
    compressed = zstandard.ZstdCompressor(
        level=19, write_checksum=True, write_content_size=True
    ).compress(packed_tar.getvalue())
    segment.write_bytes(compressed)
    catalog = json.loads(repository.log_catalog_path.read_text(encoding="utf-8"))
    catalog["container"]["compressed_hash"] = hashlib.sha256(compressed).hexdigest()
    repository._atomic_write(
        repository.log_catalog_path,
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )

    with pytest.raises(IntegrityError, match="snapshot hash mismatch"):
        repository.events()


def test_duplicate_container_member_is_rejected(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "duplicate-member")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    container_path = next(repository.log_segments_dir.glob("summary-container-*.tar.zst"))
    decompressed = zstandard.ZstdDecompressor().decompress(container_path.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        members = []
        for name in archive.getnames():
            extracted = archive.extractfile(name)
            assert extracted is not None
            members.append((name, extracted.read()))
    packed_tar = io.BytesIO()
    with tarfile.open(fileobj=packed_tar, mode="w:") as archive:
        for name, value in [*members, members[0]]:
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(value))
    compressed = zstandard.ZstdCompressor(
        level=19, write_checksum=True, write_content_size=True
    ).compress(packed_tar.getvalue())
    container_path.write_bytes(compressed)
    catalog = json.loads(repository.log_catalog_path.read_text(encoding="utf-8"))
    catalog["container"]["compressed_hash"] = hashlib.sha256(compressed).hexdigest()
    repository._atomic_write(
        repository.log_catalog_path,
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )

    with pytest.raises(IntegrityError, match="Duplicate event summary container member"):
        repository.events()


def test_tampered_logical_segment_is_rejected_after_container_hash_update(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "summary-tamper")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    container_path = next(repository.log_segments_dir.glob("summary-container-*.tar.zst"))
    decompressed = zstandard.ZstdDecompressor().decompress(container_path.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as archive:
        members = {}
        for name in archive.getnames():
            extracted = archive.extractfile(name)
            assert extracted is not None
            members[name] = extracted.read()
    summary_name = next(name for name in members if name.endswith("/summaries.jsonl"))
    first_summary = json.loads(members[summary_name].splitlines()[0])
    first_summary["event"]["actor"] = "tampered"
    members[summary_name] = (
        (json.dumps(first_summary, sort_keys=True, separators=(",", ":")) + "\n").encode()
        + b"\n".join(members[summary_name].splitlines()[1:])
        + b"\n"
    )
    packed_tar = io.BytesIO()
    with tarfile.open(fileobj=packed_tar, mode="w:") as archive:
        for name, value in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(value))
    compressed = zstandard.ZstdCompressor(
        level=19, write_checksum=True, write_content_size=True
    ).compress(packed_tar.getvalue())
    container_path.write_bytes(compressed)
    catalog = json.loads(repository.log_catalog_path.read_text(encoding="utf-8"))
    catalog["container"]["compressed_hash"] = hashlib.sha256(compressed).hexdigest()
    repository._atomic_write(
        repository.log_catalog_path,
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )

    with pytest.raises(IntegrityError, match="summary hash mismatch"):
        repository.events()


def test_legacy_payload_segment_migrates_to_body_free_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.log.store import _canonical_json, _deterministic_tar, _project_events

    repository = make_repo(tmp_path / "legacy-segment")
    for index in range(4):
        repository.add(f"title {index}", f"secret body {index}", memory_id=f"note-{index}")
    events = repository.events()
    archived = events[:2]
    raw_events = ("\n".join(event.model_dump_json() for event in archived) + "\n").encode()
    snapshot = _canonical_json(_project_events(archived))
    manifest_bytes = _canonical_json(
        {
            "schema_version": 1,
            "event_count": len(archived),
            "previous_hash": "0" * 64,
            "first_hash": archived[0].event_hash,
            "last_hash": archived[-1].event_hash,
            "events_hash": hashlib.sha256(raw_events).hexdigest(),
            "snapshot_hash": hashlib.sha256(snapshot).hexdigest(),
        }
    )
    tar_bytes = _deterministic_tar(
        [
            ("events.jsonl", raw_events),
            ("snapshot.json", snapshot),
            ("manifest.json", manifest_bytes),
        ]
    )
    compressed = zstandard.ZstdCompressor(
        level=19, write_checksum=True, write_content_size=True
    ).compress(tar_bytes)
    name = f"segment-000000000002-{archived[-1].event_hash}.tar.zst"
    segment = repository.log_segments_dir / name
    segment.write_bytes(compressed)
    repository._atomic_write(
        repository.log_catalog_path,
        _canonical_json(
            {
                "schema_version": 1,
                "segments": [
                    {
                        "name": name,
                        "compressed_hash": hashlib.sha256(compressed).hexdigest(),
                        "event_count": 2,
                        "first_hash": archived[0].event_hash,
                        "last_hash": archived[-1].event_hash,
                        "previous_hash": "0" * 64,
                    }
                ],
            }
        ),
    )
    repository._atomic_write(
        repository.events_path,
        b"".join((event.model_dump_json() + "\n").encode() for event in events[2:]),
    )

    assert len(repository.events()) == 4
    atomic_write = repository._atomic_write

    def fail_tail(path: Path, data: bytes) -> None:
        if path == repository.events_path:
            raise OSError("injected post-catalog interruption")
        atomic_write(path, data)

    monkeypatch.setattr(repository, "_atomic_write", fail_tail)
    with pytest.raises(OSError, match="post-catalog interruption"):
        repository.compact_log(keep_recent=1)
    assert segment.exists()
    monkeypatch.setattr(repository, "_atomic_write", atomic_write)
    repository.recover_pending()
    assert not segment.exists()
    assert len(repository.events()) == 4
    result = repository.compact_log(keep_recent=1)

    assert result["verification_level"] == "summary_and_snapshot"
    assert len(repository.events()) == 4
    assert not list(repository.log_segments_dir.glob("segment-*.tar.zst"))
    assert not list(repository.log_segments_dir.glob("summary-segment-*.tar.zst"))
    containers = list(repository.log_segments_dir.glob("summary-container-*.tar.zst"))
    assert len(containers) == 1
    archive_bytes = zstandard.ZstdDecompressor().decompress(containers[0].read_bytes())
    assert b"secret body" not in archive_bytes


def test_recovery_removes_unreferenced_segment_only_after_log_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "orphan")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    atomic_write = repository._atomic_write

    def fail_catalog(path: Path, data: bytes) -> None:
        if path == repository.log_catalog_path:
            raise OSError("catalog interruption")
        atomic_write(path, data)

    monkeypatch.setattr(repository, "_atomic_write", fail_catalog)
    with pytest.raises(OSError, match="catalog interruption"):
        repository.compact_log(keep_recent=1)
    orphan = next(repository.log_segments_dir.glob("summary-container-*.tar.zst"))
    monkeypatch.setattr(repository, "_atomic_write", atomic_write)
    cleaned = repository.recover_pending()
    assert orphan.relative_to(repository.root).as_posix() in cleaned
    assert not orphan.exists()
    assert len(repository.events()) == 4


def test_compact_dry_run_counts_only_unarchived_tail(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = make_repo(tmp_path / "dry-run")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    result = CliRunner().invoke(
        app,
        ["log", "compact", "--root", str(repository.root), "--keep-recent", "1"],
    )
    assert result.exit_code == 0
    assert '"would_archive_events": 0' in result.stdout


def test_log_verify_reports_payload_availability_level(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = make_repo(tmp_path / "verify-level")
    runner = CliRunner()
    initial = runner.invoke(app, ["log", "verify", "--root", str(repository.root)])
    assert initial.exit_code == 0
    assert json.loads(initial.stdout)["verification_level"] == "full_event_payloads"

    for index in range(3):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    compacted = runner.invoke(app, ["log", "verify", "--root", str(repository.root)])
    assert compacted.exit_code == 0
    summary = json.loads(compacted.stdout)
    assert summary["verification_level"] == "active_payloads_and_archived_summaries"
    assert summary["original_archived_payloads_available"] is False


def test_segment_cache_rechecks_bytes_and_returns_defensive_events(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "cache")
    for index in range(4):
        repository.add(f"title {index}", f"body {index}", memory_id=f"note-{index}")
    repository.compact_log(keep_recent=1)
    first = repository.events()
    first[0].details["caller_mutation"] = "ignored"
    assert "caller_mutation" not in repository.events()[0].details

    segment = next(repository.log_segments_dir.glob("*.tar.zst"))
    original = segment.read_bytes()
    stat = segment.stat()
    segment.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    import os

    os.utime(segment, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(IntegrityError, match="segment hash mismatch"):
        repository.events()


def test_typed_proposal_remains_reviewable_after_summary_compaction(tmp_path: Path) -> None:
    from memfw.agents.models import JsonValue, Proposal, canonical_payload_hash, mock_signature
    from memfw.curation.review import apply_approved, decide_proposal

    repository = make_repo(tmp_path / "proposal-compact")
    document = repository.add("target", "body", memory_id="target")
    output: dict[str, JsonValue] = {"score": 0.73}
    payload_hash = canonical_payload_hash(output)
    timestamp = repository._now()
    input_hash = hashlib.sha256(b"compaction-review-regression").hexdigest()
    value = Proposal(
        proposal_id="compact-review-proposal",
        kind="salience",
        target_ids=(document.id,),
        target_hashes={document.id: canonical_payload_hash(document.model_dump(mode="json"))},
        agent_name="test-agent",
        model_name="test-model",
        input_hash=input_hash,
        output=output,
        reason="review summary behavior",
        payload_hash=payload_hash,
        actor="agent:test-agent",
        signature=mock_signature("compact-review-proposal", input_hash, payload_hash, timestamp),
        created_at=timestamp,
        updated_at=timestamp,
    )
    repository.record_proposal(value)
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access"},
    )
    repository.capture_event(
        EventKind.CAPTURE_RECORD,
        actor="mechanism",
        details={
            "candidate_id": "candidate-1",
            "session_id": "session-1",
            "source": "human",
            "source_hash": hashlib.sha256(b"candidate body").hexdigest(),
        },
    )
    repository.compact_log(keep_recent=1)
    decide_proposal(repository, value.proposal_id, "approve", payload_hash=payload_hash)
    repository.compact_log(keep_recent=1)

    applied = apply_approved(repository, value.proposal_id)

    assert applied.strength == 0.73
    assert repository.get("target").strength == 0.73
    events = repository.events()
    assert any(event.kind == EventKind.MEMORY_ACCESS for event in events)
    assert any(event.kind == EventKind.CAPTURE_RECORD for event in events)
    repository.compact_log(keep_recent=1)
    before = next(repository.log_segments_dir.glob("summary-container-*.tar.zst")).read_bytes()
    noop = repository.compact_log(keep_recent=1)
    after = next(repository.log_segments_dir.glob("summary-container-*.tar.zst")).read_bytes()
    assert noop["newly_archived_event_count"] == 0
    assert before == after


def test_metadata_only_history_delta_reconstructs_and_rolls_back(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "delta")
    original = repository.add("title", "body should be stored once", memory_id="note")
    original_hash = hashlib.sha256(repository._current_path("note").read_bytes()).hexdigest()
    changed = repository.update("note", tags=["tag"], strength=0.8)
    changed_hash = hashlib.sha256(repository._current_path("note").read_bytes()).hexdigest()
    delta = repository._history_metadata_path("note", changed_hash)
    assert delta.exists()
    assert not repository._history_path("note", changed_hash).exists()
    assert b"body should be stored once" not in delta.read_bytes()
    assert repository._read_version("note", changed_hash).decode() == repository.render(changed)
    assert repository._read_version("note", original_hash).decode() == repository.render(original)

    update_event = repository.events()[-1]
    rolled_back = repository.rollback(update_event.event_id)
    assert rolled_back.body == "body should be stored once"
    assert rolled_back.tags == []
    assert rolled_back.strength == original.strength


def test_shared_body_object_survives_last_current_copy_delete(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "shared-body")
    repository.add("First", "shared body", memory_id="first")
    repository.add("Second", "shared body", memory_id="second")
    prior_hash = hashlib.sha256(repository._current_path("second").read_bytes()).hexdigest()
    repository.update("second", body="new body")
    assert not list((repository.history_dir / "bodies").glob("*.txt"))
    repository.delete("first")
    restored = repository._read_version("second", prior_hash).decode("utf-8")
    assert repository.parse(restored, expected_id="second").body == "shared body"


def test_metadata_delta_body_base_is_not_extra_rollback_retention(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "retention")
    old_body = "large body " + "x" * 1000
    repository.add("title", old_body, memory_id="note")
    repository.update("note", tags=["one"])
    repository.update("note", tags=["two"])
    repository.update("note", tags=["three"])
    old_events = [event for event in repository.events() if event.memory_id == "note"]
    initial_hash = old_events[0].after_hash or ""
    retained_metadata_hash = old_events[-1].after_hash or ""
    retained_delta = json.loads(
        repository._history_metadata_path("note", retained_metadata_hash).read_text(
            encoding="utf-8"
        )
    )
    body_hash = retained_delta["body_hash"]
    repository.update("note", body="new body")

    # The shared body CAS object remains while the old full Markdown version
    # is pruned; the body dependency does not create another rollback slot.
    assert repository._body_object_path(body_hash).exists()
    historical = repository._read_version("note", retained_metadata_hash)
    assert repository.parse(historical.decode("utf-8"), expected_id="note").body == old_body
    assert not repository._has_version("note", initial_hash)
    with pytest.raises(DeletedContentUnavailable, match="outside configured rollback retention"):
        repository.rollback(old_events[1].event_id)
    assert repository.get("note").body == "new body"
    latest_events = [event for event in repository.events() if event.memory_id == "note"]
    restored = repository.rollback(latest_events[-1].event_id)
    assert restored.tags == ["three"]


def test_metadata_history_preserves_exact_noncanonical_external_markdown_bytes(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "noncanonical-history")
    document = repository.add("Original title", "first line\nsecond line", memory_id="note")
    path = repository._current_path(document.id)
    canonical = path.read_bytes().decode("utf-8")
    # A valid external edit changes YAML order, line endings, and body whitespace.
    _, _, remainder = canonical.partition("---\n")
    frontmatter, _, body = remainder.partition("\n---\n\n")
    raw = (
        "---\r\n# preserved external comment\r\n"
        + frontmatter.replace("\n", "\r\n")
        + "\r\n---\r\n\r\n"
        + body.replace("\n", "\r\n").rstrip("\r\n")
        + "  \r\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    issues = repository.fsck()
    assert issues[0]["kind"] == "external_edit_registered"
    external_hash = hashlib.sha256(raw).hexdigest()

    repository.update(document.id, title="Updated title")

    assert repository._read_version(document.id, external_hash) == raw


def test_schema_one_base_hash_remains_a_pruning_dependency(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "schema-one-history")
    original = repository.add("Original title", "body", memory_id="note")
    base_hash = hashlib.sha256(repository._current_path(original.id).read_bytes()).hexdigest()
    updated = repository.update(original.id, title="Updated title")
    current_raw = repository._current_path(updated.id).read_bytes()
    current_hash = hashlib.sha256(current_raw).hexdigest()
    delta_path = repository._history_metadata_path(updated.id, current_hash)
    delta_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_hash": base_hash,
                "metadata_changes": {"title": "Updated title"},
            }
        ),
        encoding="utf-8",
    )
    repository._read_version(updated.id, current_hash)

    config = repository.read_config()
    config["history_keep"] = 1
    repository.configure(config)
    repository._prune_history(updated.id)

    assert repository._body_source_hash(updated.id, current_hash) == base_hash
    assert repository._has_version(updated.id, base_hash)
    assert repository._read_version(updated.id, base_hash)


def test_physical_breakdown_is_nonoverlapping_and_sums_to_total(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "breakdown")
    repository.add("anchor", "protected", memory_id="anchor", layer=Layer.ANCHOR)
    repository.add("regular", "ordinary", memory_id="regular")
    repository.compact_log(keep_recent=1)
    breakdown = measure_physical_breakdown(repository)
    assert sum(breakdown.values()) == sum(
        path.stat().st_blocks * 512
        for path in repository.root.rglob("*")
        if path.is_file()
        and path.name not in {".memfw.lock", ".pending.json"}
        and not path.name.endswith(".tmp")
    )
    assert breakdown["anchors"] > 0
    assert breakdown["events"] > 0
    assert breakdown["memories"] > 0


def test_1000_record_one_mb_budget_compacts_and_preserves_nonanchors_by_strategy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fifo"
    sequence = count()
    clock_tick = count()
    base_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = Repository(
        root,
        clock=lambda: base_time + timedelta(hours=next(clock_tick)),
        id_factory=lambda: f"batch-event-{next(sequence)}",
    )
    repository.init()
    documents = [
        {
            "title": f"Record {index:04d}",
            "body": (f"Record {index:04d} detail " + ("a" * 1000)),
            "memory_id": f"record-{index:04d}",
            "layer": "anchor" if index % 100 == 0 else "current",
            "tags": ["manual"] if index % 4 == 0 else [],
        }
        for index in range(1000)
    ]
    repository.add_many(documents)
    initial_physical = measure_physical_breakdown(repository)
    initial_logical = measure_logical_breakdown(repository)
    physical_memory_bytes = initial_physical["memories"] + initial_physical["anchors"]
    logical_memory_bytes = initial_logical["memories"] + initial_logical["anchors"]
    assert initial_physical["history"] < physical_memory_bytes * 0.1
    assert initial_logical["history"] < logical_memory_bytes * 0.1
    print(
        "initial 1000-record history ratios: "
        f"allocated={initial_physical['history']}/{physical_memory_bytes}, "
        f"logical={initial_logical['history']}/{logical_memory_bytes}"
    )
    second_root = tmp_path / "salience"
    shutil.copytree(root, second_root)

    results: dict[str, set[str]] = {}
    survivor_counts: dict[str, int] = {}
    non_anchor_counts: dict[str, int] = {}
    for target_root, strategy in ((root, "fifo"), (second_root, "salience")):
        current = Repository(
            target_root, clock=lambda: NOW, id_factory=lambda: f"policy-{next(sequence)}"
        )
        config = current.read_config()
        config.update(
            budget_mode="total",
            max_bytes=1_000_000,
            forget_policy=strategy,
            forget_action="hard",
            budget_scope="all",
            log_keep_recent=40,
        )
        current.configure(config)
        result = enforce_budget(current, now=NOW)
        assert result.satisfied_after_apply is True
        assert result.physical_bytes_after is not None and result.physical_bytes_after <= 1_000_000
        current.compact_log(keep_recent=40)
        assert measure_physical_disk_bytes(current.root) <= 1_000_000
        surviving = {document.id for document in current.list()}
        assert any(not identifier.endswith("0000") for identifier in surviving)
        anchors = {f"record-{index:04d}" for index in range(0, 1000, 100)}
        non_anchors = surviving - anchors
        assert non_anchors
        results[strategy] = surviving
        survivor_counts[strategy] = len(surviving)
        non_anchor_counts[strategy] = len(non_anchors)
    assert results["fifo"] != results["salience"]
    print(
        f"1MB total-budget survivors: FIFO={survivor_counts['fifo']} "
        f"(non-anchor={non_anchor_counts['fifo']}), "
        f"salience={survivor_counts['salience']} "
        f"(non-anchor={non_anchor_counts['salience']})"
    )
