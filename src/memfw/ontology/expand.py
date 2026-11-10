"""Expand a query through the alias projection and match memory frontmatter."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memfw.core.models import fold_temporal_aliases

from .model import AliasIndex, Concept, _frontmatter, _resolve, load_concepts, project_aliases
from .normalise import normalise

_Row = tuple[str, int, int, int, int]
_Stamp = tuple[_Row, ...]
_MemoryRef = tuple[str, dict[str, Any]]
# Both caches are keyed by directory. Each file is stamped with
# (path, mtime_ns, ctime_ns, size, inode) and the build time is kept. As in
# git's racy-index rule, a file whose mtime or ctime falls within
# ``_RACY_NS`` of (or after) the build is never trusted from the cache: a
# coarse-timestamp filesystem could hide a same-size edit in that tick.
_CONCEPT_CACHE: dict[str, tuple[_Stamp, int, tuple[dict[str, Concept], AliasIndex]]] = {}
_MEMORY_CACHE: dict[str, dict[str, tuple[_Row, int, _MemoryRef]]] = {}
_RACY_NS = 2_000_000_000
_CACHE_LIMIT = 32
_LOCK = threading.Lock()
_REF_KEYS = (
    "tenant",
    "source",
    "valid_from",
    "valid_to",
    "superseded_by",
    "created_at",
    "about",
    "applies_to",
)
RRF_K = 60


def matching_memories(
    vault: Path,
    query: str,
    *,
    tenant: str = "",
    source: str = "",
    include_history: bool = False,
    as_of: str | None = None,
    scope: str = "",
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Return ``(memory_id, why)`` rows and ambiguity candidates.

    ``scope`` selects scoped aliases (``alt_labels: [{label, scope}]``). A
    scoped label resolves only when ``scope`` equals its scope; the default
    empty scope sees unscoped labels only.

    An ambiguous alias is never a winner. ``merged_into`` is followed on the
    alias and on each memory's ``about`` and ``applies_to`` values. Hits use
    the same temporal rule as ``/search``: history shows every row, ``as_of``
    keeps a row whose start is at or before that instant and whose end is
    empty or later, and the current view drops a row with ``valid_to`` or
    ``superseded_by``.
    """

    root = Path(vault)
    directory = root / "concepts"
    if directory.is_symlink() or not directory.is_dir():
        return [], []
    concepts, index = _projection(directory)
    wanted, ambiguities = _query_oids(query, index, scope=scope)
    if not wanted:
        return [], ambiguities
    memories = root / "memories"
    if memories.is_symlink() or not memories.is_dir():
        return [], ambiguities
    hits: list[tuple[str, str]] = []
    for memory_id, meta in _memory_refs(memories):
        if str(meta.get("tenant") or "") != tenant:
            continue
        if source and str(meta.get("source") or "") != source:
            continue
        if not temporally_visible(meta, include_history=include_history, as_of=as_of):
            continue
        why = _match_why(meta, concepts, wanted)
        if why is None:
            continue
        hits.append((memory_id, why))
    return hits, ambiguities


def temporally_visible(
    meta: dict[str, Any],
    *,
    include_history: bool,
    as_of: str | None,
) -> bool:
    """Shared time gate for an alias candidate. History keeps every row.

    The legacy names ``valid_until`` and ``superseded`` are folded onto
    ``valid_to`` and ``superseded_by``.
    """
    if include_history:
        return True
    meta = fold_temporal_aliases(meta, strict=False)
    start = _parse_instant(meta.get("valid_from")) or _parse_instant(meta.get("created_at"))
    end = _parse_instant(meta.get("valid_to"))
    superseded = str(meta.get("superseded_by") or "").strip()
    if as_of is None:
        return end is None and not superseded
    point = _parse_instant(as_of)
    if point is None:
        return False
    if start is not None and start > point:
        return False
    if end is not None and end <= point:
        return False
    return True


