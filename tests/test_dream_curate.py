from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.dream.curate import curate

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _doc(memory_id: str, body: str, **fields: Any) -> MemoryDocument:
    stamp = fields.pop("updated_at", NOW - timedelta(days=1))
    values: dict[str, Any] = {
        "id": memory_id,
        "title": fields.pop("title", "note"),
        "body": body,
        "about": fields.pop("about", ["concept-a"]),
        "created_at": fields.pop("created_at", stamp),
        "updated_at": stamp,
        "reference_count": fields.pop("reference_count", 1),
    }
    values.update(fields)
    return MemoryDocument(**values)


def _actions(candidates: list[Any], action: str) -> list[Any]:
    return [item for item in candidates if item.action == action]


def test_identical_and_near_duplicates_merge_into_earliest() -> None:
    docs = [
        _doc("m1", "The build cache lives in the shared volume.", updated_at=NOW - timedelta(3)),
        _doc("m2", "the build cache  lives in the shared volume.", updated_at=NOW - timedelta(1)),
        _doc(
            "m3",
            "The build cache lives in the shared volume, really.",
            updated_at=NOW - timedelta(2),
        ),
    ]
    merges = _actions(curate(docs, now=NOW), "merge")
    assert len(merges) == 1
    merge = merges[0]
    assert merge.operation == "curate"
    assert merge.memory_ids == ["m1", "m2", "m3"]
    assert merge.changes == {"merge_into": "m1"}
    assert merge.oid == "concept-a"


def test_merge_tie_keeps_smallest_id() -> None:
    docs = [_doc("b", "same words here"), _doc("a", "same words here")]
    (merge,) = _actions(curate(docs, now=NOW), "merge")
    assert merge.memory_ids == ["a", "b"]


def test_different_concepts_are_not_merged() -> None:
    docs = [
        _doc("m1", "The build cache lives in the shared volume.", about=["concept-a"]),
        _doc("m2", "The build cache lives in the shared volume.", about=["concept-b"]),
        _doc("m3", "The build cache lives in the shared volume.", about=[]),
    ]
    assert _actions(curate(docs, now=NOW), "merge") == []


