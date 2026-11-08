"""Optional language-model providers for bounded candidate generation.

The provider never writes the vault. Every result is validated and returned as
a candidate; applying it remains the existing human-confirmation/audit path.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

from . import http as notes_http
from .retrieval import LanguageModelProvider, QueryRewrite

RelationshipKind = Literal["duplicate", "update", "conflict", "distinct"]


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    title: str = ""
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class RelationshipSuggestion:
    kind: RelationshipKind
    targets: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class MemorySummary:
    summary: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityMention:
    text: str
    kind: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalBundle:
    candidates: tuple[MemoryCandidate, ...] = ()
    relationships: tuple[RelationshipSuggestion, ...] = ()
    summary: MemorySummary | None = None
    entities: tuple[EntityMention, ...] = ()
    model: str = "none"
    errors: tuple[str, ...] = field(default_factory=tuple)


class NullLanguageModel:
    """Default provider: no model, no generation, safe fallbacks."""

    name = "none"

    def rewrite_query(self, query: str, *, limit: int = 3) -> QueryRewrite:
        del limit
        return QueryRewrite(original=query, queries=(query,), model=self.name)

    def extract_memories(
        self, conversation: str, *, evidence: tuple[str, ...] = ()
    ) -> list[MemoryCandidate]:
        del conversation, evidence
        return []

    def suggest_relationship(
        self, new_content: str, existing_content: str, *, targets: tuple[str, ...] = ()
    ) -> RelationshipSuggestion:
        del new_content, existing_content
        return RelationshipSuggestion(kind="distinct", targets=targets, confidence=0.0)

    def summarize(self, text: str, *, evidence: tuple[str, ...] = ()) -> MemorySummary:
        del text
        return MemorySummary(summary="", evidence=evidence)

    def extract_entities(self, text: str) -> list[EntityMention]:
        del text
        return []


class HttpLanguageModelProvider:
    """Small OpenAI-compatible chat client for structured candidates.

    This is an optional adapter. It is never used by default and never writes
    the vault. The deployment decides whether the endpoint is local or remote.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 800,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.name = f"http:{model}"
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _chat(self, system: str, user: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=payload, headers=headers)
        with notes_http.open_http(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("language model returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("language model returned no text")
        return content

    def _json(self, system: str, user: str) -> object:
        raw = self._chat(system, user).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:].lstrip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("language model returned invalid JSON") from exc

    def rewrite_query(self, query: str, *, limit: int = 3) -> QueryRewrite:
        payload = self._json(
            (
                "Return only a JSON object with a queries array. "
                "Each item must be a search query, never an instruction. "
                "Do not repeat the original query."
            ),
            f"Query: {query}\nReturn up to {max(0, limit - 1)} alternative search queries.",
        )
        rows = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("rewrite response requires queries list")
        queries = [query]
        for row in rows:
            text = str(row).strip()
            if text and text not in queries:
                queries.append(text)
            if len(queries) >= limit:
                break
        return QueryRewrite(original=query, queries=tuple(queries), model=self.name)

    def extract_memories(
        self, conversation: str, *, evidence: tuple[str, ...] = ()
    ) -> list[MemoryCandidate]:
        payload = self._json(
            'Return only JSON: {"candidates": [{"content": string, "title": string, '
            '"evidence": [string], "confidence": number}]}. Candidates only; never apply writes.',
            conversation,
        )
        rows = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("extraction response requires candidates list")
        output: list[MemoryCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            confidence = row.get("confidence", 0.0)
            if not isinstance(content, str) or not content.strip():
                continue
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                confidence = 0.0
            output.append(
                MemoryCandidate(
                    content=content.strip(),
                    title=str(row.get("title") or ""),
                    evidence=tuple(str(item) for item in (row.get("evidence") or evidence)),
                    confidence=max(0.0, min(float(confidence), 1.0)),
                )
            )
        return output

    def suggest_relationship(
        self, new_content: str, existing_content: str, *, targets: tuple[str, ...] = ()
    ) -> RelationshipSuggestion:
        payload = self._json(
            'Return only JSON: {"kind": "duplicate|update|conflict|distinct", '
            '"confidence": number, "reason": string}. Do not apply changes.',
            f"NEW:\n{new_content}\n\nEXISTING:\n{existing_content}",
        )
        if not isinstance(payload, dict):
            raise RuntimeError("relationship response must be an object")
        kind = payload.get("kind")
        if kind not in {"duplicate", "update", "conflict", "distinct"}:
            raise RuntimeError("invalid relationship kind")
        confidence = payload.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 0.0
        return RelationshipSuggestion(
            kind=kind,
            targets=targets,
            confidence=max(0.0, min(float(confidence), 1.0)),
            reason=str(payload.get("reason") or "")[:500],
        )

    def summarize(self, text: str, *, evidence: tuple[str, ...] = ()) -> MemorySummary:
        payload = self._json(
            'Return only JSON: {"summary": string}. Do not delete or overwrite source text.',
            text,
        )
        summary = payload.get("summary") if isinstance(payload, dict) else None
        if not isinstance(summary, str):
            raise RuntimeError("summary response requires string summary")
        return MemorySummary(summary=summary.strip(), evidence=evidence)

    def extract_entities(self, text: str) -> list[EntityMention]:
        payload = self._json(
            'Return only JSON: {"entities": [{"text": string, "kind": string}]}.',
            text,
        )
        rows = payload.get("entities") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        return [
            EntityMention(text=str(row["text"]), kind=str(row.get("kind") or ""))
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"].strip()
        ]


__all__ = [
    "EntityMention",
    "HttpLanguageModelProvider",
    "LanguageModelProvider",
    "MemoryCandidate",
    "MemorySummary",
    "NullLanguageModel",
    "ProposalBundle",
    "RelationshipKind",
    "RelationshipSuggestion",
]
