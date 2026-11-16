"""Readonly service: terms under three characters on a trigram index.

A trigram FTS5 index matches nothing for a term of one or two code points, so
a query made only of such terms (``数据 迁移``, ``AI ML``) used to return an
empty page. Those terms now go to a substring OR clause whose hits follow the
FTS hits, under the same tenant, source and temporal filters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.memfw_readonly_service as service
from scripts.memfw_readonly_service import search_vault
from tests.test_readonly_service import _memory, seed_vault

DOCUMENTS = [
    _memory("mem-mig", "数据迁移方案", "本文记录数据迁移的步骤", tenant="tenant-a"),
    _memory("mem-ml", "AI and ML notes", "notes about tooling", tenant="tenant-a"),
    _memory("mem-dl", "深度学习模型", "机器学习 与 深度学习模型 训练", tenant="tenant-a"),
    _memory("mem-tool", "AI 工具清单", "常用的工具", tenant="tenant-a"),
    _memory("mem-deploy", "deployment checklist", "deployment steps", tenant="tenant-a"),
    _memory("mem-foreign", "数据迁移 secret", "AI ML 工具 数据 迁移", tenant="tenant-b"),
    _memory(
        "mem-old",
        "旧的数据迁移",
        "数据 迁移 AI ML archive",
        tenant="tenant-a",
        valid_from=datetime(2026, 11, 6, 0, 10, tzinfo=UTC),
        valid_to=datetime(2026, 11, 6, 0, 20, tzinfo=UTC),
        superseded_by="mem-mig",
    ),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return seed_vault(tmp_path / "vault", DOCUMENTS)


def _ids(root: Path, query: str, tenant: str = "tenant-a", **kwargs: Any) -> list[str]:
    payload = search_vault(root, query, tenant=tenant, **kwargs)
    return [item["id"] for item in payload["results"]]


@pytest.mark.parametrize("query", ["数据 迁移", "迁移 数据"])
def test_two_character_cjk_terms_match(root: Path, query: str) -> None:
    ids = _ids(root, query)
    assert "mem-mig" in ids
    assert "mem-foreign" not in ids
    assert "mem-old" not in ids


def test_two_character_ascii_terms_match_case_insensitively(root: Path) -> None:
    assert "mem-ml" in _ids(root, "AI ML")
    assert "mem-ml" in _ids(root, "ai ml")
    assert "mem-tool" in _ids(root, "AI 工具")


@pytest.mark.parametrize(
    ("query", "expected"), [("机器学习", "mem-dl"), ("深度学习模型", "mem-dl")]
)
def test_long_cjk_terms_still_use_fts(root: Path, query: str, expected: str) -> None:
    payload = search_vault(root, query, tenant="tenant-a")
    assert [item["id"] for item in payload["results"]] == [expected]
    assert payload["results"][0]["raw_score"] is not None


def test_fts_hits_come_first_then_substring_hits(root: Path) -> None:
    payload = search_vault(root, "deployment AI", tenant="tenant-a", limit=10)
    results = payload["results"]
    assert results[0]["id"] == "mem-deploy"
    assert results[0]["raw_score"] is not None
    tail = [item["id"] for item in results[1:]]
    assert set(tail) == {"mem-ml", "mem-tool"}
    assert all(item["raw_score"] is None for item in results[1:])
    assert len({item["id"] for item in results}) == len(results)
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)


def test_limit_tenant_and_history_filters_apply_to_substring_hits(root: Path) -> None:
    assert len(_ids(root, "AI ML", limit=1)) == 1
    assert _ids(root, "数据 迁移", tenant="tenant-b") == ["mem-foreign"]
    assert "mem-old" in _ids(root, "数据 迁移", include_history=True)
    assert _ids(root, "数据 迁移", source="notes") == []


def test_single_two_character_needle_is_unchanged(root: Path) -> None:
    # The whole-needle substring branch orders by id and scores 1/(1+rank).
    payload = search_vault(root, "AI", tenant="tenant-a")
    assert [item["id"] for item in payload["results"]] == ["mem-ml", "mem-tool"]
    assert [item["score"] for item in payload["results"]] == [1.0, 0.5]


def test_stdlib_split_matches_the_library() -> None:
    from memfw.index.fts_terms import short_terms_clause, split_short_terms

    for terms in (["AI", "ML"], ["数据", "迁移"], ["deployment", "AI"], ["机器学习"], []):
        assert service._split_short_terms(terms) == split_short_terms(terms)
        assert service._split_short_terms(terms, "unicode61") == split_short_terms(
            terms, "unicode61"
        )
        columns = ["r.title", "r.tags"]
        assert service._short_terms_clause(columns, terms) == short_terms_clause(columns, terms)
