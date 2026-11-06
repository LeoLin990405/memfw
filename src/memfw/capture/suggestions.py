"""Non-authoritative duplicate/conflict suggestions for staged candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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
) -> list[CandidateSuggestion]:
    retriever = MemoryHybridRetriever(repository, embedder=embedder)
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
