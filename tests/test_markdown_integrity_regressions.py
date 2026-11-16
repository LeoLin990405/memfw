"""Regression tests for Markdown frontmatter parsing and shared body objects."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from memfw.core.models import EventKind, Tier
from memfw.core.repository import Repository

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def make_repo(root: Path) -> Repository:
    ids = (f"md-integrity-event-{index}" for index in count())
    repository = Repository(root, clock=lambda: NOW, id_factory=lambda: next(ids))
    repository.init()
    return repository


DASH_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("tag", {"title": "t", "tags": ["x---"]}, {}),
    ("title", {"title": "Q3---"}, {}),
    ("inner-title", {"title": "a --- b"}, {}),
    ("summary", {"title": "t"}, {"summary": "foo ---"}),
    ("tag-via-update", {"title": "t"}, {"tags": ["y---", "z"], "title": "T---"}),
]


def _assert_fields(document: Any, expected: dict[str, Any]) -> None:
    for key, value in expected.items():
        assert getattr(document, key) == value, key


@pytest.mark.parametrize(
    ("label", "added", "changed"), DASH_CASES, ids=[case[0] for case in DASH_CASES]
)
def test_frontmatter_values_ending_in_dashes_roundtrip(
    tmp_path: Path, label: str, added: dict[str, Any], changed: dict[str, Any]
) -> None:
    repository = make_repo(tmp_path / label)
    repository.add(body="real body", memory_id="dash", **added)
    if changed:
        repository.update("dash", **changed)
    expected = {"tags": [], **added, **changed}
    path = repository._current_path("dash")
    written = path.read_bytes()

    # Parse -> render of the written file is byte-identical.
    parsed = repository.parse(written.decode("utf-8"), expected_id="dash")
    assert repository.render(parsed).encode("utf-8") == written
    _assert_fields(parsed, expected)

    fetched = repository.get("dash")
    _assert_fields(fetched, expected)
    assert fetched.body == "real body"

    # A later update must not persist truncated frontmatter.
    repository.update("dash", body="new body")
    updated = repository.get("dash")
    _assert_fields(updated, expected)
    assert updated.body == "new body"

    # Reopening the vault from disk sees the same values.
    reopened = Repository(repository.root, clock=lambda: NOW).get("dash")
    _assert_fields(reopened, expected)
    assert reopened.body == "new body"
    assert not [issue for issue in repository.fsck() if "dash" in str(issue)]


def test_parse_splits_on_closing_line_only() -> None:
    text = (
        "---\nid: m\ntitle: Q3---\ncreated_at: 2026-10-18T00:00:00Z\n"
        "updated_at: 2026-10-18T00:00:00Z\n"
        "tags:\n- x---\n---\n\nbody --- here\n---\nmore\n"
    )
    document = Repository.parse(text, expected_id="m")
    assert document.title == "Q3---"
    assert document.tags == ["x---"]
    assert document.body == "body --- here\n---\nmore"


def _shared_body_after_archive(tmp_path: Path) -> tuple[Repository, str]:
    repository = make_repo(tmp_path / "shared-archive")
    repository.add("a", "SHARED-BODY-X", memory_id="a1")
    repository.update("a1", body="a new body")
    update = [
        event
        for event in repository.events()
        if event.memory_id == "a1" and event.kind == EventKind.UPDATE
    ][-1]
    repository.add("b", "SHARED-BODY-X", memory_id="b1")
    # Pruning drops the body object while b1 holds a live copy.
    repository.update("b1", title="b2")
    repository.update("b1", tier=Tier.COLD)
    repository.cold_archive("arch1", memory_ids={"b1"})
    assert repository.get("b1").is_stub
    return repository, update.event_id


def test_rollback_restores_body_shared_with_cold_archived_memory(tmp_path: Path) -> None:
    repository, update_event = _shared_body_after_archive(tmp_path)
    restored = repository.rollback(update_event)
    assert restored.body == "SHARED-BODY-X"
    assert repository.get("a1").body == "SHARED-BODY-X"


def test_shared_body_survives_erasure_of_cold_archived_memory(tmp_path: Path) -> None:
    repository, update_event = _shared_body_after_archive(tmp_path)
    repository.delete("b1")
    repository._prune_body_objects()
    restored = repository.rollback(update_event)
    assert restored.body == "SHARED-BODY-X"


def test_archived_body_lookup_searches_other_members_by_hash(tmp_path: Path) -> None:
    repository, _ = _shared_body_after_archive(tmp_path)
    body_hash = hashlib.sha256(b"SHARED-BODY-X\n").hexdigest()
    # Remove the materialized object so only b1's archived file holds the bytes.
    repository._body_object_path(body_hash).unlink(missing_ok=True)
    assert repository._archived_body_by_hash("a1", body_hash) == b"SHARED-BODY-X\n"
    assert repository._archived_body_by_hash("a1", "0" * 64) is None


def test_prune_keeps_body_objects_referenced_by_other_memories(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "prune-shared")
    repository.add("a", "KEEP-ME", memory_id="a1")
    repository.update("a1", body="a v2")
    repository.add("b", "KEEP-ME", memory_id="b1")
    first = next(
        event
        for event in repository.events()
        if event.memory_id == "a1" and event.kind == EventKind.UPDATE
    )
    repository.update("b1", body="b v2")
    repository.delete("b1")
    repository._prune_body_objects()
    assert repository.rollback(first.event_id).body == "KEEP-ME"
