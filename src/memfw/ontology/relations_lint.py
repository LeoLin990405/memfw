"""Check vault fields against the relation-type table. Read-only, opt-in.

``lint_relations`` is reached only through ``memfw concepts lint-relations``;
the existing ``memfw concepts list`` review, search, context and export never
call it. It reads frontmatter and writes nothing.

Review kinds:

* everything :func:`memfw.ontology.taxonomy.lint_taxonomy` reports;
* ``relation_unknown_type``: a concept-file key that is not a core key and not
  a relation the table lets a concept file carry;
* ``relation_dangling``: a memory's ``about``/``applies_to`` names no concept
  (checked only when the vault has ``concepts/``), or its ``superseded_by``
  names no memory (a ``memory:``/``mem:`` prefix is stripped, as below);
* ``relation_cross_tenant``: a memory's ``superseded_by``, ``evidence``,
  ``experience.evidence``, ``related``, ``open_question.conflicts`` or
  ``open_question.resolved_by`` names a memory of another tenant (a
  ``memory:``/``mem:`` prefix, in every one of these fields, is stripped the
  way the edge projection strips it). The edge
  projection (``memfw.edges``, K3) drops such an edge when it is built, and
  its query admits one caller tenant at a time. That is not a promise about
  every exit: each applies its own tenant rule (the plain-file projection,
  for one, admits the tenants ``""`` and ``fleet`` together by default), so
  the edge is reported here rather than assumed to be hidden.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memfw.core.models import fold_temporal_aliases

from .model import _CORE_KEYS, _frontmatter, load_concepts
from .relations import RelationTable, builtin_relation_table
from .taxonomy import lint_taxonomy


def lint_relations(vault: Path, table: RelationTable | None = None) -> list[dict[str, Any]]:
    """Review list for typed relations in ``vault``. Nothing is written."""
    root = Path(vault)
    table = table or builtin_relation_table()
    concepts = load_concepts(root)
    items: list[dict[str, Any]] = list(lint_taxonomy(concepts))
    allowed = table.written_on("concept")
    for oid in sorted(concepts):
        for key in sorted(concepts[oid].extras):
            if key not in allowed and key not in _CORE_KEYS:
                items.append(
                    {"kind": "relation_unknown_type", "oid": oid, "relation": key, "detail": key}
                )
    memories = _memories(root)
    tenants = {memory_id: str(meta.get("tenant") or "") for memory_id, meta in memories}
    concepts_present = (root / "concepts").is_dir() and not (root / "concepts").is_symlink()
    for memory_id, meta in memories:
        tenant = tenants[memory_id]
        if concepts_present:
            for relation in ("about", "applies_to"):
                for target in _strings(meta.get(relation)):
                    if target not in concepts:
                        items.append(_item("relation_dangling", memory_id, relation, target))
        successor = meta.get("superseded_by")
        if isinstance(successor, str) and successor.strip():
            target = _bare(successor.strip())
            if target not in tenants:
                items.append(_item("relation_dangling", memory_id, "superseded_by", target))
            elif tenants[target] != tenant:
                items.append(_item("relation_cross_tenant", memory_id, "superseded_by", target))
        experience = meta.get("experience")
        refs = [("evidence", _bare(ref)) for ref in _strings(meta.get("evidence"))]
        if isinstance(experience, dict):
            refs.extend(
                ("experience_of", _bare(ref)) for ref in _strings(experience.get("evidence"))
            )
        refs.extend(("related", _bare(ref)) for ref in _strings(meta.get("related")))
        question = meta.get("open_question")
        if isinstance(question, dict):
            refs.extend(("conflicts", _bare(ref)) for ref in _strings(question.get("conflicts")))
            refs.extend(
                ("resolved_by", _bare(ref)) for ref in _strings(question.get("resolved_by"))
            )
        for relation, target in refs:
            if target in tenants and target != memory_id and tenants[target] != tenant:
                items.append(_item("relation_cross_tenant", memory_id, relation, target))
    return items


def _item(kind: str, memory_id: str, relation: str, target: str) -> dict[str, Any]:
    return {"kind": kind, "memory_id": memory_id, "relation": relation, "detail": target}


def _memories(root: Path) -> list[tuple[str, dict[str, Any]]]:
    directory = root / "memories"
    if directory.is_symlink() or not directory.is_dir():
        return []
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        rows.append((path.stem, fold_temporal_aliases(_frontmatter(path), strict=False)))
    return rows


def _bare(ref: str) -> str:
    """A memory reference without a ``memory:``/``mem:`` prefix (as ``memfw.edges``)."""

    for prefix in ("memory:", "mem:"):
        if ref.lower().startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


__all__ = ["lint_relations"]
