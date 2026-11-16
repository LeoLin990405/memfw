"""Regression tests: MCP tools honour the bound tenant and MEMFW_ALLOWED_SOURCES."""

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


# --- S2: MEMFW_ALLOWED_SOURCES enforced beyond memory_search ---------------


def test_get_refuses_a_memory_from_a_disallowed_source(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    gateway = Gateway(repository.root, tenant="tenant-a", allowed_sources=("manual",))
    assert gateway.get("mem-alpha")["source"] == "manual"
    with pytest.raises(ValueError):
        gateway.get("mem-beta")


def test_related_hides_disallowed_sources(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    gateway = Gateway(repository.root, tenant="tenant-a", allowed_sources=("manual",))
    # Unscoped, the nas neighbour is related (sanity: the test exercises the filter).
    unscoped = Gateway(repository.root, tenant="tenant-a")
    assert "mem-beta" in {row["id"] for row in unscoped.related("mem-alpha")["related"]}
    ids = {row["id"] for row in gateway.related("mem-alpha")["related"]}
    assert "mem-beta" not in ids
    with pytest.raises(ValueError):
        gateway.related("mem-beta")


def test_context_skips_disallowed_sources(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    gateway = Gateway(repository.root, tenant="tenant-a", allowed_sources=("manual",))
    ids = {entry["id"] for entry in gateway.context()["entries"]}
    assert ids == {"mem-alpha"}
    unscoped = Gateway(repository.root, tenant="tenant-a")
    assert {entry["id"] for entry in unscoped.context()["entries"]} == {"mem-alpha", "mem-beta"}


def test_status_counts_only_the_bound_tenant(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    assert Gateway(repository.root, tenant="tenant-a").status()["memories"] == 2
    assert Gateway(repository.root, tenant="tenant-b").status()["memories"] == 1
    scoped = Gateway(repository.root, tenant="tenant-a", allowed_sources=("manual",))
    status = scoped.status()
    assert status["memories"] == 1
    assert sum(status["layers"].values()) == 1


def test_fts_allowlist_filters_before_the_page_cap(tmp_path: Path) -> None:
    """Disallowed hits that outrank allowed ones must not use up the page."""

    repository = Repository(tmp_path / "vault")
    repository.init()
    for number in range(4):
        repository.add(
            f"loud {number}",
            "zephyr zephyr zephyr zephyr zephyr",
            memory_id=f"loud-{number}",
            source="nas",
        )
    repository.add(
        "quiet",
        "zephyr appears once in a long body " + "filler " * 40,
        memory_id="quiet",
        source="manual",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(repository.root, allowed_sources=("manual",))
    found = gateway.search("zephyr", limit=2, mode="fts")
    assert [item["id"] for item in found["results"]] == ["quiet"]
    # Two allowed sources: merged and still capped at the page.
    both = Gateway(repository.root, allowed_sources=("manual", "nas"))
    rows = both.search("zephyr", limit=3, mode="fts")["results"]
    assert len(rows) == 3
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True)


def test_fts_without_an_allowlist_is_unchanged(tmp_path: Path) -> None:
    repository = _scoped_vault(tmp_path)
    found = Gateway(repository.root, tenant="tenant-a").search("corpus", limit=5, mode="fts")
    assert {item["id"] for item in found["results"]} == {"mem-alpha", "mem-beta"}
