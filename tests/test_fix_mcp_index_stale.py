"""Regression tests: the MCP search index notices added and deleted memory files."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

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


# --- I4: index staleness beyond file mtimes -------------------------------------


def test_fts_indexes_a_file_copied_in_with_an_old_mtime(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("keep", "plain note", memory_id="keep")
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(repository.root)
    assert gateway.search("rackspace", mode="fts")["results"] == []

    other = Repository(tmp_path / "other")
    other.init()
    other.add("synced", "rackspace failover runbook", memory_id="synced")
    source = other.memories_dir / "synced.md"
    old = time.time() - 3600
    os.utime(source, (old, old))
    time.sleep(0.01)
    shutil.copy2(source, repository.memories_dir / "synced.md")

    found = gateway.search("rackspace", mode="fts")
    assert [item["id"] for item in found["results"]] == ["synced"]


def test_fts_drops_a_file_deleted_outside_memfw(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("keep", "rackspace failover runbook", memory_id="keep")
    repository.add("gone", "rackspace secret runbook", memory_id="gone")
    MemoryIndex(repository.index_path).rebuild(repository)
    time.sleep(0.01)
    (repository.memories_dir / "gone.md").unlink()

    found = Gateway(repository.root).search("rackspace runbook", mode="fts")
    assert [item["id"] for item in found["results"]] == ["keep"]
    rows = MemoryIndex(repository.index_path).search("rackspace")
    assert "gone" not in json.dumps(rows, default=str)


def test_a_fresh_index_is_not_rebuilt_on_every_search(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("keep", "rackspace failover runbook", memory_id="keep")
    gateway = Gateway(repository.root)
    gateway.search("rackspace", mode="fts")
    first = repository.index_path.stat().st_mtime_ns
    time.sleep(0.01)
    gateway.search("rackspace", mode="fts")
    assert repository.index_path.stat().st_mtime_ns == first


def test_an_unparseable_file_does_not_rebuild_on_every_search(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("keep", "rackspace failover runbook", memory_id="keep")
    MemoryIndex(repository.index_path).rebuild(repository)
    time.sleep(0.01)
    (repository.memories_dir / "broken.md").write_bytes(b"\xff\xfe not utf-8")
    old = time.time() - 3600
    os.utime(repository.memories_dir / "broken.md", (old, old))
    gateway = Gateway(repository.root)
    gateway.search("rackspace", mode="fts")
    first = repository.index_path.stat().st_mtime_ns
    time.sleep(0.01)
    gateway.search("rackspace", mode="fts")
    assert repository.index_path.stat().st_mtime_ns == first
