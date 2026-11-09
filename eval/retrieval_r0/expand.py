"""R0.5 query expansion. One BM25 hop, rare terms, a second hop, then RRF.

This module does not add a judge, a model endpoint, or an API field. Noun
phrases are consecutive non-query tokens from the shipped tokenizer. The
second search uses the rare unigrams inside those phrases, because the shipped
BM25 ranker is a bag of words and this run has no tagger.
"""

from __future__ import annotations

from collections import Counter

from eval.retrieval_r0.cases import Question
from eval.retrieval_r0.corpus import BuiltCorpus, search_path
from memfw.stats.keyness import tokenize

PASSAGE_K = 5
RESULT_K = 10
RRF_K = 60
MAX_DOCUMENT_FREQUENCY = 3
TERM_LIMIT = 3


def document_frequencies(built: BuiltCorpus) -> Counter[str]:
    frequencies: Counter[str] = Counter()
    for document in built.by_id.values():
        frequencies.update(set(tokenize(f"{document.title}\n{document.body}")))
    return frequencies


def expansion_terms(
    passages: list[str],
    query: str,
    frequencies: Counter[str],
    *,
    limit: int = TERM_LIMIT,
    max_document_frequency: int = MAX_DOCUMENT_FREQUENCY,
) -> list[str]:
    """Rare unigrams from the first hop that the query did not already use.

    A term in an earlier passage outranks a term that is only in a later one.
    Within a passage, the rarer corpus term comes first.
    """

    query_terms = set(tokenize(query))
    best: dict[str, tuple[int, int, int, str]] = {}
    for rank, text in enumerate(passages):
        for term in set(tokenize(text)):
            if term in query_terms or len(term) < 4:
                continue
            frequency = frequencies.get(term, 0)
            if frequency <= 0 or frequency > max_document_frequency:
                continue
            candidate = (rank, frequency, -len(term), term)
            current = best.get(term)
            if current is None or candidate < current:
                best[term] = candidate
    ranked = sorted(best.values())
    return [term for _rank, _frequency, _length, term in ranked[:limit]]


def _rrf(lists: list[list[str]], *, rrf_k: int, limit: int) -> list[str]:
    scores: dict[str, float] = {}
    for identifiers in lists:
        for rank, identifier in enumerate(identifiers, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
    return ordered[:limit]


def expanded_search(
    built: BuiltCorpus,
    question: Question,
    frequencies: Counter[str],
    *,
    passage_k: int = PASSAGE_K,
    limit: int = RESULT_K,
    rrf_k: int = RRF_K,
) -> list[str]:
    """Return merged ids. The only query text is the question plus passage terms."""

    first = search_path("memory_bm25", built, question, limit=limit)
    passages = [
        f"{built.by_id[identifier].title}\n{built.by_id[identifier].body}"
        for identifier in first[:passage_k]
        if identifier in built.by_id
    ]
    terms = expansion_terms(passages, question.question, frequencies)
    second: list[str] = []
    if terms:
        follow_up = Question(
            question_id=question.question_id,
            case_type=question.case_type,
            split=question.split,
            question=" ".join(terms),
            tenant=question.tenant,
            evidence_ids=(),
            forbidden_ids=(),
            other_tenant_ids=(),
            poison_ids=(),
            poison_channels=(),
            hop=question.hop,
            lexical_key=question.lexical_key,
        )
        second = search_path("memory_bm25", built, follow_up, limit=limit)
    return _rrf([first, second], rrf_k=rrf_k, limit=limit)


__all__ = [
    "MAX_DOCUMENT_FREQUENCY",
    "PASSAGE_K",
    "RESULT_K",
    "RRF_K",
    "document_frequencies",
    "expanded_search",
    "expansion_terms",
]
