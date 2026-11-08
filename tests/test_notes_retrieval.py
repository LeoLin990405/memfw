from __future__ import annotations

import time
from pathlib import Path

from memfw.notes import (
    EvalCase,
    HashEmbeddingProvider,
    NotesIndex,
    NotesRetriever,
    QueryRewrite,
    RetrievalConfig,
    evaluate_ablation,
    evaluate_retriever,
)
from memfw.notes.index import NoteHit


def _note(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\n{text}\n", encoding="utf-8")


class ReverseReranker:
    name = "reverse-test"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def rerank(self, query: str, candidates, *, top_k: int):
        del query
        self.seen = [hit.path for hit in candidates]
        return list(reversed(candidates))[:top_k]


class AdditiveRewriter:
    name = "additive-test"

    def rewrite_query(self, query: str, *, limit: int = 3) -> QueryRewrite:
        del limit
        return QueryRewrite(
            original=query,
            queries=(query, "machine learning"),
            model=self.name,
        )


class BrokenReranker:
    name = "broken-test"

    def rerank(self, query: str, candidates, *, top_k: int):
        del query, candidates, top_k
        raise RuntimeError("reranker unavailable")


def test_reranker_is_optional_and_failure_degrades(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "corpus.md", "corpus concordance KWIC")
    _note(root, "machine.md", "machine learning gradient")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    reranker = ReverseReranker()
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(rerank_candidates=2, final_k=2),
        reranker=reranker,
    )
    result = retriever.search("corpus", limit=2)
    assert result.reranked is True
    assert reranker.seen
    broken = NotesRetriever(
        index,
        config=RetrievalConfig(rerank_candidates=2),
        reranker=BrokenReranker(),
    ).search("corpus", limit=2)
    assert broken.reranked is False
    assert broken.hits
    assert any("rerank:" in error for error in broken.errors)


def test_query_rewrite_keeps_original_query(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "a.md", "corpus linguistics")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(rewrite=True, candidates=10, final_k=5),
        language_model=AdditiveRewriter(),
    )
    result = retriever.search("corpus", limit=5)
    assert result.rewrites[0] == "corpus"
    assert "machine learning" in result.rewrites


def test_filters_are_applied_before_rerank(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "A/a.md", "corpus alpha")
    _note(root, "B/b.md", "corpus beta")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    reranker = ReverseReranker()
    result = NotesRetriever(
        index,
        config=RetrievalConfig(rerank_candidates=2),
        reranker=reranker,
    ).search("corpus", limit=2, path_prefix="A/")
    assert result.hits
    assert all(hit.path.startswith("A/") for hit in result.hits)
    assert reranker.seen and all(path.startswith("A/") for path in reranker.seen)


def test_ablation_and_metrics(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "a.md", "corpus concordance")
    _note(root, "b.md", "machine learning")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    cases = [
        EvalCase("q1", "corpus", ("a.md",), "content"),
        EvalCase("q2", "machine", ("b.md",), "content"),
    ]
    reports = evaluate_ablation(index, cases, limit=2)
    assert set(reports) == {"fts", "vector", "hybrid"}
    assert reports["fts"].metric(2)["recall_at_k"] == 1.0
    report = evaluate_retriever(
        NotesRetriever(index, config=RetrievalConfig(final_k=2)), cases, limit=2
    )
    assert report.metric(2)["mrr"] == 1.0
    assert report.as_dict(2)["metrics"]["queries"] == 2.0


def test_note_hit_serialization_keeps_section_id() -> None:
    hit = NoteHit(
        path="a.md",
        title="A",
        heading="H",
        content="body",
        score=1.0,
        rank=1,
        source="fts",
        section_id=42,
    )
    assert hit.as_dict()["section_id"] == 42


