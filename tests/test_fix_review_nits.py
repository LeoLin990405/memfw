"""Review follow-ups: ranked_search hides superseded memories, short-term
stopwords add no noise, and history_list stats only count visible sources."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from memfw.core.repository import Repository
from memfw.index import MemoryIndex, _supports_trigram
from memfw.index.fts_terms import split_short_terms
from memfw.mcp.server import Gateway
from scripts.memfw_readonly_service import search_vault
from tests.test_fix_mcp_history_optout import _configure, _vault
from tests.test_readonly_service import _memory, seed_vault

trigram = pytest.mark.skipif(not _supports_trigram(), reason="needs FTS5 trigram")


@pytest.mark.parametrize("ranking", ["bm25", "count"])
def test_ranked_search_hides_superseded_without_valid_to(tmp_path: Path, ranking: str) -> None:
    docs = [
        _memory("mem-old", "Old pipeline plan", "pipeline keyword", superseded_by="mem-new"),
        _memory("mem-new", "New pipeline plan", "pipeline keyword"),
    ]
    root = seed_vault(tmp_path / "vault", docs)
    index = MemoryIndex(Repository(root).index_path)
    ids = [identifier for identifier, _ in index.ranked_search("pipeline", ranking=ranking)]
    assert ids == ["mem-new"]


@pytest.mark.parametrize(
    ("terms", "short"),
    [
        (["pipeline", "is"], []),
        (["of", "to", "in"], []),
        (["AI", "ML"], ["AI", "ML"]),
        (["ai", "ml"], ["ai", "ml"]),
        (["Q3", "IT"], ["Q3", "IT"]),
        (["数据", "迁移"], ["数据", "迁移"]),
    ],
)
def test_short_term_split_skips_stopwords(terms: list[str], short: list[str]) -> None:
    assert split_short_terms(terms)[1] == short
    assert service._split_short_terms(terms)[1] == short


NOISE_DOCS = [
    _memory("mem-pipe", "pipeline design", "the pipeline is staged"),
    _memory("mem-ai", "AI and ML notes", "notes about tooling"),
    _memory("mem-noise", "unrelated", "this is a list of things"),
]


@trigram
def test_index_pipeline_is_has_no_stopword_noise(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault", NOISE_DOCS)
    index = MemoryIndex(Repository(root).index_path)
    assert [identifier for identifier, _ in index.search("pipeline is")] == ["mem-pipe"]
    assert "mem-ai" in {identifier for identifier, _ in index.search("AI ML")}


@trigram
def test_service_pipeline_is_has_no_stopword_noise(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault", NOISE_DOCS)
    assert [item["id"] for item in search_vault(root, "pipeline is")["results"]] == ["mem-pipe"]
    assert "mem-ai" in {item["id"] for item in search_vault(root, "AI ML")["results"]}


def test_history_list_stats_count_only_visible_sources(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    full = Gateway(vault, app="codex").history_list()["stats"]
    assert full["files"] == 2
    _configure(
        vault,
        conversation_share={"enabled": True, "apps": {"kimi": {"can_read": ["claude"]}}},
    )
    gateway = Gateway(vault, app="kimi")
    assert gateway.history_list()["stats"]["files"] == 1
    assert gateway.history_list(source="claude")["stats"]["files"] == 1
    assert Gateway(vault, app="codex").history_list()["stats"]["files"] == 2
