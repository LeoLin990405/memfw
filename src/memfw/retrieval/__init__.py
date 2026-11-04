"""Source-backed retrieval with automatic cold restoration and access events."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, Protocol

from memfw.core.models import EventKind, Tier
from memfw.index import MemoryIndex


class Retriever(Protocol):
    def retrieve(self, query: str, *, limit: int) -> list[str]: ...


class VaultRetriever:
    """Search the disposable index and warm cold hits before returning them.

    Latency metrics stay in memory. They never enter event details or Markdown,
    so measuring a run cannot change the durable vault bytes.
    """

    def __init__(
        self,
        repository: Any,
        index: MemoryIndex | None = None,
        *,
        ranking: str = "bm25",
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self.repository = repository
        self.index = index or MemoryIndex(repository.index_path)
        if ranking not in {"bm25", "count"}:
            raise ValueError("ranking must be 'bm25' or 'count'")
        self.ranking = ranking
        self.timer = timer
        self.last_metrics: dict[str, Any] = {
            "elapsed_seconds": 0.0,
            "result_count": 0,
            "cold_hit_count": 0,
            "cold_restore_seconds": 0.0,
            "cold_restore_seconds_by_memory": {},
        }

    def retrieve(self, query: str, *, limit: int = 20) -> list[str]:
        with self.repository._locked():
            started = self.timer()
            if not self.index.path.exists():
                self.index.rebuild(self.repository)
            rows = (
                self.index.ranked_search(query, limit=max(0, limit), ranking=self.ranking)
                if limit > 0
                else []
            )
            identifiers = [memory_id for memory_id, _ in rows]
            restore_times: dict[str, float] = {}
            for memory_id in identifiers:
                document = self.repository.get(memory_id)
                if document.is_stub or document.tier == Tier.COLD:
                    restore_started = self.timer()
                    self.repository.autowarm(memory_id, actor="mechanism")
                    restore_times[memory_id] = max(0.0, self.timer() - restore_started)
                self.repository.capture_event(
                    EventKind.MEMORY_ACCESS,
                    actor="mechanism",
                    details={"memory_id": memory_id, "operation": "retrieve"},
                )
            elapsed = max(0.0, self.timer() - started)
        self.last_metrics = {
            "elapsed_seconds": elapsed,
            "result_count": len(identifiers),
            "cold_hit_count": len(restore_times),
            "cold_restore_seconds": sum(restore_times.values()),
            "cold_restore_seconds_by_memory": restore_times,
        }
        return identifiers


__all__ = ["Retriever", "VaultRetriever"]