def test_evaluation_separates_no_answer_cases(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "a.md", "corpus concordance")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    cases = [
        EvalCase("q1", "corpus", ("a.md",), "content"),
        EvalCase("q2", "no such document", (), "no-answer"),
    ]
    report = evaluate_retriever(
        NotesRetriever(index, config=RetrievalConfig(final_k=2)), cases, limit=2
    )
    metrics = report.metric(2)
    assert metrics["queries"] == 2.0
    assert metrics["answerable_queries"] == 1.0
    assert metrics["unanswerable_queries"] == 1.0
    assert metrics["unanswerable_with_results"] == 1.0
    assert metrics["recall_at_k"] == 1.0


def test_fts_chinese_mixed_and_exact_phrase(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "cjk.md", "语料库代表性是本章重点。")
    _note(root, "mixed.md", "LancsBox 工具可以查看 KWIC。")
    _note(root, "exact.md", "GraphColl uses log Dice as the default association measure.")
    index = NotesIndex(tmp_path / "index.sqlite")
    index.index(root)
    assert NotesIndex._fts_search  # keep the helper covered through search
    assert index.search("语料库代表性", mode="fts")
    assert any(hit.path == "cjk.md" for hit in index.search("语料库代表性", mode="fts"))
    mixed = index.search("LancsBox 工具", mode="fts")
    assert any(hit.path == "mixed.md" for hit in mixed)
    exact = index.search('"log Dice"', mode="fts")
    assert any(hit.path == "exact.md" for hit in exact)


def test_http_reranker_provider_maps_results(monkeypatch) -> None:
    import io

    from memfw.notes import HttpRerankerProvider

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):  # noqa: ARG001
        assert b"documents" in request.data
        return Response(
            b'{"results":[{"index":1,"relevance_score":2.0},{"index":0,"relevance_score":1.0}]}'
        )

    monkeypatch.setattr("memfw.notes.http.open_http", fake_urlopen)
    provider = HttpRerankerProvider(
        "http://example.invalid/v1/rerank", model="test-reranker", max_doc_chars=4
    )
    candidates = [
        NoteHit("a.md", "A", "h", "abcdef", 0, 0, "hybrid"),
        NoteHit("b.md", "B", "h", "uvwxyz", 0, 0, "hybrid"),
    ]
    ranked = provider.rerank("query", candidates, top_k=2)
    assert [hit.path for hit in ranked] == ["b.md", "a.md"]
    assert ranked[0].score == 2.0
    assert ranked[0].source == "rerank:hybrid"


def test_time_and_metadata_filters_are_applied_before_results(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text(
        "---\ntitle: A\nsource: trusted\ntenant: t1\n---\n\n# A\n\ncorpus alpha\n",
        encoding="utf-8",
    )
    (root / "b.md").write_text(
        "---\ntitle: B\nsource: external\ntenant: t2\n---\n\n# B\n\ncorpus beta\n",
        encoding="utf-8",
    )
    (root / "a.md").touch()
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider())
    index.index(root)
    retriever = NotesRetriever(index, config=RetrievalConfig(final_k=5))
    result = retriever.search(
        "corpus",
        limit=5,
        metadata={"source": "trusted", "tenant": "t1"},
    )
    assert [hit.path for hit in result.hits] == ["a.md"]
    result = retriever.search("corpus", limit=5, metadata={"source": "external"})
    assert [hit.path for hit in result.hits] == ["b.md"]
    future = time.time() + 3600
    assert retriever.search("corpus", limit=5, since=future).hits == []
    assert retriever.search("corpus", limit=5, until=time.time() + 3600).hits


def test_opt_in_query_coverage_abstention(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus collocations\n", encoding="utf-8")
    index = NotesIndex(tmp_path / "index.sqlite")
    index.index(root)
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(final_k=3, min_query_coverage=0.5),
    )
    kept = retriever.search("corpus", limit=3)
    assert kept.hits and not kept.abstained
    dropped = retriever.search("quantum refrigerator maintenance", limit=3)
    assert dropped.hits == []
    assert dropped.abstained
    assert dropped.abstain_reason
