"""Reference ``ProjectionStore``: the edge projection kept in memory. Stdlib only.

``MemoryProjectionStore`` is the second implementation next to
``memfw.edges.SqliteProjectionStore`` (the persistent one). Both pass
``memfw.ports.testing.check_projection_store`` and give the same K4 paths for
the same edges. It holds nothing but the edges it was given.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable

from ..projection_store import (
    Direction,
    Edge,
    EdgeKey,
    EdgeScope,
    RebuildResult,
    check_after,
    check_query,
    checked_edges,
    edge_set_version,
    select,
)
from ..types import Availability


class MemoryProjectionStore:
    """Edges in a list, sorted by key; ``rebuild`` swaps the whole list."""

    name = "memory"

    def __init__(self) -> None:
        self._edges: tuple[Edge, ...] | None = None
        self._version: str | None = None
        self._lock = threading.Lock()

    def available(self) -> Availability:
        return Availability.ok()

    def rebuild(self, edges: Iterable[Edge]) -> RebuildResult:
        rows = tuple(checked_edges(edges))
        version = edge_set_version(rows)
        with self._lock:
            self._edges = rows
            self._version = version
        return RebuildResult(edges=len(rows), version=version)

    def version(self) -> str | None:
        with self._lock:
            return self._version

    def query(
        self,
        scope: EdgeScope,
        *,
        node: str,
        direction: Direction = "out",
        relations: Collection[str] | None = None,
        limit: int = 100,
        after: EdgeKey | None = None,
    ) -> tuple[Edge, ...]:
        names = check_query(scope, node, direction, relations, limit, after)
        with self._lock:
            rows = self._edges or ()
        return select(
            rows,
            scope,
            node=node,
            direction=direction,
            relations=names,
            limit=limit,
            after=check_after(after),
        )


__all__ = ["MemoryProjectionStore"]
