"""Non-authoritative duplicate/conflict suggestions for staged candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.models import EventKind, MemoryDocument, Tier
from ..core.repository import Repository
from ..memory_hybrid import MemoryHybridRetriever


@dataclass(frozen=True)
class CandidateSuggestion:
    memory_id: str
    title: str
    kind: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "title": self.title,
            "kind": self.kind,
            "score": round(self.score, 4),
        }


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))


def _tokens(text: str) -> set[str]:
    return {token for token in _norm(text).split() if len(token) > 1}


def suggest_related(
    repository: Repository,
    title: str,
    content: str,
    *,
    embedder: Any | None = None,
    limit: int = 5,
    embedding_allow_remote: bool = False,
    tenant: str = "",
    allowed_sources: tuple[str, ...] = (),
    admit: Callable[[MemoryDocument], bool] | None = None,
    record_access: bool = True,
) -> list[CandidateSuggestion]:
    """Memories related to a candidate being staged; read-only.

    The search is the caller's: ``tenant`` and ``allowed_sources`` bound it,
    and ``admit`` (the interface's visibility and privacy clearance) drops a
    memory the caller may not see, so its id and title never appear. The
    retriever itself writes nothing; with ``record_access`` (the Runtime's
    C6 switch) each suggestion that is shown, and only those, is warmed when
    cold and gets the one ``memory_access`` event the search path writes, in
    the same order as before, so a withheld memory is never logged or warmed.
    """

    retriever = MemoryHybridRetriever(
        repository,
        embedder=embedder,
        embedding_allow_remote=embedding_allow_remote,
        tenant=tenant,
        allowed_sources=allowed_sources,
        record_access=False,
        autowarm=False,
    )
    query = title.strip() or content[:200]
    if not query:
        return []
    try:
        result = retriever.search(query, limit=limit, mode="hybrid" if embedder else "fts")
    except Exception:
        return []
    source_tokens = _tokens(title + " " + content)
    source_norm = _norm(content)
    output: list[CandidateSuggestion] = []
    for item in result["results"]:
        try:
            document = repository.get(item["id"])
        except Exception:
            continue
        if document.tenant != tenant or (admit is not None and not admit(document)):
            continue
        if record_access:
            try:
                if document.is_stub or document.tier == Tier.COLD:
                    repository.autowarm(document.id, actor="mechanism")
                repository.capture_event(
                    EventKind.MEMORY_ACCESS,
                    actor="mechanism",
                    details={
                        "memory_id": document.id,
                        "operation": retriever.last_access_operation or "retrieve",
                    },
                )
                document = repository.get(document.id)
            except Exception:  # noqa: BLE001 - best effort, as the search path
                pass
        existing_norm = _norm(document.body)
        if source_norm and source_norm == existing_norm:
            kind = "duplicate"
        elif source_tokens & _tokens(document.title + " " + document.body):
            kind = "extends"
        else:
            kind = "related"
        negation = re.compile(r"\b(not|no|never|without|cannot)\b", re.I)
        if bool(negation.search(content)) != bool(negation.search(document.body)):
            kind = "conflicts"
        output.append(
            CandidateSuggestion(
                memory_id=document.id,
                title=document.title,
                kind=kind,
                score=float(item.get("score") or 0.0),
            )
        )
    return output


__all__ = ["CandidateSuggestion", "suggest_related"]
