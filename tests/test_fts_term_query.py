"""Search matches any query term. A whole-sentence phrase is not required."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


_CJK_QUERIES = (
    "实体记忆检索",
    "帮我看一下实体记忆检索的部署",
    "HAL9000 记忆检索 homelab",
    "部署工具 HAL9000",
    "帮我看一下 HAL9000 的部署",
)


def _unicode61_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    import memfw.index as index_module

    monkeypatch.setattr(index_module, "_supports_trigram", lambda *args, **kwargs: False)
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("检索笔记", "本文讨论面向实体记忆检索的索引", memory_id="retrieval")
    repository.add("部署记录", "HAL9000 的部署工具和部署步骤", memory_id="deploy")
    repository.add("机房", "homelab 机房里的 HAL9000", memory_id="lab")
    repository.add("天气", "今天的天气预报", memory_id="weather")
    MemoryIndex(repository.index_path).rebuild(repository)
    with sqlite3.connect(repository.index_path) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
        ).fetchone()[0]
    assert "tokenize='unicode61'" in schema
    return repository


def test_unicode61_library_and_http_share_terms_and_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.index as index_module
    from memfw.index.fts_terms import query_terms
    from scripts.memfw_readonly_service import _query_terms

    repository = _unicode61_vault(tmp_path, monkeypatch)
    index = MemoryIndex(repository.index_path)
    for query in _CJK_QUERIES:
        library_terms = query_terms(query, "unicode61")
        assert library_terms, query
        assert _query_terms(query, "unicode61") == library_terms, query
        assert _fts_query(query, "unicode61") == index_module._match_text(query, "unicode61")
        library_ids = [row[0] for row in index.search(query, limit=10)]
        http_ids = [
            item["id"]
            for item in search_vault(repository.root, query, limit=10, tenant="")["results"]
        ]
        assert library_ids, query
        assert http_ids == library_ids, query
    assert "weather" not in {
        item["id"] for item in search_vault(repository.root, "部署工具", tenant="")["results"]
    }


def test_trigram_terms_are_unchanged_by_the_shared_builder() -> None:
    from memfw.index.fts_terms import query_terms

    for query in (*_SAMPLES, *_CJK_QUERIES):
        assert query_terms(query) == fts_terms(query) == query_terms(query, "trigram")
        assert _fts_query(query) == _fts_query(query, "trigram") == fts_query(query)
    assert query_terms("帮我看一下 HAL9000 的部署") == ["帮我看一下", "HAL9000", "的部署"]


@pytest.mark.parametrize("missing", ["memfw.index.fts_terms", "jieba"])
def test_unicode61_http_reports_a_missing_term_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    import sys

    repository = _unicode61_vault(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, missing, None)
    payload = search_vault(repository.root, "实体记忆检索", limit=10, tenant="")
    assert payload["degraded"] is True
    assert any("unicode61 query terms unavailable" in item for item in payload["errors"])
    assert isinstance(payload["results"], list)
