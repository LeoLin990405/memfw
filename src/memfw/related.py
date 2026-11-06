"""Rebuildable relatedness edges for notes and memories.

Edges are derived data. Markdown remains authoritative, and no edge can write a
memory or a note. Explicit links, shared metadata/entities, and lexical overlap
are implemented here; a semantic method can be added when both sources share a
verified embedding fingerprint.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.repository import Repository
from .entities import extract_entities, load_dictionary

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}")
_STOP = {"the", "and", "for", "with", "from", "this", "that", "memory", "note"}


@dataclass(frozen=True)
class RelatedEdge:
    src_source: str
    src_id: str
    dst_source: str
    dst_id: str
    method: str
    score: float
    evidence: dict[str, Any]


@dataclass(frozen=True)
class RelatedItem:
    source: str
    id: str
    title: str
    path: str
    hash: str


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOP}


class RelatedIndex:
    """SQLite edge projection; rebuild is explicit and source-scoped."""

    def __init__(self, path: Path | str, *, dictionary: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.dictionary = tuple(dictionary)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS related_items (
                source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                path TEXT NOT NULL, hash TEXT NOT NULL,
                PRIMARY KEY (source, id)
            );
            CREATE TABLE IF NOT EXISTS related_edges (
                src_source TEXT NOT NULL, src_id TEXT NOT NULL,
                dst_source TEXT NOT NULL, dst_id TEXT NOT NULL,
                method TEXT NOT NULL, score REAL NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (src_source, src_id, dst_source, dst_id, method)
            );
            """
        )
        return connection

    def _write(
        self,
        connection: sqlite3.Connection,
        source: str,
        items: list[RelatedItem],
        candidates: dict[str, dict[tuple[str, str], tuple[float, dict[str, Any]]]],
    ) -> int:
        connection.execute("DELETE FROM related_edges WHERE src_source=?", (source,))
        connection.execute("DELETE FROM related_items WHERE source=?", (source,))
        for item in items:
            connection.execute(
                "INSERT INTO related_items(source,id,title,path,hash) VALUES (?,?,?,?,?)",
                (item.source, item.id, item.title, item.path, item.hash),
            )
        count = 0
        for src_id, neighbors in candidates.items():
            ranked = sorted(neighbors.items(), key=lambda row: (-row[1][0], row[0]))[:20]
            for (dst_source, dst_id), (score, evidence) in ranked:
                if src_id == dst_id and source == dst_source:
                    continue
                connection.execute(
                    "INSERT OR REPLACE INTO related_edges"
                    "(src_source,src_id,dst_source,dst_id,method,score,evidence) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        source,
                        src_id,
                        dst_source,
                        dst_id,
                        str(evidence.get("method", "mixed")),
                        score,
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    ),
                )
                count += 1
        connection.commit()
        return count

    def rebuild_notes(self, notes_db: Path | str) -> int:
        database = Path(notes_db)
        if not database.exists():
            raise ValueError("notes index does not exist")
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            notes = list(
                connection.execute("SELECT path,title,tags,course FROM notes ORDER BY path")
            )
            headings = defaultdict(list)
            for row in connection.execute("SELECT note_id,heading FROM sections"):
                headings[int(row[0])].append(str(row[1]))
            note_ids = {
                int(row[0]): str(row[1]) for row in connection.execute("SELECT id,path FROM notes")
            }
            links = defaultdict(set)
            for row in connection.execute("SELECT note_id,target FROM links"):
                links[int(row[0])].add(str(row[1]))
        by_path = {str(row["path"]): row for row in notes}
        tokens: dict[str, set[str]] = {}
        entities: dict[str, set[str]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for row in notes:
            path = str(row["path"])
            title = str(row["title"])
            tags = str(row["tags"] or "")
            course = str(row["course"] or "")
            note_id = next((key for key, value in note_ids.items() if value == path), None)
            heading_text = " ".join(headings.get(note_id or 0, []))
            text = " ".join([title, tags, course, heading_text])
            tokens[path] = _tokens(text)
            entities[path] = extract_entities(text, self.dictionary)
            metadata[path] = {"title": title, "tags": tags.split(), "course": course}
        # explicit wikilinks resolve to exact note paths when possible
        explicit: dict[str, set[str]] = defaultdict(set)
        for note_id, targets in links.items():
            source_path = note_ids.get(note_id)
            if source_path is None:
                continue
            for target in targets:
                if target in by_path:
                    explicit[source_path].add(target)
                elif target + ".md" in by_path:
                    explicit[source_path].add(target + ".md")
        candidates: dict[str, dict[tuple[str, str], tuple[float, dict[str, Any]]]] = {}
        inverted: dict[str, set[str]] = defaultdict(set)
        for path, values in entities.items():
            for value in values:
                inverted["e:" + value.lower()].add(path)
        for path, values in tokens.items():
            for value in values:
                inverted["t:" + value].add(path)
        for path, meta in metadata.items():
            for tag in meta["tags"]:
                inverted["tag:" + str(tag).lower()].add(path)
        for path in metadata:
            neighbors: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
            for target in explicit.get(path, set()):
                neighbors[("notes", target)] = (3.0, {"method": "explicit", "link": target})
            for value in entities.get(path, set()):
                for other in inverted.get("e:" + value.lower(), set()):
                    if other != path:
                        neighbors[("notes", other)] = (
                            max(neighbors.get(("notes", other), (0.0, {}))[0], 2.0),
                            {"method": "entity", "entity": value},
                        )
            for tag in metadata[path]["tags"]:
                for other in inverted.get("tag:" + str(tag).lower(), set()):
                    if other != path:
                        old = neighbors.get(("notes", other), (0.0, {}))[0]
                        neighbors[("notes", other)] = (
                            max(old, 0.8),
                            {"method": "tag", "tag": tag},
                        )
            for token in tokens.get(path, set()):
                for other in inverted.get("t:" + token, set()):
                    if other != path:
                        old, evidence = neighbors.get(("notes", other), (0.0, {}))
                        neighbors[("notes", other)] = (
                            old + 0.05,
                            {**evidence, "method": "lexical"},
                        )
            candidates[path] = neighbors
        items = [
            RelatedItem(
                "notes",
                path,
                str(meta["title"]),
                path,
                "",
            )
            for path, meta in metadata.items()
        ]
        connection = self._connect()
        try:
            return self._write(connection, "notes", items, candidates)
        finally:
            connection.close()

    def rebuild_memory(
        self,
        repository: Repository,
        *,
        entity_dictionary: Path | str | None = None,
    ) -> int:
        dictionary = load_dictionary(entity_dictionary) or self.dictionary
        items: list[RelatedItem] = []
        candidates: dict[str, dict[tuple[str, str], tuple[float, dict[str, Any]]]] = {}
        inverted: dict[str, set[str]] = defaultdict(set)
        tokens: dict[str, set[str]] = {}
        entities: dict[str, set[str]] = {}
        docs = [doc for doc in repository.list() if doc.valid_to is None]
        for doc in docs:
            text = " ".join([doc.title, doc.body, " ".join(doc.tags)])
            tokens[doc.id] = _tokens(text)
            entities[doc.id] = extract_entities(text, dictionary)
            items.append(
                RelatedItem(
                    "memory",
                    doc.id,
                    doc.title,
                    f"memories/{doc.id}.md",
                    str(doc.updated_at),
                )
            )
            for entity in entities[doc.id]:
                inverted["e:" + entity.lower()].add(doc.id)
            for token in tokens[doc.id]:
                inverted["t:" + token].add(doc.id)
            for related in doc.related:
                inverted["r:" + related].add(doc.id)
        for doc in docs:
            neighbors: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
            for related in doc.related:
                if related in {item.id for item in items}:
                    neighbors[("memory", related)] = (3.0, {"method": "explicit"})
            for entity in entities[doc.id]:
                for other in inverted.get("e:" + entity.lower(), set()):
                    if other != doc.id:
                        old, evidence = neighbors.get(("memory", other), (0.0, {}))
                        neighbors[("memory", other)] = (
                            max(old, 2.0),
                            {"method": "entity", "entity": entity, **evidence},
                        )
            for token in tokens[doc.id]:
                for other in inverted.get("t:" + token, set()):
                    if other != doc.id:
                        old, evidence = neighbors.get(("memory", other), (0.0, {}))
                        neighbors[("memory", other)] = (
                            old + 0.05,
                            {**evidence, "method": "lexical"},
                        )
            candidates[doc.id] = neighbors
        connection = self._connect()
        try:
            return self._write(connection, "memory", items, candidates)
        finally:
            connection.close()

    def related(
        self,
        source: str,
        identifier: str,
        *,
        limit: int = 10,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        del include_history
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT e.*, i.title, i.path FROM related_edges e "
                "LEFT JOIN related_items i ON i.source=e.dst_source AND i.id=e.dst_id "
                "WHERE e.src_source=? AND e.src_id=? "
                "ORDER BY e.score DESC, e.dst_id LIMIT ?",
                (source, identifier, max(0, limit)),
            ).fetchall()
        return [
            {
                "source": row["dst_source"],
                "id": row["dst_id"],
                "title": row["title"] or "",
                "path": row["path"] or "",
                "method": row["method"],
                "score": row["score"],
                "evidence": json.loads(row["evidence"]),
            }
            for row in rows
        ]


__all__ = ["RelatedEdge", "RelatedIndex", "RelatedItem"]
