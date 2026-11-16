"""Readonly service: a stale index is reported, never read as "no match".

An index from an earlier schema generation lacks ``memory_temporal`` or
``memory_access``. Reading it used to drop temporal filtering silently (a
superseded memory came back) or return an empty page with ``degraded`` false.
The service checks the schema read-only and answers ``degraded`` true with
the stable ``index_stale`` error. It never rebuilds or writes the index.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from memfw import index as memfw_index
from memfw.index import MemoryIndex
from scripts.memfw_readonly_service import related_vault, search_vault
from tests.test_readonly_service import seed_vault

QUERY = "alpha corpus"


def _alter(path: Path, statement: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _assert_stale(payload: dict) -> None:
    assert payload["degraded"] is True
    assert payload["errors"] == [service.INDEX_STALE]
    assert payload["results"] == []


def test_current_index_is_unchanged(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    payload = search_vault(root, QUERY, tenant="tenant-a")
    assert payload["degraded"] is False
    assert payload["errors"] == []
    assert [item["id"] for item in payload["results"]] == ["mem-alpha", "mem-beta"]


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE memory_temporal",
        "DROP TABLE memory_access",
        "PRAGMA user_version = 1",
    ],
)
def test_stale_index_is_reported_and_not_rewritten(tmp_path: Path, statement: str) -> None:
    root = seed_vault(tmp_path / "vault")
    index_path = root / "index.sqlite3"
    _alter(index_path, statement)
    before = index_path.read_bytes()
    _assert_stale(search_vault(root, QUERY, tenant="tenant-a"))
    _assert_stale(search_vault(root, QUERY, tenant="tenant-a", include_history=True))
    assert index_path.read_bytes() == before
    assert not MemoryIndex(index_path).schema_is_current()


def test_related_fallback_on_a_stale_index_is_degraded(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    _alter(root / "index.sqlite3", "DROP TABLE memory_temporal")
    result = related_vault(root, "mem-alpha", tenant="tenant-a")
    assert result["degraded"] is True
    assert result["results"] == []


def test_stdlib_schema_check_mirrors_the_library(tmp_path: Path) -> None:
    assert service.INDEX_SCHEMA_VERSION == memfw_index._INDEX_SCHEMA_VERSION
    assert service.INDEX_TABLES == memfw_index._INDEX_TABLES
    root = seed_vault(tmp_path / "vault")
    index_path = root / "index.sqlite3"
    for statement in (None, "DROP TABLE memory_temporal", "PRAGMA user_version = 0"):
        if statement:
            _alter(index_path, statement)
        connection = service._connect_index(root)
        try:
            ours = service._index_is_current(connection)
        finally:
            connection.close()
        assert ours == MemoryIndex(index_path).schema_is_current()
