"""Regression: search filters apply before the candidate cap; tags keep spaces."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from memfw.notes import HashEmbeddingProvider, NotesIndex


@pytest.fixture()
def crowded(tmp_path: Path) -> tuple[NotesIndex, Path]:
    vault = tmp_path / "vault"
    (vault / "other").mkdir(parents=True)
    (vault / "target").mkdir()
    for i in range(120):
        note = vault / "other" / f"n{i:03d}.md"
        note.write_text(f"# T\n\nkeyword keyword {i}\n", encoding="utf-8")
        os.utime(note, (1_000_000, 1_000_000))
    target = vault / "target" / "z.md"
    target.write_text("# T\n\nkeyword " + "filler " * 300 + "\n", encoding="utf-8")
    os.utime(target, (2_000_000, 2_000_000))
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider(8))
    index.index(vault)
    return index, target


@pytest.mark.parametrize("mode", ["fts", "hybrid", "vector"])
def test_filters_reach_notes_beyond_the_candidate_cap(
    crowded: tuple[NotesIndex, Path], mode: str
) -> None:
    index, _ = crowded
    expected = ["target/z.md"]
    assert [h.path for h in index.search("keyword", mode=mode, path_prefix="target/")] == expected
    assert [h.path for h in index.search("keyword", mode=mode, course="target")] == expected
    assert [h.path for h in index.search("keyword", mode=mode, since=1_500_000)] == expected
    assert index.search("keyword", mode=mode, until=1_500_000, course="target") == []


def test_path_prefix_is_literal(crowded: tuple[NotesIndex, Path]) -> None:
    index, _ = crowded
    assert index.search("keyword", mode="fts", path_prefix="targ%") == []
    assert index.search("keyword", mode="fts", path_prefix="_arget/") == []


def _tagged_vault(root: Path) -> Path:
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntags: [machine learning, rag]\n---\n# A\n\nalpha content\n", encoding="utf-8"
    )
    (root / "b.md").write_text(
        "---\ntags: [machine, learning]\n---\n# B\n\nalpha other\n", encoding="utf-8"
    )
    return root


def test_tags_with_spaces_round_trip_and_filter(tmp_path: Path) -> None:
    vault = _tagged_vault(tmp_path / "vault")
    index = NotesIndex(tmp_path / "index.sqlite")
    index.index(vault)
    by_path = {hit.path: hit.tags for hit in index.search("alpha", mode="fts")}
    assert by_path == {"a.md": ["machine learning", "rag"], "b.md": ["machine", "learning"]}
    assert [h.path for h in index.search("alpha", mode="fts", tags=["machine learning"])] == [
        "a.md"
    ]
    assert [h.path for h in index.search("alpha", mode="fts", tags=["machine"])] == ["b.md"]


def test_legacy_space_joined_tags_still_read(tmp_path: Path) -> None:
    vault = _tagged_vault(tmp_path / "vault")
    db = tmp_path / "index.sqlite"
    NotesIndex(db).index(vault)
    with sqlite3.connect(db) as connection:
        # An index built before tags_json existed.
        connection.execute("ALTER TABLE notes DROP COLUMN tags_json")
    legacy = NotesIndex(db)
    assert [h.path for h in legacy.search("alpha", mode="fts", tags=["rag"])] == ["a.md"]
    hit = legacy.search("alpha", mode="fts", tags=["rag"])[0]
    assert hit.tags == ["machine", "learning", "rag"]
    # Reindexing migrates the schema; unchanged rows keep the legacy format.
    legacy.index(vault)
    assert [h.path for h in legacy.search("alpha", mode="fts", tags=["rag"])] == ["a.md"]
