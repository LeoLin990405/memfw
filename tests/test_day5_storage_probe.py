from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from day5_storage_matrix import _write_fixture  # noqa: E402
from day5_storage_probe import build_base  # noqa: E402


def test_storage_probe_replays_creation_and_access_in_timestamp_order(tmp_path: Path) -> None:
    documents, accesses, _labels, metadata = _write_fixture(
        tmp_path / "fixture", count=12, seed=20261018
    )
    document_rows = [json.loads(line) for line in documents.read_text().splitlines()]
    access_rows = [json.loads(line) for line in accesses.read_text().splitlines()]
    created_at = {
        row["memory_id"]: datetime.fromisoformat(row["created_at"]) for row in document_rows
    }
    assert metadata["accesses_after_creation"] is True
    assert all(
        created_at[row["memory_id"]] <= datetime.fromisoformat(row["timestamp"])
        for row in access_rows
    )

    vault = tmp_path / "vault"
    result = build_base(vault, documents, accesses, ROOT / "src")
    assert result["memory_count"] == len(document_rows)
    assert result["access_event_count"] == len(access_rows)
    assert result["physical_categories_sum"] == result["physical_bytes"]

    from memfw.core.repository import Repository

    repository = Repository(vault)
    events = repository.events()
    timestamps = [event.timestamp for event in events]
    assert timestamps == sorted(timestamps)
    creation_by_id = {doc.id: doc.created_at for doc in repository.list()}
    accesses_by_id = {
        event.details["memory_id"]: event.timestamp
        for event in events
        if event.kind.value == "MEMORY_ACCESS"
    }
    assert all(
        creation_by_id[identifier] <= timestamp for identifier, timestamp in accesses_by_id.items()
    )


def test_storage_fixture_has_only_ten_explicit_exact_duplicate_pairs(tmp_path: Path) -> None:
    documents, _accesses, _labels, metadata = _write_fixture(
        tmp_path / "fixture", count=40, seed=20261018
    )
    rows = [json.loads(line) for line in documents.read_text().splitlines()]
    bodies = [row["body"] for row in rows]
    for pair_start in range(0, 20, 2):
        assert bodies[pair_start] == bodies[pair_start + 1]
    assert len(set(bodies[20:])) == 20
    assert set(bodies[:20]).isdisjoint(bodies[20:])
    assert metadata["duplicate_pairs_for_consolidation"] == 10
    assert metadata["deterministic_judge_near_duplicate_candidates"] == 10
