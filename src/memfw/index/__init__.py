"""Disposable SQLite FTS5 projection over current Markdown memories."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tarfile
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from ..core.errors import IntegrityError
from ..core.models import MemoryDocument
from ..core.repository import Repository

_ROW_COLUMNS = (
    "id",
    "title",
    "body",
    "layer",
    "tier",
    "source",
    "provenance",
    "anchor",
    "tags",
    "version",
    "summary",
    "is_stub",
    "archive_ref",
    "archived_hash",
    "created_at",
    "updated_at",
)
_SEARCH_COLUMNS = ("title", "body", "summary", "tags")
_TOKENIZER = Literal["trigram", "unicode61"]


def _supports_trigram(connect: Callable[..., Any] | None = None) -> bool:
    """Probe SQLite FTS5 trigram support without touching a vault or index file."""

    connector = connect or sqlite3.connect
    connection = connector(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE trigram_probe USING fts5(value, tokenize='trigram')"
        )
        connection.execute("DROP TABLE trigram_probe")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def _jieba() -> Callable[[str], list[str]]:
    try:
        import jieba  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "SQLite FTS5 trigram is unavailable and jieba is required for the unicode61 fallback; "
            "install the declared jieba dependency"
        ) from exc
    return lambda text: [token for token in jieba.lcut(text, cut_all=False) if token.strip()]


def _tokenize(text: str, tokenizer: _TOKENIZER, segment: Callable[[str], list[str]] | None) -> str:
    if tokenizer == "trigram":
        return text
    if segment is None:
        raise RuntimeError("unicode61 fallback requires jieba tokenization")
    return " ".join(segment(text))


def _row(document: MemoryDocument) -> tuple[object, ...]:
    return (
        document.id,
        document.title,
        document.body,
        document.layer.value,
        document.tier.value,
        document.source,
        document.provenance,
        document.anchor or "",
        " ".join(document.tags),
        document.version,
        document.summary,
        int(document.is_stub),
        document.archive_ref or "",
        document.archived_hash or "",
        document.created_at.isoformat(),
        document.updated_at.isoformat(),
    )


def _create_schema(connection: sqlite3.Connection, tokenizer: _TOKENIZER) -> None:
    connection.execute(
        "CREATE TABLE memory_rows ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, "
        "search_body TEXT NOT NULL DEFAULT '', layer TEXT NOT NULL, "
        "tier TEXT NOT NULL, source TEXT NOT NULL, provenance TEXT NOT NULL, anchor TEXT NOT NULL, "
        "tags TEXT NOT NULL, version INTEGER NOT NULL, summary TEXT NOT NULL, "
        "is_stub INTEGER NOT NULL, "
        "archive_ref TEXT NOT NULL, archived_hash TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "id UNINDEXED, title, body, summary, tags, tokenize='" + tokenizer + "')"
    )


def _insert_documents(
    connection: sqlite3.Connection,
    documents: Iterable[MemoryDocument],
    tokenizer: _TOKENIZER,
    segment: Callable[[str], list[str]] | None,
    searchable_bodies: dict[str, str] | None = None,
) -> int:
    row_placeholders = ",".join("?" for _ in _ROW_COLUMNS)
    row_sql = f"INSERT INTO memory_rows ({','.join(_ROW_COLUMNS)}) VALUES ({row_placeholders})"
    fts_sql = "INSERT INTO memory_fts (id,title,body,summary,tags) VALUES (?,?,?,?,?)"
    count = 0
    for document in sorted(documents, key=lambda item: item.id):
        effective_body = (
            searchable_bodies.get(document.id, document.body)
            if searchable_bodies is not None
            else document.body
            if not document.is_stub
            else document.summary
        )
        row_values = list(_row(document))
        connection.execute(row_sql, tuple(row_values))
        # Preserve the public raw row contract (cold stubs have an empty body)
        # while keeping a separately verified search projection for retrieval.
        connection.execute(
            "UPDATE memory_rows SET search_body=? WHERE id=?",
            (effective_body, document.id),
        )
        fts_values = (
            document.id,
            _tokenize(document.title, tokenizer, segment),
            _tokenize(effective_body, tokenizer, segment),
            _tokenize(document.summary, tokenizer, segment),
            _tokenize(" ".join(document.tags), tokenizer, segment),
        )
        connection.execute(fts_sql, fts_values)
        count += 1
    return count


class MemoryIndex:
    """Search projection. All writes happen in ``rebuild``; ``search`` is read-only."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def rebuild(self, repository: Repository) -> int:
        """Build a complete replacement under the Repository's shared file lock."""

        temporary = self.path.with_name(self.path.name + ".tmp")

        # Share the vault lock with Repository mutations. list() is read-only and the
        # reentrant lock permits rebuild to call it while holding the same file lock.
        with repository._locked():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            documents = repository.list()
            searchable_bodies = {
                document.id: _read_archived_body(repository, document)
                for document in documents
                if document.is_stub
            }
            tokenizer: _TOKENIZER = "trigram" if _supports_trigram() else "unicode61"
            segment = None if tokenizer == "trigram" else _jieba()
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(temporary)
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                _create_schema(connection, tokenizer)
                count = _insert_documents(
                    connection, documents, tokenizer, segment, searchable_bodies
                )
                connection.commit()
                connection.close()
                connection = None

                # The old index remains readable until the complete new database is
                # durable. The temporary file is a sibling, so os.replace is atomic.
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                try:
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    # Some filesystems do not support syncing directory handles.
                    pass
                return count
            except BaseException:
                if connection is not None:
                    connection.close()
                temporary.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm"):
                    Path(f"{temporary}{suffix}").unlink(missing_ok=True)
                raise

    def remove(self, memory_id: str) -> None:
        """Drop the disposable database after a hard delete."""

        del memory_id  # A full rebuild/drop avoids residual deleted FTS pages.
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def rows(self) -> list[tuple[object, ...]]:
        """Return complete raw metadata/content rows ordered by stable memory id."""

        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path)) as connection:
            return list(
                connection.execute(
                    "SELECT " + ",".join(_ROW_COLUMNS) + " FROM memory_rows ORDER BY id"
                )
            )

    def search(self, query: str, limit: int = 20) -> list[tuple[str, str]]:
        """Return (id, title) matches without writing or warming any memory."""

        needle = query.strip()
        if not self.path.exists() or limit <= 0 or len(needle) < 2:
            return []
        with closing(sqlite3.connect(self.path)) as connection:
            tokenizer = self._tokenizer(connection)
            if len(needle) == 2:
                # Trigram cannot match fewer than three code points. Preserve substring
                # lookup across every user-visible searchable field.
                rows = connection.execute(
                    "SELECT id,title FROM memory_rows "
                    "WHERE instr(title, ?) > 0 OR instr(search_body, ?) > 0 "
                    "OR instr(summary, ?) > 0 OR instr(tags, ?) > 0 "
                    "ORDER BY id LIMIT ?",
                    (needle, needle, needle, needle, limit),
                )
                return [(str(row[0]), str(row[1])) for row in rows]

            if tokenizer == "trigram":
                match = '"' + needle.replace('"', '""') + '"'
            else:
                segment = _jieba()
                terms = segment(needle)
                if not terms:
                    return []
                match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
            rows = connection.execute(
                "SELECT r.id,r.title FROM memory_fts AS f "
                "JOIN memory_rows AS r ON r.id=f.id "
                "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts),r.id LIMIT ?",
                (match, limit),
            )
            return [(str(row[0]), str(row[1])) for row in rows]

    def search_many(
        self, queries: Iterable[str], *, limit: int = 100_000
    ) -> dict[str, list[tuple[str, str]]]:
        """Search many terms on one read-only connection, preserving FTS behavior."""
        if not self.path.exists() or limit <= 0:
            return {query: [] for query in dict.fromkeys(queries)}
        unique_queries = tuple(dict.fromkeys(queries))
        output: dict[str, list[tuple[str, str]]] = {}
        with closing(sqlite3.connect(self.path)) as connection:
            tokenizer = self._tokenizer(connection)
            segment = _jieba() if tokenizer != "trigram" else None
            for query in unique_queries:
                needle = query.strip()
                if len(needle) < 2:
                    output[query] = []
                    continue
                if len(needle) == 2:
                    rows = connection.execute(
                        "SELECT id,title FROM memory_rows "
                        "WHERE instr(title, ?) > 0 OR instr(search_body, ?) > 0 "
                        "OR instr(summary, ?) > 0 OR instr(tags, ?) > 0 "
                        "ORDER BY id LIMIT ?",
                        (needle, needle, needle, needle, limit),
                    )
                else:
                    if tokenizer == "trigram":
                        match = '"' + needle.replace('"', '""') + '"'
                    else:
                        assert segment is not None
                        terms = segment(needle)
                        if not terms:
                            output[query] = []
                            continue
                        match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                    rows = connection.execute(
                        "SELECT r.id,r.title FROM memory_fts AS f "
                        "JOIN memory_rows AS r ON r.id=f.id "
                        "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts),r.id LIMIT ?",
                        (match, limit),
                    )
                output[query] = [(str(row[0]), str(row[1])) for row in rows]
        return output

    def ranked_search(
        self,
        query: str,
        limit: int = 20,
        *,
        ranking: str = "bm25",
        candidate_ids: Iterable[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Rank indexed memories with shared BM25 or legacy per-term hit counts."""
        if limit <= 0 or not self.path.exists():
            return []
        if ranking not in {"bm25", "count"}:
            raise ValueError("ranking must be 'bm25' or 'count'")
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT id,title,search_body,summary,tags FROM memory_rows ORDER BY id"
            ).fetchall()
        allowed = None if candidate_ids is None else set(candidate_ids)
        retained = [row for row in rows if allowed is None or str(row[0]) in allowed]
        if ranking == "bm25":
            from ..stats.bm25 import BM25Scorer

            documents = {
                str(identifier): " ".join((str(title), str(body), str(summary), str(tags)))
                for identifier, title, body, summary, tags in retained
            }
            ranked = BM25Scorer(documents).rank(query)
        else:
            from ..stats.keyness import tokenize

            terms = tuple(dict.fromkeys(tokenize(query)))
            hits = self.search_many(terms, limit=max(100_000, len(retained)))
            allowed_ids = {str(row[0]) for row in retained}
            counts: Counter[str] = Counter(
                identifier
                for term in terms
                for identifier, _title in hits.get(term, [])
                if identifier in allowed_ids
            )
            ranked = [
                identifier
                for identifier, _count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0])
                )
            ]
        titles = {str(row[0]): str(row[1]) for row in retained}
        return [(identifier, titles[identifier]) for identifier in ranked[:limit]]

    @staticmethod
    def _tokenizer(connection: sqlite3.Connection) -> _TOKENIZER:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        if row is None or row[0] is None:
            raise sqlite3.DatabaseError("memory_fts schema is missing")
        return "trigram" if "trigram" in str(row[0]).lower() else "unicode61"


def _read_archived_body(repository: Repository, document: MemoryDocument) -> str:
    """Read and verify the original body for a cold stub without warming it."""
    if not document.archive_ref or not document.archived_hash:
        raise IntegrityError(f"Cold stub {document.id} has no verified archive reference")
    if Path(document.archive_ref).name != document.archive_ref:
        raise IntegrityError(f"Cold stub {document.id} has an unsafe archive reference")
    archive_path = repository.archives_dir / f"{document.archive_ref}.tar.zst"
    try:
        import zstandard

        raw_tar = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
            member = archive.extractfile(f"memories/{document.id}.md")
            if member is None:
                raise IntegrityError(f"Cold archive is missing source {document.id}")
            raw_markdown = member.read()
        if hashlib.sha256(raw_markdown).hexdigest() != document.archived_hash:
            raise IntegrityError(f"Cold archive hash mismatch for {document.id}")
        archived = repository.parse(raw_markdown.decode("utf-8"), expected_id=document.id)
        if archived.is_stub or archived.id != document.id:
            raise IntegrityError(f"Cold archive contains an invalid source {document.id}")
        return archived.body
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError(f"Unable to read cold archive for {document.id}: {exc}") from exc
