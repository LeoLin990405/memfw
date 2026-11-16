"""Regressions for rollback, delete, and supersession integrity in operations."""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.core.epistemic import CausalRecord
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.storage.markdown import _split_raw_markdown

NOW = datetime(2026, 10, 18, tzinfo=UTC)

VERIFIED_RECORD: dict[str, Any] = {
    "treatment": "assignment A vs B",
    "outcome": "synthetic outcome",
    "estimand": "ATE of assignment on the outcome",
    "identification": {
        "assumptions": ["randomised assignment"],
        "untested": [],
    },
    "robustness": [{"check": "covariate balance", "result": "balanced", "failed": False}],
}


def _repo(tmp_path: Path) -> Repository:
    ids = (f"ev-{i}" for i in itertools.count())
    repository = Repository(tmp_path / "vault", clock=lambda: NOW, id_factory=lambda: next(ids))
    repository.init()
    return repository


def _last_event(repository: Repository, memory_id: str) -> str:
    return [e for e in repository.events() if e.memory_id == memory_id][-1].event_id


# --- fix 4: rollback restores every document field ------------------------


def test_rollback_restores_every_document_field(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("target", "t", memory_id="t1")
    repository.add("other", "o", memory_id="o1")
    repository.add(
        "title",
        "body",
        memory_id="m1",
        memory_type="claim",
        tenant="team-a",
        related=["o1"],
        epistemic="fact",
        causal="hypothesis",
        causal_record={
            "treatment": "x",
            "outcome": "y",
            "identification": {"assumptions": ["a"], "untested": ["b"]},
        },
        limitations=["only on one laptop"],
        evidence=["data:one"],
    )
    before = repository.get("m1")
    repository.update(
        "m1",
        type="note",
        tenant="team-b",
        related=["t1"],
        valid_from=NOW - timedelta(days=3),
        valid_to=NOW,
        superseded_by="t1",
        status="disputed",
        evidence=["data:two"],
        epistemic="judgment",
        causal="none",
        causal_record=None,
        limitations=["changed"],
    )
    repository.rollback(_last_event(repository, "m1"))
    after = repository.get("m1")
    for field in (
        "type",
        "tenant",
        "related",
        "valid_from",
        "valid_to",
        "superseded_by",
        "status",
        "evidence",
        "epistemic",
        "causal",
        "causal_record",
        "limitations",
    ):
        assert getattr(after, field) == getattr(before, field), field
    assert after.version == before.version + 2
    assert after.created_at == before.created_at


def test_rollback_restores_experience(tmp_path: Path) -> None:
    from memfw.core.epistemic import EXPERIENCE_DISCLAIMER

    repository = _repo(tmp_path)
    record = {
        "task": "fix lint",
        "conditions": ["repo clean"],
        "actions": ["run ruff"],
        "review": {"reviewer": "reviewer", "verdict": "accepted"},
    }
    repository.add(
        "experience",
        EXPERIENCE_DISCLAIMER + "\n\nran the checklist",
        memory_id="x1",
        memory_type="experience-point",
        epistemic="experience",
        experience=record,
    )
    before = repository.get("x1")
    repository.update("x1", experience={**record, "task": "changed task"})
    assert repository.get("x1").experience != before.experience
    repository.rollback(_last_event(repository, "x1"))
    assert repository.get("x1").experience == before.experience


def test_rollback_of_supersede_update_clears_link(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("old", "o", memory_id="s1")
    repository.supersede("s1", "new", "n", memory_id_new="s2")
    updates = [e for e in repository.events() if e.kind == EventKind.UPDATE]
    repository.rollback([e for e in updates if e.memory_id == "s1"][-1].event_id)
    restored = repository.get("s1")
    assert restored.valid_to is None
    assert restored.superseded_by is None


def test_human_rollback_counts_as_human_decision(tmp_path: Path) -> None:
    """Rolling back to a chat-sourced verified state needs a human decision."""
    repository = _repo(tmp_path)
    repository.add("claim", "body", memory_id="c1", source="chat")
    # Reach the state the way an approved proposal would: register it as an
    # external edit, which records the bytes without the write gate.
    current = repository.get("c1")
    verified = current.model_copy(
        update={
            "epistemic": "inference",
            "causal": "verified",
            "causal_record": CausalRecord.model_validate(VERIFIED_RECORD),
            "evidence": ["https://example.test/analysis"],
            "version": current.version + 1,
        }
    )
    path = repository.root / "memories" / "c1.md"
    path.write_text(repository.render(verified), encoding="utf-8")
    repository.fsck()
    assert repository.get("c1").causal == "verified"
    repository.update("c1", causal="hypothesis")
    repository.rollback(_last_event(repository, "c1"), actor="human")
    restored = repository.get("c1")
    assert restored.causal == "verified"
    assert restored.evidence == ["https://example.test/analysis"]


# --- fix 5: delete leaves no forgotten body object on disk ----------------


def _files_containing(root: Path, needle: bytes) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_bytes()
    )


def test_delete_erases_history_body_objects(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    secret = b"SECRET-ORIGINAL-SSN-123"
    repository.add("t", secret.decode(), memory_id="m1")
    repository.update("m1", body="v2 harmless")
    assert _files_containing(repository.root, secret)
    repository.delete("m1")
    assert _files_containing(repository.root, secret) == []
    # Later unrelated writes must not resurrect it either.
    repository.add("other", "unrelated", memory_id="m2")
    repository.update("m2", title="x")
    assert _files_containing(repository.root, secret) == []


# --- fix 7: superseded_by integrity ------------------------------------------


def test_update_rejects_missing_self_and_cyclic_superseded_by(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("a", "A body", memory_id="a1")
    repository.add("b", "B body", memory_id="b1")
    repository.add("c", "C body", memory_id="c1")
    with pytest.raises(IntegrityError, match="missing memory"):
        repository.update("a1", superseded_by="does-not-exist")
    with pytest.raises(IntegrityError, match="itself"):
        repository.update("a1", superseded_by="a1")
    with pytest.raises(IntegrityError, match="itself"):
        repository.update("a1", superseded="a1")
    repository.update("b1", superseded_by="a1")
    with pytest.raises(IntegrityError, match="cycle"):
        repository.update("a1", superseded_by="b1")
    repository.update("c1", superseded_by="b1")
    with pytest.raises(IntegrityError, match="cycle"):
        repository.update("a1", superseded_by="c1")
    assert repository.get("a1").superseded_by is None


def test_delete_clears_superseded_by_back_refs(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("x", "X", memory_id="x1")
    repository.supersede("x1", "x v2", "X2", memory_id_new="x2")
    repository.delete("x2")
    assert repository.get("x1").superseded_by is None
    last = [e for e in repository.events() if e.memory_id == "x1"][-1]
    assert last.kind == EventKind.UPDATE
    assert not [i for i in repository.fsck() if i["kind"] == "dangling_superseded_by"]


def _external_edit(repository: Repository, memory_id: str, **changes: Any) -> None:
    current = repository.get(memory_id)
    edited = current.model_copy(update={**changes, "version": current.version + 1})
    path = repository.root / "memories" / f"{memory_id}.md"
    path.write_text(repository.render(edited), encoding="utf-8")


def test_fsck_reports_dangling_superseded_by(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("a", "A body", memory_id="a1")
    _external_edit(repository, "a1", superseded_by="gone")
    issues = [i for i in repository.fsck() if i["kind"] == "dangling_superseded_by"]
    assert [i["memory_id"] for i in issues] == ["a1"]
    assert "gone" in str(issues[0]["message"])
    assert set(issues[0]) >= {"memory_id", "kind", "expected_hash", "actual_hash"}


def test_fsck_reports_cyclic_superseded_by(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("a", "A body", memory_id="a1")
    repository.add("b", "B body", memory_id="b1")
    repository.update("a1", superseded_by="b1")
    _external_edit(repository, "b1", superseded_by="a1")
    issues = [i for i in repository.fsck() if i["kind"] == "cyclic_superseded_by"]
    assert sorted(i["memory_id"] for i in issues) == ["a1", "b1"]


# --- review nits: stale rollback target, contained delete cleanup ---------


def test_rollback_onto_superseded_by_deleted_memory_names_the_cause(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("target", "t", memory_id="t1")
    repository.add("note", "n", memory_id="m1")
    repository.update("m1", actor="human", superseded_by="t1")
    repository.update("m1", actor="human", superseded_by=None)
    unlink_event = _last_event(repository, "m1")
    repository.delete("t1", actor="human")
    before = (repository.memories_dir / "m1.md").read_bytes()
    with pytest.raises(
        IntegrityError, match=r"superseded_by t1.*deleted memory.*superseded_by=None"
    ):
        repository.rollback(unlink_event)
    assert (repository.memories_dir / "m1.md").read_bytes() == before


def test_delete_cleanup_failure_is_contained_and_fsck_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    repository.add("replacement", "r", memory_id="r1")
    repository.add("old a", "a", memory_id="a1")
    repository.add("old b", "b", memory_id="b1")
    repository.update("a1", actor="human", superseded_by="r1")
    repository.update("b1", actor="human", superseded_by="r1")
    repository.update("b1", actor="human", body="b replaced FORGET-ME-BODY")
    repository.update("b1", actor="human", body="b final")

    real_update = Repository.update
    real_prune = Repository._prune_body_objects  # type: ignore[attr-defined]

    def failing_update(self: Repository, memory_id: str, *args: Any, **kwargs: Any) -> Any:
        if memory_id == "a1" and kwargs.get("superseded_by", "x") is None:
            raise RuntimeError("injected cleanup failure")
        return real_update(self, memory_id, *args, **kwargs)

    def failing_prune(self: Repository) -> None:
        raise OSError("injected prune failure")

    monkeypatch.setattr(Repository, "update", failing_update)
    monkeypatch.setattr(Repository, "_prune_body_objects", failing_prune)
    repository.delete("r1", actor="human")  # must not raise after DELETE commits

    # The ref after the failing one was still cleared.
    assert repository.get("b1").superseded_by is None
    assert repository.get("a1").superseded_by == "r1"
    assert not (repository.memories_dir / "r1.md").exists()

    monkeypatch.setattr(Repository, "update", real_update)
    monkeypatch.setattr(Repository, "_prune_body_objects", real_prune)
    issues = repository.fsck()
    assert {"memory_id": "a1", "kind": "deleted_superseded_by_cleared"}.items() <= next(
        i for i in issues if i["kind"] == "deleted_superseded_by_cleared"
    ).items()
    assert repository.get("a1").superseded_by is None
    assert not [i for i in repository.fsck() if "superseded_by" in str(i["kind"])]


def test_delete_back_ref_cleanup_does_not_parse_every_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf guard: delete must not YAML-parse the whole vault (was O(N) per delete)."""
    repository = _repo(tmp_path)
    repository.add_many(
        [{"title": f"n{i}", "body": f"body {i}", "memory_id": f"n{i:03d}"} for i in range(200)]
    )
    repository.update("n001", actor="human", superseded_by="n000")
    calls = 0
    real_parse = Repository.parse

    def counting_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(Repository, "parse", staticmethod(counting_parse))
    repository.delete("n000", actor="human")
    assert repository.get("n001").superseded_by is None
    # The old code parsed every memory (>200 parses for 200 memories); the
    # prefilter parses only files that mention the deleted id, and the shared
    # body lookup hashes before it parses. The count no longer depends on the
    # filesystem's directory order, so the bound is tight.
    assert calls < 15, calls


def test_body_lookup_parses_only_hash_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared-body lookup must not YAML-parse files whose body differs."""
    repository = _repo(tmp_path)
    repository.add_many(
        [{"title": f"n{i}", "body": f"body {i}", "memory_id": f"n{i:03d}"} for i in range(200)]
    )
    target = (repository.memories_dir / "n150.md").read_bytes()
    _, body = _split_raw_markdown(target)
    calls = 0
    real_parse = Repository.parse

    def counting_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(Repository, "parse", staticmethod(counting_parse))
    found = repository._current_body_by_hash(hashlib.sha256(body).hexdigest())
    assert found == body
    assert calls == 1, calls
    assert repository._current_body_by_hash("0" * 64) is None
    assert calls == 1, calls
