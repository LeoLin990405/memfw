"""Expand a query through the alias projection and match memory frontmatter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import AliasIndex, Concept, _frontmatter, _resolve, load_concepts, project_aliases
from .normalise import normalise

_CACHE: dict[tuple[str, ...], tuple[dict[str, Concept], AliasIndex]] = {}


def matching_memories(
    vault: Path,
    query: str,
    *,
    tenant: str = "",
    source: str = "",
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Return ``(memory_id, why)`` rows and ambiguity candidates.

    An ambiguous alias is never a winner. ``merged_into`` is followed on the
    alias and on each memory's ``about`` and ``applies_to`` values.
    """

    root = Path(vault)
    directory = root / "concepts"
    if directory.is_symlink() or not directory.is_dir():
        return [], []
    concepts, index = _projection(directory)
    wanted, ambiguities = _query_oids(query, index)
    if not wanted:
        return [], ambiguities
    memories = root / "memories"
    if memories.is_symlink() or not memories.is_dir():
        return [], ambiguities
    hits: list[tuple[str, str]] = []
    for path in sorted(memories.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        meta = _frontmatter(path)
        if str(meta.get("tenant") or "") != tenant:
            continue
        if source and str(meta.get("source") or "") != source:
            continue
        why = _match_why(meta, concepts, wanted)
        if why is None:
            continue
        hits.append((path.stem, why))
    return hits, ambiguities


def _projection(directory: Path) -> tuple[dict[str, Concept], AliasIndex]:
    stamp = [str(directory)]
    for path in sorted(directory.glob("*/*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        stamp.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    key = tuple(stamp)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    concepts = load_concepts(directory.parent)
    loaded = (concepts, project_aliases(concepts))
    _CACHE[key] = loaded
    if len(_CACHE) > 32:
        _CACHE.pop(next(iter(_CACHE)))
    return loaded


def _terms(query: str) -> list[str]:
    text = query.strip()
    if not text:
        return []
    terms: list[str] = []
    for item in (text, *text.split()):
        if item not in terms:
            terms.append(item)
    return terms


def _query_oids(
    query: str, index: AliasIndex
) -> tuple[dict[str, tuple[str, str]], list[dict[str, Any]]]:
    wanted: dict[str, tuple[str, str]] = {}
    ambiguities: list[dict[str, Any]] = []
    seen_ambiguous: set[str] = set()
    for term in _terms(query):
        key = normalise(term)
        if not key:
            continue
        if key in index.ambiguous:
            if key not in seen_ambiguous:
                seen_ambiguous.add(key)
                ambiguities.append(
                    {
                        "kind": "duplicate_alias",
                        "alias": term,
                        "detail": key,
                        "oids": list(index.ambiguous[key]),
                    }
                )
            continue
        oid = index.by_alias.get(key)
        if oid is None or key in wanted:
            continue
        wanted[key] = (oid, term)
    return wanted, ambiguities


def _match_why(
    meta: dict[str, Any],
    concepts: dict[str, Concept],
    wanted: dict[str, tuple[str, str]],
) -> str | None:
    by_oid: dict[str, str] = {}
    for oid, term in wanted.values():
        by_oid.setdefault(oid, term)
    fields = _field_oids(meta.get("about")) + _field_oids(meta.get("applies_to"))
    for raw in fields:
        resolved = _resolve(raw, concepts)
        label_text = by_oid.get(resolved)
        if label_text is None:
            continue
        label = " ".join(label_text.split())
        return f"alias({label}→{resolved})"
    return None


def _field_oids(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []
