"""Bounded path patterns over a ``ProjectionStore`` (K4). Stdlib only.

Cypher-like semantics without Cypher: a pattern is a short chain of typed
steps, matched from one start node, with hard caps on depth, fan-out and the
number of paths. Any ``ProjectionStore`` (the SQLite projection, the
in-memory reference, an external graph adapter) gives the same paths for the
same edges, because the matcher only calls ``ProjectionStore.query``, which
already applies the caller's tenant and clearance.

**Syntax** (``docs/ontology.md``, K4)::

    path  := step+                    (spaces between steps are ignored)
    step  := NAME quant? ">"          follow NAME edges forward  (source → target)
           | "<" NAME quant?          follow NAME edges backward (target → source)
    quant := "*" N | "*" M ".." N     exactly N hops, or M to N hops (1 ≤ M ≤ N)

``about>broader*1..3><about`` reads: from the start node, one ``about`` edge
forward, then one to three ``broader`` edges forward, then one ``about`` edge
backward (memories about a broader concept). ``NAME`` is a relation in the
relation table; an inverse name (``supersedes``, ``narrower``) is the stored
relation walked the other way (``<superseded_by``, ``<broader``).

**Semantics** (borrowed from Cypher):

- an edge is used at most once per path (``DIFFERENT RELATIONSHIPS``); nodes
  may repeat unless ``acyclic`` is set (``ACYCLIC``);
- every step has an upper bound; the sum of the upper bounds is at most
  ``MAX_DEPTH`` hops; a step without a quantifier is one hop; there is no
  unbounded ``*`` and no predicate;
- each node expands at most ``fanout`` neighbours per step (sorted by edge
  key), at most ``max_expansions`` store queries run, and at most ``limit``
  paths are returned. A node's neighbours are read page by page (the
  port's ``after`` cursor) until ``fanout`` + 1 visible ones are found or the
  store has no more, so hidden edges never crowd out visible ones.
  ``truncated`` is set only when a visible result was cut: more than
  ``fanout`` visible neighbours, more than ``limit`` paths, or a cap on
  queries or collected paths ran out. Hidden edges never set it;
- results are sorted by length, then node ids, then edge keys, so the same
  edges and pattern give the same answer.

``node_ok(id, kind)`` is an optional extra visibility check (the runtime
re-reads the live vault); a node it rejects is treated as absent before the
fan-out is counted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .projection_store import (
    MAX_QUERY_LIMIT,
    Direction,
    Edge,
    EdgeScope,
    ProjectionStore,
)

#: The most hops one pattern may take (sum of the steps' upper bounds).
MAX_DEPTH: Final = 4
#: Paths returned by default, and at most.
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100
#: Neighbours one node expands per step, by default and at most.
DEFAULT_FANOUT: Final = 50
MAX_FANOUT: Final = 200
#: Store queries one match may run, and paths it may collect before sorting.
MAX_EXPANSIONS: Final = 2000
MAX_COLLECTED: Final = 10_000
#: Longest pattern text accepted.
MAX_PATTERN_CHARS: Final = 200

_STEP = re.compile(
    r"\s*(?:<(?P<in>[a-z][a-z0-9_]*)(?P<qin>\*\d+(?:\.\.\d+)?)?"
    r"|(?P<out>[a-z][a-z0-9_]*)(?P<qout>\*\d+(?:\.\.\d+)?)?>)\s*"
)


class PatternError(ValueError):
    """A pattern or a cap is invalid (message is safe to show)."""


@dataclass(frozen=True, slots=True)
class Step:
    relation: str
    direction: Direction
    min_hops: int = 1
    max_hops: int = 1

    def as_text(self) -> str:
        quant = ""
        if (self.min_hops, self.max_hops) != (1, 1):
            quant = (
                f"*{self.max_hops}"
                if self.min_hops == self.max_hops
                else f"*{self.min_hops}..{self.max_hops}"
            )
        return f"{self.relation}{quant}>" if self.direction == "out" else f"<{self.relation}{quant}"


@dataclass(frozen=True, slots=True)
class PathPattern:
    steps: tuple[Step, ...]

    @property
    def max_hops(self) -> int:
        return sum(step.max_hops for step in self.steps)

    def as_text(self) -> str:
        """The canonical spelling (inverse names already resolved)."""

        return "".join(step.as_text() for step in self.steps)


def _quant(text: str | None) -> tuple[int, int]:
    if not text:
        return 1, 1
    body = text[1:]
    low_text, _, high_text = body.partition("..")
    low = int(low_text)
    high = int(high_text) if high_text else low
    if low < 1 or high < low:
        raise PatternError("a quantifier needs 1 <= M <= N")
    return low, high


def parse_path(
    text: str,
    *,
    relations: Collection[str] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> PathPattern:
    """Parse ``text`` (see the module docstring). Raises :class:`PatternError`.

    ``relations`` are the names a step may use after ``aliases`` (inverse name
    → stored name, walked the other way) are applied; ``None`` accepts any
    well-formed name.
    """

    if not isinstance(text, str) or not text.strip():
        raise PatternError("path is empty")
    if len(text) > MAX_PATTERN_CHARS:
        raise PatternError(f"path is longer than {MAX_PATTERN_CHARS} characters")
    aliases = aliases or {}
    steps: list[Step] = []
    position = 0
    while position < len(text):
        found = _STEP.match(text, position)
        if found is None or found.end() == position:
            raise PatternError(f"cannot read the path at character {position + 1}")
        position = found.end()
        if found["in"] is not None:
            name, direction, quant = found["in"], "in", found["qin"]
        else:
            name, direction, quant = found["out"], "out", found["qout"]
        low, high = _quant(quant)
        if name in aliases:
            name = aliases[name]
            direction = "out" if direction == "in" else "in"
        if relations is not None and name not in relations:
            raise PatternError(f"unknown relation {name!r}")
        steps.append(Step(name, direction, low, high))  # type: ignore[arg-type]
    pattern = PathPattern(tuple(steps))
    if pattern.max_hops > MAX_DEPTH:
        raise PatternError(f"a path may take at most {MAX_DEPTH} hops")
    return pattern


@dataclass(frozen=True, slots=True)
class MatchedPath:
    nodes: tuple[tuple[str, str], ...]
    edges: tuple[Edge, ...]

    def sort_key(self) -> tuple[Any, ...]:
        return (
            len(self.edges),
            tuple(node for node, _kind in self.nodes),
            tuple(edge.key for edge in self.edges),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [{"id": node, "kind": kind} for node, kind in self.nodes],
            "edges": [
                {
                    "relation": edge.relation,
                    "source": edge.source,
                    "target": edge.target,
                    "field": edge.field,
                    "epistemic_max": edge.epistemic_max,
                    "valid_from": edge.valid_from,
                    "valid_to": edge.valid_to,
                }
                for edge in self.edges
            ],
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    paths: tuple[MatchedPath, ...]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {"paths": [path.as_dict() for path in self.paths], "truncated": self.truncated}


NodeCheck = Callable[[str, str], bool]


@dataclass(slots=True)
class _Rows:
    """Store rows read so far for one (node, direction, relation) within a match."""

    rows: list[Edge] = field(default_factory=list)
    done: bool = False
    next_page: int = 0


def _cap(value: object, name: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PatternError(f"{name} must be an integer")
    if not 1 <= value <= high:
        raise PatternError(f"{name} must be 1..{high}")
    return value


def match(
    store: ProjectionStore,
    scope: EdgeScope,
    start: str,
    pattern: PathPattern,
    *,
    start_kind: str = "memory",
    limit: int = DEFAULT_LIMIT,
    fanout: int = DEFAULT_FANOUT,
    acyclic: bool = False,
    node_ok: NodeCheck | None = None,
    max_expansions: int = MAX_EXPANSIONS,
) -> MatchResult:
    """Paths from ``start`` that follow ``pattern`` (see the module docstring)."""

    limit = _cap(limit, "limit", MAX_LIMIT)
    fanout = _cap(fanout, "fanout", MAX_FANOUT)
    max_expansions = _cap(max_expansions, "max_expansions", MAX_EXPANSIONS)
    if not isinstance(pattern, PathPattern) or not pattern.steps:
        raise PatternError("pattern must be a parsed PathPattern")
    if pattern.max_hops > MAX_DEPTH:
        raise PatternError(f"a path may take at most {MAX_DEPTH} hops")
    if node_ok is not None and not node_ok(start, start_kind):
        return MatchResult((), False)
    steps = pattern.steps
    found: list[MatchedPath] = []
    state = {"expansions": 0, "truncated": False}
    first_page = min(MAX_QUERY_LIMIT, max(fanout + 1, 100))
    # Per-node cache within this match: the store rows read so far for
    # (node, direction, relation), and whether the store has no more.
    cache: dict[tuple[str, str, str], _Rows] = {}

    def neighbours_of(
        node: str, step: Step, used: set[tuple[str, str, str]], visited: set[str]
    ) -> list[tuple[Edge, str, str]]:
        """Up to ``fanout`` visible neighbours, paging past hidden edges."""

        known = cache.setdefault((node, step.direction, step.relation), _Rows())
        out: list[tuple[Edge, str, str]] = []
        position = 0
        fetched = False
        while True:
            while position < len(known.rows):
                edge = known.rows[position]
                position += 1
                if edge.key in used:
                    continue
                if step.direction == "out":
                    nxt, kind = edge.target, edge.target_kind
                else:
                    nxt, kind = edge.source, edge.source_kind
                if acyclic and nxt in visited:
                    continue
                if node_ok is not None and not node_ok(nxt, kind):
                    continue
                out.append((edge, nxt, kind))
                if len(out) > fanout:
                    state["truncated"] = True  # a visible neighbour is cut
                    return out[:fanout]
            if known.done:
                return out  # the store has no more
            if fetched:
                # The walk's expansion paid for its first page; later pages cost one each.
                if state["expansions"] >= max_expansions:
                    state["truncated"] = True  # the query cap ran out mid-node
                    return out
                state["expansions"] += 1
            fetched = True
            # Pages grow (doubling up to MAX_QUERY_LIMIT) while hidden rows fill them.
            size = known.next_page or first_page
            page = store.query(
                scope,
                node=node,
                direction=step.direction,
                relations=(step.relation,),
                limit=size,
                after=known.rows[-1].key if known.rows else None,
            )
            known.rows.extend(page)
            known.next_page = min(MAX_QUERY_LIMIT, size * 2)
            if len(page) < size:
                known.done = True

    def walk(
        index: int,
        taken: int,
        node: str,
        nodes: tuple[tuple[str, str], ...],
        edges: tuple[Edge, ...],
    ) -> None:
        if state["expansions"] >= max_expansions or len(found) >= MAX_COLLECTED:
            state["truncated"] = True
            return
        state["expansions"] += 1
        step = steps[index]
        used = {edge.key for edge in edges}
        visited = {item for item, _kind in nodes}
        neighbours = neighbours_of(node, step, used, visited)
        for edge, nxt, kind in neighbours:
            hops = taken + 1
            path_nodes = (*nodes, (nxt, kind))
            path_edges = (*edges, edge)
            if hops >= step.min_hops:
                if index + 1 == len(steps):
                    if len(found) >= MAX_COLLECTED:
                        state["truncated"] = True
                        return
                    found.append(MatchedPath(path_nodes, path_edges))
                else:
                    walk(index + 1, 0, nxt, path_nodes, path_edges)
            if hops < step.max_hops:
                walk(index, hops, nxt, path_nodes, path_edges)

    walk(0, 0, start, ((start, start_kind),), ())
    found.sort(key=MatchedPath.sort_key)
    truncated = bool(state["truncated"]) or len(found) > limit
    return MatchResult(tuple(found[:limit]), truncated)


__all__ = [
    "DEFAULT_FANOUT",
    "DEFAULT_LIMIT",
    "MAX_COLLECTED",
    "MAX_DEPTH",
    "MAX_EXPANSIONS",
    "MAX_FANOUT",
    "MAX_LIMIT",
    "MAX_PATTERN_CHARS",
    "MatchResult",
    "MatchedPath",
    "NodeCheck",
    "PathPattern",
    "PatternError",
    "Step",
    "match",
    "parse_path",
]
