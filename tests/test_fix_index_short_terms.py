"""Queries made only of two-character terms must match on a trigram index."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex, _supports_trigram

pytestmark = pytest.mark.skipif(not _supports_trigram(), reason="needs FTS5 trigram")


def _index(tmp_path: Path) -> MemoryIndex:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("迁移", "数据 迁移 完成", memory_id="cjk", tenant="")
    repository.add("ml", "AI ML pipeline notes", memory_id="ai", tenant="")
    repository.add("学习", "机器学习 与 深度学习模型", memory_id="deep", tenant="")
    repository.add("tools", "AI 工具 清单", memory_id="tools", tenant="")
    repository.add("other", "unrelated text", memory_id="other", tenant="")
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    return index


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("数据 迁移", {"cjk"}),
        ("迁移 数据", {"cjk"}),
        ("AI ML", {"ai", "tools"}),
        ("AI 工具", {"ai", "tools"}),
        ("机器学习", {"deep"}),
        ("深度学习模型", {"deep"}),
    ],
)
def test_short_term_queries_match(tmp_path: Path, query: str, expected: set[str]) -> None:
    index = _index(tmp_path)
    assert {identifier for identifier, _ in index.search(query)} == expected
    assert {identifier for identifier, _ in index.search_many([query])[query]} == expected


def test_fts_hits_rank_before_short_hits_and_limit_applies(tmp_path: Path) -> None:
    index = _index(tmp_path)
    # "pipeline" is an FTS hit (ai); "工具" is a short-only hit (tools).
    hits = [identifier for identifier, _ in index.search("pipeline 工具")]
    assert hits == ["ai", "tools"]
    assert [identifier for identifier, _ in index.search("pipeline 工具", limit=1)] == ["ai"]
    assert len(index.search("AI ML", limit=1)) == 1


def test_two_char_needle_unchanged(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assert {identifier for identifier, _ in index.search("数据")} == {"cjk"}
    assert {identifier for identifier, _ in index.search("AI")} == {"ai", "tools"}


def test_short_terms_respect_valid_to(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("迁移", "数据 迁移 完成", memory_id="cjk", tenant="")
    path = repository.memories_dir / "cjk.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("\n---\n", "\nvalid_to: '2026-01-01T00:00:00+00:00'\n---\n", 1),
        encoding="utf-8",
    )
    assert repository.get("cjk").valid_to is not None
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    assert index.search("数据 迁移") == []
