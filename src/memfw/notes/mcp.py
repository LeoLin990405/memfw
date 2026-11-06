"""Minimal stdio MCP facade over a local notes index.

Tools:
    notes_search(query, limit, path_prefix, tags, course)
    notes_get(path)
    notes_status()

The server is read-only and local-only. It reuses the same NotesIndex as the
CLI; embedding configuration is optional.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .feedback import FeedbackEvent, FeedbackStore
from .index import HttpEmbeddingProvider, NotesIndex
from .retrieval import HttpRerankerProvider, NotesRetriever, RetrievalConfig

SERVER_INFO = {"name": "memfw-notes", "version": "0.1.0"}
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
                "source": {"type": "string"},
                "rerank_candidates": {"type": "integer", "default": 0},
                "rerank_endpoint": {"type": "string"},
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
    ) -> None:
        self.root = root.expanduser().resolve()
        self.index = NotesIndex(db, embedder=embedder)
        self.reranker = reranker
        self.feedback_store = feedback_store

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
            return trace
        if name == "notes_status":
            return self.index.status()
        if name == "notes_get":
            rel = str(args.get("path") or "").strip().lstrip("/")
            if not rel or ".." in Path(rel).parts:
                raise ValueError("unsafe note path")
            path = (self.root / rel).resolve()
            if self.root not in path.parents or not path.is_file():
                raise FileNotFoundError(rel)
            return {"path": rel, "content": path.read_text(encoding="utf-8", errors="replace")}
        if name == "notes_search":
            query = str(args.get("query") or "")
            limit = max(1, min(int(args.get("limit", 10)), 50))
            tags = args.get("tags") or []
            reranker = self.reranker
            if args.get("rerank_endpoint"):
                reranker = HttpRerankerProvider(
                    str(args["rerank_endpoint"]),
                    model=str(args.get("rerank_model") or "reranker"),
                )
            retriever = NotesRetriever(
                self.index,
                config=RetrievalConfig(
                    mode=str(args.get("mode") or "hybrid"),  # type: ignore[arg-type]
                    candidates=max(1, min(int(args.get("candidates", 50)), 500)),
                    final_k=limit,
                    rrf_k=max(1, min(int(args.get("rrf_k", 60)), 1000)),
                    rerank_candidates=max(0, min(int(args.get("rerank_candidates", 0)), 500)),
                ),
                reranker=reranker,
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
            return {
                "mode": result.mode_used,
                "degraded": result.degraded,
                "rewrites": result.rewrites,
                "errors": result.errors,
                "candidates": result.candidates,
                "trace_id": result.trace_id,
                "task_id": result.task_id,
                "results": [hit.as_dict() for hit in result.hits],
            }
        raise KeyError(name)


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
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
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
    args = parser.parse_args(argv)
    embedder = None
    if args.endpoint:
        embedder = HttpEmbeddingProvider(
            args.endpoint,
            model=args.model,
            dimension=args.dimension,
            batch_size=args.batch_size,
        )
    feedback_store = FeedbackStore(args.feedback_db) if args.feedback_db else None
    serve(
        NotesGateway(
            args.root,
            args.db,
            embedder=embedder,
            feedback_store=feedback_store,
        )
    )


if __name__ == "__main__":
    main()
