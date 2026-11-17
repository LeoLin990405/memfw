"""Minimal stdio MCP facade over a local notes index.

Tools:
    notes_search(query, limit, path_prefix, tags, course)
    notes_get(path)
    notes_status()

The server is read-only and local-only. It reuses the same NotesIndex as the
CLI; embedding configuration is optional.

Privacy clearance (MM0): with ``--clearance`` (or ``MEMFW_NOTES_CLEARANCE``)
the server returns only notes whose privacy level is in it. A note's level is
the strictest of its frontmatter ``privacy`` field (missing →
``--legacy-privacy-default``, ``local-private`` unless set) and its
``privacy:``/``sensitivity:`` tags, read from the note file itself. Without a
clearance nothing is filtered, the behaviour before MM0.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ..ports.privacy import (
    LEGACY_PRIVACY_FALLBACK,
    admits,
    clearance_is_valid,
    legacy_default,
    metadata_level,
    parse_clearance,
)
from ..related import RelatedIndex
from .feedback import FeedbackEvent, FeedbackStore
from .index import HttpEmbeddingProvider, NoteParseError, NotesIndex, _split_frontmatter
from .retrieval import HttpRerankerProvider, NotesRetriever, RetrievalConfig, query_coverage

SERVER_INFO = {"name": "memfw-notes", "version": "0.2.0"}
CLEARANCE_ENV = "MEMFW_NOTES_CLEARANCE"
PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "notes_search",
        "description": "Hybrid section search over a local Markdown notes tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "path_prefix": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "course": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["fts", "vector", "hybrid"],
                    "default": "hybrid",
                },
                "candidates": {"type": "integer", "default": 50},
                "rrf_k": {"type": "integer", "default": 60},
                "task_id": {"type": "string"},
                "tenant": {"type": "string"},
                "min_query_coverage": {"type": "number"},
                "source": {"type": "string"},
                "rerank_candidates": {"type": "integer", "default": 0},
                "rerank_model": {"type": "string", "default": "reranker"},
                "metadata": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "since": {"type": "number"},
                "until": {"type": "number"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "notes_get",
        "description": "Read one Markdown note by path relative to the notes root.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "notes_related",
        "description": "Read derived note-to-note relatedness edges; never writes notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["path"],
        },
    },
    {
        "name": "notes_status",
        "description": "Report notes index counts, tokenizer and vector state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "retrieval_feedback",
        "description": "Record explicit client feedback for a retrieval trace. "
        "Uncited results are not positives; no label is inferred.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string"},
                "task_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["citation", "correction", "task_outcome", "permission"],
                },
                "candidate_path": {"type": "string"},
                "relevance": {"type": "string"},
                "correctness": {"type": "string"},
                "permission": {"type": "string"},
                "confidence": {"type": "number"},
                "note": {"type": "string"},
                "actor": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["trace_id", "task_id", "kind"],
        },
    },
    {
        "name": "retrieval_trace",
        "description": "Read a stored retrieval trace by trace_id (no body by default).",
        "inputSchema": {
            "type": "object",
            "properties": {"trace_id": {"type": "string"}},
            "required": ["trace_id"],
        },
    },
]


class NotesGateway:
    def __init__(
        self,
        root: Path,
        db: Path,
        *,
        embedder: HttpEmbeddingProvider | None = None,
        reranker: HttpRerankerProvider | None = None,
        feedback_store: FeedbackStore | None = None,
        related_db: Path | None = None,
        clearance: frozenset[str] | str | None = None,
        legacy_privacy_default: str = LEGACY_PRIVACY_FALLBACK,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.index = NotesIndex(db, embedder=embedder)
        self.reranker = reranker
        self.feedback_store = feedback_store
        self.related = RelatedIndex(related_db) if related_db else None
        #: ``None`` filters nothing (before MM0); otherwise the levels returned.
        self.clearance = (
            clearance
            if clearance is None or isinstance(clearance, frozenset)
            else parse_clearance(clearance)
        )
        self.legacy_privacy_default = legacy_default(legacy_privacy_default)

    def _note_path(self, rel: str) -> Path | None:
        """``rel`` under the notes root, or ``None`` when unsafe or missing."""

        cleaned = str(rel or "").strip().lstrip("/")
        if not cleaned or ".." in Path(cleaned).parts:
            return None
        path = (self.root / cleaned).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        return path

    def _cleared_hits(
        self, query: str, result: Any, limit: int, threshold: float | None
    ) -> tuple[list[Any], int, bool, str]:
        """Hits, candidate count and abstention as if withheld notes never matched.

        The retriever judged coverage on its own top hit, which may be a
        withheld note, so the decision is taken again on the visible pool.
        """

        pool = [hit for hit in (result.candidate_hits or result.hits) if self.admitted(hit.path)]
        visible = [hit for hit in result.hits if self.admitted(hit.path)]
        if threshold is None:
            return visible, len(pool), False, ""
        if not pool:
            return [], 0, True, "no candidates"
        top = pool[0]
        coverage = query_coverage(query, top.title + " " + top.heading + " " + top.content)
        if coverage < threshold:
            return [], len(pool), True, f"top query coverage {coverage:.3f} < {threshold:.3f}"
        return (visible or pool[:limit]), len(pool), False, ""

    def _status(self) -> dict[str, Any]:
        """``notes_status``; with a clearance the counts cover only visible notes."""

        status = self.index.status()
        if self.clearance is None or not status.get("exists"):
            return status
        with sqlite3.connect(self.index.db_path) as connection:
            rows = connection.execute(
                "SELECT n.path, COUNT(s.id) FROM notes n "
                "LEFT JOIN sections s ON s.note_id = n.id GROUP BY n.id"
            ).fetchall()
        visible = [(str(path), int(count)) for path, count in rows if self.admitted(str(path))]
        status["notes"] = len(visible)
        status["sections"] = sum(count for _path, count in visible)
        # Vector counts are not split per note here; do not report the total.
        status["vectors"] = None
        return status

    def admitted(self, rel: str) -> bool:
        """Whether the clearance admits the note at ``rel`` (always, without one).

        Read from the note file, not the index, so a stale index cannot widen
        access. An unreadable note, bad frontmatter or a path outside the root
        is withheld (fail closed).
        """

        if self.clearance is None:
            return True
        path = self._note_path(rel)
        if path is None:
            return False
        try:
            frontmatter, _body, _offset = _split_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, NoteParseError):
            return False
        return admits(self.clearance, metadata_level(frontmatter, self.legacy_privacy_default))

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name == "retrieval_feedback":
            if self.feedback_store is None:
                raise RuntimeError("feedback store is not configured")
            feedback_id = self.feedback_store.add_feedback(
                FeedbackEvent(
                    trace_id=str(args.get("trace_id") or ""),
                    task_id=str(args.get("task_id") or ""),
                    kind=str(args.get("kind") or "") or "citation",  # type: ignore[arg-type]
                    candidate_path=str(args.get("candidate_path") or ""),
                    relevance=str(args.get("relevance") or "unknown"),
                    correctness=str(args.get("correctness") or "unknown"),
                    permission=str(args.get("permission") or "unknown"),
                    confidence=float(args.get("confidence") or 0.0),
                    note=str(args.get("note") or "")[:500],
                    actor=str(args.get("actor") or ""),
                    source=str(args.get("source") or ""),
                )
            )
            return {"feedback_id": feedback_id}
        if name == "retrieval_trace":
            if self.feedback_store is None:
                raise RuntimeError("feedback store is not configured")
            trace = self.feedback_store.trace(str(args.get("trace_id") or ""))
            if trace is None:
                raise ValueError("unknown trace")
            if self.clearance is not None:
                trace["candidates"] = [
                    candidate
                    for candidate in trace.get("candidates") or []
                    if isinstance(candidate, dict) and self.admitted(str(candidate.get("path")))
                ]
            return trace
        if name == "notes_related":
            if self.related is None:
                raise RuntimeError("related index is not configured")
            identifier = str(args.get("path") or "")
            limit = max(1, min(int(args.get("limit", 10)), 50))
            if self.clearance is None:
                related = self.related.related("notes", identifier, limit=limit)
            else:
                if not self.admitted(identifier):
                    raise FileNotFoundError(identifier)
                # Only note neighbours can be checked against the clearance;
                # anything else is withheld. Over-fetch so the page can fill.
                related = [
                    item
                    for item in self.related.related("notes", identifier, limit=50)
                    if _related_note_path(item) is not None
                    and self.admitted(str(_related_note_path(item)))
                ][:limit]
            return {"path": identifier, "related": related}
        if name == "notes_status":
            return self._status()
        if name == "notes_get":
            rel = str(args.get("path") or "").strip().lstrip("/")
            if not rel or ".." in Path(rel).parts:
                raise ValueError("unsafe note path")
            path = (self.root / rel).resolve()
            if self.root not in path.parents or not path.is_file():
                raise FileNotFoundError(rel)
            if not self.admitted(rel):
                # The same answer as a missing note: existence is not revealed.
                raise FileNotFoundError(rel)
            return {"path": rel, "content": path.read_text(encoding="utf-8", errors="replace")}
        if name == "notes_search":
            query = str(args.get("query") or "")
            limit = max(1, min(int(args.get("limit", 10)), 50))
            tags = args.get("tags") or []
            retriever = NotesRetriever(
                self.index,
                config=RetrievalConfig(
                    mode=str(args.get("mode") or "hybrid"),  # type: ignore[arg-type]
                    candidates=max(1, min(int(args.get("candidates", 50)), 500)),
                    final_k=limit,
                    rrf_k=max(1, min(int(args.get("rrf_k", 60)), 1000)),
                    rerank_candidates=max(0, min(int(args.get("rerank_candidates", 0)), 500)),
                ),
                reranker=self.reranker,
                trace_store=self.feedback_store,
            )
            result = retriever.search(
                query,
                limit=limit,
                task_id=args.get("task_id"),
                tenant=str(args.get("tenant") or ""),
                source=str(args.get("source") or ""),
                path_prefix=args.get("path_prefix"),
                tags=[str(tag) for tag in tags],
                course=args.get("course"),
                metadata={
                    str(key): str(value) for key, value in (args.get("metadata") or {}).items()
                }
                or None,
                since=args.get("since"),
                until=args.get("until"),
            )
            hits = list(result.hits)
            candidates = result.candidates
            abstained, abstain_reason = result.abstained, result.abstain_reason
            if self.clearance is not None:
                hits, candidates, abstained, abstain_reason = self._cleared_hits(
                    query, result, limit, retriever.config.min_query_coverage
                )
            return {
                "mode": result.mode_used,
                "degraded": result.degraded,
                "rewrites": result.rewrites,
                "errors": result.errors,
                "candidates": candidates,
                "trace_id": result.trace_id,
                "task_id": result.task_id,
                "abstained": abstained,
                "abstain_reason": abstain_reason,
                "results": [hit.as_dict() for hit in hits],
            }
        raise KeyError(name)


def _related_note_path(item: Any) -> str | None:
    """The note path of one ``RelatedIndex.related`` row, or ``None`` if it is not a note."""

    if not isinstance(item, dict) or item.get("source") not in ("notes", "notes_sections"):
        return None
    value = item.get("path") or (item.get("id") if item.get("source") == "notes" else None)
    return str(value) if value else None


_CONFIG_ERRORS = frozenset(
    {
        "feedback store is not configured",
        "related index is not configured",
    }
)


def _client_error_text(exc: Exception) -> str:
    """Safe MCP error text. SQLite and other internal messages stay server-side."""

    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    lowered = text.lower()
    internal = "like or glob" in lowered or "pattern too complex" in lowered
    if internal or isinstance(exc, sqlite3.Error):
        return "internal error"
    if isinstance(exc, FileNotFoundError):
        return text[:300] or "not found"
    if isinstance(exc, ValueError):
        return text[:300] or "invalid request"
    if isinstance(exc, KeyError):
        return "unknown tool"
    if isinstance(exc, RuntimeError) and text in _CONFIG_ERRORS:
        return text
    return "internal error"


def handle_request(gateway: NotesGateway, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None  # notification
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            payload = gateway.call(name, args)
        except Exception as exc:  # noqa: BLE001 - MCP boundary
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"error": _client_error_text(exc)})}
                    ],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": False,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method {method}"},
    }


def serve(gateway: NotesGateway) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(gateway, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memfw-notes-mcp")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--model", default="embedding-model")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--feedback-db", type=Path)
    parser.add_argument("--related-db", type=Path)
    parser.add_argument("--rerank-endpoint")
    parser.add_argument("--rerank-model", default="reranker")
    parser.add_argument(
        "--clearance",
        default=None,
        help=(
            "privacy levels to return, comma separated (public, shared, local-private); "
            f"falls back to {CLEARANCE_ENV}; unset means no privacy filtering"
        ),
    )
    parser.add_argument(
        "--legacy-privacy-default",
        default=LEGACY_PRIVACY_FALLBACK,
        help="level of notes without a privacy field (default local-private)",
    )
    args = parser.parse_args(argv)
    clearance = args.clearance if args.clearance is not None else os.environ.get(CLEARANCE_ENV)
    if not clearance_is_valid(clearance):
        parser.error("--clearance must list only public, shared, local-private")
    embedder = None
    if args.endpoint:
        embedder = HttpEmbeddingProvider(
            args.endpoint,
            model=args.model,
            dimension=args.dimension,
            batch_size=args.batch_size,
        )
    reranker = None
    if args.rerank_endpoint:
        reranker = HttpRerankerProvider(args.rerank_endpoint, model=args.rerank_model)
    feedback_store = FeedbackStore(args.feedback_db) if args.feedback_db else None
    serve(
        NotesGateway(
            args.root,
            args.db,
            embedder=embedder,
            reranker=reranker,
            feedback_store=feedback_store,
            related_db=args.related_db,
            clearance=clearance,
            legacy_privacy_default=args.legacy_privacy_default,
        )
    )


if __name__ == "__main__":
    main()
