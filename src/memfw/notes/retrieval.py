"""Configurable notes retrieval pipeline: rewrite, recall, fusion, rerank.

The index is the recall engine. This module owns the policy layer that a
deployment can tune without changing the stored index. No concrete model is
loaded here: rerankers and language models are injected through protocols.
"""

from __future__ import annotations

import json
import re
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

import jieba  # type: ignore[import-untyped]

from . import http as notes_http
from .feedback import CandidateTrace, FeedbackStore, RetrievalTrace
from .index import NoteHit, NotesIndex

Mode = Literal["fts", "vector", "hybrid"]


class RerankerProvider(Protocol):
    """Score query-document pairs and return a re-ordered candidate list."""

    name: str

    def rerank(self, query: str, candidates: Sequence[NoteHit], *, top_k: int) -> list[NoteHit]: ...


class NoOpReranker:
    name = "none"

    def rerank(self, query: str, candidates: Sequence[NoteHit], *, top_k: int) -> list[NoteHit]:
        del query
        return list(candidates[:top_k])


class HttpRerankerProvider:
    """OpenAI/llama.cpp ``/v1/rerank`` client with bounded truncation."""

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        timeout: float = 60.0,
        retries: int = 1,
        max_doc_chars: int = 4000,
    ) -> None:
        if max_doc_chars < 1:
            raise ValueError("max_doc_chars must be positive")
        self.endpoint = endpoint
        self.model = model
        self.name = f"http-rerank:{model}"
        self.timeout = timeout
        self.retries = max(0, retries)
        self.max_doc_chars = max_doc_chars

    def rerank(self, query: str, candidates: Sequence[NoteHit], *, top_k: int) -> list[NoteHit]:
        if not candidates:
            return []
        documents = [hit.content[: self.max_doc_chars] for hit in candidates]
        payload = json.dumps(
            {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(top_k, len(candidates)),
            }
        ).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(
                    self.endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with notes_http.open_http(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                if isinstance(body, dict):
                    rows = body.get("results")
                else:
                    rows = body
                if not isinstance(rows, list):
                    raise RuntimeError("reranker returned an unexpected response")
                scored: list[tuple[float, NoteHit]] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    index = row.get("index")
                    score = row.get("relevance_score", row.get("score"))
                    if (
                        isinstance(index, bool)
                        or not isinstance(index, int)
                        or index < 0
                        or index >= len(candidates)
                        or isinstance(score, bool)
                        or not isinstance(score, (int, float))
                    ):
                        continue
                    scored.append((float(score), candidates[index]))
                if not scored:
                    raise RuntimeError("reranker returned no valid scores")
                scored.sort(key=lambda item: item[0], reverse=True)
                return [
                    NoteHit(
                        path=hit.path,
                        title=hit.title,
                        heading=hit.heading,
                        content=hit.content,
                        score=score,
                        rank=position,
                        source=f"rerank:{hit.source}",
                        tags=hit.tags,
                        section_id=hit.section_id,
                        content_hash=hit.content_hash,
                    )
                    for position, (score, hit) in enumerate(scored[:top_k], start=1)
                ]
            except Exception as exc:  # noqa: BLE001 - retry/degrade boundary
                last = exc
                if attempt < self.retries:
                    continue
        raise RuntimeError(f"rerank request failed: {last}") from last


@dataclass(frozen=True)
class QueryRewrite:
    original: str
    queries: tuple[str, ...]
    model: str = "none"
    evidence: dict[str, str] = field(default_factory=dict)


class LanguageModelProvider(Protocol):
    """A language model used only for bounded candidate generation tasks."""

    name: str

    def rewrite_query(self, query: str, *, limit: int = 3) -> QueryRewrite: ...


class NullLanguageModel:
    name = "none"

    def rewrite_query(self, query: str, *, limit: int = 3) -> QueryRewrite:
        del limit
        return QueryRewrite(original=query, queries=(query,), model=self.name)


class SparseRetriever(Protocol):
    """Reserved interface; not wired until it beats the existing FTS5 baseline."""

    name: str

    def search(self, query: str, *, limit: int) -> list[NoteHit]: ...


@dataclass(frozen=True)
class RetrievalConfig:
    mode: Mode = "hybrid"
    candidates: int = 50
    final_k: int = 10
    rrf_k: int = 60
    rerank_candidates: int = 0
    rewrite: bool = False
    max_rewrites: int = 3
    auto_degrade: bool = True
    min_query_coverage: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"fts", "vector", "hybrid"}:
            raise ValueError("mode must be fts, vector or hybrid")
        if self.candidates < 1:
            raise ValueError("candidates must be positive")
        if self.final_k < 1:
            raise ValueError("final_k must be positive")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        if self.rerank_candidates < 0:
            raise ValueError("rerank_candidates must be non-negative")
        if self.max_rewrites < 1:
            raise ValueError("max_rewrites must be positive")


@dataclass
class RetrievalResult:
    query: str
    hits: list[NoteHit]
    mode_used: str
    degraded: bool = False
    rewrites: list[str] = field(default_factory=list)
    reranked: bool = False
    errors: list[str] = field(default_factory=list)
    candidates: int = 0
    candidate_hits: list[NoteHit] = field(default_factory=list)
    trace_id: str | None = None
    task_id: str | None = None
    abstained: bool = False
    abstain_reason: str = ""


def _dedupe(hits: Sequence[NoteHit]) -> list[NoteHit]:
    seen: set[tuple[str, int | None]] = set()
    output: list[NoteHit] = []
    for hit in hits:
        key = (hit.path, hit.section_id)
        if key in seen:
            continue
        seen.add(key)
        output.append(hit)
    return output


def query_coverage(query: str, text: str) -> float:
    """Fraction of query terms present in a candidate, for opt-in abstention."""

    def terms(value: str) -> set[str]:
        output: list[str] = []
        for word in jieba.lcut(value.lower()):
            word = word.strip()
            if not word or re.fullmatch(r"[\W_]+", word):
                continue
            if len(word) == 1 and "\u4e00" <= word <= "\u9fff":
                continue
            output.append(word)
        output.extend(re.findall(r"[a-z][a-z0-9-]+", value.lower()))
        return set(output)

    query_terms = terms(query)
    if not query_terms:
        return 0.0
    return len(query_terms & terms(text)) / len(query_terms)


class NotesRetriever:
    """Policy pipeline over :class:`NotesIndex`.

    Rewrites are additive: the original query is always searched first. A
    failed reranker or language model never suppresses lexical/vector results.
    """

    def __init__(
        self,
        index: NotesIndex,
        *,
        config: RetrievalConfig | None = None,
        reranker: RerankerProvider | None = None,
        language_model: LanguageModelProvider | None = None,
        sparse: SparseRetriever | None = None,
        trace_store: FeedbackStore | None = None,
    ) -> None:
        self.index = index
        self.config = config or RetrievalConfig()
        self.reranker = reranker
        self.language_model = language_model
        self.sparse = sparse
        self.trace_store = trace_store

    def _record_trace(
        self,
        result: RetrievalResult,
        *,
        task_id: str | None,
        tenant: str,
        source: str,
    ) -> RetrievalResult:
        if self.trace_store is None or not task_id:
            return result
        trace_id = str(uuid.uuid4())
        model_versions: dict[str, str] = {}
        embedder = self.index.embedder
        if embedder is not None:
            model_versions["embedding"] = str(
                getattr(embedder, "fingerprint", "") or getattr(embedder, "model", "")
            )
        if self.reranker is not None:
            model_versions["reranker"] = str(getattr(self.reranker, "name", ""))
        if self.language_model is not None:
            model_versions["language_model"] = str(getattr(self.language_model, "name", ""))
        trace_hits = result.candidate_hits or result.hits
        candidates = tuple(
            CandidateTrace(
                path=hit.path,
                rank=hit.rank,
                score=float(hit.score),
                section_id=hit.section_id,
                content_hash=hit.content_hash,
                source=hit.source,
            )
            for hit in trace_hits
        )
        try:
            self.trace_store.record_trace(
                RetrievalTrace(
                    trace_id=trace_id,
                    task_id=task_id,
                    query=result.query,
                    mode=result.mode_used,
                    rewrites=tuple(result.rewrites),
                    candidates=candidates,
                    model_versions=model_versions,
                    tenant=tenant,
                    source=source,
                    include_content=False,
                )
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must not break search
            result.errors.append(f"trace: {exc}")
            return result
        result.trace_id = trace_id
        result.task_id = task_id
        return result

    def _rewrite(self, query: str, errors: list[str]) -> list[str]:
        if not self.config.rewrite or self.language_model is None:
            return [query]
        try:
            rewrite = self.language_model.rewrite_query(query, limit=self.config.max_rewrites)
        except Exception as exc:  # noqa: BLE001 - query rewrite is optional
            errors.append(f"rewrite: {exc}")
            return [query]
        candidates = [query]
        for candidate in rewrite.queries:
            text = str(candidate).strip()
            if text and text not in candidates:
                candidates.append(text)
            if len(candidates) >= self.config.max_rewrites:
                break
        return candidates

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        path_prefix: str | None = None,
        tags: Sequence[str] = (),
        course: str | None = None,
        metadata: dict[str, str] | None = None,
        since: float | None = None,
        until: float | None = None,
        task_id: str | None = None,
        tenant: str = "",
        source: str = "",
    ) -> RetrievalResult:
        final_k = limit or self.config.final_k
        errors: list[str] = []
        rewrites = self._rewrite(query, errors)
        fused: dict[tuple[str, int | None], tuple[float, NoteHit, int]] = {}
        for text in rewrites:
            hits = self.index.search(
                text,
                limit=self.config.candidates,
                path_prefix=path_prefix,
                tags=tags,
                course=course,
                metadata=metadata,
                since=since,
                until=until,
                mode=self.config.mode,
                candidates=self.config.candidates,
                rrf_k=self.config.rrf_k,
            )
            for rank, hit in enumerate(hits, start=1):
                contribution = 1.0 / (self.config.rrf_k + rank)
                key = (hit.path, hit.section_id)
                if key in fused:
                    score, previous, best_rank = fused[key]
                    fused[key] = (score + contribution, previous, min(best_rank, rank))
                else:
                    fused[key] = (contribution, hit, rank)
        ranked = sorted(fused.values(), key=lambda item: (-item[0], item[1].path))
        candidates: list[NoteHit] = []
        for position, (score, hit, _) in enumerate(ranked, start=1):
            candidates.append(
                NoteHit(
                    path=hit.path,
                    title=hit.title,
                    heading=hit.heading,
                    content=hit.content,
                    score=score,
                    rank=position,
                    source=hit.source,
                    tags=hit.tags,
                    section_id=hit.section_id,
                    content_hash=hit.content_hash,
                )
            )
        if not candidates:
            if self.config.mode in {"vector", "hybrid"} and self.config.auto_degrade:
                fallback = NotesRetriever(
                    self.index,
                    config=RetrievalConfig(
                        mode="fts",
                        candidates=self.config.candidates,
                        final_k=final_k,
                        rrf_k=self.config.rrf_k,
                        auto_degrade=False,
                        min_query_coverage=self.config.min_query_coverage,
                    ),
                ).search(
                    query,
                    limit=final_k,
                    path_prefix=path_prefix,
                    tags=tags,
                    course=course,
                    metadata=metadata,
                    since=since,
                    until=until,
                )
                fallback.errors = [*errors, *fallback.errors, "vector recall unavailable; used FTS"]
                return self._record_trace(fallback, task_id=task_id, tenant=tenant, source=source)
            return self._record_trace(
                RetrievalResult(
                    query=query,
                    hits=[],
                    abstained=self.config.min_query_coverage is not None,
                    abstain_reason=(
                        "no candidates" if self.config.min_query_coverage is not None else ""
                    ),
                    mode_used=self.config.mode,
                    degraded=self.config.mode != "fts",
                    rewrites=rewrites,
                    errors=errors,
                ),
                task_id=task_id,
                tenant=tenant,
                source=source,
            )
        reranked = False
        if self.reranker is not None and self.config.rerank_candidates > 0:
            head = candidates[: self.config.rerank_candidates]
            tail = candidates[self.config.rerank_candidates :]
            try:
                reranked_head = self.reranker.rerank(query, head, top_k=len(head))
                if not reranked_head:
                    raise RuntimeError("reranker returned no candidates")
                known = {(hit.path, hit.section_id) for hit in head}
                valid = [hit for hit in reranked_head if (hit.path, hit.section_id) in known]
                if not valid:
                    raise RuntimeError("reranker returned unknown candidates")
                candidates = _dedupe([*valid, *tail])
                reranked = True
            except Exception as exc:  # noqa: BLE001 - rerank failure degrades
                errors.append(f"rerank: {exc}")
        for rank, hit in enumerate(candidates[:final_k], start=1):
            candidates[rank - 1] = NoteHit(
                path=hit.path,
                title=hit.title,
                heading=hit.heading,
                content=hit.content,
                score=hit.score,
                rank=rank,
                source=hit.source,
                tags=hit.tags,
                section_id=hit.section_id,
                content_hash=hit.content_hash,
            )
        hits = candidates[:final_k]
        abstained = False
        reason = ""
        threshold = self.config.min_query_coverage
        if threshold is not None and hits:
            coverage = query_coverage(
                query,
                hits[0].title + " " + hits[0].heading + " " + hits[0].content,
            )
            if coverage < threshold:
                abstained = True
                reason = f"top query coverage {coverage:.3f} < {threshold:.3f}"
                hits = []
        return self._record_trace(
            RetrievalResult(
                query=query,
                hits=hits,
                mode_used=self.config.mode,
                degraded=self.config.mode != "fts"
                and not any(hit.source in {"vector", "hybrid"} for hit in candidates),
                rewrites=rewrites,
                reranked=reranked,
                errors=errors,
                candidates=len(candidates),
                candidate_hits=list(candidates),
                abstained=abstained,
                abstain_reason=reason,
            ),
            task_id=task_id,
            tenant=tenant,
            source=source,
        )
