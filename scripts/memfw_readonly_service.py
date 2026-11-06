#!/usr/bin/env python3
"""Minimal read-only HTTP facade over a MemFW vault's disposable index.

No write path exists. Intended for a private tailnet host; authentication is a
shared token supplied through the environment. The service reads Markdown and
SQLite only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

MAX_QUERY = 512
MAX_LIMIT = 50
TOKEN_HEADER = "X-Memfw-Token"


class ServiceError(ValueError):
    pass


def _connect(root: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(root / 'index.sqlite3'), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _metadata(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.startswith("---\n"):
            return {}
        _, header, _ = raw.split("---\n", 2)
        parsed = yaml.safe_load(header)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}


def _fts_query(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def related_vault(
    root: Path | str,
    memory_id: str,
    *,
    limit: int = 10,
    tenant: str = "",
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    if not memory_id or "/" in memory_id or ".." in memory_id:
        raise ServiceError("invalid memory id")
    path = base / "memories" / f"{memory_id}.md"
    if not path.is_file():
        raise ServiceError("memory not found")
    metadata = _metadata(path)
    if tenant and str(metadata.get("tenant") or "") != tenant:
        raise ServiceError("memory not visible in tenant")
    title = str(metadata.get("title") or "")
    body = ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("---\n"):
        try:
            _, _, body = raw.split("---\n", 2)
        except ValueError:
            body = raw
    query = (title or " ".join(body.split()[:8]))[:512]
    result = search_vault(base, query, limit=max(2, min(limit + 1, 50)), tenant=tenant)
    results = [item for item in result["results"] if item["id"] != memory_id][:limit]
    return {"mode": "fts", "degraded": True, "memory_id": memory_id, "results": results}


def search_vault(
    root: Path | str,
    query: str,
    *,
    limit: int = 10,
    source: str = "",
    tenant: str = "",
    include_history: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    if not (base / "vault.yaml").exists() or not (base / "index.sqlite3").exists():
        raise ServiceError("not a MemFW vault or index missing")
    needle = query.strip()
    if not needle or len(needle) > MAX_QUERY:
        raise ServiceError("query must be 1..512 characters")
    limit = max(1, min(int(limit), MAX_LIMIT))
    connection = _connect(base)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_rows)")}
        if not {"id", "title", "summary", "source", "tier", "layer"} <= columns:
            raise ServiceError("unsupported MemFW index schema")
        temporal = _has_table(connection, "memory_temporal")
        clauses = []
        params: list[Any] = []
        if temporal:
            if include_history:
                pass
            elif as_of:
                clauses.append(
                    "EXISTS (SELECT 1 FROM memory_temporal t WHERE t.id=r.id "
                    "AND (t.valid_from='' OR t.valid_from <= ?) "
                    "AND (t.valid_to='' OR t.valid_to > ?))"
                )
                params.extend([as_of, as_of])
            else:
                clauses.append("r.id NOT IN (SELECT id FROM memory_temporal WHERE valid_to != '')")
        if len(needle) == 2:
            clauses.append(
                "(instr(r.title, ?) > 0 OR instr(r.search_body, ?) > 0 "
                "OR instr(r.summary, ?) > 0 OR instr(r.tags, ?) > 0)"
            )
            params.extend([needle, needle, needle, needle])
            sql = "SELECT r.*, 0.0 AS score FROM memory_rows r"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY r.id LIMIT ?"
        else:
            clauses.append("memory_fts MATCH ?")
            params.append(_fts_query(needle))
            sql = (
                "SELECT r.*, bm25(memory_fts) AS score FROM memory_fts "
                "JOIN memory_rows r ON r.id=memory_fts.id"
            )
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY score,r.id LIMIT ?"
        params.append(limit)
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    results = []
    for row in rows:
        document_path = base / "memories" / f"{row['id']}.md"
        metadata = _metadata(document_path)
        if not metadata:
            continue
        if tenant and str(metadata.get("tenant") or "") != tenant:
            continue
        if source and str(row["source"]) != source:
            continue
        if not include_history and (metadata.get("valid_to") or metadata.get("superseded_by")):
            continue
        results.append(
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "summary": str(row["summary"]),
                "source": str(row["source"]),
                "tier": str(row["tier"]),
                "layer": str(row["layer"]),
                "tenant": str(metadata.get("tenant") or ""),
                "score": float(row["score"] or 0.0),
                "content_hash": hashlib.sha256(document_path.read_bytes()).hexdigest(),
            }
        )
    return {
        "mode": "fts",
        "degraded": True,
        "errors": [],
        "results": results,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "memfw-readonly/1"

    def _authorized(self) -> bool:
        token = os.environ.get("MEMFW_READONLY_TOKEN", "")
        provided = self.headers.get(TOKEN_HEADER, "")
        return bool(token) and hmac.compare_digest(token, provided)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "memfw-readonly"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/search", "/related"}:
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ServiceError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ServiceError("request must be an object")
            root = os.environ.get("MEMFW_READONLY_ROOT", "")
            if self.path == "/related":
                result = related_vault(
                    root,
                    str(payload.get("memory_id") or ""),
                    limit=int(payload.get("limit") or 10),
                    tenant=str(payload.get("tenant") or ""),
                )
            else:
                result = search_vault(
                    root,
                    str(payload.get("query") or ""),
                    limit=int(payload.get("limit") or 10),
                    source=str(payload.get("source") or ""),
                    tenant=str(payload.get("tenant") or ""),
                    include_history=bool(payload.get("include_history", False)),
                    as_of=(str(payload["as_of"]) if payload.get("as_of") else None),
                )
            self._json(200, result)
        except ServiceError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # boundary
            self._json(500, {"error": type(exc).__name__})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    if not os.environ.get("MEMFW_READONLY_TOKEN"):
        raise SystemExit("MEMFW_READONLY_TOKEN is required")
    os.environ["MEMFW_READONLY_ROOT"] = str(Path(args.root).expanduser().resolve())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"memfw read-only service on {args.host}:{args.port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
