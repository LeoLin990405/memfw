"""Regression: a successful rerank does not mark hybrid retrieval as degraded."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from memfw.notes import HashEmbeddingProvider, NotesIndex, NotesRetriever, RetrievalConfig
from memfw.notes.index import NoteHit


class LabellingReranker:
    """Reverses candidates and relabels them the way HttpRerankerProvider does."""

    def rerank(self, query: str, candidates: Sequence[NoteHit], *, top_k: int) -> list[NoteHit]:
        del query
        return [replace(hit, source=f"rerank:{hit.source}") for hit in reversed(candidates)][:top_k]


def _index(tmp_path: Path, *, embedder: bool) -> NotesIndex:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\nalpha content\n", encoding="utf-8")
    (vault / "b.md").write_text("# B\n\nalpha beta\n", encoding="utf-8")
    index = NotesIndex(
        tmp_path / "index.sqlite", embedder=HashEmbeddingProvider(8) if embedder else None
    )
    index.index(vault)
    return index


def test_reranked_hybrid_result_is_not_degraded(tmp_path: Path) -> None:
    index = _index(tmp_path, embedder=True)
    plain = NotesRetriever(index, config=RetrievalConfig(mode="hybrid")).search("alpha")
    assert plain.degraded is False
    reranked = NotesRetriever(
        index,
        config=RetrievalConfig(mode="hybrid", rerank_candidates=10),
        reranker=LabellingReranker(),
    ).search("alpha")
    assert reranked.reranked is True
    assert all(hit.source.startswith("rerank:") for hit in reranked.hits)
    assert reranked.degraded is False


def test_reranked_result_without_vectors_is_still_degraded(tmp_path: Path) -> None:
    index = _index(tmp_path, embedder=False)
    result = NotesRetriever(
        index,
        config=RetrievalConfig(mode="hybrid", rerank_candidates=10),
        reranker=LabellingReranker(),
    ).search("alpha")
    assert result.reranked is True
    assert result.degraded is True
