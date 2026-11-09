"""Search matches any query term. A whole-sentence phrase is not required."""

from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.index.fts_terms import fts_query, fts_terms
from memfw.mcp.server import Gateway
from scripts.memfw_readonly_service import _fts_query, search_vault

_SAMPLES = (
    "HAL9000 homelab",
    "HAL9000 homelab HAL9000",
    "HomeLab homelab",
    "a AND b",
    "foo*",
    'x"y',
    "(z)",
    "NEAR(a b)",
    "...",
    "帮我看一下 HAL9000 的部署",
    " ".join(f"term{index:02d}" for index in range(20)),
)


def test_library_and_http_build_the_same_match() -> None:
    for query in _SAMPLES:
        assert _fts_query(query) == fts_query(query)
    assert fts_query("HAL9000 homelab") == '"HAL9000" OR "homelab"'
    assert fts_query("a AND b") == '"AND"'
    assert fts_query("foo*") == '"foo"'
    assert fts_query('x"y') is None
    assert fts_query("(z)") is None
    assert fts_query("NEAR(a b)") == '"NEAR"'
    assert fts_query("...") is None
    assert len(fts_terms(" ".join(f"term{index:02d}" for index in range(20)))) == 16


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "Both",
        "HAL9000 rack in the homelab",
        memory_id="both",
        tenant="alpha",
    )
    repository.add("Only HA", "HAL9000 manual", memory_id="only-ha", tenant="alpha")
    repository.add("Only lab", "homelab notes", memory_id="only-lab", tenant="alpha")
    repository.add("Weather", "weather bulletin", memory_id="weather", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def test_or_query_covers_each_term_and_ranks_the_overlap_first(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    combined = search_vault(repository.root, "HAL9000 homelab", limit=10, tenant="alpha")
    ha_only = search_vault(repository.root, "HAL9000", limit=10, tenant="alpha")
    lab_only = search_vault(repository.root, "homelab", limit=10, tenant="alpha")
    combined_ids = [item["id"] for item in combined["results"]]
    assert combined_ids[0] == "both"
    assert {"both", "only-ha", "only-lab"} <= set(combined_ids)
    assert "weather" not in combined_ids
    assert len(combined_ids) >= len(ha_only["results"])
    assert len(combined_ids) >= len(lab_only["results"])
    assert all(0.0 <= item["score"] <= 1.0 for item in combined["results"])

    library = [item[0] for item in MemoryIndex(repository.index_path).search("HAL9000 homelab")]
    assert library[0] == "both"
    assert set(library) == set(combined_ids)

    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    mcp_ids = [item["id"] for item in gateway.search("HAL9000 homelab", mode="fts")["results"]]
    assert mcp_ids == combined_ids


def test_natural_language_keeps_the_embedded_term(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    result = search_vault(
        repository.root, "帮我看一下 HAL9000 的部署", limit=10, tenant="alpha"
    )
    assert {item["id"] for item in result["results"]} == {"both", "only-ha"}


def test_operators_and_punctuation_stay_empty_results(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    index = MemoryIndex(repository.index_path)
    for query in ("a AND b", "foo*", 'x"y', "(z)", "NEAR(a b)", "..."):
        result = search_vault(repository.root, query, limit=10, tenant="alpha")
        assert result["results"] == [], query
        assert result["mode"] == "fts"
        assert result["degraded"] is False
        assert index.search(query) == [], query


def test_two_character_query_stays_on_the_substring_branch(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    short = search_vault(repository.root, "HA", limit=10, tenant="alpha")
    assert {item["id"] for item in short["results"]} == {"both", "only-ha"}
    assert all(item["raw_score"] is None for item in short["results"])
    folded = search_vault(repository.root, "ha", limit=10, tenant="alpha")
    assert [item["id"] for item in folded["results"]] == [item["id"] for item in short["results"]]
