"""MemoryIndex.search hides a memory superseded without a valid_to (I5 follow-up)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from tests.test_readonly_service import _memory, seed_vault

DOCS = [
    _memory("mem-old", "Old pipeline plan", "pipeline keyword AI", superseded_by="mem-new"),
    _memory("mem-new", "New pipeline plan", "pipeline keyword AI"),
]


@pytest.mark.parametrize("query", ["pipeline", "pipeline keyword", "AI", "AI ML"])
def test_superseded_memory_is_not_current(tmp_path: Path, query: str) -> None:
    root = seed_vault(tmp_path / "vault", DOCS)
    index = MemoryIndex(Repository(root).index_path)
    ids = [identifier for identifier, _title in index.search(query)]
    assert ids == ["mem-new"]
    many = index.search_many([query])[query]
    assert [identifier for identifier, _title in many] == ["mem-new"]
