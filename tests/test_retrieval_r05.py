"""R0.5 expansion uses the shipped BM25 path and does not read gold ids."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.retrieval_r0.cases import generate
from eval.retrieval_r0.corpus import load_corpus, search_path
from eval.retrieval_r0.expand import document_frequencies, expanded_search, expansion_terms
from eval.retrieval_r0.metrics import all_evidence, evidence_recall
from eval.retrieval_r0.r05 import gain_interval
from memfw.index import MemoryIndex


# Loopback is unused here, but load_corpus stages through the readonly service.
@pytest.fixture(autouse=True)
def disable_network() -> None:
    """Allow the in-process loopback /stage client used while loading a corpus."""


def test_gain_interval_stays_at_zero_when_nothing_changes() -> None:
    zero = gain_interval([0.0] * 30, n_bootstrap=80, seed=1)
    assert zero["lower"] <= 0 <= zero["upper"]
    positive = gain_interval([1.0] * 30, n_bootstrap=80, seed=1)
    assert positive["lower"] > 0
    assert positive["n"] == 30


def test_expansion_terms_skip_query_terms_and_common_terms() -> None:
    frequencies = {"marker": 10, "zzsecret": 2, "allocation": 1}
    terms = expansion_terms(
        ["The owner asked about the project marker zzsecret"],
        "What allocation was approved for the project the owner asked about",
        frequencies,  # type: ignore[arg-type]
    )
    assert terms == ["zzsecret"]


def test_expansion_retrieves_the_second_hop_without_the_gold_id(tmp_path: Path) -> None:
    corpus = generate(4, seed=8)[("report", "multihop")]
    built = load_corpus(corpus, tmp_path / "vault")
    frequencies = document_frequencies(built)
    seen: list[str] = []
    real = MemoryIndex.ranked_search

    def spy(  # type: ignore[no-untyped-def]
        self: MemoryIndex,
        query: str,
        limit: int = 20,
        *,
        ranking: str = "bm25",
        candidate_ids=None,
    ):
        seen.append(query)
        return real(self, query, limit=limit, ranking=ranking, candidate_ids=candidate_ids)

    MemoryIndex.ranked_search = spy  # type: ignore[method-assign]
    try:
        baseline_total = 0.0
        expanded_total = 0.0
        for question in corpus.questions:
            baseline = search_path("memory_bm25", built, question, limit=10)
            expanded = expanded_search(built, question, frequencies)
            baseline_total += evidence_recall(
                baseline, question.evidence_ids, question.forbidden_ids, k=10
            )
            expanded_total += evidence_recall(
                expanded, question.evidence_ids, question.forbidden_ids, k=10
            )
            assert (
                all_evidence(baseline, question.evidence_ids, question.forbidden_ids, k=10) == 0.0
            )
            assert (
                all_evidence(expanded, question.evidence_ids, question.forbidden_ids, k=10) == 1.0
            )
            for evidence_id in question.evidence_ids:
                assert all(evidence_id not in query for query in seen)
    finally:
        MemoryIndex.ranked_search = real  # type: ignore[method-assign]
    assert baseline_total == 0.5 * len(corpus.questions)
    assert expanded_total == len(corpus.questions)


def test_single_hop_expansion_keeps_the_open_record(tmp_path: Path) -> None:
    corpus = generate(4, seed=8)[("report", "time_update")]
    built = load_corpus(corpus, tmp_path / "vault")
    frequencies = document_frequencies(built)
    for question in corpus.questions:
        baseline = search_path("memory_bm25", built, question, limit=10)
        expanded = expanded_search(built, question, frequencies)
        base = evidence_recall(baseline, question.evidence_ids, question.forbidden_ids, k=10)
        after = evidence_recall(expanded, question.evidence_ids, question.forbidden_ids, k=10)
        assert base == 1
        assert after == 1
