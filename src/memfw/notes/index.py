"""A disposable, section-level index over a Markdown notes tree.

This module deliberately keeps the derived index independent of any one
search engine:

* SQLite FTS5 provides lexical recall (trigram when available, unicode61
  with jieba fallback otherwise).
* sqlite-vec provides vector recall when the extension is installed.
* A provider-neutral embedding interface lets a deployment use a local
  OpenAI-compatible server, a cloud endpoint, or a deterministic test double.
* If embeddings are unavailable, search degrades to FTS instead of failing.

The index is derived data: it never writes the Markdown tree.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import math
import re
import sqlite3
import time
import urllib.request
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from ..index.fts_terms import MAX_TERMS, query_terms, short_terms_clause, split_short_terms
from . import http as notes_http

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MAX_SECTION_CHARS = 4000
SECTION_OVERLAP = 200
EMBED_MAX_CHARS = 8000
BACKFILL_BATCH = 64
FTS_K = 60
FTS_TOKENIZERS = ("trigram", "unicode61")
_TOKENIZE_RE = re.compile(r"tokenize\s*=\s*['\"]?(\w+)")


class NoteParseError(RuntimeError):
    """A Markdown note could not be parsed."""


class EmbeddingProvider(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        """Return one vector per input text; role selects query/document policy."""


class HashEmbeddingProvider:
    """Deterministic local embedding double for tests and offline fallback."""

    def __init__(self, dimension: int = 32) -> None:
        self.dimension = dimension

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        del role
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            values = [
                ((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(self.dimension)
            ]
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append([value / norm for value in values])
        return vectors


class HttpEmbeddingProvider:
    """OpenAI-compatible ``/v1/embeddings`` client with batching and retries."""

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        dimension: int,
        api_key: str | None = None,
        batch_size: int = 16,
        timeout: float = 120.0,
        retries: int = 3,
        query_instruction: str = "",
        document_instruction: str = "",
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.dimension = dimension
        self.api_key = api_key
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.query_instruction = query_instruction
        self.document_instruction = document_instruction

    @property
    def fingerprint(self) -> str:
        marker = (
            f"{self.model}|{self.dimension}|{self.query_instruction}|{self.document_instruction}"
        )
        return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        instruction = self.query_instruction if role == "query" else self.document_instruction
        prepared = [instruction + text if instruction else text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prepared), self.batch_size):
            batch = list(prepared[start : start + self.batch_size])
            payload = json.dumps({"model": self.model, "input": batch}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            last: Exception | None = None
            for attempt in range(self.retries + 1):
                try:
                    request = urllib.request.Request(self.endpoint, data=payload, headers=headers)
                    with notes_http.open_http(request, timeout=self.timeout) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    rows = body.get("data") if isinstance(body, dict) else None
                    if not isinstance(rows, list) or len(rows) != len(batch):
                        raise RuntimeError("embedding endpoint returned an unexpected shape")
                    ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
                    for item in ordered:
                        vector = item.get("embedding")
                        if not isinstance(vector, list) or len(vector) != self.dimension:
                            raise RuntimeError("embedding dimension mismatch")
                        vectors.append([float(value) for value in vector])
                    break
                except Exception as exc:  # noqa: BLE001 - retry boundary
                    last = exc
                    if attempt < self.retries:
                        time.sleep(min(10.0, 0.5 * 2**attempt))
            else:
                raise RuntimeError(f"embedding request failed: {last}") from last
        return vectors


@dataclass(frozen=True)
class ParsedSection:
    heading: str
    level: int
    ordinal: int
    content: str
    start_line: int
    end_line: int
    hash: str


@dataclass(frozen=True)
class ParsedNote:
    path: str
    title: str
    content_hash: str
    mtime: float
    frontmatter: dict[str, Any]
    tags: list[str]
    links: list[str]
    course: str
    sections: list[ParsedSection] = field(default_factory=list)


@dataclass(frozen=True)
class NoteHit:
    path: str
    title: str
    heading: str
    content: str
    score: float
    rank: int
    source: str
    tags: list[str] = field(default_factory=list)
    section_id: int | None = None
    content_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "title": self.title,
            "heading": self.heading,
            "content": self.content,
            "score": self.score,
            "rank": self.rank,
            "source": self.source,
            "tags": self.tags,
        }
        if self.section_id is not None:
            payload["section_id"] = self.section_id
        if self.content_hash:
            payload["content_hash"] = self.content_hash
        return payload


def _embed(provider: EmbeddingProvider, texts: Sequence[str], *, role: str) -> list[list[float]]:
    """Call role-aware providers while tolerating older embed(texts) implementations."""
    method = provider.embed
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return method(texts)
    if "role" in parameters:
        return method(texts, role=role)
    return method(texts)


def _sha256(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    if not text.startswith("---"):
        return {}, text, 0
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text, 0
    raw = "".join(lines[1:end])
    try:
        loaded = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise NoteParseError(f"invalid frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise NoteParseError("frontmatter must be a mapping")
    return dict(loaded), "".join(lines[end + 1 :]), end + 1


def _tags(frontmatter: dict[str, Any]) -> list[str]:
    raw = frontmatter.get("tags", frontmatter.get("tag", []))
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _chunks(content: str, heading: str, ordinal: int, start_line: int) -> list[ParsedSection]:
    text = content.strip("\n")
    if not text.strip():
        return []
    pieces: list[str] = []
    if len(text) <= MAX_SECTION_CHARS:
        pieces = [text]
    else:
        offset = 0
        while offset < len(text):
            end = min(len(text), offset + MAX_SECTION_CHARS)
            if end < len(text):
                newline = text.rfind("\n", offset + MAX_SECTION_CHARS // 2, end)
                if newline > offset:
                    end = newline
            piece = text[offset:end].strip()
            if piece:
                pieces.append(piece)
            if end >= len(text):
                break
            offset = max(offset + 1, end - SECTION_OVERLAP)
    return [
        ParsedSection(
            heading=heading,
            level=0,
            ordinal=ordinal + index,
            content=piece,
            start_line=start_line,
            end_line=start_line + piece.count("\n"),
            hash=_sha256(heading + "\n" + piece),
        )
        for index, piece in enumerate(pieces)
    ]


def parse_markdown(path: Path, root: Path) -> ParsedNote:
    """Parse one Markdown note into heading sections without writing it."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise NoteParseError(str(exc)) from exc
    relative = path.relative_to(root).as_posix()
    frontmatter, body, offset = _split_frontmatter(text)
    title = str(frontmatter.get("title") or "").strip()
    if not title:
        match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
        title = match.group(1).strip() if match else path.stem
    course = relative.split("/", 1)[0] if "/" in relative else ""
    lines = body.splitlines(keepends=True)
    sections: list[ParsedSection] = []
    current_heading = ""
    current_start = 0
    buffer: list[str] = []
    ordinal = 0

    def flush(end_line: int) -> None:
        nonlocal ordinal
        content = "".join(buffer)
        for section in _chunks(content, current_heading, ordinal, current_start):
            sections.append(section)
            ordinal += 1

    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.rstrip("\n"))
        if match:
            flush(index)
            buffer = []
            current_heading = match.group(2).strip()
            current_start = offset + index + 1
        else:
            buffer.append(line)
    flush(len(lines))
    links = [target.strip() for target in WIKILINK_RE.findall(body) if target.strip()]
    return ParsedNote(
        path=relative,
        title=html.unescape(title),
        content_hash=_sha256(text),
        mtime=path.stat().st_mtime,
        frontmatter=frontmatter,
        tags=_tags(frontmatter),
        links=links,
        course=course,
        sections=sections,
    )


