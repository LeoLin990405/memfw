"""Deterministic BM25 ranking over a fixed document corpus."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping

from .keyness import tokenize


class BM25Scorer:
    """Build an immutable BM25 index using MemFW's shared tokenizer.

    The scorer is independent of repositories and never emits access events. Pass
    speaker-aware text in ``documents`` when matching a dialogue retrieval baseline.
    """

    def __init__(
        self,
        documents: Mapping[str, str],
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        if not math.isfinite(k1) or k1 <= 0:
            raise ValueError("k1 must be a positive finite number")
        if not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("b must be a finite number between 0 and 1")
        self.k1 = k1
        self.b = b
        self._documents = {
            identifier: Counter(tokenize(text)) for identifier, text in sorted(documents.items())
        }
        self._lengths = {
            identifier: sum(counts.values()) for identifier, counts in self._documents.items()
        }
        self._average_length = (
            sum(self._lengths.values()) / len(self._lengths) if self._lengths else 0.0
        )
        self._document_frequency: Counter[str] = Counter(
            term for counts in self._documents.values() for term in counts
        )

    @staticmethod
    def _corpus_stats(
        documents: Mapping[str, Counter[str]],
    ) -> tuple[dict[str, int], float, Counter[str]]:
        lengths = {identifier: sum(counts.values()) for identifier, counts in documents.items()}
        average = sum(lengths.values()) / len(lengths) if lengths else 0.0
        frequencies = Counter(term for counts in documents.values() for term in counts)
        return lengths, average, frequencies

    def _score_terms(
        self,
        terms: Iterable[str],
        document_id: str,
        *,
        documents: Mapping[str, Counter[str]],
        lengths: Mapping[str, int],
        average_length: float,
        document_frequency: Counter[str],
    ) -> float:
        counts = documents.get(document_id)
        if counts is None or not average_length:
            return 0.0
        length = lengths[document_id]
        score = 0.0
        count = len(documents)
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            df = document_frequency[term]
            inverse_frequency = max(0.0, math.log(1.0 + (count - df + 0.5) / (df + 0.5)))
            denominator = frequency + self.k1 * (1.0 - self.b + self.b * length / average_length)
            score += inverse_frequency * frequency * (self.k1 + 1.0) / denominator
        return score

    def score(self, query: str, document_id: str) -> float:
        """Return one document's BM25 score for ``query`` (zero for unknown IDs)."""
        return self._score_terms(
            dict.fromkeys(tokenize(query)),
            document_id,
            documents=self._documents,
            lengths=self._lengths,
            average_length=self._average_length,
            document_frequency=self._document_frequency,
        )

    def for_candidates(self, candidate_ids: Iterable[str]) -> BM25Scorer:
        """Return a scorer whose IDF/length statistics use exactly these IDs."""
        selected = set(candidate_ids)
        subset = object.__new__(BM25Scorer)
        subset.k1 = self.k1
        subset.b = self.b
        subset._documents = {
            identifier: counts
            for identifier, counts in self._documents.items()
            if identifier in selected
        }
        subset._lengths, subset._average_length, subset._document_frequency = subset._corpus_stats(
            subset._documents
        )
        return subset

    def rank(self, query: str, *, candidate_ids: Iterable[str] | None = None) -> list[str]:
        """Rank a corpus, or the exact retained-ID subset, deterministically.

        When supplied, ``candidate_ids`` defines the ranking corpus, so document
        frequency and average length match retrieval after retention.
        """
        if candidate_ids is None:
            documents = self._documents
            lengths = self._lengths
            average_length = self._average_length
            document_frequency = self._document_frequency
        else:
            selected_ids = set(candidate_ids)
            documents = {
                identifier: counts
                for identifier, counts in self._documents.items()
                if identifier in selected_ids
            }
            lengths, average_length, document_frequency = self._corpus_stats(documents)
        terms = tuple(dict.fromkeys(tokenize(query)))
        scored = [
            (
                self._score_terms(
                    terms,
                    identifier,
                    documents=documents,
                    lengths=lengths,
                    average_length=average_length,
                    document_frequency=document_frequency,
                ),
                identifier,
            )
            for identifier in documents
        ]
        return [
            identifier
            for score, identifier in sorted(scored, key=lambda row: (-row[0], row[1]))
            if score > 0
        ]
