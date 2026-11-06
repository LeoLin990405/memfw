"""Hybrid retrieval for the memory vault.

Markdown under ``memories/`` remains authoritative. The notes index used here is
a disposable projection, rebuilt from that directory and never written back.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core.models import EventKind, MemoryDocument, Tier
from .core.repository import Repository
from .entities import EntityIndex, load_dictionary
from .notes import HttpEmbeddingProvider, NotesIndex, NotesRetriever, RetrievalConfig
from .notes.versioning import (
    IndexCompatibilityError,
    IndexManifest,
    build_manifest,
    read_manifest,
    verify_manifest,
    write_manifest,
)
from .retrieval import VaultRetriever


class MemoryHybridRetriever:
    """Search memory Markdown with FTS or optional hybrid retrieval.

    A single retriever is bound to one repository, so tenant/source isolation is
    the vault boundary; an optional source filter narrows results further.
    """

    def __init__(
        self,
        repository: Repository,
        *,
        embedder: HttpEmbeddingProvider | None = None,
        index_path: Path | str | None = None,
        manifest_path: Path | str | None = None,
        candidates: int = 50,
        rrf_k: int = 60,
        entity_enabled: bool = False,
        entity_dictionary: Path | str | None = None,
        tenant: str = "",
        allowed_sources: tuple[str, ...] = (),
    ) -> None:
        self.repository = repository
        self.embedder = embedder
        self.notes_index = NotesIndex(
            index_path or (repository.root / ".memory-hybrid.sqlite"),
            embedder=embedder,
        )
        self.manifest_path = Path(
            manifest_path or (repository.root / ".memory-hybrid.manifest.json")
        )
        self.candidates = max(1, candidates)
        self.rrf_k = max(1, rrf_k)
        self.entity_enabled = entity_enabled
        self.tenant = tenant
        self.allowed_sources = tuple(allowed_sources)
        self.entity_index = EntityIndex(
            repository.root / ".memory-entities.sqlite",
            dictionary=load_dictionary(entity_dictionary),
        )
        self.last_metrics: dict[str, Any] = {}

    def rebuild(self) -> dict[str, Any]:
        self.repository.init()
        counts = self.notes_index.index(self.repository.memories_dir)
        if self.entity_enabled:
            counts["entities"] = self.entity_index.rebuild(self.repository)
        manifest = build_manifest(self.notes_index, labels={"kind": "memory-vault"})
        write_manifest(self.manifest_path, manifest)
        return {**counts, "manifest": manifest.as_dict()}

    def _ensure_index(self) -> None:
        if not self.notes_index.db_path.exists():
            self.rebuild()
            return
        if not self.manifest_path.exists():
            return
        try:
            manifest = read_manifest(self.manifest_path)
            verify_manifest(self.notes_index, manifest)
            current_fingerprint = str(
                getattr(self.embedder, "fingerprint", "") if self.embedder else ""
            )
            if manifest.embedding_fingerprint != current_fingerprint:
                self.rebuild()
        except IndexCompatibilityError:
            self.rebuild()

    def notes_index_manifest(self) -> IndexManifest:
        return read_manifest(self.manifest_path)

    @staticmethod
    def _visible(
        document: MemoryDocument,
        *,
        include_history: bool,
        as_of: datetime | None,
    ) -> bool:
        if include_history:
            return True
        if as_of is not None:
            start = document.valid_from or document.created_at
            end = document.valid_to
            return start <= as_of and (end is None or end > as_of)
        return document.valid_to is None

    def _source_allowed(self, source: str, requested: str) -> bool:
        if requested and source != requested:
            return False
        return not self.allowed_sources or source in self.allowed_sources

    @staticmethod
    def _parse_as_of(value: str | datetime | None) -> datetime | None:
        if value is None or isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return parsed.astimezone(UTC)

    def _fts_search(
        self,
        query: str,
        limit: int,
        source: str,
        *,
        include_history: bool = False,
        as_of: datetime | None = None,
        tenant: str = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        errors: list[str] = []
        notes_result = None
        try:
            self._ensure_index()
            retriever = NotesRetriever(
                self.notes_index,
                config=RetrievalConfig(
                    mode="fts",
                    candidates=self.candidates,
                    final_k=max(1, limit),
                    rrf_k=self.rrf_k,
                ),
            )
            notes_result = retriever.search(query, limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fts index unavailable: {exc}")
        if notes_result is not None:
            results: list[dict[str, Any]] = []
            for hit in notes_result.hits:
                memory_id = Path(hit.path).stem
                try:
                    document = self.repository.get(memory_id)
                except Exception:
                    continue
                if document.tenant != tenant:
                    continue
                if not self._visible(document, include_history=include_history, as_of=as_of):
                    continue
                if not self._source_allowed(document.source, source):
                    continue
                if document.is_stub or document.tier == Tier.COLD:
                    self.repository.autowarm(document.id, actor="mechanism")
                    document = self.repository.get(document.id)
                self.repository.capture_event(
                    EventKind.MEMORY_ACCESS,
                    actor="mechanism",
                    details={"memory_id": document.id, "operation": "fts_retrieve"},
                )
                results.append(
                    {
                        "id": document.id,
                        "title": document.title,
                        "score": hit.score,
                        "source": document.source,
                        "path": f"memories/{document.id}.md",
                    }
                )
            return results, errors
        legacy = VaultRetriever(self.repository).retrieve(query, limit=max(0, limit))
        results = []
        for memory_id in legacy:
            document = self.repository.get(memory_id)
            if document.tenant != tenant:
                continue
            if not self._visible(document, include_history=include_history, as_of=as_of):
                continue
            if not self._source_allowed(document.source, source):
                continue
            results.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "score": 0.0,
                    "source": document.source,
                    "path": f"memories/{document.id}.md",
                }
            )
        return results, errors

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "hybrid",
        source: str = "",
        include_history: bool = False,
        as_of: str | datetime | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query required")
        self.repository.init()
        if mode not in {"fts", "hybrid", "vector"}:
            raise ValueError("mode must be fts, hybrid or vector")
        as_of_dt = self._parse_as_of(as_of)
        tenant_value = self.tenant if tenant is None else tenant
        errors: list[str] = []
        mode_used = "fts"
        degraded = mode != "fts"
        results: list[dict[str, Any]] = []
        started = time.perf_counter()
        if mode != "fts" and self.embedder is not None:
            try:
                self._ensure_index()
                retriever = NotesRetriever(
                    self.notes_index,
                    config=RetrievalConfig(
                        mode=mode,  # type: ignore[arg-type]
                        candidates=self.candidates,
                        final_k=max(1, limit),
                        rrf_k=self.rrf_k,
                    ),
                )
                notes_result = retriever.search(query, limit=max(1, limit))
                for hit in notes_result.hits:
                    if hit.path.startswith("/"):
                        path = Path(hit.path)
                    else:
                        path = Path(hit.path)
                    memory_id = path.stem
                    try:
                        document = self.repository.get(memory_id)
                    except Exception:
                        continue
                    if document.is_stub or document.tier == Tier.COLD:
                        self.repository.autowarm(memory_id, actor="mechanism")
                        document = self.repository.get(memory_id)
                    if document.tenant != tenant_value:
                        continue
                    if not self._source_allowed(document.source, source):
                        continue
                    if not self._visible(document, include_history=include_history, as_of=as_of_dt):
                        continue
                    self.repository.capture_event(
                        EventKind.MEMORY_ACCESS,
                        actor="mechanism",
                        details={"memory_id": memory_id, "operation": "hybrid_retrieve"},
                    )
                    results.append(
                        {
                            "id": document.id,
                            "title": document.title,
                            "score": hit.score,
                            "source": document.source,
                            "path": f"memories/{document.id}.md",
                        }
                    )
                mode_used = notes_result.mode_used
                degraded = notes_result.degraded
                errors.extend(notes_result.errors)
            except Exception as exc:  # noqa: BLE001 - optional index/model boundary
                errors.append(f"hybrid unavailable: {exc}")
                results = []
        if not results:
            results, fts_errors = self._fts_search(
                query,
                limit,
                source,
                include_history=include_history,
                as_of=as_of_dt,
                tenant=tenant_value,
            )
            errors.extend(fts_errors)
            mode_used = "fts"
            degraded = mode != "fts"
        if self.entity_enabled:
            if not self.entity_index.path.exists():
                self.entity_index.rebuild(self.repository)
            entity_ids = self.entity_index.search(query, limit=self.candidates)
            if entity_ids:
                scores: dict[str, float] = {
                    item["id"]: 1.0 / (self.rrf_k + position)
                    for position, item in enumerate(results, start=1)
                }
                for position, memory_id in enumerate(entity_ids, start=1):
                    scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (self.rrf_k + position)
                by_id = {item["id"]: item for item in results}
                for memory_id in scores:
                    if memory_id in by_id:
                        continue
                    try:
                        document = self.repository.get(memory_id)
                    except Exception:
                        continue
                    if document.tenant != tenant_value:
                        continue
                    if not self._visible(document, include_history=include_history, as_of=as_of_dt):
                        continue
                    if not self._source_allowed(document.source, source):
                        continue
                    by_id[memory_id] = {
                        "id": document.id,
                        "title": document.title,
                        "score": 0.0,
                        "source": document.source,
                        "path": f"memories/{document.id}.md",
                    }
                results = sorted(
                    by_id.values(), key=lambda item: (-scores.get(item["id"], 0.0), item["id"])
                )
                for item in results:
                    item["score"] = scores.get(item["id"], 0.0)
        results = results[: max(1, limit)]
        model_versions = {}
        if self.embedder is not None:
            model_versions["embedding"] = str(
                getattr(self.embedder, "fingerprint", "")
                or getattr(self.embedder, "model", "")
                or type(self.embedder).__name__
            )
        self.last_metrics = {
            "elapsed_seconds": max(0.0, time.perf_counter() - started),
            "result_count": len(results),
        }
        return {
            "mode": mode_used,
            "degraded": degraded,
            "errors": errors,
            "model_versions": model_versions,
            "results": results,
        }


__all__ = ["MemoryHybridRetriever"]
