"""Usage, policy, and physical-vault statistics."""

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from memfw.core.models import EventKind, Layer, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.runtime import (
    RuntimeStats,
    measure_logical_breakdown,
    measure_physical_breakdown,
    measure_vault,
)


class _PolicyUsage(Protocol):
    count: int
    bytes: int


class _Budget(Protocol):
    max_bytes: int | None
    max_count: int | None


class _Decision(Protocol):
    reason: str
    memory_id: str


class _Plan(Protocol):
    usage: _PolicyUsage
    limits: _Budget
    decisions: tuple[_Decision, ...]
    forgotten_ids: tuple[str, ...]


@dataclass(frozen=True)
class UsageStats:
    count: int
    bytes_used: int
    anchor_count: int
    cold_count: int


def measure(documents: Iterable[MemoryDocument]) -> UsageStats:
    docs = tuple(documents)
    return UsageStats(
        len(docs),
        sum(len(d.body.encode("utf-8")) for d in docs),
        sum(d.anchor is not None or d.layer.value == "anchor" for d in docs),
        sum(d.tier.value == "cold" for d in docs),
    )


def proposal_stats(result: _Plan) -> dict[str, int]:
    usage = result.usage
    forgotten = set(result.forgotten_ids)
    anchors = sum(decision.reason == "protected_anchor" for decision in result.decisions)
    return {
        "retained_count": usage.count,
        "retained_bytes": usage.bytes,
        "forget_proposal_count": len(forgotten),
        "over_bytes": (
            max(0, usage.bytes - result.limits.max_bytes)
            if result.limits.max_bytes is not None
            else 0
        ),
        "over_count": (
            max(0, usage.count - result.limits.max_count)
            if result.limits.max_count is not None
            else 0
        ),
        "anchor_floor_unmet": 0,
        "cold_floor_unmet": 0,
        "anchor_retained": anchors,
    }


class MemoryStats:
    """Compatibility implementation of the day-one stats protocol."""

    def __init__(self, documents: Iterable[MemoryDocument] = ()) -> None:
        self.documents = tuple(documents)

    def count_by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for doc in self.documents:
            counts[doc.tier.value] = counts.get(doc.tier.value, 0) + 1
        return counts

    def source_coverage(self) -> float:
        if not self.documents:
            return 0.0
        return sum(bool(doc.source) for doc in self.documents) / len(self.documents)


@dataclass(frozen=True)
class VaultStats:
    """Snapshot with body budget usage kept separate from disk allocation."""

    memory_count: int
    hot_count: int
    cold_count: int
    hot_ratio: float
    cold_ratio: float
    layer_counts: dict[str, int]
    body_bytes: int
    hot_body_bytes: int
    cold_body_bytes: int
    physical_disk_bytes: int
    recent_forget_count: int
    recent_forget_window_days: int
    physical_breakdown: dict[str, int] = field(default_factory=dict)
    body_budget_scope: str = (
        "UTF-8 Markdown body bytes only; logs, indexes, archives, frontmatter, "
        "and physical allocation are excluded"
    )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def measure_runtime(
    repository: Repository,
    *,
    now: datetime | None = None,
    recent_days: int = 30,
) -> VaultStats:
    """Measure body bytes, layer/tier distribution, physical allocation, and recent forgets."""

    if recent_days < 0:
        raise ValueError("recent_days must be non-negative")
    point = now or repository._now()
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    docs = repository.list()
    hot = [doc for doc in docs if doc.tier == Tier.HOT]
    cold = [doc for doc in docs if doc.tier == Tier.COLD]

    def body_bytes(rows: Iterable[MemoryDocument]) -> int:
        from memfw.curation.engine import _with_archived_body

        total = 0
        for listed_document in rows:
            document = _with_archived_body(repository, listed_document)
            total += len(document.body.encode("utf-8"))
        return total

    total_count = len(docs)
    forget_count = sum(
        1
        for event in repository.events()
        if (
            event.kind == EventKind.FORGET
            and event.details.get("operation") in {"cool", "delete"}
            or event.kind == EventKind.DELETE
            and event.details.get("operation") == "delete"
        )
        and point - timedelta(days=recent_days) <= event.timestamp <= point
    )
    allocated = measure_physical_disk_bytes(repository.root)
    layer_counts = {layer.value: sum(doc.layer == layer for doc in docs) for layer in Layer}
    return VaultStats(
        memory_count=total_count,
        hot_count=len(hot),
        cold_count=len(cold),
        hot_ratio=(len(hot) / total_count if total_count else 0.0),
        cold_ratio=(len(cold) / total_count if total_count else 0.0),
        layer_counts=layer_counts,
        body_bytes=body_bytes(docs),
        hot_body_bytes=body_bytes(hot),
        cold_body_bytes=body_bytes(cold),
        physical_disk_bytes=allocated,
        recent_forget_count=forget_count,
        recent_forget_window_days=recent_days,
        physical_breakdown=measure_physical_breakdown(repository),
    )


def measure_physical_disk_bytes(root: Path) -> int:
    """Count allocated vault bytes without parsing Markdown, logs, or archives.

    This is the physical-budget metric's filesystem primitive. It intentionally
    excludes transient lock, pending-transaction, and atomic-write temporary files.
    """

    allocated = 0
    if root.exists():
        for path in root.rglob("*"):
            if (
                path.is_file()
                and path.name not in {".memfw.lock", ".pending.json"}
                and not path.name.endswith(".tmp")
            ):
                stat = path.stat()
                allocated += stat.st_blocks * 512 if hasattr(stat, "st_blocks") else stat.st_size
    return allocated


__all__ = [
    "BM25Scorer",
    "MemoryStats",
    "RuntimeStats",
    "UsageStats",
    "VaultStats",
    "measure",
    "measure_runtime",
    "measure_physical_disk_bytes",
    "measure_physical_breakdown",
    "measure_logical_breakdown",
    "measure_vault",
    "proposal_stats",
]
