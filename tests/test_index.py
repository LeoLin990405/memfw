from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import memfw.index as index_module
from memfw.core.models import Tier
from memfw.core.repository import Repository
from memfw.index import MemoryIndex


def fixed_repo(root: Path) -> Repository:
    sequence = iter(f"event-{number}" for number in range(100))
    return Repository(
        root,
        clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        id_factory=lambda: next(sequence),
    )


def source_snapshot(repository: Repository) -> dict[str, str]:
    paths = [repository.config_path, repository.events_path, repository.event_state_path]
    paths.extend(repository.memories_dir.glob("*.md"))
    paths.extend(repository.history_dir.rglob("*.md"))
    paths.extend(repository.archives_dir.glob("*.tar.zst"))
    return {
        path.relative_to(repository.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.exists()
    }


def test_trigram_search_uses_body_and_indexes_cold_stub_summary_without_writes(
    tmp_path: Path,
) -> None:
    repository = fixed_repo(tmp_path / "vault")
    repository.init()
    repository.add(
        "与查询词无关的标题",
        "这是唯一的人工智能记忆内容",
        memory_id="hot-note",
        source="unit-test",
        provenance="human-authored",
        tags=["research", "中文"],
    )
    repository.add(
        "Cold title",
        "coldsummaryuniqueword should stay available",
        memory_id="cold-note",
        tier=Tier.COLD,
    )
    repository.update("cold-note", summary="coldsummaryuniqueword concise summary")
    repository.cold_archive("cold-archive")
    source_before = source_snapshot(repository)

    index = MemoryIndex(repository.index_path)
    assert index.rebuild(repository) == 2
    rows = index.rows()
    assert [row[0] for row in rows] == ["cold-note", "hot-note"]
    cold = rows[0]
    assert cold[2] == ""
    assert cold[10] == "coldsummaryuniqueword concise summary"
    assert cold[11] == 1
    assert cold[12] == "cold-archive"
    assert cold[13] == repository.get("cold-note").archived_hash
    assert index.search("人工智能记忆") == [("hot-note", "与查询词无关的标题")]
    assert index.search("coldsummaryuniqueword") == [("cold-note", "Cold title")]
    assert index.ranked_search("stay available") == [("cold-note", "Cold title")]
    assert index.search_many(["人工智能记忆", "coldsummaryuniqueword"]) == {
        "人工智能记忆": index.search("人工智能记忆", limit=100_000),
        "coldsummaryuniqueword": index.search("coldsummaryuniqueword", limit=100_000),
    }

    first_rows = index.rows()
    first_results = index.search("人工智能记忆")
    second_count = index.rebuild(repository)
    assert second_count == 2
    assert index.rows() == first_rows
    assert index.search("人工智能记忆") == first_results
    assert source_snapshot(repository) == source_before

    # Search is read-only. Its query may open SQLite but cannot change database bytes.
    after_rebuild_hash = hashlib.sha256(repository.index_path.read_bytes()).hexdigest()
    index.search("人工智能记忆")
    index.search("coldsummaryuniqueword")
    assert hashlib.sha256(repository.index_path.read_bytes()).hexdigest() == after_rebuild_hash


def test_two_character_query_matches_all_fields_and_body_without_title(
    tmp_path: Path,
) -> None:
    repository = fixed_repo(tmp_path / "vault")
    repository.init()
    repository.add("标题里有记住", "title-only needle absent", memory_id="title-only")
    repository.add("ordinary title", "请记住这段正文", memory_id="body-match")
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)

    # The body hit proves matching does not depend on the title. The separate
    # title hit preserves short Chinese title search behavior.
    assert index.search("记住") == [
        ("body-match", "ordinary title"),
        ("title-only", "标题里有记住"),
    ]
    assert index.search("标题") == [("title-only", "标题里有记住")]
    assert index.search("记") == []


def test_rebuild_replaces_old_index_only_after_complete_database(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository = fixed_repo(tmp_path / "vault")
    repository.init()
    repository.add("Title", "existing searchable body", memory_id="stable")
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    old_bytes = repository.index_path.read_bytes()

    def fail_insert(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(index_module, "_insert_documents", fail_insert)
    with pytest.raises(RuntimeError, match="injected insert failure"):
        index.rebuild(repository)
    assert repository.index_path.read_bytes() == old_bytes
    assert not repository.index_path.with_name(repository.index_path.name + ".tmp").exists()
    assert index.search("searchable") == [("stable", "Title")]


def test_missing_trigram_probe_uses_real_jieba_unicode61_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import jieba  # type: ignore[import-untyped]

    assert callable(jieba.lcut)
    repository = fixed_repo(tmp_path / "vault")
    repository.init()
    repository.add(
        "标题不包含搜索词",
        "本文讨论面向实体记忆检索的索引",
        memory_id="fallback-note",
    )
    real_connect = sqlite3.connect

    class UnsupportedTrigramProbe:
        def execute(self, sql: str) -> UnsupportedTrigramProbe:
            if "tokenize='trigram'" in sql:
                raise sqlite3.OperationalError("no such tokenizer: trigram")
            raise AssertionError(f"unexpected probe SQL: {sql}")

        def close(self) -> None:
            return None

    def fake_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        if str(database) == ":memory:":
            return UnsupportedTrigramProbe()
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    index = MemoryIndex(repository.index_path)
    assert index.rebuild(repository) == 1
    with real_connect(repository.index_path) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
        ).fetchone()[0]
    assert "tokenize='unicode61'" in schema
    assert index.search("实体记忆检索") == [("fallback-note", "标题不包含搜索词")]


def test_complete_rows_keep_metadata_and_hot_content_verbatim(tmp_path: Path) -> None:
    repository = fixed_repo(tmp_path / "vault")
    repository.init()
    document = repository.add(
        "Full row title",
        "verbatim hot body",
        memory_id="full-row",
        source="manual-source",
        provenance="human-authored",
        anchor="project-x",
        tags=["one", "two"],
    )
    source_before = source_snapshot(repository)
    index = MemoryIndex(repository.index_path)

    index.rebuild(repository)
    row = index.rows()[0]

    assert row == (
        document.id,
        document.title,
        document.body,
        document.layer.value,
        document.tier.value,
        document.source,
        document.provenance,
        document.anchor,
        "one two",
        document.version,
        document.summary,
        0,
        "",
        "",
        document.created_at.isoformat(),
        document.updated_at.isoformat(),
    )
    assert source_snapshot(repository) == source_before
