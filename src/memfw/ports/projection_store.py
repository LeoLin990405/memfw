"""The projection-store port: a rebuildable derived store (architecture section 4). Stdlib only.

A ``ProjectionStore`` holds data derived from the vault that can be thrown
away and rebuilt at any time; it is never the authority for anything and
never writes the vault. Version 1 of the port carries the K3 typed-edge
projection (``docs/ontology.md``): rows are :class:`Edge` values, built from
vault fields by ``memfw.edges.collect_edges``.

The contract (``memfw.ports.testing.check_projection_store``):

- ``rebuild(edges)`` replaces the whole content with ``edges`` (an edge that
  is not passed again is gone) and returns a :class:`RebuildResult`. Two
  edges with the same :attr:`Edge.key` raise ``ValueError``; anything that is
  not an ``Edge`` raises ``TypeError``. A failed rebuild leaves the previous
  content in place.
- ``version()`` is ``None`` before the first rebuild, then
  :func:`edge_set_version` of the stored edges: the same edges give the same
  version in every implementation, whatever order they were passed in.
- ``query(scope, node=..., direction=..., relations=..., limit=...)`` returns
  the edges leaving (``out``) or entering (``in``) ``node`` that
  :func:`edge_visible` admits for ``scope``, sorted by :attr:`Edge.key`, at
  most ``limit`` (1..``MAX_QUERY_LIMIT``) of them.

**Visibility (fail closed).** Every edge carries the tenant and privacy level
of both endpoints. A memory endpoint has a tenant and a level; a concept
endpoint has a level only when its file states one; attachments, notes,
external references and tasks have neither (``None``, *neutral*): they are
only ever reached through the memory or concept that names them. An edge is
visible to a scope only when every endpoint tenant that is set equals the
scope's tenant and every endpoint level that is set is admitted by the
scope's clearance (``memfw.ports.privacy.admits``; ``unknown`` never is). So
a private endpoint hides the edge, and a query from a hidden node returns
nothing, the same as a query from a node that does not exist.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

from .privacy import PRIVACY_LEVELS, admits, strictest
from .types import Availability

PROJECTION_STORE_PORT_VERSION: Final = 1

#: Node kinds an edge end may have (the K2 relation table's ``node_kinds``).
NODE_KINDS: Final[tuple[str, ...]] = (
    "memory",
    "concept",
    "attachment",
    "note",
    "external",
    "task",
)
#: Levels an endpoint may carry; ``None`` (neutral) is allowed too.
ENDPOINT_LEVELS: Final[frozenset[str]] = frozenset({*PRIVACY_LEVELS, "unknown"})
#: The epistemic caps an edge may carry (``memfw.core.epistemic.EPISTEMIC_VALUES``).
EPISTEMIC_CAPS: Final[tuple[str, ...]] = ("experience", "fact", "judgment", "inference")
#: The most edges one ``query`` call may ask for.
MAX_QUERY_LIMIT: Final = 1000
#: Longest node id or field path an edge may carry.
MAX_ID_CHARS: Final = 512
#: A relation name, as the relation table spells it.
RELATION_RULE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

Direction = Literal["out", "in"]
DIRECTIONS: Final[tuple[str, ...]] = ("out", "in")
_VERSION_PREFIX: Final = "edges-v1:"


def _text(value: object, what: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str")
    if not value or len(value) > MAX_ID_CHARS or any(ch in value for ch in "\n\r\x00"):
        raise ValueError(f"{what} must be 1..{MAX_ID_CHARS} characters on one line")


@dataclass(frozen=True, slots=True)
class Edge:
    """One typed edge between two nodes, with what each end may be shown to.

    ``field`` is the vault field the edge was read from (``about``,
    ``open_question.conflicts``, ``body`` ...). ``valid_from``/``valid_to``
    are ISO 8601 times or ``None``. ``epistemic_max`` is the relation's cap
    from the relation table. Invalid values raise ``ValueError`` (``TypeError``
    for a wrong type) at construction.
    """

    relation: str
    source: str
    target: str
    source_kind: str
    target_kind: str
    field: str
    source_tenant: str | None = None
    target_tenant: str | None = None
    source_privacy: str | None = None
    target_privacy: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    epistemic_max: str = "fact"

    def __post_init__(self) -> None:
        if not isinstance(self.relation, str) or not RELATION_RULE.fullmatch(self.relation):
            raise ValueError("relation must match RELATION_RULE")
        _text(self.source, "source")
        _text(self.target, "target")
        _text(self.field, "field")
        for kind in (self.source_kind, self.target_kind):
            if kind not in NODE_KINDS:
                raise ValueError(f"node kind must be one of {', '.join(NODE_KINDS)}")
        for tenant in (self.source_tenant, self.target_tenant):
            if tenant is not None and not isinstance(tenant, str):
                raise TypeError("tenant must be a str or None")
        for level in (self.source_privacy, self.target_privacy):
            if level is not None and level not in ENDPOINT_LEVELS:
                raise ValueError("privacy must be a privacy level, 'unknown' or None")
        _text(self.valid_from, "valid_from", optional=True)
        _text(self.valid_to, "valid_to", optional=True)
        if self.epistemic_max not in EPISTEMIC_CAPS:
            raise ValueError("epistemic_max must be an epistemic type")

    @property
    def key(self) -> tuple[str, str, str]:
        """``(relation, source, target)``: unique within one projection."""

        return (self.relation, self.source, self.target)

    @property
    def privacy(self) -> str | None:
        """The strictest endpoint level, ``None`` when both ends are neutral."""

        levels = [p for p in (self.source_privacy, self.target_privacy) if p is not None]
        return strictest(levels) if levels else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "source": self.source,
            "target": self.target,
            "source_kind": self.source_kind,
            "target_kind": self.target_kind,
            "field": self.field,
            "source_tenant": self.source_tenant,
            "target_tenant": self.target_tenant,
            "source_privacy": self.source_privacy,
            "target_privacy": self.target_privacy,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "epistemic_max": self.epistemic_max,
        }


@dataclass(frozen=True, slots=True)
class EdgeScope:
    """Who is asking: one tenant (exact match) and a clearance (required).

    ``clearance`` is a set of ``public``/``shared``/``local-private``; there is
    no "no filter" value, so a store can never be asked for everything.
    """

    tenant: str
    clearance: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str):
            raise TypeError("tenant must be a str")
        if not isinstance(self.clearance, frozenset):
            raise TypeError("clearance must be a frozenset")
        if not self.clearance <= set(PRIVACY_LEVELS):
            raise ValueError("clearance may list only " + ", ".join(PRIVACY_LEVELS))


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """What a rebuild stored: the edge count and the new :func:`edge_set_version`."""

    edges: int
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {"edges": self.edges, "version": self.version}


def edge_visible(edge: Edge, scope: EdgeScope) -> bool:
    """Whether ``scope`` may see ``edge`` (see the module docstring). The one rule."""

    for tenant in (edge.source_tenant, edge.target_tenant):
        if tenant is not None and tenant != scope.tenant:
            return False
    for level in (edge.source_privacy, edge.target_privacy):
        if level is not None and not admits(scope.clearance, level):
            return False
    return True


def checked_edges(edges: Iterable[object]) -> list[Edge]:
    """``edges`` as a list sorted by key; ``TypeError``/``ValueError`` as ``rebuild`` raises."""

    out: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Edge):
            raise TypeError("rebuild() takes Edge values")
        if edge.key in seen:
            raise ValueError(f"duplicate edge {edge.relation} {edge.source} -> {edge.target}")
        seen.add(edge.key)
        out.append(edge)
    return sorted(out, key=lambda item: item.key)


def edge_set_version(edges: Iterable[Edge]) -> str:
    """A content version of an edge set: same edges → same string, in any order."""

    digest = hashlib.sha256()
    for edge in sorted(edges, key=lambda item: item.key):
        line = json.dumps(edge.as_dict(), sort_keys=True, ensure_ascii=False)
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return _VERSION_PREFIX + digest.hexdigest()


def check_query(
    scope: object, node: object, direction: object, relations: object, limit: object
) -> frozenset[str] | None:
    """Validate ``query`` arguments; return ``relations`` as a frozenset (or ``None``)."""

    if not isinstance(scope, EdgeScope):
        raise TypeError("scope must be an EdgeScope")
    if not isinstance(node, str):
        raise TypeError("node must be a str")
    if direction not in DIRECTIONS:
        raise ValueError("direction must be 'out' or 'in'")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an int")
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_QUERY_LIMIT}")
    if relations is None:
        return None
    if isinstance(relations, str) or not isinstance(relations, Collection):
        raise TypeError("relations must be a collection of relation names")
    names = frozenset(relations)
    if not all(isinstance(name, str) for name in names):
        raise TypeError("relations must be a collection of relation names")
    return names


def select(
    edges: Sequence[Edge],
    scope: EdgeScope,
    *,
    node: str,
    direction: Direction,
    relations: frozenset[str] | None,
    limit: int,
) -> tuple[Edge, ...]:
    """The reference answer to ``query`` over ``edges`` (sorted by key)."""

    end = "source" if direction == "out" else "target"
    out: list[Edge] = []
    for edge in sorted(edges, key=lambda item: item.key):
        if getattr(edge, end) != node:
            continue
        if relations is not None and edge.relation not in relations:
            continue
        if not edge_visible(edge, scope):
            continue
        out.append(edge)
        if len(out) >= limit:
            break
    return tuple(out)


@runtime_checkable
class ProjectionStore(Protocol):
    """A rebuildable derived store of :class:`Edge` rows (see the module docstring)."""

    name: str

    def available(self) -> Availability: ...

    def rebuild(self, edges: Iterable[Edge]) -> RebuildResult: ...

    def version(self) -> str | None: ...

    def query(
        self,
        scope: EdgeScope,
        *,
        node: str,
        direction: Direction = "out",
        relations: Collection[str] | None = None,
        limit: int = 100,
    ) -> tuple[Edge, ...]: ...


__all__ = [
    "DIRECTIONS",
    "ENDPOINT_LEVELS",
    "EPISTEMIC_CAPS",
    "MAX_ID_CHARS",
    "MAX_QUERY_LIMIT",
    "NODE_KINDS",
    "PROJECTION_STORE_PORT_VERSION",
    "RELATION_RULE",
    "Direction",
    "Edge",
    "EdgeScope",
    "ProjectionStore",
    "RebuildResult",
    "check_query",
    "checked_edges",
    "edge_set_version",
    "edge_visible",
    "select",
]
