"""Regression: reindexing a changed or removed note without an embedder."""

from __future__ import annotations

from pathlib import Path

from memfw.notes import NotesIndex


def test_reindex_changed_and_deleted_note_without_embedder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text("# A\n\nalpha content\n", encoding="utf-8")
    index = NotesIndex(tmp_path / "index.sqlite")
    assert index.index(vault)["updated"] == 1

    note.write_text("# A\n\nalpha changed\n", encoding="utf-8")
    counts = index.index(vault)
    assert counts["updated"] == 1
    assert [hit.path for hit in index.search("changed", mode="fts")] == ["a.md"]

    note.unlink()
    counts = index.index(vault)
    assert counts["deleted"] == 1
    assert index.status()["notes"] == 0
