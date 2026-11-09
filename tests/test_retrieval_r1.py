"""R1: HTTP /search and MCP memory_search share one FTS implementation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from scripts.memfw_readonly_service import ServiceError, read_path, search_vault


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "Alpha",
        "corpus keyword alpha",
        memory_id="mem-alpha",
        tenant="alpha",
        source="manual",
    )
    repository.add(
        "Notes fact",
        "corpus keyword notes",
        memory_id="mem-notes",
        tenant="alpha",
        source="notes",
        provenance="imported",
    )
    repository.add(
        "Other tenant",
        "corpus keyword beta",
        memory_id="mem-beta",
        tenant="beta",
        source="manual",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def test_provenance_type_uses_the_four_discrete_values() -> None:
    assert read_path.provenance_type("human-authored", "manual") == "user_said"
    assert read_path.provenance_type("agent_inferred", "stage") == "agent_inferred"
    assert read_path.provenance_type("imported", "notes") == "imported"
    assert read_path.provenance_type("derived", "manual") == "derived"
    assert read_path.provenance_type("", "nas") == "imported"


def test_http_and_mcp_fts_return_the_same_hits(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    http = search_vault(repository.root, "corpus keyword", limit=10, tenant="alpha")
    mcp = Gateway(repository.root, tenant="alpha").search("corpus keyword", limit=10, mode="fts")
    assert http["mode"] == "fts"
    assert mcp["mode"] == "fts"
    assert [item["id"] for item in http["results"]] == [item["id"] for item in mcp["results"]]
    assert [item["id"] for item in http["results"]] == ["mem-alpha", "mem-notes"]
    keys = (
        "id",
        "content_hash",
        "source",
        "provenance_type",
        "why",
        "index_version",
        "sot_version",
        "stale",
    )
    for left, right in zip(http["results"], mcp["results"], strict=True):
        for key in keys:
            assert left[key] == right[key], key
        assert left["why"] == "lexical"
        assert left["stale"] is False
        assert left["index_version"] == left["sot_version"] == "1"
    by_id = {item["id"]: item for item in http["results"]}
    assert by_id["mem-alpha"]["provenance_type"] == "user_said"
    assert by_id["mem-notes"]["provenance_type"] == "imported"
    assert by_id["mem-alpha"]["content_hash"].startswith("sha256:")


def test_index_version_mismatch_marks_the_hit_stale(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    with sqlite3.connect(repository.index_path) as connection:
        connection.execute("UPDATE memory_rows SET version = 99 WHERE id = 'mem-alpha'")
    http = search_vault(repository.root, "corpus keyword", limit=10, tenant="alpha")
    mcp = Gateway(repository.root, tenant="alpha").search("corpus keyword", limit=10, mode="fts")
    alpha = next(item for item in http["results"] if item["id"] == "mem-alpha")
    assert alpha["stale"] is True
    assert alpha["index_version"] == "99"
    assert alpha["sot_version"] == "1"
    mcp_alpha = next(item for item in mcp["results"] if item["id"] == "mem-alpha")
    assert mcp_alpha["stale"] is True
    assert mcp_alpha["index_version"] == alpha["index_version"]


def test_http_mode_helper_refuses_anything_but_fts() -> None:
    from scripts.memfw_readonly_service import reject_unavailable_search_mode

    reject_unavailable_search_mode({})
    reject_unavailable_search_mode({"mode": "fts"})
    with pytest.raises(ServiceError) as caught:
        reject_unavailable_search_mode({"mode": "multihop"})
    assert caught.value.status == 400
    assert caught.value.message == "mode is not available"
