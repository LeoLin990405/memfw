"""R2: MCP mode=multihop expands once. HTTP stays on FTS."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.retrieval.multihop import classify_query, expansion_rounds
from scripts.memfw_readonly_service import ServiceError, reject_unavailable_search_mode


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "labelkey marker",
        "labelkey marker zzsecretalpha",
        memory_id="bridge",
        tenant="alpha",
    )
    repository.add(
        "Crate",
        "Crate count tied to zzsecretalpha equals forty",
        memory_id="gold",
        tenant="alpha",
    )
    repository.add(
        "Other",
        "Unrelated bulletin about weather patterns",
        memory_id="other",
        tenant="alpha",
    )
    repository.add(
        "Foreign",
        "The labelkey marker zzsecretalpha sits abroad",
        memory_id="foreign",
        tenant="beta",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def test_routes_id_quotes_and_temporal_cues() -> None:
    assert classify_query("bridge") == "direct"
    assert classify_query("memories/bridge.md") == "direct"
    assert classify_query('"labelkey marker"') == "direct"
    assert classify_query("最新 labelkey marker") == "temporal"
    assert classify_query("Where is the labelkey marker recorded") == "expand"
    assert expansion_rounds(1) == 1
    assert expansion_rounds(2) == 2
    with pytest.raises(ValueError):
        expansion_rounds(3)


def test_multihop_retrieves_the_second_hop_and_fts_does_not(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha")
    query = "Where is the labelkey marker recorded"
    single = gateway.search(query, limit=10, mode="fts")
    expanded = gateway.search(query, limit=10, mode="multihop")
    assert "gold" not in {item["id"] for item in single["results"]}
    assert "gold" in {item["id"] for item in expanded["results"]}
    assert "foreign" not in {item["id"] for item in expanded["results"]}
    assert expanded["degraded"] is False
    assert expanded["mode"] == "multihop"
    gold = next(item for item in expanded["results"] if item["id"] == "gold")
    assert gold["why"] == "expansion"
    assert gold["provenance_type"] == "user_said"
    assert gold["content_hash"].startswith("sha256:")
    assert gold["stale"] is False


def test_no_new_hit_falls_back_and_marks_degraded(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha")
    direct = gateway.search("bridge", limit=5, mode="multihop")
    assert direct["route"] == "direct"
    assert [item["id"] for item in direct["results"]] == ["bridge"]
    assert direct["degraded"] is False
    temporal = gateway.search("最新 labelkey marker", limit=5, mode="multihop")
    assert temporal["route"] == "temporal"
    assert temporal["degraded"] is False
    assert "gold" not in {item["id"] for item in temporal["results"]}
    alone = gateway.search("weather patterns", limit=5, mode="multihop")
    assert alone["route"] == "expand"
    assert alone["degraded"] is True
    assert [item["id"] for item in alone["results"]] == ["other"]


def test_http_search_rejects_multihop() -> None:
    with pytest.raises(ServiceError) as caught:
        reject_unavailable_search_mode({"mode": "multihop"})
    assert caught.value.status == 400
    assert caught.value.message == "mode is not available"
