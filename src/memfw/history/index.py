"""Read-only transcript index over configured agent session logs.

The index is derived data (``<vault>/.memfw-history.sqlite`` by default). It
stores, per entry, only a location (source, relative path, byte offset, line
number, a line digest) plus lineage ids, a timestamp and a redacted snippet.
Raw log lines are never copied into the index or the vault. Lines that fail
the secret scan are skipped; only a per-file count is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from memfw.index import _supports_trigram
from memfw.index.fts_terms import fts_match

from .parse import parse_line
from .redact import has_secret, redact

DEFAULT_DB_NAME = ".memfw-history.sqlite"
CONFIG_KEY = "history_sources"
SUFFIXES = {".jsonl": True, ".log": False}
SNIPPET_CHARS = 400
MAX_LINE_BYTES = 8 * 1024 * 1024
DEFAULT_READ_BYTES = 8 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_CONTEXT = 20
SCHEMA_VERSION = "1"
WITHHELD = "[withheld: secret detected]"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SPACE = re.compile(r"\s+")


class HistoryError(ValueError):
    """A refused or invalid history request."""


@dataclass(frozen=True)
class SourceRoot:
    """One configured transcript root. ``name`` is used in every location."""

    name: str
    path: Path

    @classmethod
    def create(cls, name: str, path: Path | str) -> SourceRoot:
        if not _NAME.match(name):
            raise HistoryError(f"invalid history source name: {name!r}")
        raw = Path(path).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        if raw.is_symlink():
            raise HistoryError(f"history source {name!r} must not be a symlink")
        resolved = raw.resolve()
        if not resolved.is_dir():
            raise HistoryError(f"history source {name!r} is not a directory")
        return cls(name, resolved)


def sources_from_config(config: dict[str, Any]) -> list[SourceRoot]:
    """Read ``history_sources`` from a vault config mapping.

    Accepted shapes: a list of ``{name, path}`` mappings, or a ``{name: path}``
    mapping. Nothing is hard-coded; no config means no sources.
    """

    raw = config.get(CONFIG_KEY)
    if raw is None:
        return []
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(key), str(value)) for key, value in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or "name" not in item or "path" not in item:
                raise HistoryError(f"{CONFIG_KEY} entries need name and path")
            pairs.append((str(item["name"]), str(item["path"])))
    else:
        raise HistoryError(f"{CONFIG_KEY} must be a list or mapping")
    return _unique([SourceRoot.create(name, path) for name, path in pairs])


def parse_source_option(value: str) -> SourceRoot:
    """Parse a ``NAME=PATH`` command-line source."""

    if "=" not in value:
        raise HistoryError("source must be NAME=PATH")
    name, path = value.split("=", 1)
    return SourceRoot.create(name.strip(), path.strip())


def _unique(sources: list[SourceRoot]) -> list[SourceRoot]:
    seen: dict[str, SourceRoot] = {}
    for source in sources:
        if source.name in seen and seen[source.name].path != source.path:
            raise HistoryError(f"duplicate history source name: {source.name}")
        seen[source.name] = source
    return list(seen.values())


def _snippet(text: str) -> str:
    collapsed = _SPACE.sub(" ", redact(text)).strip()
    if len(collapsed) > SNIPPET_CHARS:
        return collapsed[: SNIPPET_CHARS - 1] + "…"
    return collapsed


def _digest(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()[:16]


def _tail(text: str, size: int = 200) -> str:
    return text[-size:]


def split_ref(ref: str) -> tuple[str, str, int]:
    """Split ``source:relative/path@offset`` into its parts."""

    head, sep, offset = ref.rpartition("@")
    name, colon, path = head.partition(":")
    if not sep or not colon or not name or not path or not offset.isdigit():
        raise HistoryError("ref must look like SOURCE:PATH@OFFSET")
    return name, path, int(offset)


def make_ref(source: str, path: str, offset: int) -> str:
    return f"{source}:{path}@{offset}"


class TranscriptIndex:
    """Derived SQLite index over transcript source roots."""

    def __init__(self, db: Path | str, sources: Iterable[SourceRoot]) -> None:
        self.db = Path(db)
        self.sources = {source.name: source for source in _unique(list(sources))}

    # -- storage -----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        tokenizer = "trigram" if _supports_trigram() else "unicode61"
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                entries INTEGER NOT NULL,
                skipped_secret INTEGER NOT NULL,
                skipped_other INTEGER NOT NULL,
                PRIMARY KEY (source, path)
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                offset INTEGER NOT NULL,
                line INTEGER NOT NULL,
                length INTEGER NOT NULL,
                digest TEXT NOT NULL,
                session_id TEXT NOT NULL,
                parent_session_id TEXT NOT NULL,
                window_id TEXT NOT NULL,
                parent_window_id TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                parent_entry_id TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                agent TEXT NOT NULL,
                ts TEXT NOT NULL,
                snippet TEXT NOT NULL,
                UNIQUE (source, path, offset),
                FOREIGN KEY (source, path) REFERENCES files (source, path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS entries_session ON entries (source, session_id, offset);
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                snippet, tokenize='{tokenizer}'
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('tokenizer', ?)", (tokenizer,)
        )
        return connection

    @staticmethod
    def _tokenizer(connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT value FROM meta WHERE key = 'tokenizer'").fetchone()
        return str(row["value"]) if row else "trigram"

    # -- path safety --------------------------------------------------------------

    def _source(self, name: str) -> SourceRoot:
        source = self.sources.get(name)
        if source is None:
            raise HistoryError(f"unknown history source: {name}")
        return source

    def safe_path(self, name: str, relative: str) -> Path:
        """Resolve ``relative`` under source ``name``; refuse symlinks and escapes."""

        source = self._source(name)
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise HistoryError("path is outside the configured history roots")
        if source.path.is_symlink():
            raise HistoryError("history source root is a symlink")
        current = source.path
        for part in pure.parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError as exc:
                raise HistoryError("transcript file not found") from exc
            if stat.S_ISLNK(mode):
                raise HistoryError("refusing to follow a symlink")
        resolved = current.resolve()
        if not resolved.is_relative_to(source.path):
            raise HistoryError("path is outside the configured history roots")
        if not resolved.is_file():
            raise HistoryError("transcript path is not a regular file")
        return resolved

    def _walk(self, source: SourceRoot) -> Iterator[tuple[str, Path, os.stat_result | None]]:
        """Yield (relative path, path, stat); stat is None for refused symlinks."""

        for directory, dirnames, filenames in os.walk(source.path, followlinks=False):
            dirnames.sort()
            base = Path(directory)
            for dirname in list(dirnames):
                if (base / dirname).is_symlink():
                    dirnames.remove(dirname)
                    yield (base / dirname).relative_to(source.path).as_posix(), base / dirname, None
            for filename in sorted(filenames):
                path = base / filename
                if path.suffix not in SUFFIXES:
                    continue
                relative = path.relative_to(source.path).as_posix()
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    yield relative, path, None
                elif stat.S_ISREG(info.st_mode):
                    yield relative, path, info

    # -- indexing -----------------------------------------------------------------

    def refresh(self) -> dict[str, int]:
        """Incrementally index all sources; unchanged (size, mtime) files are skipped."""

        counts = {
            "files_indexed": 0,
            "files_unchanged": 0,
            "files_removed": 0,
            "symlinks_refused": 0,
            "entries_indexed": 0,
            "skipped_secret": 0,
            "skipped_other": 0,
        }
        with self._connect() as connection:
            known_sources = [
                row["source"] for row in connection.execute("SELECT DISTINCT source FROM files")
            ]
            for stale in set(known_sources) - set(self.sources):
                counts["files_removed"] += self._drop(connection, stale, None)
            for source in self.sources.values():
                seen: set[str] = set()
                for relative, path, info in self._walk(source):
                    if info is None:
                        counts["symlinks_refused"] += 1
                        continue
                    seen.add(relative)
                    row = connection.execute(
                        "SELECT size, mtime_ns FROM files WHERE source = ? AND path = ?",
                        (source.name, relative),
                    ).fetchone()
                    if row and row["size"] == info.st_size and row["mtime_ns"] == info.st_mtime_ns:
                        counts["files_unchanged"] += 1
                        continue
                    result = self._index_file(connection, source, relative, path, info)
                    counts["files_indexed"] += 1
                    for key in ("entries_indexed", "skipped_secret", "skipped_other"):
                        counts[key] += result[key]
                indexed = {
                    row["path"]
                    for row in connection.execute(
                        "SELECT path FROM files WHERE source = ?", (source.name,)
                    )
                }
                for gone in indexed - seen:
                    counts["files_removed"] += self._drop(connection, source.name, gone)
        return counts

    @staticmethod
    def _drop(connection: sqlite3.Connection, source: str, path: str | None) -> int:
        where, args = (
            ("source = ?", (source,))
            if path is None
            else (
                "source = ? AND path = ?",
                (source, path),
            )
        )
        connection.execute(
            f"DELETE FROM entries_fts WHERE rowid IN (SELECT id FROM entries WHERE {where})", args
        )
        cursor = connection.execute(f"DELETE FROM files WHERE {where}", args)
        return cursor.rowcount

    def _index_file(
        self,
        connection: sqlite3.Connection,
        source: SourceRoot,
        relative: str,
        path: Path,
        info: os.stat_result,
    ) -> dict[str, int]:
        self._drop(connection, source.name, relative)
        jsonl = SUFFIXES[path.suffix]
        stem = path.stem
        session = ""
        parent_session = ""
        window_number = 0
        window_id = "w0"
        parent_window = ""
        previous_text = ""
        rows: list[tuple[Any, ...]] = []
        skipped_secret = skipped_other = 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            offset = 0
            for number, raw in enumerate(
                iter(lambda: handle.readline(MAX_LINE_BYTES + 1), b""), start=1
            ):
                start = offset
                offset += len(raw)
                if len(raw) > MAX_LINE_BYTES:
                    skipped_other += 1
                    # Drain the rest of an oversized line.
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_LINE_BYTES)
                        offset += len(raw)
                    continue
                line = raw.decode("utf-8", errors="replace")
                record = parse_line(line, jsonl=jsonl)
                if record is None:
                    if line.strip():
                        skipped_other += 1
                    continue
                if record.session_id and record.session_id != session:
                    session = record.session_id
                    parent_session = record.parent_session_id
                elif record.parent_session_id and not parent_session:
                    parent_session = record.parent_session_id
                if record.new_window:
                    parent_window = window_id
                    window_number += 1
                    window_id = f"w{window_number}"
                if record.window_id and record.window_id != window_id:
                    parent_window = record.parent_window_id or window_id
                    window_id = record.window_id
                # The scan covers the whole raw line (tool inputs too) and the
                # decoded text joined to the previous entry's tail, so a key on
                # one line with its value on the next is still caught.
                joined = _tail(previous_text) + "\n" + record.text
                if has_secret(line) or has_secret(joined) or has_secret(record.text):
                    skipped_secret += 1
                    previous_text = ""
                    continue
                previous_text = record.text
                if not record.text.strip():
                    continue
                rows.append(
                    (
                        source.name,
                        relative,
                        start,
                        number,
                        len(raw),
                        _digest(raw),
                        session or stem,
                        parent_session,
                        window_id,
                        parent_window,
                        record.entry_id or f"L{number}",
                        record.parent_entry_id,
                        record.role,
                        record.kind,
                        record.agent,
                        record.timestamp,
                        _snippet(record.text),
                    )
                )
        connection.execute(
            "INSERT INTO files (source, path, size, mtime_ns, entries, skipped_secret,"
            " skipped_other) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source.name,
                relative,
                info.st_size,
                info.st_mtime_ns,
                len(rows),
                skipped_secret,
                skipped_other,
            ),
        )
        for row in rows:
            cursor = connection.execute(
                "INSERT INTO entries (source, path, offset, line, length, digest, session_id,"
                " parent_session_id, window_id, parent_window_id, entry_id, parent_entry_id,"
                " role, kind, agent, ts, snippet) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            connection.execute(
                "INSERT INTO entries_fts (rowid, snippet) VALUES (?, ?)",
                (cursor.lastrowid, row[-1]),
            )
        return {
            "entries_indexed": len(rows),
            "skipped_secret": skipped_secret,
            "skipped_other": skipped_other,
        }

    # -- queries ------------------------------------------------------------------

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ref": make_ref(row["source"], row["path"], row["offset"]),
            "source": row["source"],
            "path": row["path"],
            "offset": row["offset"],
            "line": row["line"],
            "session_id": row["session_id"],
            "parent_session_id": row["parent_session_id"] or None,
            "window_id": row["window_id"],
            "parent_window_id": row["parent_window_id"] or None,
            "entry_id": row["entry_id"],
            "parent_entry_id": row["parent_entry_id"] or None,
            "role": row["role"],
            "kind": row["kind"],
            "agent": row["agent"],
            "timestamp": row["ts"] or None,
            "snippet": row["snippet"],
        }

    @staticmethod
    def _filters(
        source: str | None, session: str | None, since: str | None, until: str | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if source:
            clauses.append("e.source = ?")
            args.append(source)
        if session:
            clauses.append("e.session_id = ?")
            args.append(session)
        if since:
            clauses.append("e.ts >= ?")
            args.append(since)
        if until:
            clauses.append("e.ts <= ?")
            args.append(until)
        return (" AND ".join(clauses) or "1"), args

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS files, COALESCE(SUM(entries), 0) AS entries,"
                " COALESCE(SUM(skipped_secret), 0) AS skipped_secret,"
                " COALESCE(SUM(skipped_other), 0) AS skipped_other FROM files"
            ).fetchone()
            return {key: int(row[key]) for key in row.keys()}

    def list(
        self,
        *,
        source: str | None = None,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Sessions with lineage; with ``session``, that session's windows and entries."""

        bounded = max(1, min(int(limit), 500))
        where, args = self._filters(source, session, since, until)
        with self._connect() as connection:
            if session:
                rows = connection.execute(
                    f"SELECT e.* FROM entries e WHERE {where}"
                    " ORDER BY e.source, e.path, e.offset LIMIT ?",
                    (*args, bounded),
                ).fetchall()
                entries = [self._entry(row) for row in rows]
                windows: dict[str, dict[str, Any]] = {}
                for entry in entries:
                    window = windows.setdefault(
                        entry["window_id"],
                        {
                            "window_id": entry["window_id"],
                            "parent_window_id": entry["parent_window_id"],
                            "entries": 0,
                            "first_ref": entry["ref"],
                        },
                    )
                    window["entries"] += 1
                return {
                    "session_id": session,
                    "windows": list(windows.values()),
                    "entries": entries,
                }
            rows = connection.execute(
                "SELECT e.source, e.session_id, MAX(e.parent_session_id) AS parent_session_id,"
                " MIN(NULLIF(e.ts, '')) AS started_at, MAX(NULLIF(e.ts, '')) AS ended_at,"
                " COUNT(*) AS entries, COUNT(DISTINCT e.window_id) AS windows,"
                " MIN(e.agent) AS agent, MIN(e.path) AS path"
                f" FROM entries e WHERE {where} GROUP BY e.source, e.session_id"
                " ORDER BY started_at DESC, e.source, e.session_id LIMIT ?",
                (*args, bounded),
            ).fetchall()
        sessions = [
            {
                "source": row["source"],
                "session_id": row["session_id"],
                "parent_session_id": row["parent_session_id"] or None,
                "agent": row["agent"],
                "path": row["path"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "entries": row["entries"],
                "windows": row["windows"],
            }
            for row in rows
        ]
        return {"sessions": sessions, "stats": self.stats()}

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Full-text search over redacted snippets."""

        text = query.strip()
        if not text:
            raise HistoryError("query required")
        bounded = max(1, min(int(limit), 100))
        where, args = self._filters(source, session, since, until)
        with self._connect() as connection:
            match = fts_match(text, self._tokenizer(connection))
            rows: list[sqlite3.Row] = []
            if match is not None:
                try:
                    rows = connection.execute(
                        "SELECT e.*, bm25(entries_fts) AS score FROM entries_fts"
                        " JOIN entries e ON e.id = entries_fts.rowid"
                        f" WHERE entries_fts MATCH ? AND {where}"
                        " ORDER BY score, e.ts DESC LIMIT ?",
                        (match, *args, bounded),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                # Short terms (under three characters) cannot match a trigram index.
                needle = (
                    "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                rows = connection.execute(
                    f"SELECT e.*, 0.0 AS score FROM entries e WHERE e.snippet LIKE ? ESCAPE '\\'"
                    f" AND {where} ORDER BY e.ts DESC LIMIT ?",
                    (needle, *args, bounded),
                ).fetchall()
        results = []
        for row in rows:
            entry = self._entry(row)
            entry["score"] = round(-float(row["score"]), 6)
            results.append(entry)
        return {"query": query, "results": results}

    def read(
        self,
        ref: str,
        *,
        before: int = 2,
        after: int = 2,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        """Redacted, size-capped window of entries around a stored location."""

        name, relative, offset = split_ref(ref)
        cap = max(256, min(int(max_bytes), MAX_READ_BYTES))
        before = max(0, min(int(before), MAX_CONTEXT))
        after = max(0, min(int(after), MAX_CONTEXT))
        path = self.safe_path(name, relative)
        with self._connect() as connection:
            anchor = connection.execute(
                "SELECT * FROM entries WHERE source = ? AND path = ? AND offset = ?",
                (name, relative, offset),
            ).fetchone()
            if anchor is None:
                raise HistoryError("no indexed entry at this location")
            previous = connection.execute(
                "SELECT * FROM entries WHERE source = ? AND path = ? AND offset < ?"
                " ORDER BY offset DESC LIMIT ?",
                (name, relative, offset, before),
            ).fetchall()
            following = connection.execute(
                "SELECT * FROM entries WHERE source = ? AND path = ? AND offset > ?"
                " ORDER BY offset LIMIT ?",
                (name, relative, offset, after),
            ).fetchall()
        rows = [*reversed(previous), anchor, *following]
        entries: list[dict[str, Any]] = []
        used = 0
        truncated = False
        jsonl = SUFFIXES.get(path.suffix, True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            for row in rows:
                handle.seek(row["offset"])
                raw = handle.read(row["length"])
                if _digest(raw) != row["digest"]:
                    raise HistoryError("transcript changed since indexing; re-index first")
                line = raw.decode("utf-8", errors="replace")
                record = parse_line(line, jsonl=jsonl)
                text = record.text if record else ""
                if has_secret(line) or has_secret(text):
                    text = WITHHELD
                text = redact(text)
                room = cap - used
                encoded = text.encode("utf-8")
                if len(encoded) > room:
                    text = encoded[: max(0, room)].decode("utf-8", errors="ignore")
                    truncated = True
                used += len(text.encode("utf-8"))
                entry = self._entry(row)
                entry.pop("snippet")
                entry["text"] = text
                entry["anchor"] = row["offset"] == offset
                entries.append(entry)
                if used >= cap:
                    truncated = truncated or row is not rows[-1]
                    break
        # A key on one entry and its value on the next is redacted as a pair.
        joined = "\n".join(entry["text"] for entry in entries)
        if redact(joined) != joined:
            for entry in entries:
                entry["text"] = redact(entry["text"])
        return {
            "ref": ref,
            "session_id": anchor["session_id"],
            "window_id": anchor["window_id"],
            "max_bytes": cap,
            "bytes": used,
            "truncated": truncated,
            "entries": entries,
        }


def default_db(vault_root: Path) -> Path:
    return Path(vault_root) / DEFAULT_DB_NAME


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
