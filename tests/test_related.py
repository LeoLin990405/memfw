from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.notes.index import NotesIndex
from memfw.related import RelatedIndex


def test_notes_and_memory_related_edges(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# Corpus collocations\n\nKWIC and log Dice", encoding="utf-8")
    (notes / "b.md").write_text("# Corpus methods\n\nKWIC and collocations", encoding="utf-8")
    (notes / "c.md").write_text("# NAS backup\n\nsynology hyper backup", encoding="utf-8")
    notes_index = NotesIndex(tmp_path / "notes.sqlite")
    notes_index.index(notes)
    related = RelatedIndex(tmp_path / "related.sqlite")
    assert related.rebuild_notes(notes_index.db_path) > 0
    rows = related.related("notes", "a.md", limit=5)
    assert any(row["id"] == "b.md" for row in rows)
    assert all(row["source"] == "notes" for row in rows)

    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add("Corpus", "KWIC collocations", memory_id="m1", tags=["corpus"])
    repo.add("Corpus methods", "KWIC and log Dice", memory_id="m2", tags=["corpus"])
    repo.add("NAS", "synology backup", memory_id="m3", tags=["nas"])
    assert related.rebuild_memory(repo) > 0
    memory_rows = related.related("memory", "m1", limit=5)
    assert any(row["id"] == "m2" for row in memory_rows)
    assert all(row["source"] == "memory" for row in memory_rows)
