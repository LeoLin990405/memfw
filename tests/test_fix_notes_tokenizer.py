"""Regression: a notes index keeps the FTS tokenizer it was built with."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import memfw.notes.index as notes_index
from memfw.notes import NotesIndex


def test_unicode61_index_reopened_where_trigram_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("jieba")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# 笔记\n\n向量数据库很好用\n", encoding="utf-8")
    db = tmp_path / "index.sqlite"
    monkeypatch.setattr(notes_index, "_supports_trigram", lambda: False)
    NotesIndex(db).index(vault)

    monkeypatch.setattr(notes_index, "_supports_trigram", lambda: True)
    reopened = NotesIndex(db)
    assert reopened.status()["fts_tokenizer"] == "unicode61"
    # A note added after reopening must be stored as jieba segments too.
    (vault / "b.md").write_text("# 记录\n\n关系数据库迁移计划\n", encoding="utf-8")
    reopened.index(vault)
    assert reopened.fts_tokenizer == "unicode61"
    paths = [hit.path for hit in NotesIndex(db).search("迁移计划", mode="fts")]
    assert paths == ["b.md"]
    with sqlite3.connect(db) as connection:
        stored = connection.execute("SELECT value FROM meta WHERE key='fts_tokenizer'").fetchone()
    assert stored == ("unicode61",)


def test_legacy_index_without_meta_key_uses_schema_tokenizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("jieba")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# 笔记\n\n向量数据库很好用\n", encoding="utf-8")
    db = tmp_path / "index.sqlite"
    monkeypatch.setattr(notes_index, "_supports_trigram", lambda: False)
    NotesIndex(db).index(vault)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM meta WHERE key='fts_tokenizer'")
    monkeypatch.setattr(notes_index, "_supports_trigram", lambda: True)
    assert NotesIndex(db).status()["fts_tokenizer"] == "unicode61"
