"""The one module where the recall layer touches memfw internals.

Everything else in ``memfw.recall`` depends only on ``memfw.ports`` and its
own siblings, so the coordinator can be replaced or extracted; this module is
the adapter between it and today's vault and retrievers.

- ``VaultIdResolver`` / ``meta_from_document``: ``memory_id`` refs → the vault
  document's filter metadata (read-only).
- ``default_resolvers``: the id resolver plus ``MemoryPathResolver`` on top.
- ``FtsProvider``: the shared ``search_vault`` that MCP ``memory_search``
  ``mode=fts`` and HTTP ``/search`` use.
- ``HybridProvider``: ``MemoryHybridRetriever`` in ``hybrid`` or ``vector``
  mode, with ``record_access=False`` and ``autowarm=False``: it appends no
  ``memory_access`` event and warms no cold or stub hit
  (it may build the disposable ``.memory-hybrid.sqlite`` index on first use,
  which is a projection, not an event). Unavailable without an embedder or
  ``sqlite-vec``; when the retriever falls back to FTS internally the provider
  reports itself unavailable, so the FTS provider is the one that answers.
- ``MultihopProvider``: ``search_multihop``.
- ``open_question_index`` / ``open_question_labels_for``: the U2 labels
  (``open_question:<id>``, ``conflicted``) from the vault's visible,
  unresolved open questions (``memfw.recall.labels``).

None of them changes the code it wraps; index preparation is injected
(``prepare``) so the wiring PR can pass the same staleness check the MCP
server uses.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.types import (
    Availability,
    Candidate,
    ExtraValue,
    ProviderUnavailable,
    RecallBudget,
    RefKind,
)
from memfw.projection import tag_privacy_class

from .labels import OpenQuestionIndex, open_question_labels
from .policy import ACTIVE_LIFECYCLES, FilterPolicy, MemoryMeta, classify_privacy, evaluate
from .resolve import MemoryPathResolver, Resolution, Resolver

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


_PRIVACY_RANK = {"none": 0, "local-private": 1, "unknown": 2}


def _strictest_privacy(values: Sequence[str]) -> str | None:
    """Several ``privacy:`` tags: the strictest wins, never the first one.

    Tag order must not decide visibility, so ``privacy:none`` next to
    ``privacy:local-private`` (in any case) is ``local-private``.
    """

    if not values:
        return None
    return max(values, key=lambda value: _PRIVACY_RANK[classify_privacy(value)])


def _strongest_lifecycle(values: Sequence[str]) -> str | None:
    """Several ``lifecycle:`` tags: retired > superseded > any non-active > active."""

    if not values:
        return None

    def rank(value: str) -> int:
        cleaned = value.strip().lower()
        if cleaned == "retired":
            return 3
        if cleaned == "superseded":
            return 2
        if cleaned not in ACTIVE_LIFECYCLES:
            return 1
        return 0

    return max(values, key=rank)


def meta_from_document(document: MemoryDocument) -> MemoryMeta:
    """Filter metadata from a parsed vault document.

    Privacy comes from the ``privacy`` field and tags until the meta-index is
    joined: the field and every ``privacy:<value>`` tag are declared values
    (the strictest wins), and ``sensitivity:*``
    tags are classed exactly like the projection does
    (:func:`memfw.projection.tag_privacy_class`): any case, only
    ``sensitivity:private`` is ``local-private``, and any other
    ``sensitivity:`` value is ``unknown`` (fail closed).
    """

    privacy_values: list[str] = []
    lifecycle_values: list[str] = []
    for tag in document.tags:
        lowered = tag.lower()
        if lowered.startswith("privacy:"):
            privacy_values.append(tag.split(":", 1)[1])
        elif lowered.startswith("lifecycle:"):
            lifecycle_values.append(tag.split(":", 1)[1])
    # The ``privacy`` field (MM0) is one more declared value: the strictest of
    # it and the ``privacy:`` tags wins. A missing field adds nothing here, so
    # recall reads older memories exactly as before.
    if document.privacy is not None:
        privacy_values.append(document.privacy)
    declared = _strictest_privacy(privacy_values)
    lifecycle = _strongest_lifecycle(lifecycle_values)
    tag_class = tag_privacy_class(document.tags)
    return MemoryMeta(
        memory_id=document.id,
        tenant=document.tenant,
        source=document.source,
        privacy=declared,
        sensitivity_private=tag_class == "local-private",
        sensitivity_class=tag_class,
        status=document.status,
        lifecycle=lifecycle,
        superseded_by=document.superseded_by,
        valid_from=document.valid_from,
        valid_to=document.valid_to,
        valid_until=None,
        created_at=document.created_at,
        tier=document.tier.value,
        is_stub=document.is_stub,
        layer=document.layer.value,
        version=document.version,
    )


class VaultIdResolver:
    """``memory_id`` refs → the vault document's metadata. Read-only."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self._repository = Repository(self.root)

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        out: dict[str, Resolution] = {}
        memories = self.root / "memories"
        for ref in refs:
            if ref in out or not _ID_RE.fullmatch(ref):
                continue
            path = memories / f"{ref}.md"
            if path.is_symlink() or not path.is_file():
                continue
            try:
                document = self._repository.get(ref)
            except Exception:  # noqa: BLE001 - unreadable file fails closed
                out[ref] = Resolution(ref, None)
                continue
            out[ref] = Resolution(document.id, meta_from_document(document))
        return out


