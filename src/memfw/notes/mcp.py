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

from .index import HttpEmbeddingProvider, NotesIndex

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
]


class NotesGateway:
    def __init__(
        self, root: Path, db: Path, *, embedder: HttpEmbeddingProvider | None = None
    ) -> None:
        self.root = root.expanduser().resolve()
        self.index = NotesIndex(db, embedder=embedder)

    def call(self, name: str, args: dict[str, Any]) -> Any:
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
            hits = self.index.search(
                query,
                limit=limit,
                path_prefix=args.get("path_prefix"),
                tags=[str(tag) for tag in tags],
                course=args.get("course"),
            )
            return [hit.as_dict() for hit in hits]
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
    args = parser.parse_args(argv)
    embedder = None
    if args.endpoint:
        embedder = HttpEmbeddingProvider(
            args.endpoint,
            model=args.model,
            dimension=args.dimension,
            batch_size=args.batch_size,
        )
    serve(NotesGateway(args.root, args.db, embedder=embedder))


if __name__ == "__main__":
    main()
