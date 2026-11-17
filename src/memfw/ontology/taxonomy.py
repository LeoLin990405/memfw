"""Taxonomy hierarchy (K1): optional ``broader`` on concept files.

``broader: [<oid>, ...]`` names a concept's parents. ``narrower`` is never
written; :func:`narrower_index` derives it. Nothing on the search or context
path reads either: query expansion along ``broader`` is not implemented.

Lint (:func:`lint_taxonomy`) only reports on concepts that carry ``broader``,
so a vault without the field gets no new review items:

* ``broader_invalid``: the value is not an oid or a list of oids;
* ``broader_dangling``: a parent oid has no concept file;
* ``broader_merged``: a parent was merged into another concept; ``resolved``
  names the oid to point at instead;
* ``broader_cycle``: following ``broader`` (through merges) loops back. A
  merged concept's own ``broader`` counts for the concept it was merged into,
  so a loop through it is found too;
* ``broader_on_merged``: a concept that was merged into another still carries
  ``broader``; ``resolved`` names the concept its parents now count for.
"""

from __future__ import annotations

from typing import Any

from .model import _TAXONOMY_KEYS, Concept, _resolve

#: Concept-file keys owned by the taxonomy. The generic relation lint skips them.
TAXONOMY_KEYS = _TAXONOMY_KEYS


def broader_of(concept: Concept) -> tuple[str, ...] | None:
    """The concept's ``broader`` oids, ``()`` when absent, ``None`` when malformed."""
    if "broader" not in concept.extras:
        return ()
    return _oids(concept.extras["broader"])


def narrower_index(concepts: dict[str, Concept]) -> dict[str, tuple[str, ...]]:
    """Parent oid → child oids, derived from ``broader``. Parents follow ``merged_into``."""
    children: dict[str, set[str]] = {}
    for oid, concept in concepts.items():
        for parent in broader_of(concept) or ():
            if parent not in concepts:
                continue
            target = _resolve(parent, concepts)
            if target != oid:
                children.setdefault(target, set()).add(oid)
    return {parent: tuple(sorted(oids)) for parent, oids in sorted(children.items())}


def lint_taxonomy(concepts: dict[str, Concept]) -> list[dict[str, Any]]:
    """Review items for ``broader``. Empty when no concept carries it."""
    items: list[dict[str, Any]] = []
    graph: dict[str, list[str]] = {}
    for oid in sorted(concepts):
        concept = concepts[oid]
        if "broader" not in concept.extras:
            continue
        parents = broader_of(concept)
        if parents is None:
            items.append({"kind": "broader_invalid", "oid": oid, "detail": "broader"})
            continue
        source = _resolve(oid, concepts)
        if concept.merged_into:
            items.append(
                {"kind": "broader_on_merged", "oid": oid, "detail": "broader", "resolved": source}
            )
        edges: list[str] = graph.get(source, [])
        for parent in parents:
            if parent not in concepts:
                items.append({"kind": "broader_dangling", "oid": oid, "detail": parent})
                continue
            target = _resolve(parent, concepts)
            if target != parent:
                items.append(
                    {
                        "kind": "broader_merged",
                        "oid": oid,
                        "detail": parent,
                        "resolved": target,
                    }
                )
            if target == source and parent != oid:
                # A self loop made only by following merges (A broader B, B merged
                # into A; or a merged concept's broader naming its target) is
                # not a cycle in the taxonomy. A literal ``broader: [self]`` is.
                continue
            if target not in edges:
                edges.append(target)
        if edges:
            graph[source] = edges
    for members in _cycles(graph):
        items.append(
            {
                "kind": "broader_cycle",
                "oid": members[0],
                "detail": " ".join(members),
                "oids": list(members),
            }
        )
    return items


def _oids(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else None
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        if item.strip() not in out:
            out.append(item.strip())
    return tuple(out)


def _cycles(graph: dict[str, list[str]]) -> list[tuple[str, ...]]:
    """Strongly connected components that loop (size > 1, or a self edge). Iterative Tarjan."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[tuple[str, ...]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work[-1]
            if position == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = graph.get(node, [])
            if position < len(successors):
                work[-1] = (node, position + 1)
                nxt = successors[position]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members: list[str] = []
                while True:
                    item = stack.pop()
                    on_stack.discard(item)
                    members.append(item)
                    if item == node:
                        break
                if len(members) > 1 or node in graph.get(node, []):
                    found.append(tuple(sorted(members)))
    return sorted(found)


__all__ = ["TAXONOMY_KEYS", "broader_of", "lint_taxonomy", "narrower_index"]