def test_key_value_conflict_newer_supersedes_older() -> None:
    older = _doc(
        "old",
        "Retry Count: 3\nowner = team red",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _doc(
        "new",
        "retry-count: 5\nOwner = team red",
        valid_from=datetime(2026, 3, 1, tzinfo=UTC),
    )
    (item,) = _actions(curate([older, newer], now=NOW), "supersede")
    assert item.memory_ids == ["new", "old"]
    assert item.changes == {
        "superseded_by": "new",
        "valid_to": datetime(2026, 3, 1, tzinfo=UTC).isoformat(),
    }
    assert "retry_count" in item.reason
    assert "human" in item.reason.lower()
    assert _actions(curate([older, newer], now=NOW), "merge") == []


def test_prose_numeric_conflict_supersedes() -> None:
    older = _doc("p1", "sensor reads 18 degrees", updated_at=NOW - timedelta(days=10))
    newer = _doc("p2", "sensor reads 21 degrees", updated_at=NOW - timedelta(days=2))
    (item,) = _actions(curate([older, newer], now=NOW), "supersede")
    assert item.memory_ids == ["p2", "p1"]
    assert item.oid == "concept-a"
    assert item.changes["superseded_by"] == "p2"
    assert item.changes["valid_to"] == newer.updated_at.isoformat()


def test_already_superseded_pair_not_reproposed() -> None:
    older = _doc(
        "p1",
        "sensor reads 18 degrees",
        updated_at=NOW - timedelta(days=10),
        superseded_by="p2",
        valid_to=NOW - timedelta(days=2),
    )
    newer = _doc("p2", "sensor reads 21 degrees", updated_at=NOW - timedelta(days=2))
    candidates = curate([older, newer], now=NOW)
    assert _actions(candidates, "supersede") == []
    assert _actions(candidates, "mark-stale") == []


def test_superseded_without_valid_to_marked_stale() -> None:
    newer = _doc("n", "fresh fact", valid_from=datetime(2026, 4, 1, tzinfo=UTC))
    older = _doc("o", "old fact entirely different", superseded_by="n")
    (item,) = _actions(curate([older, newer], now=NOW), "mark-stale")
    assert item.memory_ids == ["o"]
    assert item.changes == {"valid_to": datetime(2026, 4, 1, tzinfo=UTC).isoformat()}


def test_superseded_by_unknown_memory_is_not_marked() -> None:
    older = _doc("o", "old fact", superseded_by="missing")
    assert _actions(curate([older], now=NOW), "mark-stale") == []


def test_old_unreferenced_unconfirmed_marked_stale_only() -> None:
    old = NOW - timedelta(days=400)
    docs = [
        _doc("stale", "alpha topic", about=["c1"], updated_at=old, reference_count=0),
        _doc(
            "confirmed",
            "beta topic",
            about=["c2"],
            updated_at=old,
            reference_count=0,
            status="confirmed",
        ),
        _doc("used", "gamma topic", about=["c3"], updated_at=old, reference_count=4),
        _doc("recent", "delta topic", about=["c4"], reference_count=0),
    ]
    stale = _actions(curate(docs, now=NOW), "mark-stale")
    assert [item.memory_ids for item in stale] == [["stale"]]
    assert stale[0].changes == {"valid_to": NOW.isoformat()}
    assert stale[0].oid == "c1"
    assert _actions(curate(docs, now=NOW, stale_days=500), "mark-stale") == []


def test_output_is_deterministic_and_sorted() -> None:
    old = NOW - timedelta(days=400)
    docs = [
        _doc("z", "sensor reads 18 degrees", about=["b"], updated_at=old, reference_count=0),
        _doc("y", "sensor reads 21 degrees", about=["b"]),
        _doc("x", "same words here", about=["a"]),
        _doc("w", "same words here", about=["a"]),
    ]
    first = [item.as_dict() for item in curate(docs, now=NOW)]
    second = [item.as_dict() for item in curate(list(reversed(docs)), now=NOW)]
    assert first == second
    keys = [(item["oid"], item["action"], item["memory_ids"]) for item in first]
    assert keys == sorted(keys)
    assert {item["action"] for item in first} == {"merge", "supersede", "mark-stale"}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_curate_never_modifies_repository(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    repository.init()
    repository.add(title="greenhouse", body="The greenhouse sensor reads 18 degrees.")
    repository.add(title="greenhouse", body="The greenhouse sensor reads 21 degrees.")
    repository.add(title="greenhouse", body="The greenhouse sensor reads 21 degrees.")
    documents = Repository(tmp_path).list()
    linked = [doc.model_copy(update={"about": ["greenhouse"]}) for doc in documents]
    before = _snapshot(tmp_path)
    dumped = [doc.model_dump() for doc in linked]

    candidates = curate(linked, now=NOW)

    assert {item.action for item in candidates} >= {"merge", "supersede"}
    assert _snapshot(tmp_path) == before
    assert [doc.model_dump() for doc in linked] == dumped


def test_paraphrases_with_the_same_values_merge() -> None:
    docs = [
        _doc("p1", "The service listens on port 80.", updated_at=NOW - timedelta(5)),
        _doc("p2", "Service port is 80.", updated_at=NOW - timedelta(2)),
        _doc("q1", "The lamp colour is blue.", about=["concept-lamp"]),
        _doc("q2", "Lamp color is blue.", about=["concept-lamp"]),
        _doc("r1", "The badge color is red.", about=["concept-badge"]),
        _doc("r2", "The badge size is small.", about=["concept-badge"]),
    ]
    merges = {tuple(item.memory_ids) for item in _actions(curate(docs, now=NOW), "merge")}
    assert ("p1", "p2") in merges
    assert ("q1", "q2") in merges
    assert not any("r1" in pair for pair in merges)


def test_prose_attribute_conflict_is_detected() -> None:
    docs = [
        _doc("c1", "The cluster color is blue.", updated_at=NOW - timedelta(4)),
        _doc("c2", "The cluster color is green.", updated_at=NOW - timedelta(1)),
    ]
    (supersede,) = _actions(curate(docs, now=NOW), "supersede")
    assert supersede.memory_ids == ["c2", "c1"]
    assert "cluster_color" in supersede.reason
