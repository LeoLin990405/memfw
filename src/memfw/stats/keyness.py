"""Offline G² term salience against a rolling document background corpus."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import jieba  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from memfw.policies import PolicyCandidate

_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|[0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_DEFAULT_STOPWORDS = frozenset(
    (
        "a an and are as at be been but by for from had has have he her hers him his i if in "
        "into is it its me my no not of on or our ours she so than that the their theirs them "
        "they this those to was we were what when where which who will with you your yours "
        "的 了 和 是 在 我 你 他 她 它 我们 你们 他们 这 那 个 也 都 而 及 与 或 但 又 被 把 就 对 "
        "还 很 有 没 不 为 从 到 上 下 中 以 及 其 之 后 前"
    ).split()
)


@dataclass(frozen=True, slots=True)
class TermKeyness:
    term: str
    document_count: int
    reference_count: int
    g2: float
    percent_difference: float | None
    reference_absent: bool


@dataclass(frozen=True, slots=True)
class KeynessResult:
    score: float
    terms: tuple[TermKeyness, ...]


def tokenize(text: str) -> tuple[str, ...]:
    """Tokenize English words and Chinese text without network/model access."""
    tokens: list[str] = []
    for match in _TOKEN.finditer(text.lower()):
        value = match.group()
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            tokens.extend(
                token.strip() for token in jieba.cut(value, cut_all=False) if token.strip()
            )
        elif len(value) > 1:
            tokens.append(value)
    return tuple(tokens)


def _g2(a: int, b: int, c: int, d: int) -> float:
    cells = (a, b, c, d)
    row1, row2 = a + b, c + d
    col1, col2 = a + c, b + d
    total = row1 + row2
    if not total or not row1 or not row2 or not col1 or not col2:
        return 0.0
    expected = (row1 * col1 / total, row1 * col2 / total, row2 * col1 / total, row2 * col2 / total)
    return 2.0 * sum(
        value * math.log(value / exp)
        for value, exp in zip(cells, expected, strict=True)
        if value and exp
    )


class KeynessScorer:
    """Score each document against other vault documents or an explicit reference."""

    def __init__(
        self,
        reference_texts: Sequence[str] | None = None,
        *,
        top_k: int = 5,
        minimum_term_frequency: int = 2,
        stopwords: Sequence[str] | None = None,
    ) -> None:
        if top_k < 1 or minimum_term_frequency < 1:
            raise ValueError("top_k and minimum_term_frequency must be positive")
        self.reference_texts = None if reference_texts is None else tuple(reference_texts)
        self.top_k = top_k
        self.minimum_term_frequency = minimum_term_frequency
        self.stopwords = _DEFAULT_STOPWORDS | frozenset(
            word.casefold() for word in (stopwords or ())
        )

    def _tokens(self, text: str) -> Counter[str]:
        return Counter(token for token in tokenize(text) if token not in self.stopwords)

    def score_documents(self, documents: Mapping[str, str]) -> dict[str, KeynessResult]:
        tokenized = {key: self._tokens(text) for key, text in sorted(documents.items())}
        result: dict[str, KeynessResult] = {}
        for identifier, counts in tokenized.items():
            if self.reference_texts is None:
                ref = sum(
                    (other for key, other in tokenized.items() if key != identifier), Counter()
                )
            else:
                ref = sum((self._tokens(text) for text in self.reference_texts), Counter())
            doc_n, ref_n = sum(counts.values()), sum(ref.values())
            if not doc_n or not ref_n:
                result[identifier] = KeynessResult(0.0, ())
                continue
            terms: list[TermKeyness] = []
            for term, frequency in counts.items():
                if frequency < self.minimum_term_frequency:
                    continue
                reference_frequency = ref[term]
                doc_rate, ref_rate = frequency / doc_n, reference_frequency / ref_n
                # Only terms used at a higher normalized rate than reference
                # count as salient; underrepresented terms cannot inflate score.
                if doc_rate <= ref_rate:
                    continue
                g2 = _g2(
                    frequency,
                    doc_n - frequency,
                    reference_frequency,
                    ref_n - reference_frequency,
                )
                # %DIFF is undefined at zero reference rate; None represents
                # division by zero honestly. G² remains unsmoothed.
                percent = 100.0 * (doc_rate - ref_rate) / ref_rate if ref_rate else None
                terms.append(
                    TermKeyness(
                        term,
                        frequency,
                        reference_frequency,
                        g2,
                        percent,
                        reference_absent=reference_frequency == 0,
                    )
                )
            terms.sort(key=lambda item: (-item.g2, item.term))
            # Aggregate only the configurable top-k repeated, non-stopword terms.
            score = 1.0 - math.exp(-sum(term.g2 for term in terms[: self.top_k]) / 20.0)
            result[identifier] = KeynessResult(min(1.0, max(0.0, score)), tuple(terms))
        return result

    def score_rolling_documents(
        self, documents: Sequence[tuple[str, datetime, str]], *, window_size: int = 20
    ) -> dict[str, KeynessResult]:
        """Compare each document with the prior ``window_size`` vault documents.

        Chronology is deterministic by timestamp then id. The first item has no
        background and receives score zero; it never falls back to labels.
        """
        if window_size < 1:
            raise ValueError("window_size must be positive")
        ordered = sorted(documents, key=lambda row: (row[1], row[0]))
        output: dict[str, KeynessResult] = {}
        prior: list[str] = []
        for identifier, _created_at, body in ordered:
            if not prior:
                output[identifier] = KeynessResult(0.0, ())
            else:
                output[identifier] = KeynessScorer(
                    prior[-window_size:],
                    top_k=self.top_k,
                    minimum_term_frequency=self.minimum_term_frequency,
                    stopwords=tuple(self.stopwords),
                ).score_documents({identifier: body})[identifier]
            prior.append(body)
        return output


def observable_strength(
    access_count: int,
    last_accessed_at: datetime | None,
    now: datetime,
    keyness: float,
    *,
    created_at: datetime | None = None,
    half_life_days: float = 30.0,
) -> float:
    """Experiment-only strength from use, recency, and measured distinctiveness.

    Unaccessed documents age from ``created_at``; accessed documents age from
    their latest access. This prevents old, unused memories appearing fresh.
    """
    if access_count < 0 or not 0.0 <= keyness <= 1.0 or half_life_days <= 0:
        raise ValueError("invalid observable-strength input")
    reference = last_accessed_at or created_at or now
    if reference.tzinfo is None or now.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    age = max(0.0, (now - reference).total_seconds() / 86400.0)
    recency = math.exp(-math.log(2.0) * age / half_life_days)
    use = 1.0 - math.exp(-0.35 * access_count)
    return min(1.0, max(0.0, 0.5 * recency + 0.25 * use + 0.25 * keyness))


def apply_keyness(
    candidates: Sequence[PolicyCandidate], scorer: KeynessScorer
) -> list[PolicyCandidate]:
    """Return PolicyCandidate copies annotated from their body and vault cohort."""
    from dataclasses import replace

    documents = {item.id: item.body for item in candidates}
    scores = scorer.score_documents(documents)
    return [replace(item, keyness=scores[item.id].score) for item in candidates]
