"""Retrieval metrics for one ranked id list."""

from __future__ import annotations

import math


def evidence_recall(
    retrieved: list[str],
    evidence_ids: tuple[str, ...],
    forbidden_ids: tuple[str, ...],
    *,
    k: int,
) -> float:
    """Fraction of gold ids in the top k.

    An empty gold set is a deleted-fact question: the score is 1 only when no
    forbidden id is in the top k.
    """

    top = retrieved[:k]
    if not evidence_ids:
        return 0.0 if any(identifier in top for identifier in forbidden_ids) else 1.0
    gold = set(evidence_ids)
    return len(gold.intersection(top)) / len(gold)


def all_evidence(
    retrieved: list[str],
    evidence_ids: tuple[str, ...],
    forbidden_ids: tuple[str, ...],
    *,
    k: int,
) -> float:
    """1 when every gold id is in the top k, else 0.

    An empty gold set scores 1 only when no forbidden id is in the top k.
    """

    top = retrieved[:k]
    if not evidence_ids:
        return 0.0 if any(identifier in top for identifier in forbidden_ids) else 1.0
    return 1.0 if set(evidence_ids).issubset(top) else 0.0


def percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile. ``percent`` is in 0..100."""

    if not values:
        raise ValueError("values must be non-empty")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    ordered = sorted(values)
    rank = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("values must be non-empty")
    return sum(values) / len(values)


__all__ = ["all_evidence", "evidence_recall", "mean", "percentile"]
