"""Multihop must not return a memory whose file was deleted outside memfw."""

from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.retrieval.multihop import search_multihop


def test_externally_deleted_memory_is_dropped(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("keep", "rackspace failover runbook", memory_id="keep", tenant="")
    repository.add("gone", "rackspace secret runbook to be removed", memory_id="gone", tenant="")
    MemoryIndex(repository.index_path).rebuild(repository)

    before = search_multihop(repository, "rackspace runbook", tenant="")
    assert {item["id"] for item in before["results"]} == {"keep", "gone"}

    (repository.memories_dir / "gone.md").unlink()  # sync / git pull / rm
    after = search_multihop(repository, "rackspace runbook", tenant="")
    assert [item["id"] for item in after["results"]] == ["keep"]
    assert search_multihop(repository, "gone", tenant="")["results"] == []