def default_resolvers(root: Path | str) -> dict[RefKind, Resolver]:
    ids = VaultIdResolver(root)
    return {"memory_id": ids, "path": MemoryPathResolver(ids)}


def _indexed_question_ids(repository: Repository, tenant: str) -> list[str] | None:
    """Ids the search index lists as ``open-question`` in ``tenant``; None if unreadable.

    The index only narrows which files are read: each one is still parsed
    from the vault and judged by the call's policy. Callers run the index
    staleness check first (``Runtime.ensure_index``).
    """

    path = repository.index_path
    if path.is_symlink() or not path.is_file():
        return None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            rows = connection.execute(
                "SELECT id FROM memory_access WHERE memory_type = ? AND tenant = ? ORDER BY id",
                (OPEN_QUESTION_TYPE, tenant),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    return [str(row[0]) for row in rows]


#: Bytes every open-question file contains (its ``type``): the fallback scan,
#: used only when the index cannot be read, parses only files that have them.
_OPEN_QUESTION_MARKER = OPEN_QUESTION_TYPE.encode("ascii")


def _scanned_question_ids(repository: Repository) -> list[str]:
    memories = repository.root / "memories"
    if memories.is_symlink() or not memories.is_dir():
        return []
    found: list[str] = []
    for path in sorted(memories.glob("*.md")):
        if path.is_symlink():
            continue
        try:
            if _OPEN_QUESTION_MARKER in path.read_bytes():
                found.append(path.stem)
        except OSError:
            continue
    return found


def _is_open(document: MemoryDocument, policy: FilterPolicy, now: datetime) -> bool:
    """An unresolved, current open question that ``policy`` would keep.

    A question with ``valid_to`` set is never shown, even for an ``as_of``
    before that instant (conservative: ``valid_to`` on a question means it
    was resolved or withdrawn, and its file already names the answer).
    """

    block = document.open_question
    if document.type != OPEN_QUESTION_TYPE or block is None:
        return False
    if block.resolved_by or document.valid_to is not None or document.superseded_by:
        return False
    return evaluate(meta_from_document(document), policy, now=now).kept


def open_questions(
    repository: Repository, policy: FilterPolicy, *, now: datetime
) -> list[MemoryDocument]:
    """The unresolved open questions ``policy`` lets this caller see, by id.

    ``repository`` is the caller's open vault (resolved per call). Candidates
    come from the search index (``memory_access``: type ``open-question`` in
    the policy's tenant); without a readable index, from a scan of
    ``memories/``. Each candidate is read from the vault and listed only when
    it is unresolved (no ``resolved_by``, no ``valid_to``, not superseded; see
    ``_is_open``) and ``evaluate`` keeps it under ``policy`` (tenant, source,
    ``as_of``; for ``strict`` also privacy, status, lifecycle, archive): a
    question in another tenant, or one the policy drops, is never returned.
    An unreadable file is skipped.
    """

    ids = _indexed_question_ids(repository, policy.tenant)
    if ids is None:
        ids = _scanned_question_ids(repository)
    found: list[MemoryDocument] = []
    for memory_id in ids:
        if not _ID_RE.fullmatch(memory_id):
            continue
        path = repository.root / "memories" / f"{memory_id}.md"
        if path.is_symlink():
            continue
        try:
            document = repository.get(memory_id)
        except (MemfwError, OSError, UnicodeDecodeError, ValueError):
            continue
        if document.open_question is None or not _is_open(document, policy, now):
            continue
        found.append(document)
    return found


def open_question_index(
    repository: Repository, policy: FilterPolicy, *, now: datetime
) -> OpenQuestionIndex:
    """The open questions ``policy`` lets this caller see, keyed for labelling.

    The questions are :func:`open_questions`'s, so a question in another
    tenant, or one the policy drops, contributes nothing and its id can never
    appear in a label.
    """

    return OpenQuestionIndex.build(
        (document.id, list(document.about), list(document.open_question.conflicts))
        for document in open_questions(repository, policy, now=now)
        if document.open_question is not None
    )


def open_question_labels_for(
    repository: Repository,
    memory_ids: Sequence[str],
    policy: FilterPolicy,
    *,
    now: datetime,
) -> dict[str, tuple[str, ...]]:
    """Open-question labels for each hit id that has any (others are absent).

    ``now`` is the instant the policy is evaluated at (the coordinator's, for
    ``two_stage``). The hit's ``about`` is read from the vault; a hit outside
    ``policy``'s tenant, or one that cannot be read, gets none. No hits, or no
    visible open question, means nothing else is read.
    """

    if not memory_ids:
        return {}
    index = open_question_index(repository, policy, now=now)
    if not index:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for memory_id in dict.fromkeys(memory_ids):
        if not _ID_RE.fullmatch(memory_id):
            continue
        try:
            document = repository.get(memory_id)
        except (MemfwError, OSError, UnicodeDecodeError, ValueError):
            continue
        if document.tenant != policy.tenant:
            continue
        labels = open_question_labels(memory_id, document.about, index)
        if labels:
            out[memory_id] = labels
    return out


#: Same page cap as the MCP ``memory_search`` paths.
PAGE_CAP = 50

Prepare = Callable[[Repository], None]

#: ``observe(page_items, payloads)``: the raw rows behind the candidates and
#: the raw search payloads, for an in-process caller that builds its own rows
#: (the MCP server's freshness fields). Nothing of it enters a ``Candidate``.
Observe = Callable[[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], None]


def _vault_ok(root: Path) -> bool:
    return root.is_dir() and (root / "vault.yaml").is_file()


def _page(limit: int) -> int:
    return max(1, min(int(limit), PAGE_CAP))


def _candidates(name: str, items: Sequence[dict[str, Any]], *, why: str) -> list[Candidate]:
    out: list[Candidate] = []
    for item in items:
        identifier = str(item.get("id") or "")
        if not identifier:
            continue
        extra: dict[str, ExtraValue] = {"why": str(item.get("why") or why)}
        out.append(
            Candidate(
                ref=identifier,
                ref_kind="memory_id",
                score=float(item.get("score") or 0.0),
                provider=name,
                rank=len(out) + 1,
                extra=extra,
            )
        )
    return out


class FtsProvider:
    """Lexical recall through ``search_vault`` (the ``mode=fts`` path)."""

    lexical = True

    def __init__(
        self,
        root: Path | str,
        *,
        name: str = "fts",
        source: str = "",
        include_history: bool = False,
        scope: str = "",
        allowed_sources: tuple[str, ...] = (),
        prepare: Prepare | None = None,
        observe: Observe | None = None,
    ) -> None:
        self.name = name
        self.root = Path(root).expanduser()
        self.source = source
        self.include_history = include_history
        self.scope = scope
        self.allowed_sources = tuple(allowed_sources)
        self.prepare = prepare
        self.observe = observe
        self.projection_version = ""

    def available(self) -> Availability:
        if not _vault_ok(self.root):
            return Availability.missing("no_vault")
        if self.prepare is None and not (self.root / "index.sqlite3").is_file():
            return Availability.missing("no_index")
        try:
            import memfw.index.readonly  # noqa: F401
        except ImportError:
            return Availability.missing("readonly_service_missing")
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        del budget  # one bounded SQLite read; the coordinator enforces the deadline
        from memfw.index import readonly

        if self.prepare is not None:
            self.prepare(Repository(self.root))
        # Same shape as MCP ``memory_search`` ``mode=fts``: with an allow-list
        # and no explicit source, one read per allowed source so the SQL
        # filters by source before the page cap; then a stable score sort and
        # one cut to the page.
        page = _page(limit)
        if self.source or not self.allowed_sources:
            sources: tuple[str, ...] = (self.source,)
        else:
            sources = tuple(dict.fromkeys(self.allowed_sources))
        merged: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        for each in sources:
            payload = readonly.search_vault(
                self.root,
                query,
                limit=page,
                source=each,
                tenant=tenant,
                include_history=self.include_history,
                as_of=as_of.isoformat() if as_of is not None else None,
                scope=self.scope,
            )
            payloads.append(payload)
            merged.extend(
                item
                for item in payload["results"]
                if not self.allowed_sources or item["source"] in self.allowed_sources
            )
        if len(sources) > 1:
            merged.sort(key=lambda item: -float(item.get("score") or 0.0))
        if self.observe is not None:
            self.observe(merged[:page], payloads)
        return _candidates(self.name, merged[:page], why="lexical")


class HybridProvider:
    """Semantic recall through ``MemoryHybridRetriever`` (hybrid or vector)."""

    lexical = False

    def __init__(
        self,
        root: Path | str,
        *,
        embedder: Any,
        name: str = "hybrid",
        mode: Literal["hybrid", "vector"] = "hybrid",
        source: str = "",
        include_history: bool = False,
        allowed_sources: tuple[str, ...] = (),
        entity_enabled: bool = False,
        observe: Observe | None = None,
    ) -> None:
        if mode not in {"hybrid", "vector"}:
            raise ValueError("mode must be hybrid or vector")
        self.name = name
        self.root = Path(root).expanduser()
        self.embedder = embedder
        self.mode = mode
        self.source = source
        self.include_history = include_history
        self.allowed_sources = tuple(allowed_sources)
        self.entity_enabled = entity_enabled
        self.observe = observe
        self.projection_version = ""

    def available(self) -> Availability:
        if not _vault_ok(self.root):
            return Availability.missing("no_vault")
        if self.embedder is None:
            return Availability.missing("no_embedder")
        if importlib.util.find_spec("sqlite_vec") is None:
            return Availability.missing("sqlite_vec_missing")
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        del budget
        if self.embedder is None:
            raise ProviderUnavailable("no_embedder")
        from memfw.memory_hybrid import MemoryHybridRetriever

        retriever = MemoryHybridRetriever(
            Repository(self.root),
            embedder=self.embedder,
            tenant=tenant,
            entity_enabled=self.entity_enabled,
            allowed_sources=self.allowed_sources,
            # Read-only provider: the coordinator filters these hits later, so
            # the Runtime records access only for the ones it returns.
            record_access=False,
            # Nor does it warm cold or stub hits: the Runtime warms the ones
            # it returns, after the filter.
            autowarm=False,
        )
        result = retriever.search(
            query,
            limit=_page(limit),
            mode=self.mode,
            source=self.source,
            include_history=self.include_history,
            as_of=as_of,
            tenant=tenant,
        )
        if result.get("mode") != self.mode:
            # The retriever answered from FTS. That is the FTS provider's job.
            raise ProviderUnavailable("fell_back_to_fts")
        fingerprint = str(getattr(self.embedder, "fingerprint", "") or "")
        self.projection_version = fingerprint
        if self.observe is not None:
            self.observe(result["results"], [result])
        return _candidates(self.name, result["results"], why="semantic")


class MultihopProvider:
    """Routed lexical search with query-time term expansion (``search_multihop``)."""

    lexical = False

    def __init__(
        self,
        root: Path | str,
        *,
        name: str = "multihop",
        source: str = "",
        include_history: bool = False,
        allowed_sources: tuple[str, ...] = (),
        rounds: int | None = None,
        prepare: Prepare | None = None,
    ) -> None:
        self.name = name
        self.root = Path(root).expanduser()
        self.source = source
        self.include_history = include_history
        self.allowed_sources = tuple(allowed_sources)
        self.rounds = rounds
        self.prepare = prepare
        self.projection_version = ""

    def available(self) -> Availability:
        if not _vault_ok(self.root):
            return Availability.missing("no_vault")
        if self.prepare is None and not (self.root / "index.sqlite3").is_file():
            return Availability.missing("no_index")
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        del budget
        from memfw.retrieval.multihop import search_multihop

        repository = Repository(self.root)
        if self.prepare is not None:
            self.prepare(repository)
        result = search_multihop(
            repository,
            query,
            limit=_page(limit),
            tenant=tenant,
            source=self.source,
            allowed_sources=self.allowed_sources,
            include_history=self.include_history,
            as_of=as_of,
            rounds=self.rounds,
        )
        return _candidates(self.name, result["results"], why="lexical")


__all__ = [
    "PAGE_CAP",
    "FtsProvider",
    "HybridProvider",
    "MultihopProvider",
    "Observe",
    "Prepare",
    "VaultIdResolver",
    "default_resolvers",
    "meta_from_document",
    "open_question_index",
    "open_questions",
    "open_question_labels_for",
]
