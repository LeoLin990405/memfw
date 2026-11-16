"""/related compares tenants exactly, the empty tenant included (S1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.memfw_readonly_service import ServiceError, related_vault
from tests.test_readonly_service import _memory, build_related, seed_vault

DOCS = [
    _memory("mem-free", "Free corpus note", "shared corpus keyword", tags=("shared",)),
    _memory(
        "mem-a1",
        "Tenant A corpus note",
        "shared corpus keyword",
        tenant="tenant-a",
        tags=("shared",),
    ),
    _memory(
        "mem-b1",
        "Tenant B corpus note",
        "shared corpus keyword",
        tenant="tenant-b",
        tags=("shared",),
    ),
]


@pytest.mark.parametrize("edges", [False, True])
def test_untenanted_request_cannot_see_tenanted_memory(tmp_path: Path, edges: bool) -> None:
    root = seed_vault(tmp_path / "vault", DOCS)
    related_db = build_related(root) if edges else None
    with pytest.raises(ServiceError) as caught:
        related_vault(root, "mem-a1", limit=10, related_db=related_db)
    assert caught.value.status == 404


@pytest.mark.parametrize("edges", [False, True])
def test_untenanted_neighbours_exclude_other_tenants(tmp_path: Path, edges: bool) -> None:
    root = seed_vault(tmp_path / "vault", DOCS)
    related_db = build_related(root) if edges else None
    result = related_vault(root, "mem-free", limit=10, related_db=related_db)
    ids = {item["id"] for item in result["results"]}
    assert not ids & {"mem-a1", "mem-b1"}, result


def test_tenanted_request_keeps_its_neighbours(tmp_path: Path) -> None:
    docs = [*DOCS, _memory("mem-a2", "Tenant A peer", "shared corpus keyword", tenant="tenant-a")]
    root = seed_vault(tmp_path / "vault", docs)
    result = related_vault(
        root, "mem-a1", limit=10, related_db=build_related(root), tenant="tenant-a"
    )
    ids = {item["id"] for item in result["results"]}
    assert "mem-a2" in ids
    assert not ids & {"mem-free", "mem-b1"}
