"""Regression tests: MCP boolean arguments are strict booleans."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.core.models import Layer
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.related import RelatedIndex


def _scoped_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "alpha manual",
        "alpha corpus keyword manual note",
        memory_id="mem-alpha",
        tenant="tenant-a",
        source="manual",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    repository.add(
        "beta nas",
        "alpha corpus keyword nas secret",
        memory_id="mem-beta",
        tenant="tenant-a",
        source="nas",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    repository.add(
        "gamma other tenant",
        "alpha corpus keyword other tenant",
        memory_id="mem-gamma",
        tenant="tenant-b",
        source="manual",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    RelatedIndex(repository.root / ".memfw-related.sqlite").rebuild_memory(repository)
    return repository


# --- S7: include_history string flags -----------------------------------------


def test_include_history_string_false_is_rejected_not_truthy(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    handler = Gateway(repository.root, tenant="tenant-a").handlers()["memory_search"]
    with pytest.raises(ValueError):
        handler({"query": "corpus", "mode": "fts", "include_history": "false"})
    rows = handler({"query": "corpus", "mode": "fts", "include_history": False})["results"]
    assert rows
