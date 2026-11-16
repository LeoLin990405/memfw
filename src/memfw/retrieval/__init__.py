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

    ``record_access`` (default true) appends one ``memory_access`` event per
    returned id; ``autowarm`` (default true) restores a cold or stub hit to
    hot. A caller that filters the ids afterwards passes false for both and
    does that itself for what it keeps. ``admit``, when given, is asked about
    each hit's document first: a hit it rejects (another tenant, say) is still
    returned, for the caller to drop, but is neither warmed nor logged.
    """

    def __init__(
        self,
        repository: Any,
        index: MemoryIndex | None = None,
        *,
        ranking: str = "bm25",
        timer: Callable[[], float] = perf_counter,
        record_access: bool = True,
        autowarm: bool = True,
        admit: Callable[[Any], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.record_access = record_access
        self.autowarm = autowarm
        self.admit = admit
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
            if not self.index.schema_is_current():
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
                if self.admit is not None and not self.admit(document):
                    continue
                if self.autowarm and (document.is_stub or document.tier == Tier.COLD):
                    restore_started = self.timer()
                    self.repository.autowarm(memory_id, actor="mechanism")
                    restore_times[memory_id] = max(0.0, self.timer() - restore_started)
                if self.record_access:
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
