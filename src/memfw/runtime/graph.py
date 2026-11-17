"""The edge projection (K3) and bounded path patterns (K4) as one caller sees them.

Wiring only: ``memfw.edges`` builds and stores the edges behind the
``ProjectionStore`` port, ``memfw.ports.patterns`` parses and matches a path.
This module picks the store, turns the caller's settings into an
``EdgeScope`` and adds the live check every other exit applies:

* **tenant**: one tenant, exact match (default ``""``), like the Runtime and
  ``memfw project obsidian``;
* **clearance**: a set of privacy levels, default ``public,shared``; there is
  no "no filter" value here (fail closed). ``unknown`` is never admitted;
* **allowed sources**: when set, a memory from another source is hidden;
* **live vault**: every memory on a path is re-read from the vault and
  checked against the three rules above, with the vault's current
  ``legacy_privacy_default``; a concept file that states a privacy level is
  re-read too. A memory that was deleted or made private after the last
  rebuild is therefore hidden before the next one. A concept node must still
  exist in the vault: a concept file (or a label/alias of one, through the
  alias index), else a value a memory the caller may see names in ``about``
  or ``applies_to`` (read on first need, once per match). Any other concept
  node, such as one only a deleted memory named, is hidden;
* **which vault**: the projection must have been built from this vault
  (``memfw.edges.vault_store`` checks the identity it records), else the
  match fails with "projection belongs to another vault: rebuild".

A hidden node is treated as absent: a path through it is never returned, it
does not count towards the fan-out, and a start node the caller may not see
answers exactly like a start node that does not exist (no paths). Nothing
here writes the vault; only ``rebuild_edge_projection`` writes, and only the
projection file.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext
from typing import Any, Final

from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.edges import (
    INVERSE_ALIASES,
    EdgeError,
    SqliteProjectionStore,
    concept_level,
    concept_ref,
    legacy_path,
    rebuild,
    vault_store,
)
from memfw.ontology import builtin_relation_table, load_concepts, project_aliases
from memfw.ports.patterns import (
    DEFAULT_FANOUT,
    DEFAULT_LIMIT,
    PathPattern,
    PatternError,
    match,
    parse_path,
)
from memfw.ports.privacy import REMOTE_CLEARANCE, admits, effective_level
from memfw.ports.projection_store import EdgeScope, ProjectionStore

#: The clearance a graph query gets when the caller sets none.
DEFAULT_CLEARANCE: Final = REMOTE_CLEARANCE
_UNKNOWN_KIND: Final = "unknown"


class GraphError(ValueError):
    """A graph request cannot be answered (message is safe to show)."""


def rebuild_edge_projection(repository: Repository) -> dict[str, Any]:
    """Rebuild the edge projection from the vault: ``{path, edges, version}``.

    ``path`` is the projection file (outside the vault, ``memfw.edges``). When
    an ``edges.sqlite3`` from an earlier build sits at the vault root, the
    answer also carries ``legacy_file`` (its name): it is ignored and never
    deleted here, and the caller may suggest removing it.
    """

    store = vault_store(repository)
    result = rebuild(repository, store)
    payload: dict[str, Any] = {"path": str(store.path), **result.as_dict()}
    legacy = legacy_path(repository.root)
    if legacy.is_file() or legacy.is_symlink():
        payload["legacy_file"] = legacy.name
    return payload


class _LiveCheck:
    """``node_ok`` for the matcher: re-reads memories and concepts from the vault."""

    def __init__(
        self,
        repository: Repository,
        *,
        tenant: str,
        clearance: frozenset[str],
        allowed_sources: tuple[str, ...],
    ) -> None:
        self.repository = repository
        self.tenant = tenant
        self.clearance = clearance
        self.allowed_sources = allowed_sources
        self.legacy = repository.legacy_privacy_default()
        self.concepts = load_concepts(repository.root)
        self.aliases = project_aliases(self.concepts)
        self._memories: dict[str, bool] = {}
        self._named: frozenset[str] | None = None

    def memory(self, memory_id: str) -> MemoryDocument | None:
        try:
            return self.repository.get(memory_id)
        except (MemfwError, OSError, UnicodeDecodeError, ValueError):
            return None

    def memory_visible(self, memory_id: str) -> bool:
        if memory_id not in self._memories:
            document = self.memory(memory_id)
            self._memories[memory_id] = document is not None and self._admits(document)
        return self._memories[memory_id]

    def _admits(self, document: MemoryDocument) -> bool:
        if document.tenant != self.tenant:
            return False
        if self.allowed_sources and document.source not in self.allowed_sources:
            return False
        level = effective_level(
            document.privacy,
            document.tags,
            legacy=self.legacy,
            sensitivity=document.top_level_sensitivity,
        )
        return admits(self.clearance, level)

    def named_concepts(self) -> frozenset[str]:
        """``about``/``applies_to`` values of the memories the caller may see (read once)."""

        if self._named is None:
            names: set[str] = set()
            try:
                documents = self.repository.list()
            except (MemfwError, OSError, UnicodeDecodeError, ValueError):
                documents = []  # fail closed: no unregistered concept is shown
            for document in documents:
                visible = self._admits(document)
                self._memories.setdefault(document.id, visible)
                if not visible:
                    continue
                for value in (*document.about, *document.applies_to):
                    if isinstance(value, str) and value.strip():
                        names.add(value.strip())
            self._named = frozenset(names)
        return self._named

    def concept_visible(self, node: str) -> bool:
        if node in self.concepts:
            level = concept_level(self.concepts[node])
            return level is None or admits(self.clearance, level)
        found = concept_ref(self.concepts, self.aliases, node)
        if found is not None:
            return found[1] is None or admits(self.clearance, found[1])
        # Not a concept of this vault: shown only while a visible memory names it.
        return node in self.named_concepts()

    def __call__(self, node: str, kind: str) -> bool:
        if kind == "memory":
            return self.memory_visible(node)
        if kind == "concept":
            return self.concept_visible(node)
        return True

    def start(self, value: str) -> tuple[str, str]:
        """``(id, kind)`` of the node ``value`` names; the kind may be ``unknown``."""

        text = value.strip()
        bare = text
        for prefix in ("memory:", "mem:"):
            if bare.lower().startswith(prefix):
                bare = bare[len(prefix) :]
                break
        if self.memory(bare) is not None:
            return bare, "memory"
        found = concept_ref(self.concepts, self.aliases, text)
        if found is not None:
            return found[0], "concept"
        return text, _UNKNOWN_KIND


def _clearance(value: Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return DEFAULT_CLEARANCE
    try:
        return EdgeScope("", frozenset(value)).clearance
    except (TypeError, ValueError) as exc:
        raise GraphError(str(exc)) from exc


def graph_match(
    repository: Repository,
    *,
    start: str,
    path: str,
    tenant: str = "",
    clearance: Iterable[str] | None = None,
    allowed_sources: Iterable[str] = (),
    limit: int = DEFAULT_LIMIT,
    fanout: int = DEFAULT_FANOUT,
    acyclic: bool = False,
    store: ProjectionStore | None = None,
) -> dict[str, Any]:
    """Paths from ``start`` along ``path`` that the caller may see.

    ``store`` defaults to the vault's edge projection; a missing projection,
    or one built from another vault, raises ``GraphError`` (run ``memfw graph
    rebuild``). The answer is
    ``{from, path, paths, truncated}``; ``path`` is the canonical spelling
    (inverse names resolved).
    """

    if not isinstance(start, str) or not start.strip():
        raise GraphError("--from is empty")
    table = builtin_relation_table()
    stored = table.names() - set(INVERSE_ALIASES)
    try:
        pattern = parse_path(path, relations=stored, aliases=INVERSE_ALIASES)
    except PatternError as exc:
        raise GraphError(str(exc)) from exc
    scope = EdgeScope(tenant, _clearance(clearance))
    try:
        backend: ProjectionStore = store if store is not None else vault_store(repository)
    except MemfwError as exc:
        raise GraphError(f"vault identity unreadable: {exc}") from exc
    # One read-only connection for the whole match (SQLite store).
    with backend.reading() if isinstance(backend, SqliteProjectionStore) else nullcontext():
        return _match(
            repository,
            backend,
            scope,
            pattern,
            start,
            tenant,
            allowed_sources,
            limit=limit,
            fanout=fanout,
            acyclic=acyclic,
        )


def _match(
    repository: Repository,
    backend: ProjectionStore,
    scope: EdgeScope,
    pattern: PathPattern,
    start: str,
    tenant: str,
    allowed_sources: Iterable[str],
    *,
    limit: int,
    fanout: int,
    acyclic: bool,
) -> dict[str, Any]:
    try:
        if backend.version() is None:
            raise GraphError("edge projection missing: run `memfw graph rebuild`")
    except EdgeError as exc:
        raise GraphError(str(exc)) from exc
    check = _LiveCheck(
        repository,
        tenant=tenant,
        clearance=scope.clearance,
        allowed_sources=tuple(allowed_sources),
    )
    node, kind = check.start(start)
    try:
        result = match(
            backend,
            scope,
            node,
            pattern,
            start_kind=kind,
            limit=limit,
            fanout=fanout,
            acyclic=acyclic,
            node_ok=check,
        )
    except PatternError as exc:
        raise GraphError(str(exc)) from exc
    except EdgeError as exc:
        raise GraphError(str(exc)) from exc
    paths: list[dict[str, Any]] = []
    truncated = result.truncated
    for found in result.paths:
        payload = found.as_dict()
        if payload["nodes"][0]["kind"] == _UNKNOWN_KIND:
            first = found.edges[0]
            real = first.source_kind if first.source == node else first.target_kind
            if not check(node, real):
                # A start the vault no longer shows (a memory deleted or hidden
                # since the rebuild) answers like one that never existed.
                paths, truncated = [], False
                break
            payload["nodes"][0]["kind"] = real
        paths.append(payload)
    return {
        "from": start,
        "path": pattern.as_text(),
        "paths": paths,
        "truncated": truncated,
    }


__all__ = [
    "DEFAULT_CLEARANCE",
    "GraphError",
    "graph_match",
    "rebuild_edge_projection",
]
