"""A memory with superseded_by but no valid_to is history, not current."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider
from memfw.retrieval.multihop import search_multihop


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("new", "rackspace failover runbook v2", memory_id="new", tenant="")
    repository.add("old", "rackspace failover runbook v1", memory_id="old", tenant="")
    path = repository.memories_dir / "old.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\n---\n", "\nsuperseded_by: new\n---\n", 1), encoding="utf-8")
    old = repository.get("old")
    assert old.superseded_by == "new"
    assert old.valid_to is None
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def test_multihop_hides_superseded(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    current = search_multihop(repository, "rackspace runbook", tenant="")
    assert [item["id"] for item in current["results"]] == ["new"]
    history = search_multihop(repository, "rackspace runbook", tenant="", include_history=True)
    assert {item["id"] for item in history["results"]} == {"new", "old"}


@pytest.mark.parametrize("mode", ["fts", "hybrid"])
def test_memory_hybrid_hides_superseded(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path)
    retriever = MemoryHybridRetriever(repository, embedder=HashEmbeddingProvider(8))
    retriever.rebuild()
    current = retriever.search("rackspace runbook", limit=5, mode=mode, tenant="")
    assert [item["id"] for item in current["results"]] == ["new"]
    history = retriever.search(
        "rackspace runbook", limit=5, mode=mode, tenant="", include_history=True
    )
    assert {item["id"] for item in history["results"]} == {"new", "old"}
