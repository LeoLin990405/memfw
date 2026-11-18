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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.repository import Repository
from .entities import extract_entities, load_dictionary
from .notes.index import EmbeddingProvider

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
    text: str = ""
    tenant: str = ""
    scope: str = ""


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if token.lower() not in _STOP}


_Pair = tuple[str, str]
_MethodMap = dict[str, tuple[float, dict[str, Any]]]


def _bucket(neighbors: dict[_Pair, _MethodMap], key: _Pair) -> _MethodMap:
    return neighbors.setdefault(key, {})


def _set_max(bucket: _MethodMap, method: str, score: float, evidence: dict[str, Any]) -> None:
    previous = bucket.get(method, (0.0, {}))[0]
    bucket[method] = (max(previous, score), evidence)


def _add_score(bucket: _MethodMap, method: str, delta: float, evidence: dict[str, Any]) -> None:
    previous = bucket.get(method, (0.0, {}))[0]
    bucket[method] = (previous + delta, evidence)


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
                path TEXT NOT NULL, hash TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                tenant TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
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
        candidates: dict[str, dict[_Pair, _MethodMap]],
    ) -> int:
        connection.execute("DELETE FROM related_edges WHERE src_source=?", (source,))
        connection.execute("DELETE FROM related_items WHERE source=?", (source,))
        for item in items:
            connection.execute(
                "INSERT INTO related_items(source,id,title,path,hash,text,tenant,scope) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    item.source,
                    item.id,
                    item.title,
                    item.path,
                    item.hash,
                    item.text,
                    item.tenant,
                    item.scope,
                ),
            )
        # Cap inside each destination tenant. A global top-20 lets another
        # tenant's earlier ids crowd out the caller's own neighbour.
        tenant_of = {(item.source, item.id): item.tenant for item in items}
        count = 0
        for src_id, neighbors in candidates.items():
            grouped: dict[str, list[tuple[_Pair, _MethodMap]]] = defaultdict(list)
            for pair, methods in neighbors.items():
                grouped[tenant_of.get(pair, "")].append((pair, methods))
            ranked: list[tuple[_Pair, _MethodMap]] = []
            for group in grouped.values():
                ranked.extend(
                    sorted(
                        group,
                        key=lambda row: (
                            -max((score for score, _evidence in row[1].values()), default=0.0),
                            row[0],
                        ),
                    )[:20]
                )
            for (dst_source, dst_id), methods in ranked:
                if src_id == dst_id and source == dst_source:
                    continue
                # One row per method. Scores stay raw; HTTP /related applies x/(1+x).
                for method in sorted(methods):
                    score, evidence = methods[method]
                    payload = {**evidence, "method": method}
                    connection.execute(
                        "INSERT OR REPLACE INTO related_edges"
                        "(src_source,src_id,dst_source,dst_id,method,score,evidence) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            source,
                            src_id,
                            dst_source,
                            dst_id,
                            method,
                            score,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
            metadata[path] = {
                "title": title,
                "tags": tags.split(),
                "course": course,
                "headings": heading_text,
            }
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
        candidates: dict[str, dict[_Pair, _MethodMap]] = {}
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
            neighbors: dict[_Pair, _MethodMap] = {}
            for target in explicit.get(path, set()):
                _set_max(
                    _bucket(neighbors, ("notes", target)),
                    "explicit",
                    3.0,
                    {"method": "explicit", "link": target},
                )
            for value in entities.get(path, set()):
                for other in inverted.get("e:" + value.lower(), set()):
                    if other != path:
                        _set_max(
                            _bucket(neighbors, ("notes", other)),
                            "entity",
                            2.0,
                            {"method": "entity", "entity": value},
                        )
            for tag in metadata[path]["tags"]:
                for other in inverted.get("tag:" + str(tag).lower(), set()):
                    if other != path:
                        _set_max(
                            _bucket(neighbors, ("notes", other)),
                            "tag",
                            0.8,
                            {"method": "tag", "tag": tag},
                        )
            for token in tokens.get(path, set()):
                for other in inverted.get("t:" + token, set()):
                    if other != path:
                        _add_score(
                            _bucket(neighbors, ("notes", other)),
                            "lexical",
                            0.05,
                            {"method": "lexical"},
                        )
            candidates[path] = neighbors
        items = [
            RelatedItem(
                "notes",
                path,
                str(meta["title"]),
                path,
                "",
                " ".join([str(meta["title"]), str(meta["course"]), str(meta.get("headings", ""))]),
                "",
                str(meta["course"]),
            )
            for path, meta in metadata.items()
        ]
        connection = self._connect()
        try:
            return self._write(connection, "notes", items, candidates)
        finally:
            connection.close()

    def rebuild_sections(
        self,
        notes_db: Path | str,
        *,
        max_sections: int = 2000,
        tenant: str = "",
        scope: str | None = None,
    ) -> int:
        """Build section-level edges for a bounded notes subset.

        Section edges are intentionally bounded: a full 169k-section all-pairs
        graph is not a useful default. Operators can raise ``max_sections`` or
        build per collection.
        """
        database = Path(notes_db)
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT s.id AS section_id, s.heading, s.content, n.path, n.title, "
                "n.tags, n.course FROM sections s JOIN notes n ON n.id=s.note_id "
                "ORDER BY s.id LIMIT ?",
                (max(1, max_sections),),
            ).fetchall()
        items: list[RelatedItem] = []
        tokens: dict[str, set[str]] = {}
        entities: dict[str, set[str]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for row in rows:
            path = str(row["path"])
            identifier = f"{path}#s{int(row['section_id'])}"
            text = " ".join([str(row["title"]), str(row["heading"]), str(row["content"])])
            if tenant:
                continue
            if scope is not None and str(row["course"] or "") != scope:
                continue
            tokens[identifier] = _tokens(text)
            entities[identifier] = extract_entities(text, self.dictionary)
            metadata[identifier] = {
                "title": str(row["title"]),
                "path": path,
                "scope": str(row["course"] or ""),
            }
            items.append(
                RelatedItem(
                    "notes_sections",
                    identifier,
                    str(row["title"]),
                    path,
                    "",
                    text[:4000],
                    tenant,
                    str(row["course"] or ""),
                )
            )
        inverted: dict[str, set[str]] = defaultdict(set)
        for identifier, values in entities.items():
            for value in values:
                inverted["e:" + value.lower()].add(identifier)
        for identifier, values in tokens.items():
            for value in values:
                inverted["t:" + value].add(identifier)
        candidates: dict[str, dict[_Pair, _MethodMap]] = {}
        for identifier in metadata:
            neighbors: dict[_Pair, _MethodMap] = {}
            for entity in entities.get(identifier, set()):
                for other in inverted.get("e:" + entity.lower(), set()):
                    if other != identifier:
                        _set_max(
                            _bucket(neighbors, ("notes_sections", other)),
                            "entity",
                            2.0,
                            {"method": "entity", "entity": entity},
                        )
            for token in tokens.get(identifier, set()):
                for other in inverted.get("t:" + token, set()):
                    if other != identifier:
                        _add_score(
                            _bucket(neighbors, ("notes_sections", other)),
                            "lexical",
                            0.05,
                            {"method": "lexical"},
                        )
            candidates[identifier] = neighbors
        connection = self._connect()
        try:
            return self._write(connection, "notes_sections", items, candidates)
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
        candidates: dict[str, dict[_Pair, _MethodMap]] = {}
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
                    " ".join([doc.title, doc.body, " ".join(doc.tags)]),
                    doc.tenant,
                    doc.source,
                )
            )
            for entity in entities[doc.id]:
                inverted["e:" + entity.lower()].add(doc.id)
            for token in tokens[doc.id]:
                inverted["t:" + token].add(doc.id)
            for tag in doc.tags:
                inverted["tag:" + str(tag).lower()].add(doc.id)
            for related in doc.related:
                inverted["r:" + related].add(doc.id)
        known_ids = {item.id for item in items}
        for doc in docs:
            neighbors: dict[_Pair, _MethodMap] = {}
            for related in doc.related:
                if related in known_ids:
                    _set_max(
                        _bucket(neighbors, ("memory", related)),
                        "explicit",
                        3.0,
                        {"method": "explicit"},
                    )
            for tag in doc.tags:
                for other in inverted.get("tag:" + str(tag).lower(), set()):
                    if other != doc.id:
                        _set_max(
                            _bucket(neighbors, ("memory", other)),
                            "tag",
                            0.8,
                            {"method": "tag", "tag": tag},
                        )
            for entity in entities[doc.id]:
                for other in inverted.get("e:" + entity.lower(), set()):
                    if other != doc.id:
                        _set_max(
                            _bucket(neighbors, ("memory", other)),
                            "entity",
                            2.0,
                            {"method": "entity", "entity": entity},
                        )
            for token in tokens[doc.id]:
                for other in inverted.get("t:" + token, set()):
                    if other != doc.id:
                        _add_score(
                            _bucket(neighbors, ("memory", other)),
                            "lexical",
                            0.05,
                            {"method": "lexical"},
                        )
            candidates[doc.id] = neighbors
        connection = self._connect()
        try:
            return self._write(connection, "memory", items, candidates)
        finally:
            connection.close()

    def build_semantic(
        self,
        embedder: EmbeddingProvider,
        *,
        limit: int = 10,
        admit: Callable[[str, str, str], bool] | None = None,
        allow_remote: bool | None = None,
    ) -> int:
        """Build semantic edges across all indexed sources with one model.

        The embedding fingerprint is recorded in each edge. This method is opt-in
        and never runs automatically. ``admit(source, id, path)``, when given, decides
        which items may be sent to the embedder (the locality gate of an
        off-host one, :func:`shareable_item`): the others get no vector and no
        semantic edge. Without ``admit``, an off-host embedder
        (``embedder_sends_off_host``) gets nothing at all unless
        ``allow_remote`` (``None``: ``MEMFW_EMBEDDING_ALLOW_REMOTE``) opts in:
        the caller must say which items are shareable.
        """
        if admit is None:
            from .notes.http import embedder_sends_off_host
            from .notes.index import remote_embedding_allowed

            allowed = remote_embedding_allowed() if allow_remote is None else allow_remote
            if not allowed and embedder_sends_off_host(embedder):
                admit = shareable_item()  # no source to read: nothing is shareable
        try:
            import sqlite_vec  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("sqlite_vec is required for semantic relatedness") from exc
        connection = self._connect()
        try:
            connection.execute("DELETE FROM related_edges WHERE method='semantic'")
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(related_items)")
            }
            if "text" not in columns:
                connection.execute(
                    "ALTER TABLE related_items ADD COLUMN text TEXT NOT NULL DEFAULT ''"
                )
            rows = connection.execute(
                "SELECT rowid,source,id,title,text,path FROM related_items ORDER BY rowid"
            ).fetchall()
            if admit is not None:
                rows = [
                    row
                    for row in rows
                    if admit(str(row["source"]), str(row["id"]), str(row["path"] or ""))
                ]
            if not rows:
                return 0
            connection.execute("DROP TABLE IF EXISTS related_vectors")
            dimension = int(getattr(embedder, "dimension", 0) or 0)
            if dimension <= 0:
                raise ValueError("embedder dimension must be positive")
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            connection.execute(
                f"CREATE VIRTUAL TABLE related_vectors USING vec0(embedding float[{dimension}])"
            )
            texts = [(str(row["title"]) + " " + str(row["text"]))[:4000] for row in rows]
            vectors = embedder.embed(texts, role="document")
            for row, vector in zip(rows, vectors, strict=False):
                connection.execute(
                    "INSERT INTO related_vectors(rowid,embedding) VALUES (?,?)",
                    (int(row["rowid"]), json.dumps(vector)),
                )
            fingerprint = str(
                getattr(embedder, "fingerprint", "") or getattr(embedder, "model", "")
            )
            count = 0
            for row, query_vector in zip(rows, vectors, strict=False):
                neighbors = connection.execute(
                    "SELECT rowid,distance FROM related_vectors WHERE embedding MATCH ? AND k = ?",
                    (json.dumps(query_vector), min(limit + 1, len(rows))),
                ).fetchall()
                for neighbor in neighbors:
                    if int(neighbor["rowid"]) == int(row["rowid"]):
                        continue
                    target = next(
                        item for item in rows if int(item["rowid"]) == int(neighbor["rowid"])
                    )
                    score = 1.0 / (1.0 + float(neighbor["distance"]))
                    connection.execute(
                        "INSERT OR REPLACE INTO related_edges"
                        "(src_source,src_id,dst_source,dst_id,method,score,evidence) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            str(row["source"]),
                            str(row["id"]),
                            str(target["source"]),
                            str(target["id"]),
                            "semantic",
                            score,
                            json.dumps(
                                {"method": "semantic", "model": fingerprint},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ),
                    )
                    count += 1
            connection.commit()
            return count
        finally:
            connection.close()

    def related(
        self,
        source: str,
        identifier: str,
        *,
        limit: int = 10,
        include_history: bool = False,
        tenant: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        del include_history
        if not self.path.exists():
            return []
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(related_items)")}
        if "tenant" not in columns:
            connection.execute(
                "ALTER TABLE related_items ADD COLUMN tenant TEXT NOT NULL DEFAULT ''"
            )
        if "scope" not in columns:
            connection.execute(
                "ALTER TABLE related_items ADD COLUMN scope TEXT NOT NULL DEFAULT ''"
            )
        if tenant is not None:
            subject = connection.execute(
                "SELECT tenant FROM related_items WHERE source=? AND id=?",
                (source, identifier),
            ).fetchone()
            if subject is None or str(subject["tenant"]) != tenant:
                connection.close()
                return []
        filters = []
        params: list[Any] = []
        if tenant is not None:
            filters.append("i.tenant=?")
            params.append(tenant)
        if scope is not None:
            filters.append("i.scope=?")
            params.append(scope)
        where = (" AND " + " AND ".join(filters)) if filters else ""
        outgoing = connection.execute(
            "SELECT e.*, i.title, i.path FROM related_edges e "
            "LEFT JOIN related_items i ON i.source=e.dst_source AND i.id=e.dst_id "
            "WHERE e.src_source=? AND e.src_id=?" + where,
            [source, identifier, *params],
        ).fetchall()
        incoming = connection.execute(
            "SELECT e.*, i.title, i.path FROM related_edges e "
            "LEFT JOIN related_items i ON i.source=e.src_source AND i.id=e.src_id "
            "WHERE e.dst_source=? AND e.dst_id=?" + where,
            [source, identifier, *params],
        ).fetchall()
        connection.close()
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for row, direction in [(row, "outgoing") for row in outgoing] + [
            (row, "incoming") for row in incoming
        ]:
            other_source = row["dst_source"] if direction == "outgoing" else row["src_source"]
            other_id = row["dst_id"] if direction == "outgoing" else row["src_id"]
            key = (str(other_source), str(other_id))
            item = merged.get(key)
            if item is None:
                item = {
                    "source": str(other_source),
                    "id": str(other_id),
                    "title": row["title"] or "",
                    "path": row["path"] or "",
                    "methods": [],
                    "score": 0.0,
                    "evidence": [],
                }
                merged[key] = item
            item["score"] = max(float(item["score"]), float(row["score"]))
            method_name = str(row["method"])
            if method_name not in item["methods"]:
                item["methods"].append(method_name)
            item["evidence"].append(
                {"direction": direction, "method": row["method"], **json.loads(row["evidence"])}
            )
        return sorted(
            merged.values(), key=lambda item: (-item["score"], item["source"], item["id"])
        )[: max(0, limit)]


__all__ = ["RelatedEdge", "RelatedIndex", "RelatedItem"]


def shareable_item(
    *, repository: Repository | None = None, notes_db: Path | str | None = None
) -> Callable[[str, str, str], bool]:
    """``admit(source, id, path)`` for ``build_semantic`` under the locality gate.

    A ``memory`` item is shareable when ``repository`` holds it and its
    effective level (the vault's ``legacy_privacy_default`` for a memory
    without ``privacy``) is ``public`` or ``shared``; a ``notes`` or ``notes_sections``
    item when ``notes_db`` lists its path and its indexed frontmatter reads ``public`` or
    ``shared`` (a note without ``privacy`` is ``local-private``). Anything
    else, or an item that cannot be read, is not.
    """

    from .ports.privacy import LEGACY_PRIVACY_FALLBACK, effective_level, metadata_level

    legacy = LEGACY_PRIVACY_FALLBACK
    if repository is not None:
        try:
            legacy = repository.legacy_privacy_default()
        except Exception:  # noqa: BLE001 - unreadable config: the strict default
            legacy = LEGACY_PRIVACY_FALLBACK
    notes: dict[str, str] = {}
    if notes_db is not None and Path(notes_db).is_file():
        from .notes.index import FRONTMATTER_SPLIT_VERSION

        with sqlite3.connect(notes_db) as connection:
            try:
                version = connection.execute(
                    "SELECT value FROM index_versions WHERE key='frontmatter_split'"
                ).fetchone()
            except sqlite3.Error:
                version = None
            # Frontmatter stored by an older split cannot be trusted: no note is
            # shareable until ``memfw notes index`` has re-read the tree.
            rows = (
                connection.execute("SELECT path, frontmatter FROM notes").fetchall()
                if version is not None and str(version[0]) == FRONTMATTER_SPLIT_VERSION
                else []
            )
            for path, frontmatter in rows:
                try:
                    loaded = json.loads(frontmatter or "{}")
                    level = metadata_level(
                        loaded if isinstance(loaded, dict) else {"privacy": "unknown"},
                        LEGACY_PRIVACY_FALLBACK,
                    )
                except Exception:  # noqa: BLE001 - unreadable: not shared
                    level = "unknown"
                notes[str(path)] = level

    def admit(source: str, item_id: str, path: str) -> bool:
        if source == "memory" and repository is not None:
            try:
                document = repository.get(item_id)
            except Exception:  # noqa: BLE001 - unreadable: never sent
                return False
            level = effective_level(
                document.privacy,
                document.tags,
                legacy=legacy,
                sensitivity=document.top_level_sensitivity,
            )
            return level in ("public", "shared")
        if source in ("notes", "notes_sections"):
            return notes.get(path or item_id) in ("public", "shared")
        return False

    return admit
