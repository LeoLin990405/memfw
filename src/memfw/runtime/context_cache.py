"""The ``memory_context`` cache (C3, the CAG part): id lists and versions, never bodies.

What a cache entry saves is the expensive part of the ``core`` slot: reading
and parsing every memory file (``Repository.list()``, which also replays the
event log) to find the few anchors and procedural memories. An entry holds
only the ids (and frontmatter ``version``) of the slot's *pool*: the memories
that are core-shaped (``memfw.context.is_core_shaped``) and visible to the
bound tenant and sources. No body, title or query text is ever stored.

Key: ``(tenant, policy, slot, scope)`` where ``scope`` is the vault root and
the allowed sources. Each entry carries the vault ``fingerprint`` it was built
under. ``pack_version`` is derived from the slot's content (ids, versions,
file hashes, carried lengths), so it can only change when a memory file
changes, a memory file is added or removed, or the filter's verdict changes
over time; an entry is therefore used only while the fingerprint (stat of
every memory file, ``events.jsonl`` and ``vault.yaml``) is unchanged, and on a
hit every pooled memory is re-read from the vault and filtered again with the
call's own ``keep`` (``strict`` included, at the call's ``now``). The slot is
then built exactly as without the cache (bodies, versions and hashes from a
fresh read), so a hit returns the same pack, and the same ``pack_version``,
as a fresh build. Anything unexpected on a hit (a pooled file missing,
unreadable, unparsable or at another version) falls back to a full build.

Only ``core`` is cached. ``handoff`` (the conversation share, with
``conversation_share`` applied per call) and ``topics`` (counts over every
visible memory) are rebuilt on every call: re-reading and re-filtering them
*is* their full build, so a cache would save nothing and could only go stale.

The cache is per process (one per ``Runtime`` unless one is injected), bounded
(least recently used entries are evicted) and thread-safe.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from memfw.context import (
    CORE_SLOT,
    CoreCandidates,
    Keep,
    core_candidates,
    is_core_shaped,
    select_core,
)
from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository

#: Entries a default ``ContextCache`` keeps (one per tenant, policy, slot and scope).
DEFAULT_MAX_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Who the entry is for: tenant, filter policy, slot and scope (vault root, sources)."""

    tenant: str
    policy: str
    slot: str
    scope: tuple[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class CachedIds:
    """One entry: the vault fingerprint it was built under and the pool's ids and versions."""

    fingerprint: str
    ids: tuple[str, ...]
    versions: tuple[int, ...]


class ContextCache:
    """A bounded, thread-safe LRU of ``CacheKey -> CachedIds`` with hit/miss counters."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[CacheKey, CachedIds] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: CacheKey) -> CachedIds | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key: CacheKey, entry: CachedIds) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def discard(self, key: CacheKey) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def record(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1

    def keys(self) -> list[CacheKey]:
        with self._lock:
            return list(self._entries)

    def stats(self) -> dict[str, int]:
        """``hits``, ``misses`` and ``size`` (entries held)."""

        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._entries)}


def _stat_line(label: str, path: str) -> str:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"{label}\0absent"
    return f"{label}\0{st.st_ino}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ctime_ns}"


def vault_fingerprint(repository: Repository) -> str | None:
    """A sha256 over the stat of every file the ``core`` slot's selection depends on.

    Every ``*.md`` in ``memories/`` (name, inode, size, mtime and ctime in
    nanoseconds; a symlink by its target), ``events.jsonl`` (every vault write
    appends to it), ``vault.yaml`` and the pending-transaction marker. Adding,
    removing, renaming or rewriting any of them changes it; ctime cannot be
    set back by hand, so a ``touch`` to an old mtime still changes it. ``None``
    when a file cannot be stat'ed (the call is then not cached).
    """

    lines = [
        _stat_line("vault.yaml", str(repository.root / "vault.yaml")),
        _stat_line("events", str(repository.events_path)),
        _stat_line("pending", str(repository.pending_path)),
    ]
    directory = repository.memories_dir
    try:
        with os.scandir(directory) as scan:
            names = sorted(entry.name for entry in scan if entry.name.endswith(".md"))
    except FileNotFoundError:
        names = []
        lines.append("memories\0absent")
    except OSError:
        return None
    try:
        lines.extend(_stat_line(name, os.path.join(directory, name)) for name in names)
    except OSError:
        return None
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8", "surrogateescape") + b"\n")
    return digest.hexdigest()


def _reread(repository: Repository, entry: CachedIds) -> list[MemoryDocument] | None:
    """The pooled memories parsed from the vault now, or ``None`` if any is not as cached.

    A pending transaction raises, exactly as ``Repository.list()`` does.
    """

    repository.assert_no_pending()
    documents: list[MemoryDocument] = []
    for memory_id, version in zip(entry.ids, entry.versions, strict=True):
        path = repository.memories_dir / f"{memory_id}.md"
        try:
            document = repository.parse(path.read_bytes().decode("utf-8"), expected_id=memory_id)
        except (OSError, UnicodeDecodeError, ValueError, MemfwError):
            return None
        if document.version != version:
            return None
        documents.append(document)
    return documents


def cached_core_candidates(
    cache: ContextCache,
    *,
    tenant: str,
    policy: str,
    allowed_sources: tuple[str, ...],
    visible: Callable[[MemoryDocument], bool],
) -> CoreCandidates:
    """A ``CoreCandidates`` that serves the pool from ``cache`` while the vault is unchanged.

    ``visible`` is the static part every policy's ``keep`` implies (tenant and
    allowed sources); it decides which ids go into the entry. The returned
    list is always ``select_core(pool, keep)`` with ``keep`` the call's own,
    so it equals ``select_core(repository.list(), keep)``.
    """

    def candidates(repository: Repository, keep: Keep) -> list[MemoryDocument]:
        fingerprint = vault_fingerprint(repository)
        if fingerprint is None:
            return core_candidates(repository, keep)
        key = CacheKey(
            tenant=tenant,
            policy=policy,
            slot=CORE_SLOT,
            scope=(str(repository.root), tuple(allowed_sources)),
        )
        entry = cache.get(key)
        if entry is not None and entry.fingerprint == fingerprint:
            pool = _reread(repository, entry)
            if pool is not None:
                cache.record(hit=True)
                return select_core(pool, keep)
        cache.record(hit=False)
        documents = repository.list()
        pool = [
            document for document in documents if is_core_shaped(document) and visible(document)
        ]
        cache.put(
            key,
            CachedIds(
                fingerprint=fingerprint,
                ids=tuple(document.id for document in pool),
                versions=tuple(document.version for document in pool),
            ),
        )
        return select_core(pool, keep)

    return candidates


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "CacheKey",
    "CachedIds",
    "ContextCache",
    "cached_core_candidates",
    "vault_fingerprint",
]
