"""Regression: vectors are rebuilt when the embedder or its dimension changes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from memfw.notes import HashEmbeddingProvider, NotesIndex


class ConstantEmbedder:
    """Tiny fake: every text maps to the same unit vector chosen by ``axis``."""

    def __init__(self, axis: int, *, dimension: int = 4, fingerprint: str = "") -> None:
        self.axis = axis
        self.dimension = dimension
        self.fingerprint = fingerprint or f"axis-{axis}"
        self.calls = 0

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        del role
        self.calls += len(texts)
        return [[1.0 if i == self.axis else 0.0 for i in range(self.dimension)] for _ in texts]


def _vault(root: Path) -> Path:
    root.mkdir()
    (root / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\nbeta\n", encoding="utf-8")
    return root


def test_dimension_change_rebuilds_vectors(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    db = tmp_path / "index.sqlite"
    NotesIndex(db, embedder=HashEmbeddingProvider(8)).index(vault)
    (vault / "b.md").write_text("# B\n\nbeta2\n", encoding="utf-8")
    index = NotesIndex(db, embedder=HashEmbeddingProvider(16))
    counts = index.index(vault)
    status = index.status()
    assert counts["embedded"] == status["sections"]
    assert status["vectors"] == status["sections"]
    assert status["dimension"] == "16"
    hits = index.search("alpha", mode="vector")
    assert hits and all(hit.source == "vector" for hit in hits)


def test_embedder_added_later_embeds_unchanged_notes(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    db = tmp_path / "index.sqlite"
    NotesIndex(db).index(vault)
    index = NotesIndex(db, embedder=HashEmbeddingProvider(8))
    counts = index.index(vault)
    assert counts["unchanged"] == 2
    assert index.status()["vectors"] == index.status()["sections"]
    assert index.search("zzz unrelated", mode="vector")


def test_same_dimension_new_fingerprint_reembeds(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    db = tmp_path / "index.sqlite"
    NotesIndex(db, embedder=ConstantEmbedder(0)).index(vault)
    changed = ConstantEmbedder(1)
    stale = NotesIndex(db, embedder=changed)
    # Before reindexing, vectors from the old model are not used.
    assert stale.search("alpha", mode="vector") == []
    stale.index(vault)
    assert changed.calls == 1 + stale.status()["sections"]  # one query, then every section
    assert stale.search("alpha", mode="vector")


def test_run_without_embedder_keeps_vectors_usable(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    db = tmp_path / "index.sqlite"
    NotesIndex(db, embedder=ConstantEmbedder(0)).index(vault)
    NotesIndex(db).index(vault)
    again = ConstantEmbedder(0)
    index = NotesIndex(db, embedder=again)
    assert index.search("alpha", mode="vector")
    assert index.index(vault)["embedded"] == 0
    assert again.calls == 1  # the query only
