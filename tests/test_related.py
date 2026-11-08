from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.notes.index import NotesIndex
from memfw.related import RelatedIndex


def test_notes_and_memory_related_edges(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text(
        "# Corpus collocations\n\nKWIC and log Dice [[b]]", encoding="utf-8"
    )
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


def test_related_returns_backlinks_and_memory_explicit(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\n[[b]] unique alpha", encoding="utf-8")
    (notes / "b.md").write_text("# B\n\nunique beta", encoding="utf-8")
    index = NotesIndex(tmp_path / "notes.sqlite")
    index.index(notes)
    related = RelatedIndex(tmp_path / "related.sqlite")
    related.rebuild_notes(index.db_path)
    backlinks = related.related("notes", "b.md", limit=5)
    assert any(row["id"] == "a.md" and row["source"] == "notes" for row in backlinks)

    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add("A", "alpha shared token", memory_id="a")
    repo.add("B", "beta shared token", memory_id="b", related=["a"])
    related.rebuild_memory(repo)
    rows = related.related("memory", "a", limit=5)
    match = next(row for row in rows if row["id"] == "b")
    assert "explicit" in match["methods"]
    assert match["score"] == 3.0


def test_semantic_edges_are_opt_in_and_cross_source(tmp_path: Path) -> None:
    from memfw.notes import HashEmbeddingProvider

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# Alpha\n\nalpha corpus", encoding="utf-8")
    (notes / "b.md").write_text("# Beta\n\nbeta corpus", encoding="utf-8")
    notes_index = NotesIndex(tmp_path / "notes.sqlite")
    notes_index.index(notes)
    related = RelatedIndex(tmp_path / "related.sqlite")
    related.rebuild_notes(notes_index.db_path)
    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add("Alpha memory", "alpha corpus", memory_id="m-alpha")
    related.rebuild_memory(repo)
    assert related.build_semantic(HashEmbeddingProvider(8), limit=3) > 0
    rows = related.related("memory", "m-alpha", limit=10)
    assert any(row["source"] == "notes" for row in rows)
    assert any("semantic" in row["methods"] for row in rows)


def test_section_edges_and_tenant_filter(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# Corpus\n\nKWIC collocations", encoding="utf-8")
    (notes / "b.md").write_text("# Corpus methods\n\nKWIC collocations", encoding="utf-8")
    index = NotesIndex(tmp_path / "notes.sqlite")
    index.index(notes)
    related = RelatedIndex(tmp_path / "related.sqlite")
    assert related.rebuild_sections(index.db_path, max_sections=10) > 0
    section_rows = related.related("notes_sections", "a.md#s1", limit=5)
    assert section_rows

    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add("A", "alpha corpus", memory_id="a", tenant="t1", tags=["corpus"])
    repo.add("B", "alpha corpus", memory_id="b", tenant="t2", tags=["corpus"])
    repo.add("C", "alpha corpus", memory_id="c", tenant="t1", tags=["corpus"])
    related.rebuild_memory(repo)
    rows_t1 = related.related("memory", "a", tenant="t1", limit=5)
    rows_t2 = related.related("memory", "a", tenant="t2", limit=5)
    assert rows_t1
    assert any(row["id"] == "c" and row["source"] == "memory" for row in rows_t1)
    assert all(row["source"] == "memory" for row in rows_t1)
    assert all(row["id"] != "b" for row in rows_t1)
    same = next(row for row in rows_t1 if row["id"] == "c")
    assert "tag" in same["methods"]
    assert same["score"] == 0.8
    assert rows_t2 == []


def test_same_tenant_neighbour_survives_foreign_top_rank(tmp_path: Path) -> None:
    """Twenty-one earlier foreign ids must not erase the caller's neighbour."""
    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add(
        "Z subject",
        "shared corpus token",
        memory_id="z-subject",
        tenant="tenant-a",
        tags=["shared"],
    )
    repo.add(
        "Z target",
        "shared corpus token",
        memory_id="z-target",
        tenant="tenant-a",
        tags=["shared"],
    )
    for index in range(21):
        repo.add(
            f"Foreign {index:02d}",
            "shared corpus token",
            memory_id=f"b-foreign-{index:02d}",
            tenant="tenant-b",
            tags=["shared"],
        )
    related = RelatedIndex(tmp_path / "related.sqlite")
    related.rebuild_memory(repo)
    rows = related.related("memory", "z-subject", tenant="tenant-a", limit=10)
    assert [row["id"] for row in rows] == ["z-target"]
    foreign = related.related("memory", "b-foreign-00", tenant="tenant-b", limit=30)
    assert foreign
    assert all(row["id"] != "z-target" for row in foreign)