def fuse_rows(
    lexical: list[dict[str, Any]],
    alias: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fuse two ranked channels with RRF, then truncate.

    An empty alias channel keeps the lexical page and its scores. A row that
    appears in both channels keeps the lexical fields and the alias ``why``.
    """
    if limit <= 0:
        return []
    if not alias:
        return list(lexical[:limit])
    scores: dict[str, float] = {}
    for channel in (
        [str(row["id"]) for row in lexical],
        [str(row["id"]) for row in alias],
    ):
        seen: set[str] = set()
        for rank, memory_id in enumerate(channel, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (RRF_K + rank)
    order = sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))
    lexical_by = {str(row["id"]): row for row in lexical}
    alias_by = {str(row["id"]): row for row in alias}
    fused: list[dict[str, Any]] = []
    for memory_id in order[:limit]:
        if memory_id in lexical_by:
            row = lexical_by[memory_id]
            if memory_id in alias_by and alias_by[memory_id].get("why"):
                row["why"] = alias_by[memory_id]["why"]
            row["score"] = scores[memory_id]
            fused.append(row)
        else:
            row = alias_by[memory_id]
            row["score"] = scores[memory_id]
            fused.append(row)
    return fused


def _parse_instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clock_ns() -> int:
    return time.time_ns()


def _file_row(path: Path) -> _Row | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _stamp(directory: Path, pattern: str) -> _Stamp:
    """One row per regular file. Stats only, no reads."""
    rows: list[_Row] = []
    for path in sorted(directory.glob(pattern)):
        if path.is_symlink() or not path.is_file():
            continue
        row = _file_row(path)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _settled(row: _Row, built_ns: int) -> bool:
    """True when the file's times are safely older than the cache build."""
    return max(row[1], row[2]) < built_ns - _RACY_NS


def _remember(cache: dict[str, Any], key: str, value: Any) -> None:
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > _CACHE_LIMIT:
        cache.pop(next(iter(cache)))


def _projection(directory: Path) -> tuple[dict[str, Concept], AliasIndex]:
    """Alias map built from the concept files only, rebuilt when one changes."""
    key = str(directory)
    built_ns = _clock_ns()
    stamp = _stamp(directory, "*/*.md")
    with _LOCK:
        cached = _CONCEPT_CACHE.get(key)
    if (
        cached is not None
        and cached[0] == stamp
        and all(_settled(row, cached[1]) for row in stamp)
    ):
        return cached[2]
    concepts = load_concepts(directory.parent)
    loaded = (concepts, project_aliases(concepts))
    with _LOCK:
        _remember(_CONCEPT_CACHE, key, (stamp, built_ns, loaded))
    return loaded


def _memory_refs(directory: Path) -> list[_MemoryRef]:
    """``(memory_id, folded frontmatter subset)`` for every memory file.

    Only the fields the alias channel filters on are kept. A memory file is
    reused from the cache only when its stamp is unchanged and settled, so a
    repeated query costs one ``stat`` per file and no reads; a file removed
    from the directory drops out with its stamp.
    """
    key = str(directory)
    built_ns = _clock_ns()
    stamp = _stamp(directory, "*.md")
    with _LOCK:
        previous = dict(_MEMORY_CACHE.get(key, {}))
    entries: dict[str, tuple[_Row, int, _MemoryRef]] = {}
    refs: list[_MemoryRef] = []
    for row in stamp:
        cached = previous.get(row[0])
        if cached is not None and cached[0] == row and _settled(row, cached[1]):
            entries[row[0]] = cached
            refs.append(cached[2])
            continue
        path = Path(row[0])
        meta = fold_temporal_aliases(_frontmatter(path), strict=False)
        ref = (path.stem, {name: meta[name] for name in _REF_KEYS if name in meta})
        entries[row[0]] = (row, built_ns, ref)
        refs.append(ref)
    with _LOCK:
        _remember(_MEMORY_CACHE, key, entries)
    return refs


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
    query: str, index: AliasIndex, *, scope: str = ""
) -> tuple[dict[str, tuple[str, str]], list[dict[str, Any]]]:
    wanted: dict[str, tuple[str, str]] = {}
    ambiguities: list[dict[str, Any]] = []
    seen_ambiguous: set[str] = set()
    for term in _terms(query):
        key = normalise(term)
        if not key:
            continue
        oid, candidates = index.lookup(key, scope)
        if candidates:
            if key not in seen_ambiguous:
                seen_ambiguous.add(key)
                ambiguities.append(
                    {
                        "kind": "duplicate_alias",
                        "alias": term,
                        "detail": key,
                        "oids": list(candidates),
                    }
                )
            continue
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