def _note_file_inside_root(path: Path, root: Path) -> bool:
    """False for symlinks and for files that resolve outside ``root``."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            if cursor.is_symlink():
                return False
        except OSError:
            return False
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return True


Filters = tuple[list[str], list[object]]
NO_FILTERS: Filters = ([], [])


def _where(filters: Filters) -> tuple[str, list[object]]:
    """`` AND ...`` suffix for ``filters`` (clauses over ``s`` and ``n``)."""
    clauses, parameters = filters
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), list(parameters)


def _substring_rows(
    connection: sqlite3.Connection, needle: str, limit: int, filters: Filters = NO_FILTERS
) -> list[sqlite3.Row]:
    """Bounded LIKE probe. A too-complex pattern yields no rows, not a sqlite message."""

    pattern = f"%{needle}%"
    suffix, parameters = _where(filters)
    try:
        return list(
            connection.execute(
                "SELECT s.id, 0.0 AS score FROM sections s "
                "JOIN notes n ON n.id=s.note_id "
                "WHERE (s.content LIKE ? OR s.heading LIKE ? OR n.title LIKE ?)"
                + suffix
                + " LIMIT ?",
                (pattern, pattern, pattern, *parameters, limit),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def _decode_tags(tags: object, tags_json: object = None) -> list[str]:
    """Tags of a ``notes`` row: ``tags_json`` when present, else the legacy space-joined text."""
    if isinstance(tags_json, str) and tags_json:
        try:
            loaded = json.loads(tags_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
    return str(tags or "").split()


def _has_tags(tags: object, tags_json: object, wanted_json: str) -> int:
    """SQL function: 1 when the note carries every tag in ``wanted_json``."""
    note_tags = set(_decode_tags(tags, tags_json))
    return int(all(str(tag) in note_tags for tag in json.loads(wanted_json)))


class NotesIndex:
    """SQLite-backed section index. All writes are explicit; search is read-only."""

    def __init__(self, db_path: Path | str, *, embedder: EmbeddingProvider | None = None) -> None:
        self.db_path = Path(db_path)
        self.embedder = embedder
        self.vec_enabled = False
        self.fts_tokenizer = ""
        self._segment: Callable[[str], str] | None = None
        self._set_tokenizer("trigram" if _supports_trigram() else "unicode61")

    def _set_tokenizer(self, tokenizer: str) -> None:
        self.fts_tokenizer = tokenizer
        self._segment = None
        if tokenizer == "unicode61":
            try:
                import jieba  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("jieba is required when FTS5 trigram is unavailable") from exc
            self._segment = lambda text: " ".join(jieba.lcut(text, cut_all=False))

    def _stored_tokenizer(self, connection: sqlite3.Connection) -> str:
        """Tokenizer the existing index was built with, or "" for a new index.

        ``meta.fts_tokenizer`` is written at build time; an index from before
        that key existed is read from the ``sections_fts`` schema instead.
        """
        if self._has_table(connection, "meta"):
            row = connection.execute("SELECT value FROM meta WHERE key='fts_tokenizer'").fetchone()
            if row is not None and str(row[0]) in FTS_TOKENIZERS:
                return str(row[0])
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='sections_fts'"
        ).fetchone()
        if row is not None and row[0]:
            match = _TOKENIZE_RE.search(str(row[0]))
            if match and match.group(1) in FTS_TOKENIZERS:
                return match.group(1)
            return "unicode61"  # FTS5's default tokenizer
        return ""

    def _use_stored_tokenizer(self, connection: sqlite3.Connection) -> None:
        stored = self._stored_tokenizer(connection)
        if stored and stored != self.fts_tokenizer:
            self._set_tokenizer(stored)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("memfw_has_tags", 3, _has_tags, deterministic=True)
        return connection

    @staticmethod
    def _tags_json_column(connection: sqlite3.Connection) -> str:
        """``n.tags_json`` on current indexes, ``NULL`` on ones built before it existed."""
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}
        return "n.tags_json" if "tags_json" in columns else "NULL"

    def _filters(
        self,
        connection: sqlite3.Connection,
        *,
        path_prefix: str | None,
        tags: Sequence[str],
        course: str | None,
        metadata: dict[str, str] | None,
        since: float | None,
        until: float | None,
    ) -> Filters:
        """SQL clauses over ``sections s JOIN notes n`` for the search filters.

        They are applied while collecting candidates, so a filter never
        empties a result that the candidate cap cut short.
        """
        clauses: list[str] = []
        parameters: list[object] = []
        if path_prefix:
            clauses.append("substr(n.path, 1, ?) = ?")
            parameters.extend([len(path_prefix), path_prefix])
        if course:
            clauses.append("n.course = ?")
            parameters.append(course)
        if tags:
            tags_json = self._tags_json_column(connection)
            clauses.append(f"memfw_has_tags(n.tags, {tags_json}, ?) = 1")
            parameters.append(json.dumps([str(tag) for tag in tags], ensure_ascii=False))
        if since is not None:
            clauses.append("n.mtime >= ?")
            parameters.append(float(since))
        if until is not None:
            clauses.append("n.mtime <= ?")
            parameters.append(float(until))
        for key, value in (metadata or {}).items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", key):
                raise ValueError("metadata filter keys must be identifiers")
            clauses.append(f"json_extract(n.frontmatter, '$.{key}') = ?")
            parameters.append(str(value))
        return clauses, parameters

    def _load_vec(self, connection: sqlite3.Connection) -> bool:
        try:
            import importlib

            sqlite_vec = importlib.import_module("sqlite_vec")
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            self.vec_enabled = True
            return True
        except Exception:  # noqa: BLE001 - optional extension
            self.vec_enabled = False
            return False

    def _dimension(self) -> int:
        return int(getattr(self.embedder, "dimension", 0) or 0)

    def _embedder_identity(self) -> str:
        """Provider class, dimension and fingerprint the stored vectors came from."""
        if self.embedder is None:
            return ""
        fingerprint = getattr(self.embedder, "fingerprint", "")
        return f"{type(self.embedder).__name__}|{self._dimension()}|{fingerprint}"

    def _vectors_stale(self, meta: dict[str, str]) -> bool:
        """True when stored vectors came from a different embedder or dimension."""
        stored = meta.get("embedding_identity")
        if stored is not None:
            return stored != self._embedder_identity()
        # Index from before ``embedding_identity``: compare the legacy keys.
        expected = {
            "embedding_model": type(self.embedder).__name__,
            "embedding_fingerprint": str(getattr(self.embedder, "fingerprint", "")),
            "dimension": str(self._dimension()),
        }
        return any(meta.get(key, value) != value for key, value in expected.items())

    def _drop_vectors(self, connection: sqlite3.Connection) -> None:
        for table in ("vec_sections", "section_vectors"):
            if self._has_table(connection, table):
                try:
                    connection.execute(f"DROP TABLE {table}")
                except sqlite3.OperationalError:
                    pass  # vec0 table without sqlite-vec loaded; unusable anyway

    def _store_vectors(
        self,
        connection: sqlite3.Connection,
        section_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        for section_id, vector in zip(section_ids, vectors, strict=True):
            if len(vector) != self._dimension():
                raise RuntimeError("embedding dimension mismatch")
            if self.vec_enabled:
                connection.execute(
                    "INSERT INTO vec_sections(section_id,embedding) VALUES (?,?)",
                    (section_id, array("f", vector).tobytes()),
                )
            else:
                connection.execute(
                    "INSERT INTO section_vectors(section_id,dim,data) VALUES (?,?,?)",
                    (section_id, len(vector), array("f", vector).tobytes()),
                )

    def _backfill_vectors(self, connection: sqlite3.Connection) -> int:
        """Embed every section that has no vector yet, e.g. after an embedder change."""
        if self.embedder is None:
            return 0
        table = "vec_sections" if self.vec_enabled else "section_vectors"
        if not self._has_table(connection, table):
            return 0
        rows = connection.execute(
            "SELECT s.id, s.heading, s.content, n.title FROM sections s "
            f"JOIN notes n ON n.id=s.note_id WHERE s.id NOT IN (SELECT section_id FROM {table}) "
            "ORDER BY s.id"
        ).fetchall()
        embedded = 0
        for start in range(0, len(rows), BACKFILL_BATCH):
            batch = rows[start : start + BACKFILL_BATCH]
            texts = [
                (str(row["title"]) + "\n" + str(row["heading"]) + "\n" + str(row["content"]))[
                    :EMBED_MAX_CHARS
                ]
                for row in batch
            ]
            vectors = _embed(self.embedder, texts, role="document")
            self._store_vectors(connection, [int(row["id"]) for row in batch], vectors)
            embedded += len(batch)
            connection.commit()
        return embedded

    def _tokenize(self, text: str) -> str:
        if self._segment is None:
            return text
        return self._segment(text)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._load_vec(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    frontmatter TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    course TEXT NOT NULL,
                    tags_json TEXT
                );
                CREATE TABLE IF NOT EXISTS sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    heading TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS links (
                    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    target TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            if self._tags_json_column(connection) == "NULL":
                # ``tags`` stays space-joined for older readers; ``tags_json``
                # keeps tags that contain spaces intact.
                connection.execute("ALTER TABLE notes ADD COLUMN tags_json TEXT")
            self._use_stored_tokenizer(connection)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('fts_tokenizer',?)",
                (self.fts_tokenizer,),
            )
            dimension = self._dimension()
            connection.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
                    heading, content, title, tokenize='{self.fts_tokenizer}'
                )
            """)
            meta = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM meta").fetchall()
            }
            if self.embedder is not None and self._vectors_stale(meta):
                # A different model or dimension: old vectors are not comparable.
                self._drop_vectors(connection)
            if dimension:
                if self.vec_enabled:
                    connection.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_sections USING vec0("
                        f"section_id INTEGER PRIMARY KEY, embedding float[{dimension}])"
                    )
                else:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS section_vectors ("
                        "section_id INTEGER PRIMARY KEY, dim INTEGER NOT NULL, data BLOB NOT NULL)"
                    )
            fingerprint = getattr(self.embedder, "fingerprint", "") if self.embedder else ""
            embedding_meta = {
                "embedding_model": type(self.embedder).__name__ if self.embedder else "",
                "embedding_fingerprint": str(fingerprint),
                "dimension": str(dimension),
            }
            if self.embedder is not None:
                embedding_meta["embedding_identity"] = self._embedder_identity()
            # Without an embedder the stored vectors and their metadata stay
            # as they are, so a later run with the same embedder reuses them.
            verb = "REPLACE" if self.embedder is not None else "IGNORE"
            for key, value in embedding_meta.items():
                connection.execute(
                    f"INSERT OR {verb} INTO meta(key,value) VALUES (?,?)", (key, value)
                )
            connection.commit()

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"exists": False, "notes": 0, "sections": 0, "vectors": 0}
        with self._connect() as connection:
            self._load_vec(connection)
            self._use_stored_tokenizer(connection)
            notes = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            sections = connection.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
            vectors = 0
            if self.vec_enabled:
                try:
                    vectors = connection.execute("SELECT COUNT(*) FROM vec_sections").fetchone()[0]
                except sqlite3.OperationalError:
                    vectors = 0
            else:
                try:
                    vectors = connection.execute("SELECT COUNT(*) FROM section_vectors").fetchone()[
                        0
                    ]
                except sqlite3.OperationalError:
                    vectors = 0
            meta = {
                row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM meta")
            }
        return {
            "exists": True,
            "notes": notes,
            "sections": sections,
            "vectors": vectors,
            "fts_tokenizer": self.fts_tokenizer,
            "vec_enabled": self.vec_enabled,
            **meta,
        }

    def _delete_note(self, connection: sqlite3.Connection, note_id: int) -> None:
        section_ids = [
            row[0]
            for row in connection.execute("SELECT id FROM sections WHERE note_id=?", (note_id,))
        ]
        for section_id in section_ids:
            connection.execute("DELETE FROM sections_fts WHERE rowid=?", (section_id,))
            # Vector tables exist only when an embedder was configured, and
            # vec_sections can only be touched with sqlite-vec loaded.
            if self.vec_enabled and self._has_table(connection, "vec_sections"):
                connection.execute("DELETE FROM vec_sections WHERE section_id=?", (section_id,))
            if self._has_table(connection, "section_vectors"):
                connection.execute("DELETE FROM section_vectors WHERE section_id=?", (section_id,))
        connection.execute("DELETE FROM links WHERE note_id=?", (note_id,))
        connection.execute("DELETE FROM sections WHERE note_id=?", (note_id,))
        connection.execute("DELETE FROM notes WHERE id=?", (note_id,))

    def index(
        self,
        root: Path | str,
        *,
        max_files: int | None = None,
        progress: Any | None = None,
        commit_every: int = 100,
    ) -> dict[str, int]:
        """Incrementally index ``root`` and return counts of work performed."""

        root = Path(root).expanduser()
        if not root.is_dir():
            raise ValueError("root must be a directory")
        self.initialize()
        counts = {
            "files": 0,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
            "sections": 0,
            "embedded": 0,
        }
        paths = [path for path in sorted(root.rglob("*.md")) if _note_file_inside_root(path, root)]
        if max_files is not None:
            paths = paths[: max(0, max_files)]
        seen: set[str] = set()
        with self._connect() as connection:
            self._load_vec(connection)
            for path in paths:
                try:
                    note = parse_markdown(path, root)
                except NoteParseError:
                    continue
                counts["files"] += 1
                seen.add(note.path)
                row = connection.execute(
                    "SELECT id,content_hash FROM notes WHERE path=?", (note.path,)
                ).fetchone()
                if row is not None and row["content_hash"] == note.content_hash:
                    counts["unchanged"] += 1
                    continue
                if row is not None:
                    self._delete_note(connection, int(row["id"]))
                cursor = connection.execute(
                    "INSERT INTO notes("
                    "path,title,content_hash,mtime,frontmatter,tags,course,tags_json"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        note.path,
                        note.title,
                        note.content_hash,
                        note.mtime,
                        json.dumps(note.frontmatter, ensure_ascii=False, default=str),
                        " ".join(note.tags),
                        note.course,
                        json.dumps(note.tags, ensure_ascii=False),
                    ),
                )
                note_id = int(cursor.lastrowid or 0)
                for target in note.links:
                    connection.execute(
                        "INSERT INTO links(note_id,target) VALUES (?,?)", (note_id, target)
                    )
                section_rows: list[tuple[int, str, str]] = []
                for section in note.sections:
                    section_cursor = connection.execute(
                        "INSERT INTO sections("
                        "note_id,heading,level,ordinal,content,hash,start_line,end_line"
                        ") VALUES (?,?,?,?,?,?,?,?)",
                        (
                            note_id,
                            section.heading,
                            section.level,
                            section.ordinal,
                            section.content,
                            section.hash,
                            section.start_line,
                            section.end_line,
                        ),
                    )
                    section_id = int(section_cursor.lastrowid or 0)
                    connection.execute(
                        "INSERT INTO sections_fts(rowid,heading,content,title) VALUES (?,?,?,?)",
                        (
                            section_id,
                            self._tokenize(section.heading),
                            self._tokenize(section.content),
                            self._tokenize(note.title),
                        ),
                    )
                    section_rows.append((section_id, section.heading, section.content))
                    counts["sections"] += 1
                if self.embedder is not None and section_rows:
                    texts = [
                        (note.title + "\n" + heading + "\n" + content)[:EMBED_MAX_CHARS]
                        for _, heading, content in section_rows
                    ]
                    vectors = _embed(self.embedder, texts, role="document")
                    self._store_vectors(connection, [row[0] for row in section_rows], vectors)
                    counts["embedded"] += len(section_rows)
                counts["updated"] += 1
                if commit_every > 0 and counts["updated"] % commit_every == 0:
                    connection.commit()
                if progress is not None:
                    progress(counts)
            if max_files is None:
                for row in connection.execute("SELECT id,path FROM notes").fetchall():
                    if row["path"] not in seen:
                        self._delete_note(connection, int(row["id"]))
                        counts["deleted"] += 1
            connection.commit()
            counts["embedded"] += self._backfill_vectors(connection)
            connection.commit()
        return counts

    def _fts_terms(self, needle: str) -> list[str]:
        """Terms for ``needle`` against this index's tokenizer.

        A double-quoted query is one phrase. Otherwise the shared
        ``query_terms`` builder is used; on a trigram index CJK tokens are
        also split by jieba so a sentence matches notes containing only part
        of it. Terms shorter than three characters are kept: ``_fts_search``
        routes them around the trigram tokenizer.
        """
        stripped = needle.strip()
        if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
            phrase = stripped[1:-1].strip()
            return [phrase] if len(phrase) >= 2 else []
        base = query_terms(stripped, self.fts_tokenizer)
        tokens: list[str] = []
        for term in base:
            tokens.append(term)
            if self.fts_tokenizer == "trigram" and not term.isascii():
                try:
                    import jieba

                    tokens.extend(piece.strip() for piece in jieba.lcut(term, cut_all=False))
                except ImportError:  # pragma: no cover - declared dependency
                    pass
        seen: set[str] = set()
        output: list[str] = []
        for token in tokens:
            key = token.casefold()
            if len(token) < 2 or key in seen:
                continue
            seen.add(key)
            output.append(token)
            if len(output) == MAX_TERMS:
                break
        return output

    def _fts_search(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
        filters: Filters = NO_FILTERS,
    ) -> list[tuple[int, float]]:
        needle = query.strip()
        if not needle:
            return []
        terms = self._fts_terms(needle)
        long_terms, short_terms = split_short_terms(terms, self.fts_tokenizer)
        rows: list[sqlite3.Row] = []
        if long_terms:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in long_terms)
            suffix, filter_parameters = _where(filters)
            join = (
                " JOIN sections s ON s.id=sections_fts.rowid JOIN notes n ON n.id=s.note_id"
                if suffix
                else ""
            )
            try:
                rows = connection.execute(
                    "SELECT sections_fts.rowid AS id, bm25(sections_fts) AS score "
                    "FROM sections_fts" + join + " WHERE sections_fts MATCH ?" + suffix + " "
                    "ORDER BY score LIMIT ?",
                    (expression, *filter_parameters, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if short_terms and len(rows) < limit:
            # Trigram FTS5 cannot match a term shorter than three characters
            # ("AI", "数据"); match those by substring and rank them after the
            # BM25 hits.
            clause, parameters = short_terms_clause(
                ["s.heading", "s.content", "n.title"], short_terms
            )
            found = {int(row["id"]) for row in rows}
            suffix, filter_parameters = _where(filters)
            try:
                extra = connection.execute(
                    "SELECT s.id AS id, 0.0 AS score FROM sections s "
                    "JOIN notes n ON n.id=s.note_id WHERE " + clause + suffix + " "
                    "ORDER BY s.id LIMIT ?",
                    (*parameters, *filter_parameters, limit + len(found)),
                ).fetchall()
            except sqlite3.OperationalError:
                extra = []
            for row in extra:
                if int(row["id"]) not in found:
                    found.add(int(row["id"]))
                    rows.append(row)
                if len(rows) >= limit:
                    break
        if not rows:
            # A single character, or terms no tokenizer matched: fall back to a
            # bounded substring probe rather than dropping the query.
            rows = _substring_rows(connection, needle, limit, filters)
        return [(int(row["id"]), float(row["score"])) for row in rows[:limit]]

    def _vector_search(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
        filters: Filters = NO_FILTERS,
    ) -> list[tuple[int, float]]:
        if self.embedder is None:
            return []
        try:
            vector = _embed(self.embedder, [query], role="query")[0]
        except Exception:  # noqa: BLE001 - degrade to FTS
            return []
        if len(vector) != self._dimension():
            return []
        stored = (
            connection.execute("SELECT value FROM meta WHERE key='embedding_identity'").fetchone()
            if self._has_table(connection, "meta")
            else None
        )
        if stored is not None and str(stored[0]) != self._embedder_identity():
            return []  # vectors from another embedder; reindex rebuilds them
        allowed: set[int] | None = None
        suffix, filter_parameters = _where(filters)
        if suffix:
            allowed = {
                int(row[0])
                for row in connection.execute(
                    "SELECT s.id FROM sections s JOIN notes n ON n.id=s.note_id WHERE 1" + suffix,
                    tuple(filter_parameters),
                )
            }
            if not allowed:
                return []
        if self._has_table(connection, "vec_sections"):
            blob = array("f", vector).tobytes()
            try:
                if allowed is None:
                    rows = connection.execute(
                        "SELECT section_id, distance FROM vec_sections "
                        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                        (blob, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT section_id, distance FROM vec_sections WHERE embedding MATCH ? "
                        "AND section_id IN (SELECT value FROM json_each(?)) "
                        "ORDER BY distance LIMIT ?",
                        (blob, json.dumps(sorted(allowed)), limit),
                    ).fetchall()
                return [(int(row["section_id"]), float(row["distance"])) for row in rows]
            except sqlite3.OperationalError:
                return []
        if not self._has_table(connection, "section_vectors"):
            return []
        rows = connection.execute("SELECT section_id,dim,data FROM section_vectors").fetchall()
        scored: list[tuple[int, float]] = []
        for row in rows:
            dim = int(row["dim"])
            if dim != len(vector):
                continue
            if allowed is not None and int(row["section_id"]) not in allowed:
                continue
            data = array("f")
            data.frombytes(row["data"])
            dot = sum(a * b for a, b in zip(data, vector, strict=True))
            scored.append((int(row["section_id"]), 1.0 - dot))
        scored.sort(key=lambda item: item[1])
        return scored[:limit]

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        path_prefix: str | None = None,
        tags: Sequence[str] = (),
        course: str | None = None,
        metadata: dict[str, str] | None = None,
        since: float | None = None,
        until: float | None = None,
        mode: str = "hybrid",
        candidates: int | None = None,
        rrf_k: int = FTS_K,
    ) -> list[NoteHit]:
        if not query.strip() or limit <= 0 or not self.db_path.exists():
            return []
        if mode not in {"fts", "vector", "hybrid"}:
            raise ValueError("mode must be fts, vector or hybrid")
        if candidates is not None and candidates < 1:
            raise ValueError("candidates must be positive")
        candidate_limit = candidates or max(limit * 8, 50)
        with self._connect() as connection:
            self._load_vec(connection)
            self._use_stored_tokenizer(connection)
            filters = self._filters(
                connection,
                path_prefix=path_prefix,
                tags=tags,
                course=course,
                metadata=metadata,
                since=since,
                until=until,
            )
            lexical = (
                self._fts_search(connection, query, candidate_limit, filters)
                if mode in {"fts", "hybrid"}
                else []
            )
            semantic = (
                self._vector_search(connection, query, candidate_limit, filters)
                if mode in {"vector", "hybrid"}
                else []
            )
            tags_json = self._tags_json_column(connection)
        fused: dict[int, tuple[float, str]] = {}
        for rank, (section_id, _) in enumerate(lexical, start=1):
            score, source = fused.get(section_id, (0.0, ""))
            fused[section_id] = (score + 1.0 / (rrf_k + rank), source + "fts")
        for rank, (section_id, _) in enumerate(semantic, start=1):
            score, source = fused.get(section_id, (0.0, ""))
            fused[section_id] = (score + 1.0 / (rrf_k + rank), source + "vec")
        if not fused:
            return []
        suffix, filter_parameters = _where(filters)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.id AS section_id, s.heading, s.content, s.ordinal, "
                "s.hash AS section_hash, n.path, n.title, n.tags, n.course, "
                f"{tags_json} AS tags_json "
                "FROM sections s JOIN notes n ON n.id=s.note_id "
                f"WHERE s.id IN ({','.join('?' for _ in fused)})" + suffix,
                (*fused, *filter_parameters),
            ).fetchall()
        hits: list[NoteHit] = []
        seen_notes: set[str] = set()
        for row in sorted(rows, key=lambda item: -fused.get(int(item["section_id"]), (0, ""))[0]):
            path = str(row["path"])
            if path in seen_notes:
                continue
            note_tags = _decode_tags(row["tags"], row["tags_json"])
            seen_notes.add(path)
            fused_score, sources = fused[int(row["section_id"])]
            hits.append(
                NoteHit(
                    path=path,
                    title=str(row["title"]),
                    heading=str(row["heading"]),
                    content=str(row["content"]),
                    score=fused_score,
                    rank=len(hits) + 1,
                    source=(
                        "hybrid"
                        if "fts" in sources and "vec" in sources
                        else "vector"
                        if "vec" in sources
                        else "fts"
                    ),
                    tags=note_tags,
                    section_id=int(row["section_id"]),
                    content_hash=str(row["section_hash"] or ""),
                )
            )
            if len(hits) >= limit:
                break
        return hits


def _supports_trigram() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value, tokenize='trigram')")
        connection.execute("DROP TABLE probe")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()
