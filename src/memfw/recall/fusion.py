"""Reciprocal rank fusion with per-provider weights and a total order.

``score(id) = Σ_p weight_p · w_id / (k + rank_p(id))`` where ``rank_p`` is the
1-based position of ``id`` in provider ``p``'s filtered list and ``w_id`` the
filter weight (e.g. ``disputed`` lowers it). Ties break by best rank, then by
the position of the first provider that returned the id (provider order is
the route order), then by id, so the output never depends on dict or thread
order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_RRF_K = 60


@dataclass(frozen=True, slots=True)
class FusedHit:
    memory_id: str
    score: float
    providers: tuple[str, ...]
    ranks: Mapping[str, int] = field(default_factory=dict)


def rrf_fuse(
    lists: Sequence[tuple[str, Sequence[str]]],
    *,
    weights: Mapping[str, float] | None = None,
    item_weights: Mapping[str, float] | None = None,
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """Fuse ranked id lists, given as ``(provider, ids)`` in route order.

    A duplicate id inside one list keeps its first (best) position. A
    provider weight of 0 removes its contribution but keeps it in
    ``providers`` for labelling.
    """

    if k < 1:
        raise ValueError("k must be at least 1")
    weights = weights or {}
    item_weights = item_weights or {}
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    first_provider: dict[str, int] = {}
    seen_by: dict[str, list[str]] = {}
    ranks: dict[str, dict[str, int]] = {}
    for order, (provider, ids) in enumerate(lists):
        weight = float(weights.get(provider, 1.0))
        if weight < 0:
            raise ValueError("provider weights must not be negative")
        position = 0
        local: set[str] = set()
        for memory_id in ids:
            if memory_id in local:
                continue
            local.add(memory_id)
            position += 1
            contribution = weight * float(item_weights.get(memory_id, 1.0)) / (k + position)
            scores[memory_id] = scores.get(memory_id, 0.0) + contribution
            best_rank[memory_id] = min(best_rank.get(memory_id, position), position)
            first_provider.setdefault(memory_id, order)
            seen_by.setdefault(memory_id, []).append(provider)
            ranks.setdefault(memory_id, {})[provider] = position
    ordered = sorted(
        scores,
        key=lambda memory_id: (
            -scores[memory_id],
            best_rank[memory_id],
            first_provider[memory_id],
            memory_id,
        ),
    )
    if limit is not None:
        ordered = ordered[: max(0, limit)]
    return [
        FusedHit(
            memory_id=memory_id,
            score=scores[memory_id],
            providers=tuple(seen_by[memory_id]),
            ranks=dict(ranks[memory_id]),
        )
        for memory_id in ordered
    ]


__all__ = ["DEFAULT_RRF_K", "FusedHit", "rrf_fuse"]
