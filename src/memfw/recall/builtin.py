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
  mode. Unavailable without an embedder or ``sqlite-vec``; when the retriever
  falls back to FTS internally the provider reports itself unavailable, so
  the FTS provider is the one that answers.
- ``MultihopProvider``: ``search_multihop``.

None of them changes the code it wraps; index preparation is injected
(``prepare``) so the wiring PR can pass the same staleness check the MCP
server uses.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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

from .policy import MemoryMeta
from .resolve import MemoryPathResolver, Resolution, Resolver

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def meta_from_document(document: MemoryDocument) -> MemoryMeta:
    """Filter metadata from a parsed vault document.

    Privacy comes from tags until the meta-index is joined: a
    ``privacy:<value>`` tag gives the declared value and
    ``sensitivity:private`` marks the memory private.
    """

    declared: str | None = None
    lifecycle: str | None = None
    for tag in document.tags:
        if tag.startswith("privacy:") and declared is None:
            declared = tag.split(":", 1)[1]
        elif tag.startswith("lifecycle:") and lifecycle is None:
            lifecycle = tag.split(":", 1)[1]
    return MemoryMeta(
        memory_id=document.id,
        tenant=document.tenant,
        source=document.source,
        privacy=declared,
        sensitivity_private="sensitivity:private" in document.tags,
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


#: Same page cap as the MCP ``memory_search`` paths.
PAGE_CAP = 50

Prepare = Callable[[Repository], None]


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
    ) -> None:
        self.name = name
        self.root = Path(root).expanduser()
        self.source = source
        self.include_history = include_history
        self.scope = scope
        self.allowed_sources = tuple(allowed_sources)
        self.prepare = prepare
        self.projection_version = ""

    def available(self) -> Availability:
        if not _vault_ok(self.root):
            return Availability.missing("no_vault")
        if self.prepare is None and not (self.root / "index.sqlite3").is_file():
            return Availability.missing("no_index")
        try:
            from memfw.memory_hybrid import shared_readonly

            shared_readonly()
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
        from memfw.memory_hybrid import shared_readonly

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
        for each in sources:
            payload = shared_readonly().search_vault(
                self.root,
                query,
                limit=page,
                source=each,
                tenant=tenant,
                include_history=self.include_history,
                as_of=as_of.isoformat() if as_of is not None else None,
                scope=self.scope,
            )
            merged.extend(
                item
                for item in payload["results"]
                if not self.allowed_sources or item["source"] in self.allowed_sources
            )
        if len(sources) > 1:
            merged.sort(key=lambda item: -float(item.get("score") or 0.0))
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
    "Prepare",
    "VaultIdResolver",
    "default_resolvers",
    "meta_from_document",
]
