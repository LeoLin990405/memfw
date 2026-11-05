from __future__ import annotations

from pathlib import Path

from memfw.notes import HashEmbeddingProvider, NotesIndex, parse_markdown


def _note(path: Path, title: str, body: str, tags: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\n{tags}---\n\n# {title}\n\n{body}\n", encoding="utf-8")


def test_parse_markdown_sections_links_and_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "Lancaster" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntitle: Corpus Notes\ntags: [linguistics, corpus]\n---\n\n"
        "Intro text.\n\n## Week 1\n\nSee [[other-note]].\n\n### Detail\n\nKWIC.\n",
        encoding="utf-8",
    )
    note = parse_markdown(path, tmp_path)
    assert note.title == "Corpus Notes"
    assert note.course == "Lancaster"
    assert note.tags == ["linguistics", "corpus"]
    assert note.links == ["other-note"]
    assert [section.heading for section in note.sections] == ["", "Week 1", "Detail"]


def test_index_search_incremental_and_fallback(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root / "Lancaster" / "a.md", "Corpus Linguistics", "KWIC concordance corpus methods.")
    _note(root / "CS" / "b.md", "Machine Learning", "gradient descent and neural networks.")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    first = index.index(root)
    assert first["updated"] == 2
    assert first["sections"] >= 2
    status = index.status()
    assert status["notes"] == 2
    assert status["vectors"] == status["sections"]

    hits = index.search("corpus", limit=3)
    assert hits and hits[0].path.endswith("Lancaster/a.md")
    assert hits[0].source in {"hybrid", "fts"}

    _note(root / "Lancaster" / "a.md", "Corpus Linguistics", "updated concordance only.")
    second = index.index(root)
    assert second["updated"] == 1
    assert second["unchanged"] == 1

    fts_only = NotesIndex(tmp_path / "fts-only.sqlite")
    fts_only.index(root)
    hits = fts_only.search("gradient", limit=3)
    assert hits and hits[0].path.endswith("CS/b.md")
    assert hits[0].source == "fts"


def test_index_commits_checkpoints_before_a_late_failure(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root / "a.md", "A", "first")
    _note(root / "b.md", "B", "second")
    _note(root / "c.md", "C", "third")

    class FailingEmbedder(HashEmbeddingProvider):
        def __init__(self) -> None:
            super().__init__(8)
            self.calls = 0

        def embed(self, texts):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls >= 3:
                raise RuntimeError("simulated embedding failure")
            return super().embed(texts)

    db = tmp_path / "partial.sqlite"
    index = NotesIndex(db, embedder=FailingEmbedder())
    try:
        index.index(root, commit_every=1)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected failure")
    reopened = NotesIndex(db)
    assert reopened.status()["notes"] >= 1
