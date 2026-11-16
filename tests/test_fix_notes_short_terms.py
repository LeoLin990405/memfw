"""Regression: two-character terms on a trigram notes index."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.notes import NotesIndex


@pytest.fixture()
def index(tmp_path: Path) -> NotesIndex:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "cjk.md").write_text(
        "# 笔记\n\n今天学习机器学习和深度学习模型。数据迁移已经完成。\n", encoding="utf-8"
    )
    (vault / "latin.md").write_text("# Tools\n\nNew AI and ML tooling.\n", encoding="utf-8")
    (vault / "aitool.md").write_text("# 工具箱\n\n常用 AI 工具清单。\n", encoding="utf-8")
    (vault / "other.md").write_text("# Other\n\nnothing relevant here\n", encoding="utf-8")
    built = NotesIndex(tmp_path / "index.sqlite")
    built.index(vault)
    if built.fts_tokenizer != "trigram":
        pytest.skip("SQLite FTS5 trigram unavailable")
    return built


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("数据 迁移", "cjk.md"),
        ("迁移 数据", "cjk.md"),
        ("机器学习", "cjk.md"),
        ("深度学习模型", "cjk.md"),
        ("AI ML", "latin.md"),
        ("AI 工具", "aitool.md"),
    ],
)
def test_short_terms_reach_trigram_index(index: NotesIndex, query: str, expected: str) -> None:
    paths = [hit.path for hit in index.search(query, mode="fts")]
    assert expected in paths
    assert "other.md" not in paths


def test_fts_hits_rank_before_substring_hits(index: NotesIndex) -> None:
    # "machine"-style long terms go through BM25; the short "AI" term adds
    # substring hits after them, without duplicates.
    paths = [hit.path for hit in index.search("tooling AI", mode="fts")]
    assert paths[0] == "latin.md"
    assert "aitool.md" in paths
    assert len(paths) == len(set(paths))


def test_limit_is_respected_with_short_terms(index: NotesIndex) -> None:
    assert len(index.search("AI 工具 数据", mode="fts", limit=1)) == 1
