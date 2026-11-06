"""Small local code retrieval index: lexical + optional semantic + structure.

It deliberately avoids a new service. Python files are chunked by module and
top-level function/class with the standard library AST; lexical FTS and a symbol
table are rebuildable projections. Embeddings are optional and degrade to
lexical+structure.
"""

from __future__ import annotations

import ast
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

IGNORED = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build"}
_SYMBOL_DEF = re.compile(
    r"(?:where\s+is\s+)?([A-Za-z_]\w*)\s+(?:defined|definition)|def\s+([A-Za-z_]\w*)|class\s+([A-Za-z_]\w*)",
    re.I,
)
_CALL = re.compile(r"(?:who\s+calls|callers\s+of|calls?)\s+([A-Za-z_]\w*)", re.I)


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]: ...


@dataclass(frozen=True)
class CodeChunk:
    path: str
    symbol: str
    kind: str
    start_line: int
    end_line: int
    content: str


def _chunks_for_file(root: Path, path: Path) -> list[CodeChunk]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    relative = str(path.relative_to(root))
    lines = text.splitlines()
    chunks: list[CodeChunk] = [CodeChunk(relative, "", "module", 1, max(1, len(lines)), text)]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return chunks
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = int(getattr(node, "lineno", 1))
        end = int(getattr(node, "end_lineno", start))
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        chunks.append(
            CodeChunk(
                relative,
                node.name,
                kind,
                start,
                end,
                "\n".join(lines[start - 1 : end]),
            )
        )
    return chunks


def index_code(
    root: Path | str, db_path: Path | str, *, embedder: Embedder | None = None
) -> dict[str, int]:
    base = Path(root).expanduser().resolve()
    database = Path(db_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(database.name + ".tmp")
    temporary.unlink(missing_ok=True)
    chunks: list[CodeChunk] = []
    for path in sorted(base.rglob("*.py")):
        if any(part in IGNORED for part in path.parts):
            continue
        chunks.extend(_chunks_for_file(base, path))
    tokenizer = "trigram"
    connection = sqlite3.connect(temporary)
    try:
        connection.execute(
            "CREATE TABLE chunks (id INTEGER PRIMARY KEY, path TEXT, symbol TEXT, "
            "kind TEXT, start_line INTEGER, end_line INTEGER, content TEXT, embedding TEXT)"
        )
        connection.execute(
            f"CREATE VIRTUAL TABLE code_fts USING fts5(path,symbol,content,tokenize='{tokenizer}')"
        )
        for chunk in chunks:
            cursor = connection.execute(
                "INSERT INTO chunks(path,symbol,kind,start_line,end_line,content) "
                "VALUES (?,?,?,?,?,?)",
                (
                    chunk.path,
                    chunk.symbol,
                    chunk.kind,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                ),
            )
            connection.execute(
                "INSERT INTO code_fts(rowid,path,symbol,content) VALUES (?,?,?,?)",
                (cursor.lastrowid, chunk.path, chunk.symbol, chunk.content),
            )
        embedded = 0
        if embedder is not None and chunks:
            vectors = embedder.embed([chunk.content for chunk in chunks], role="document")
            for row, vector in zip(chunks, vectors, strict=False):
                connection.execute(
                    "UPDATE chunks SET embedding=? WHERE path=? AND symbol=?",
                    (json.dumps(vector), row.path, row.symbol),
                )
                embedded += 1
        connection.commit()
    finally:
        connection.close()
    temporary.replace(database)
    return {
        "files": len({chunk.path for chunk in chunks}),
        "chunks": len(chunks),
        "embedded": embedded,
    }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    aa = math.sqrt(sum(x * x for x in a))
    bb = math.sqrt(sum(y * y for y in b))
    return dot / (aa * bb) if aa and bb else 0.0


class CodeIndex:
    def __init__(self, db_path: Path | str, *, embedder: Embedder | None = None) -> None:
        self.db_path = Path(db_path)
        self.embedder = embedder

    def _rows(self) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute("SELECT * FROM chunks ORDER BY id"))
        finally:
            connection.close()

    def _lexical(self, query: str, limit: int) -> list[int]:
        connection = sqlite3.connect(self.db_path)
        try:
            terms = [term for term in re.findall(r"[\w.]+", query) if len(term) > 1]
            if not terms:
                return []
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            rows = connection.execute(
                "SELECT rowid FROM code_fts WHERE code_fts MATCH ? ORDER BY bm25(code_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
            return [int(row[0]) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def _semantic(self, query: str, limit: int) -> list[int]:
        if self.embedder is None or not self.db_path.exists():
            return []
        rows = self._rows()
        if not rows:
            return []
        query_vector = self.embedder.embed([query], role="query")[0]
        scored = []
        for row in rows:
            if not row["embedding"]:
                continue
            try:
                vector = json.loads(row["embedding"])
            except ValueError:
                continue
            scored.append((_cosine(query_vector, vector), int(row["id"])))
        return [identifier for _, identifier in sorted(scored, reverse=True)[:limit]]

    def _structure(self, query: str, limit: int) -> list[int]:
        rows = self._rows()
        definition = _SYMBOL_DEF.search(query)
        call = _CALL.search(query)
        if definition:
            name = next(group for group in definition.groups() if group)
            return [
                int(row["id"])
                for row in rows
                if row["symbol"] == name
                or re.search(rf"\b(def|class)\s+{re.escape(name)}\b", row["content"])
            ][:limit]
        if call:
            name = call.group(1)
            return [
                int(row["id"])
                for row in rows
                if re.search(rf"\b{re.escape(name)}\s*\(", row["content"])
            ][:limit]
        return []

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip() or not self.db_path.exists():
            return []
        lexical = self._lexical(query, max(limit, 30))
        structural = self._structure(query, max(limit, 30))
        semantic = self._semantic(query, max(limit, 30))
        scores: dict[int, float] = {}
        for route in (lexical, structural, semantic):
            for rank, identifier in enumerate(route, start=1):
                scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (60 + rank)
        rows = {int(row["id"]): row for row in self._rows()}
        output = []
        for identifier in sorted(
            scores,
            key=lambda item: (
                -scores[item],
                0 if rows.get(item) is not None and rows[item]["symbol"] else 1,
                item,
            ),
        )[:limit]:
            row = rows.get(identifier)
            if row is None:
                continue
            output.append(
                {
                    "path": row["path"],
                    "symbol": row["symbol"],
                    "kind": row["kind"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "score": round(scores[identifier], 6),
                    "content": row["content"][:2000],
                }
            )
        return output

    def repo_map(self, *, recent: int = 10) -> dict[str, Any]:
        rows = self._rows()
        paths = sorted({str(row["path"]) for row in rows})
        directories: dict[str, int] = {}
        for path in paths:
            top = path.split("/")[0] if "/" in path else "."
            directories[top] = directories.get(top, 0) + 1
        entries = [
            path
            for path in paths
            if path in {"pyproject.toml", "README.md"}
            or path.endswith("__main__.py")
            or path.endswith("cli.py")
        ]
        recent_files = []
        for row in rows:
            path = self.db_path.parent / row["path"]
            if path.exists():
                recent_files.append((path.stat().st_mtime, row["path"]))
        recent_files = [path for _, path in sorted(set(recent_files), reverse=True)[:recent]]
        return {
            "entries": entries,
            "directories": dict(sorted(directories.items())),
            "recent_files": recent_files,
            "files": len(paths),
        }


__all__ = ["CodeChunk", "CodeIndex", "index_code"]
