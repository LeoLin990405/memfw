"""History search routes terms under three characters around trigram FTS.

A trigram index cannot match a two-character term (``AI``, ``数据``). Those
terms are matched by substring and merged after the FTS hits, so a query of
only short terms, in any order, still finds its entries.

Fixtures are synthetic and live under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memfw.history import SourceRoot, TranscriptIndex

TEXTS = [
    "the db is fine",
    "数据 迁移 完成",
    "rollback plan ready",
    "AI 工具 清单",
    "ML pipeline notes",
    "机器学习 入门",
    "深度学习模型 训练",
]


@pytest.fixture
def index(tmp_path: Path) -> TranscriptIndex:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.jsonl").write_text(
        "".join(
            json.dumps({"session_id": "s1", "message": text}, ensure_ascii=False) + "\n"
            for text in TEXTS
        ),
        "utf-8",
    )
    built = TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("s", root)])
    built.refresh()
    with built._open() as connection:
        if built._tokenizer(connection) != "trigram":
            pytest.skip("SQLite without FTS5 trigram")
    return built


def _snippets(index: TranscriptIndex, query: str, **kwargs: object) -> list[str]:
    return [row["snippet"] for row in index.search(query, **kwargs)["results"]]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("数据 迁移", {"数据 迁移 完成"}),
        ("迁移 数据", {"数据 迁移 完成"}),
        ("AI ML", {"AI 工具 清单", "ML pipeline notes"}),
        ("机器学习", {"机器学习 入门"}),
        ("深度学习模型", {"深度学习模型 训练"}),
        ("AI 工具", {"AI 工具 清单"}),
        ("fine db", {"the db is fine"}),
    ],
)
def test_short_terms_match(index: TranscriptIndex, query: str, expected: set[str]) -> None:
    assert set(_snippets(index, query)) == expected


def test_fts_hits_come_first_then_short_term_hits(index: TranscriptIndex) -> None:
    result = index.search("rollback db")
    snippets = [row["snippet"] for row in result["results"]]
    assert snippets == ["rollback plan ready", "the db is fine"]
    assert result["results"][0]["score"] != 0.0


def test_merged_results_are_deduplicated_and_limited(index: TranscriptIndex) -> None:
    snippets = _snippets(index, "pipeline ML AI", limit=2)
    assert snippets[0] == "ML pipeline notes"
    assert len(snippets) == 2 == len(set(snippets))


def test_short_terms_keep_filters(index: TranscriptIndex) -> None:
    assert _snippets(index, "数据 迁移", session="other") == []
    assert _snippets(index, "数据 迁移", source="s") == ["数据 迁移 完成"]


def test_long_term_query_is_unchanged(index: TranscriptIndex) -> None:
    result = index.search("rollback")
    assert result["fallback"] is False
    assert [row["snippet"] for row in result["results"]] == ["rollback plan ready"]
