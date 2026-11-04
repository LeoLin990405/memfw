"""Minimal stdio MCP server for a local memfw vault.

Tools:
    memory_search(query, limit)      -> FTS5-ranked memory ids and titles
    memory_get(memory_id)            -> one Markdown memory
    memory_remember(content, title)  -> stage a candidate under _candidates/remember/
    memory_status()                  -> vault and log summary

The server is deliberately local-only: it has no remote host or external search backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memfw.core.models import Layer, Tier
from memfw.core.repository import Repository
from memfw.retrieval import VaultRetriever

SERVER_INFO = {"name": "memfw", "version": "0.1.0"}
PROTOCOL = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": "Search the local memfw vault with its rebuildable FTS5 index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_get",
        "description": "Read one memory by id (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "memory_remember",
        "description": "Stage a new memory candidate for human review; never writes the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}, "title": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "memory_status",
        "description": "Return vault size, layer/tier counts, and log verification summary.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return value[:48] or "memory"


class Gateway:
    """MCP tool implementations bound to one local vault root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    def _repo(self) -> Repository:
        repository = Repository(self.root)
        repository.init()
        return repository

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query required")
        repository = self._repo()
        retriever = VaultRetriever(repository)
        identifiers = retriever.retrieve(query, limit=max(0, min(limit, 50)))
        results = []
        for memory_id in identifiers:
            document = repository.get(memory_id)
            results.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "layer": document.layer.value,
                    "tier": document.tier.value,
                    "updated_at": document.updated_at.isoformat(),
                }
            )
        return {"root": str(self.root), "results": results}

    def get(self, memory_id: str) -> dict[str, Any]:
        document = self._repo().get(memory_id)
        return {
            "id": document.id,
            "title": document.title,
            "body": document.body,
            "layer": document.layer.value,
            "tier": document.tier.value,
            "source": document.source,
            "provenance": document.provenance,
            "tags": list(document.tags),
        }

    def remember(self, content: str, title: str = "memory") -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("content required")
        directory = self.root / "_candidates" / "remember"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = directory / f"{stamp}-{_slug(title)}.md"
        body = (
            "---\n"
            f"name: {json.dumps(title, ensure_ascii=False)}\n"
            "metadata:\n"
            "  type: candidate\n"
            "  lifecycle: active\n"
            f"  created_at: {stamp}\n"
            "---\n\n"
            f"{text}\n"
        )
        path.write_text(body, encoding="utf-8")
        return {"staged_candidate": str(path), "authority": False}

    def status(self) -> dict[str, Any]:
        repository = self._repo()
        documents = repository.list()
        layers = {layer.value: 0 for layer in Layer}
        tiers = {tier.value: 0 for tier in Tier}
        for document in documents:
            layers[document.layer.value] += 1
            tiers[document.tier.value] += 1
        events = repository.events()
        return {
            "root": str(repository.root),
            "memories": len(documents),
            "layers": layers,
            "tiers": tiers,
            "index_exists": repository.index_path.exists(),
            "log": {
                "event_count": len(events),
                "head": events[-1].event_hash if events else "0" * 64,
            },
        }

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "memory_search": lambda args: self.search(
                str(args.get("query") or ""), int(args.get("limit") or 10)
            ),
            "memory_get": lambda args: self.get(str(args.get("memory_id") or "")),
            "memory_remember": lambda args: self.remember(
                str(args.get("content") or ""), str(args.get("title") or "memory")
            ),
            "memory_status": lambda _args: self.status(),
        }

    def respond(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            self.respond(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            )
            return
        if method in ("notifications/initialized", "initialized"):
            return
        if method == "ping":
            self.respond({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            return
        if method == "tools/list":
            self.respond({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
            return
        if method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            handler = self.handlers().get(name)
            if handler is None:
                self.respond(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"unknown tool: {name}"},
                    }
                )
                return
            try:
                result = handler(params.get("arguments") or {})
                self.respond(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, ensure_ascii=False),
                                }
                            ],
                            "isError": False,
                        },
                    }
                )
            except Exception as exc:  # pragma: no cover - protocol boundary
                self.respond(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": str(exc)[:500]}],
                            "isError": True,
                        },
                    }
                )
            return
        if msg_id is not None:
            self.respond(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
            )


def serve(root: Path | str) -> int:
    gateway = Gateway(root)
    sys.stderr.write("memfw MCP server ready\n")
    sys.stderr.flush()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            gateway.handle(json.loads(text))
        except json.JSONDecodeError:
            continue
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memfw-mcp", description="Local memfw MCP server")
    parser.add_argument(
        "--root",
        default=os.environ.get("MEMFW_ROOT", "."),
        help="memfw vault root",
    )
    args = parser.parse_args(argv)
    return serve(Path(args.root))


if __name__ == "__main__":
    raise SystemExit(main())
