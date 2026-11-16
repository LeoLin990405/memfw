"""Regression tests for event-log integrity defects found in the vault audit."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import zstandard

from memfw.core.errors import IntegrityError
from memfw.core.models import Event, EventKind, Tier
from memfw.core.repository import Repository


def make_repo(root: Path) -> Repository:
    sequence: Iterator[str] = iter(f"event-{index}" for index in range(100000))
    repository = Repository(
        root,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )
    repository.init()
    return repository


def access(repository: Repository, **extra: object) -> Event:
    return repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="human",
        details={"memory_id": "target", "operation": "access", **extra},
    )


# --- A: a deleted events.jsonl must not read as an empty history -------------


@pytest.mark.parametrize("compact", [False, True])
def test_missing_event_log_with_committed_state_is_integrity_error(
    tmp_path: Path, compact: bool
) -> None:
    repository = make_repo(tmp_path / "vault")
    for _ in range(4):
        access(repository)
    if compact:
        repository.compact_log(keep_recent=2)
    repository.events_path.unlink()

    with pytest.raises(IntegrityError, match="Event log is missing"):
        Repository(repository.root).events()


def test_append_after_event_log_deletion_raises_integrity_error(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    for _ in range(4):
        access(repository)
    repository.events_path.unlink()
    reopened = Repository(repository.root)

    with pytest.raises(IntegrityError, match="Event log is missing"):
        access(reopened)
    with pytest.raises(IntegrityError, match="Event log is missing"):
        reopened.add("title", "body", memory_id="one")
    assert not repository.events_path.exists()


def test_missing_event_log_on_empty_vault_still_reads_empty(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.events_path.unlink()
    assert Repository(repository.root).events() == []


# --- B: archived summaries are re-verified and anchored by catalog_hash ------


def _rewrite_summaries(
    repository: Repository,
    transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    *,
    reanchor_state: bool,
) -> None:
    """Rewrite the summary container with every hash recomputed (a full forgery)."""
    from memfw.log.store import _canonical_json, _deterministic_tar

    catalog = json.loads(repository.log_catalog_path.read_text(encoding="utf-8"))
    container = repository.log_segments_dir / catalog["container"]["name"]
    tar_bytes = zstandard.ZstdDecompressor().decompress(container.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as archive:
        content = {}
        for name in archive.getnames():
            member = archive.extractfile(name)
            assert member is not None
            content[name] = member.read()
    prefix = "segments/00000000/"
    rows = [json.loads(line) for line in content[prefix + "summaries.jsonl"].splitlines()]
    rows = transform(rows)
    previous = "0" * 64
    lines = []
    for row in rows:
        payload = {key: value for key, value in row.items() if key != "summary_hash"}
        payload["previous_summary_hash"] = previous
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        lines.append(json.dumps({**payload, "summary_hash": digest}, sort_keys=True))
        previous = digest
    summaries = ("\n".join(lines) + "\n").encode()
    manifest = json.loads(content[prefix + "manifest.json"])
    manifest["summaries_hash"] = hashlib.sha256(summaries).hexdigest()
    manifest["last_summary_hash"] = previous
    manifest_bytes = _canonical_json(manifest)
    content[prefix + "summaries.jsonl"] = summaries
    content[prefix + "manifest.json"] = manifest_bytes
    order = ["summaries.jsonl", "snapshot.json", "manifest.json"]
    compressed = zstandard.ZstdCompressor(
        level=19, write_checksum=True, write_content_size=True
    ).compress(_deterministic_tar([(prefix + name, content[prefix + name]) for name in order]))
    container.write_bytes(compressed)
    catalog["container"]["compressed_hash"] = hashlib.sha256(compressed).hexdigest()
    catalog["segments"][0]["manifest_hash"] = hashlib.sha256(manifest_bytes).hexdigest()
    catalog["segments"][0]["summary_hash"] = previous
    catalog_bytes = _canonical_json(catalog)
    repository.log_catalog_path.write_bytes(catalog_bytes)
    if reanchor_state:
        state = json.loads(repository.event_state_path.read_text(encoding="utf-8"))
        state["catalog_hash"] = hashlib.sha256(catalog_bytes).hexdigest()
        repository.event_state_path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )


def _forge_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["event"]["actor"] = "agent:evil"
    rows[0]["event"]["details"]["operation"] = "forged"
    return rows


def _forge_first_as_lossy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A forger who also flips the lossless flag defeats per-row re-verification;
    # the catalog_hash anchor in events.state.json must still catch it.
    rows = _forge_first(rows)
    rows[0]["lossless"] = False
    return rows


def _strip_to_legacy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"event": row["event"], "summary_hash": row["summary_hash"]} for row in rows]


def _compacted(tmp_path: Path) -> Repository:
    repository = make_repo(tmp_path / "vault")
    for _ in range(4):
        access(repository)
    repository.compact_log(keep_recent=1)
    return repository


def test_forged_summary_container_is_rejected_by_catalog_anchor(tmp_path: Path) -> None:
    repository = _compacted(tmp_path)
    _rewrite_summaries(repository, _forge_first_as_lossy, reanchor_state=False)

    with pytest.raises(IntegrityError, match="catalog_hash"):
        Repository(repository.root).events()


def test_append_does_not_reseed_cursor_over_forged_catalog(tmp_path: Path) -> None:
    repository = _compacted(tmp_path)
    state_before = repository.event_state_path.read_bytes()
    _rewrite_summaries(repository, _forge_first_as_lossy, reanchor_state=False)

    with pytest.raises(IntegrityError, match="catalog_hash"):
        access(Repository(repository.root))
    assert repository.event_state_path.read_bytes() == state_before


def test_forged_lossless_summary_is_rejected_even_when_state_is_reanchored(
    tmp_path: Path,
) -> None:
    repository = _compacted(tmp_path)
    _rewrite_summaries(repository, _forge_first, reanchor_state=True)

    with pytest.raises(IntegrityError, match="original event"):
        Repository(repository.root).events()


def test_new_summary_rows_carry_original_event_digest(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    for _ in range(3):
        access(repository)
    originals = repository.events()
    repository.compact_log(keep_recent=1)
    seen: list[dict[str, Any]] = []

    def capture(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen.extend(rows)
        return rows

    _rewrite_summaries(repository, capture, reanchor_state=True)
    assert [row["original_event_digest"] for row in seen] == [
        event.event_hash for event in originals[:2]
    ]
    assert all(row["lossless"] is True for row in seen)
    # Re-compaction re-summarizes archived summaries without losing the digest.
    access(repository)
    repository.compact_log(keep_recent=1)
    assert [event.event_hash for event in repository.events()[:3]] == [
        event.event_hash for event in originals
    ]


def test_legacy_summary_rows_without_digest_still_load(tmp_path: Path) -> None:
    repository = _compacted(tmp_path)
    expected = [event.event_hash for event in repository.events()]
    _rewrite_summaries(repository, _strip_to_legacy, reanchor_state=True)

    reopened = Repository(repository.root)
    assert [event.event_hash for event in reopened.events()] == expected
    access(reopened)
    assert len(reopened.events()) == len(expected) + 1


def test_legacy_summary_rows_still_require_catalog_anchor(tmp_path: Path) -> None:
    repository = _compacted(tmp_path)
    _rewrite_summaries(
        repository, lambda rows: _strip_to_legacy(_forge_first(rows)), reanchor_state=False
    )

    with pytest.raises(IntegrityError, match="catalog_hash"):
        Repository(repository.root).events()


def test_crash_after_log_truncation_needs_explicit_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repo(tmp_path / "vault")
    for _ in range(4):
        access(repository)
    expected = [event.event_hash for event in repository.events()]

    def crash(*args: object, **kwargs: object) -> None:
        raise OSError("injected crash before state rewrite")

    monkeypatch.setattr(repository, "_seed_event_cursor", crash)
    with pytest.raises(OSError, match="before state rewrite"):
        repository.compact_log(keep_recent=1)
    monkeypatch.undo()

    reopened = Repository(repository.root)
    with pytest.raises(IntegrityError, match="catalog_hash"):
        reopened.events()
    assert reopened.recover_pending() == ["events.jsonl"]
    assert [event.event_hash for event in reopened.events()] == expected


# --- C: summaries keep the detail keys recovery reads back --------------------


def test_compaction_keeps_config_hashes_read_by_configure_recovery(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    config = repository.read_config()
    repository.configure({**config, "history_keep": 3})
    for _ in range(2):
        access(repository)
    original = next(e for e in repository.events() if e.kind == EventKind.CONFIG_CHANGE)
    repository.compact_log(keep_recent=1)

    archived = next(e for e in repository.events() if e.kind == EventKind.CONFIG_CHANGE)
    assert archived.is_compacted_summary
    assert archived.details["after_config_hash"] == original.details["after_config_hash"]
    assert archived.details["before_config_hash"] == original.details["before_config_hash"]


def test_compaction_keeps_cold_stub_storage_marker(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    repository.add("cold source", "body " * 50, memory_id="cold-source", tier=Tier.COLD)
    repository.cold_archive("archive-c")
    access(repository)
    original = [e.details.get("storage") for e in repository.events()]
    assert "cold_stub" in original
    repository.compact_log(keep_recent=1)

    archived = repository.events()
    assert archived[0].is_compacted_summary
    assert [e.details.get("storage") for e in archived] == original


def test_compaction_keeps_proposal_snapshot_hash_mode(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "vault")
    details = {"proposal_id": "p-1", "target_snapshot_hash_mode": "source_semantic_v1"}
    with repository._locked():
        repository._append_event(
            EventKind.AGENT_PROPOSAL, "proposal", actor="agent:test", details=details
        )
    access(repository)
    repository.compact_log(keep_recent=1)

    archived = repository.events()[0]
    assert archived.is_compacted_summary
    assert archived.details == details
