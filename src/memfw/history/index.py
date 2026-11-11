"""Read-only transcript index over configured agent session logs.

The index is derived data (``<vault>/.memfw-history.sqlite`` by default). It
stores, per entry, only a location (source, relative path, byte offset, line
number, a line digest) plus redacted lineage ids, a timestamp and a redacted
snippet. Raw log lines are never copied into the index or the vault. Lines
that fail the secret scan are skipped; only a per-file count is kept.

Files are opened one path component at a time with ``O_NOFOLLOW`` relative to
the already-open parent directory, so a symlink swapped in after the walk (or
a root that became a symlink) is refused rather than followed.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from filelock import FileLock

from memfw.index import _supports_trigram
from memfw.index.fts_terms import fts_match, jieba_segmenter

from .parse import Record, parse_line
from .redact import MAX_SCAN_CHARS, has_secret, redact, redact_path, secret_continues

DEFAULT_DB_NAME = ".memfw-history.sqlite"
CONFIG_KEY = "history_sources"
LIMITS_KEY = "history_limits"
SUFFIXES = {".jsonl": True, ".log": False}
SNIPPET_CHARS = 400
FIELD_CHARS = 256
MAX_LINE_BYTES = MAX_SCAN_CHARS
DEFAULT_READ_BYTES = 8 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_CONTEXT = 20
LIKE_SCAN_ROWS = 50_000
DIGEST_SPAN = 4096
SCHEMA_VERSION = "2"
WITHHELD = "[withheld: secret detected]"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FID = re.compile(r"^[0-9a-f]{16}$")
_SPACE = re.compile(r"\s+")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_REFRESH_LOCK = threading.Lock()


class HistoryError(ValueError):
    """A refused or invalid history request."""


class _Refused(Exception):
    """A path that must not be opened (symlink, escape, swapped file)."""


@dataclass(frozen=True)
class HistoryLimits:
    """Caps on index size. Indexing stops (and says so) when one is reached."""

    max_files: int = 100_000
    max_entries: int = 2_000_000
    max_db_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> HistoryLimits:
        raw = config.get(LIMITS_KEY) or {}
        if not isinstance(raw, dict):
            raise HistoryError(f"{LIMITS_KEY} must be a mapping")
        values: dict[str, int] = {}
        for key in ("max_files", "max_entries", "max_db_bytes"):
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise HistoryError(f"{LIMITS_KEY}.{key} must be a positive integer")
                values[key] = value
        return cls(**values)


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

    def usable(self) -> bool:
        """Re-check, at use time, that the root is still a real directory."""

        try:
            mode = os.lstat(self.path).st_mode
        except OSError:
            return False
        return stat.S_ISDIR(mode)


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


@functools.lru_cache(maxsize=8192)
def _field(value: str) -> str:
    """Redacted metadata field. Cached: session and window ids repeat on every line."""

    return redact(value)[:FIELD_CHARS] if value else ""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def file_id(path: str) -> str:
    """Opaque, stable id of a relative path; refs use it so paths never echo back."""

    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def split_ref(ref: str) -> tuple[str, str, int]:
    """Split ``SOURCE:FILE@OFFSET``; FILE is a file id or a relative path."""

    head, sep, offset = ref.rpartition("@")
    name, colon, path = head.partition(":")
    if not sep or not colon or not name or not path or not offset.isdigit():
        raise HistoryError("ref must look like SOURCE:FILE@OFFSET")
    return name, path, int(offset)


def make_ref(source: str, fid: str, offset: int) -> str:
    return f"{source}:{fid}@{offset}"


def _check_relative(relative: str) -> list[str]:
    pure = PurePosixPath(relative)
    parts = relative.split("/")
    if (
        pure.is_absolute()
        or "\\" in relative
        or "\0" in relative
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise HistoryError("path is outside the configured history roots")
    return parts


def _open_under(root: Path, parts: list[str]) -> int:
    """Open ``root/parts`` without following any symlink; return a file descriptor."""

    try:
        directory = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise _Refused("history source root is not a usable directory") from exc
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            except OSError as exc:
                raise _Refused("refusing to follow a symlink") from exc
            os.close(directory)
            directory = child
        try:
            descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
        except FileNotFoundError as exc:
            raise _Refused("transcript file not found") from exc
        except OSError as exc:
            raise _Refused("refusing to follow a symlink") from exc
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise _Refused("transcript path is not a regular file")
    return descriptor


def _pread(descriptor: int, offset: int, size: int) -> bytes:
    chunks: list[bytes] = []
    while size > 0:
        chunk = os.pread(descriptor, size, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _lines(descriptor: int, start: int) -> Iterator[tuple[int, bytes, int, bool]]:
    """Yield (offset, line, length, complete) from ``start``.

    A line longer than ``MAX_LINE_BYTES`` is yielded with empty bytes and its
    true length, so it is skipped without being held in memory or scanned.
    """

    chunk_size = 1024 * 1024
    offset = start
    buffer = b""
    skipping = 0
    position = start
    while True:
        chunk = os.pread(descriptor, chunk_size, position)
        if not chunk:
            if skipping:
                yield offset, b"", skipping + len(buffer), False
            elif buffer:
                yield offset, buffer, len(buffer), False
            return
        position += len(chunk)
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if not skipping and len(buffer) > MAX_LINE_BYTES:
                    skipping = len(buffer)
                    buffer = b""
                elif skipping:
                    skipping += len(buffer)
                    buffer = b""
                break
            if skipping:
                length = skipping + newline + 1
                yield offset, b"", length, True
                skipping = 0
            else:
                length = newline + 1
                line = buffer[:length]
                if length > MAX_LINE_BYTES:
                    yield offset, b"", length, True
                else:
                    yield offset, line, length, True
            offset += length
            buffer = buffer[newline + 1 :]


def _record_secret(line: str, record: Record) -> bool:
    """Scan the raw line, then every decoded string and metadata field.

    Decoded strings are joined with a NUL separator, so one scan covers them
    and no pattern can match across two separate strings.
    """

    if len(line) > MAX_SCAN_CHARS:
        return True
    if has_secret(line):
        return True
    joined = "\n\0\n".join((*record.strings, *record.metadata(), record.text))
    if len(joined) > 4 * MAX_SCAN_CHARS:
        return True
    return has_secret(joined)


class TranscriptIndex:
    """Derived SQLite index over transcript source roots."""

    def __init__(
        self,
        db: Path | str,
        sources: Iterable[SourceRoot],
        *,
        limits: HistoryLimits | None = None,
    ) -> None:
        self.db = Path(db)
        self.sources = {source.name: source for source in _unique(list(sources))}
        self.limits = limits or HistoryLimits()
        self._segment: Any = None

    # -- storage -----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None and row["value"] != SCHEMA_VERSION:
            # Derived data: an older layout is dropped and rebuilt.
            connection.executescript(
                "DROP TABLE IF EXISTS entries_fts; DROP TABLE IF EXISTS entries;"
                " DROP TABLE IF EXISTS files; DELETE FROM meta;"
            )
        tokenizer = "trigram" if _supports_trigram() else "unicode61"
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS files (
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                fid TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                dev INTEGER NOT NULL,
                entries INTEGER NOT NULL,
                skipped_secret INTEGER NOT NULL,
                skipped_other INTEGER NOT NULL,
                resume_offset INTEGER NOT NULL,
                head_digest TEXT NOT NULL,
                tail_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY (source, path),
                UNIQUE (source, fid)
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
        for key, value in (("schema_version", SCHEMA_VERSION), ("tokenizer", tokenizer)):
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
        return connection

    @staticmethod
    def _tokenizer(connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT value FROM meta WHERE key = 'tokenizer'").fetchone()
        return str(row["value"]) if row else "trigram"

    def _fts_text(self, text: str, tokenizer: str) -> str:
        """Stored FTS text: raw for trigram, jieba-segmented for unicode61 (as queries are)."""

        if tokenizer == "trigram":
            return text
        if self._segment is None:
            self._segment = jieba_segmenter()
        return " ".join(self._segment(text))

    # -- path safety --------------------------------------------------------------

    def _source(self, name: str) -> SourceRoot:
        source = self.sources.get(name)
        if source is None:
            raise HistoryError(f"unknown history source: {name}")
        return source

    def _walk(self, source: SourceRoot) -> Iterator[tuple[str, os.stat_result | None]]:
        """Yield (relative path, lstat); lstat is None for refused symlinks."""

        for directory, dirnames, filenames in os.walk(source.path, followlinks=False):
            dirnames.sort()
            base = Path(directory)
            for dirname in list(dirnames):
                if (base / dirname).is_symlink():
                    dirnames.remove(dirname)
                    yield (base / dirname).relative_to(source.path).as_posix(), None
            for filename in sorted(filenames):
                path = base / filename
                if path.suffix not in SUFFIXES:
                    continue
                relative = path.relative_to(source.path).as_posix()
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    yield relative, None
                elif stat.S_ISREG(info.st_mode):
                    yield relative, info

    # -- indexing -----------------------------------------------------------------

    def refresh(self, *, min_interval: float = 0.0) -> dict[str, Any]:
        """Incrementally index all sources.

        Unchanged files (size, mtime, ctime, inode) are skipped; a file that
        only grew is resumed from its last complete line. With
        ``min_interval``, a refresh within that many seconds of the last one is
        skipped. Concurrent refreshes are serialised by a lock file.
        """

        counts: dict[str, Any] = {
            "files_indexed": 0,
            "files_resumed": 0,
            "files_unchanged": 0,
            "files_removed": 0,
            "files_refused": 0,
            "symlinks_refused": 0,
            "sources_refused": 0,
            "entries_indexed": 0,
            "skipped_secret": 0,
            "skipped_other": 0,
            "limit_reached": None,
            "skipped_recent": False,
        }
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with _REFRESH_LOCK, FileLock(str(self.db) + ".lock", timeout=60):
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'last_refresh'"
                ).fetchone()
                now = time.time()
                if (
                    min_interval > 0
                    and row is not None
                    and now - float(row["value"]) < min_interval
                ):
                    counts["skipped_recent"] = True
                    return counts
                self._refresh(connection, counts)
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_refresh', ?)",
                    (str(time.time()),),
                )
        return counts

    def _refresh(self, connection: sqlite3.Connection, counts: dict[str, Any]) -> None:
        known = [row["source"] for row in connection.execute("SELECT DISTINCT source FROM files")]
        for stale in set(known) - set(self.sources):
            counts["files_removed"] += self._drop(connection, stale, None)
        for source in self.sources.values():
            if not source.usable():
                # The root became a symlink or vanished: drop what it had.
                counts["sources_refused"] += 1
                counts["files_removed"] += self._drop(connection, source.name, None)
                continue
            seen: set[str] = set()
            for relative, info in self._walk(source):
                if info is None:
                    counts["symlinks_refused"] += 1
                    continue
                seen.add(relative)
                if counts["limit_reached"]:
                    continue
                row = connection.execute(
                    "SELECT * FROM files WHERE source = ? AND path = ?", (source.name, relative)
                ).fetchone()
                if row and (
                    row["size"],
                    row["mtime_ns"],
                    row["ctime_ns"],
                    row["ino"],
                    row["dev"],
                ) == (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev):
                    counts["files_unchanged"] += 1
                    continue
                if row is None and self._file_count(connection) >= self.limits.max_files:
                    counts["limit_reached"] = "max_files"
                    continue
                try:
                    result = self._index_file(connection, source, relative, info, row)
                except _Refused:
                    counts["files_refused"] += 1
                    self._drop(connection, source.name, relative)
                    continue
                counts["files_resumed" if result["resumed"] else "files_indexed"] += 1
                for key in ("entries_indexed", "skipped_secret", "skipped_other"):
                    counts[key] += result[key]
                if result["limit"]:
                    counts["limit_reached"] = result["limit"]
                elif self.db.stat().st_size > self.limits.max_db_bytes:
                    counts["limit_reached"] = "max_db_bytes"
            indexed = {
                row["path"]
                for row in connection.execute(
                    "SELECT path FROM files WHERE source = ?", (source.name,)
                )
            }
            for gone in indexed - seen:
                counts["files_removed"] += self._drop(connection, source.name, gone)

    @staticmethod
    def _file_count(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    @staticmethod
    def _entry_count(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    @staticmethod
    def _drop(
        connection: sqlite3.Connection, source: str, path: str | None, from_offset: int = -1
    ) -> int:
        args: tuple[str, ...]
        if path is None:
            where, args = "source = ?", (source,)
        else:
            where, args = "source = ? AND path = ?", (source, path)
        if from_offset >= 0:
            connection.execute(
                "DELETE FROM entries_fts WHERE rowid IN (SELECT id FROM entries"
                f" WHERE {where} AND offset >= ?)",
                (*args, from_offset),
            )
            connection.execute(
                f"DELETE FROM entries WHERE {where} AND offset >= ?", (*args, from_offset)
            )
            return 0
        connection.execute(
            f"DELETE FROM entries_fts WHERE rowid IN (SELECT id FROM entries WHERE {where})", args
        )
        return connection.execute(f"DELETE FROM files WHERE {where}", args).rowcount

    @staticmethod
    def _span_digests(descriptor: int, end: int) -> tuple[str, str]:
        head = _pread(descriptor, 0, min(DIGEST_SPAN, end))
        tail_start = max(0, end - DIGEST_SPAN)
        tail = _pread(descriptor, tail_start, end - tail_start)
        return _digest(head), _digest(tail)

    def _index_file(
        self,
        connection: sqlite3.Connection,
        source: SourceRoot,
        relative: str,
        info: os.stat_result,
        previous: sqlite3.Row | None,
    ) -> dict[str, Any]:
        descriptor = _open_under(source.path, relative.split("/"))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise _Refused("file changed between walk and open")
            return self._index_open(connection, source, relative, descriptor, opened, previous)
        finally:
            os.close(descriptor)

    def _index_open(
        self,
        connection: sqlite3.Connection,
        source: SourceRoot,
        relative: str,
        descriptor: int,
        info: os.stat_result,
        previous: sqlite3.Row | None,
    ) -> dict[str, Any]:
        jsonl = SUFFIXES[PurePosixPath(relative).suffix]
        stem = PurePosixPath(relative).stem
        state: dict[str, Any] = {
            "session": "",
            "parent_session": "",
            "window_number": 0,
            "window_id": "w0",
            "parent_window": "",
            "previous_tail": "",
            "line": 0,
        }
        start = 0
        skipped_secret = skipped_other = 0
        resumed = False
        before_count = self._entry_count(connection)
        if (
            previous is not None
            and (previous["ino"], previous["dev"]) == (info.st_ino, info.st_dev)
            and info.st_size >= previous["resume_offset"]
            and (previous["head_digest"], previous["tail_digest"])
            == self._span_digests(descriptor, previous["resume_offset"])
        ):
            # Append-only growth: keep earlier entries, resume at the last complete line.
            resumed = True
            start = previous["resume_offset"]
            state.update(json.loads(previous["state"]))
            skipped_secret = previous["skipped_secret"]
            skipped_other = previous["skipped_other"]
            self._drop(connection, source.name, relative, from_offset=start)
        else:
            self._drop(connection, source.name, relative)
        fid = file_id(relative)
        connection.execute(
            "INSERT OR IGNORE INTO files (source, path, fid, size, mtime_ns, ctime_ns, ino, dev,"
            " entries, skipped_secret, skipped_other, resume_offset, head_digest, tail_digest,"
            " state) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', '', '{}')",
            (source.name, relative, fid),
        )
        tokenizer = self._tokenizer(connection)
        budget = self.limits.max_entries - self._entry_count(connection)
        limit: str | None = None
        resume_offset = start
        number = int(state["line"])
        previous_text = str(state["previous_tail"])
        for offset, raw, length, complete in _lines(descriptor, start):
            if budget <= 0:
                limit = "max_entries"
                break
            number += 1
            if complete:
                resume_offset = offset + length
            if not raw:
                if complete:
                    skipped_other += 1
                else:
                    number -= 1
                continue
            line = raw.decode("utf-8", errors="replace")
            record = parse_line(line, jsonl=jsonl)
            if record is None:
                if line.strip() and complete:
                    skipped_other += 1
                if not complete:
                    number -= 1
                continue
            if not complete:
                # A trailing line still being written: index it, but resume before it.
                resume_offset = offset
            # Parser state is persisted for resumes, so it only ever holds
            # redacted values.
            session_id = _field(record.session_id)
            parent_session_id = _field(record.parent_session_id)
            if session_id and session_id != state["session"]:
                state["session"] = session_id
                state["parent_session"] = parent_session_id
            elif parent_session_id and not state["parent_session"]:
                state["parent_session"] = parent_session_id
            if record.new_window:
                state["parent_window"] = state["window_id"]
                state["window_number"] += 1
                state["window_id"] = f"w{state['window_number']}"
            window_id = _field(record.window_id)
            if window_id and window_id != state["window_id"]:
                state["parent_window"] = _field(record.parent_window_id) or state["window_id"]
                state["window_id"] = window_id
            # Scanned: the raw line, every decoded string (keys, values, JSON
            # inside strings), the metadata fields, and the text joined to the
            # previous entry's tail (a key on one line, its value on the next).
            if _record_secret(line, record) or secret_continues(previous_text, record.text):
                skipped_secret += 1
                previous_text = ""
                continue
            if not record.text.strip():
                continue
            previous_text = record.text[-200:]
            row = (
                source.name,
                relative,
                offset,
                number,
                len(raw),
                _digest(raw),
                _field(state["session"] or stem),
                _field(state["parent_session"]),
                _field(state["window_id"]),
                _field(state["parent_window"]),
                _field(record.entry_id or f"L{number}"),
                _field(record.parent_entry_id),
                _field(record.role),
                _field(record.kind),
                _field(record.agent),
                _field(record.timestamp),
                _snippet(record.text),
            )
            cursor = connection.execute(
                "INSERT OR REPLACE INTO entries (source, path, offset, line, length, digest,"
                " session_id, parent_session_id, window_id, parent_window_id, entry_id,"
                " parent_entry_id, role, kind, agent, ts, snippet) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            connection.execute(
                "INSERT INTO entries_fts (rowid, snippet) VALUES (?, ?)",
                (cursor.lastrowid, self._fts_text(row[-1], tokenizer)),
            )
            budget -= 1
            if not complete:
                number -= 1
        state["line"] = number
        # Only a redacted tail is kept, for the split key/value check on resume.
        state["previous_tail"] = redact(previous_text)[-200:]
        head, tail = self._span_digests(descriptor, resume_offset)
        entries = int(
            connection.execute(
                "SELECT COUNT(*) FROM entries WHERE source = ? AND path = ?",
                (source.name, relative),
            ).fetchone()[0]
        )
        # When a limit stopped us early, record the size we got to, so the
        # next refresh sees a change and resumes from there.
        recorded_size = resume_offset if limit else info.st_size
        connection.execute(
            "UPDATE files SET size = ?, mtime_ns = ?, ctime_ns = ?, ino = ?, dev = ?, entries = ?,"
            " skipped_secret = ?, skipped_other = ?, resume_offset = ?, head_digest = ?,"
            " tail_digest = ?, state = ? WHERE source = ? AND path = ?",
            (
                recorded_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
                info.st_dev,
                entries,
                skipped_secret,
                skipped_other,
                resume_offset,
                head,
                tail,
                json.dumps(state),
                source.name,
                relative,
            ),
        )
        prior_secret = previous["skipped_secret"] if resumed and previous is not None else 0
        prior_other = previous["skipped_other"] if resumed and previous is not None else 0
        return {
            "entries_indexed": self._entry_count(connection) - before_count,
            "skipped_secret": skipped_secret - prior_secret,
            "skipped_other": skipped_other - prior_other,
            "resumed": resumed,
            "limit": limit,
        }

    # -- queries ------------------------------------------------------------------

    @staticmethod
    def _entry(row: sqlite3.Row, fid: str) -> dict[str, Any]:
        return {
            "ref": make_ref(row["source"], fid, row["offset"]),
            "source": row["source"],
            "path": redact_path(row["path"]),
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

    _SELECT = "SELECT e.*, f.fid AS fid FROM entries e JOIN files f USING (source, path)"

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
                    f"{self._SELECT} WHERE {where} ORDER BY e.source, e.path, e.offset LIMIT ?",
                    (*args, bounded),
                ).fetchall()
                entries = [self._entry(row, row["fid"]) for row in rows]
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
                "path": redact_path(row["path"]),
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
        fallback = False
        with self._connect() as connection:
            match = fts_match(text, self._tokenizer(connection))
            rows: list[sqlite3.Row] = []
            if match is not None:
                try:
                    rows = connection.execute(
                        "SELECT e.*, f.fid AS fid, bm25(entries_fts) AS score FROM entries_fts"
                        " JOIN entries e ON e.id = entries_fts.rowid"
                        " JOIN files f USING (source, path)"
                        f" WHERE entries_fts MATCH ? AND {where}"
                        " ORDER BY score, e.ts DESC LIMIT ?",
                        (match, *args, bounded),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                # Short terms (under three characters) cannot match a trigram
                # index. The substring fallback only scans the newest rows.
                fallback = True
                needle = (
                    "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                rows = connection.execute(
                    f"{self._SELECT.replace('e.*,', 'e.*, 0.0 AS score,')}"
                    " WHERE e.id IN (SELECT id FROM entries ORDER BY id DESC LIMIT ?)"
                    f" AND e.snippet LIKE ? ESCAPE '\\' AND {where} ORDER BY e.ts DESC LIMIT ?",
                    (LIKE_SCAN_ROWS, needle, *args, bounded),
                ).fetchall()
        results = []
        for row in rows:
            entry = self._entry(row, row["fid"])
            entry["score"] = round(-float(row["score"]), 6)
            results.append(entry)
        return {"query": query, "fallback": fallback, "results": results}

    def _file_row(self, connection: sqlite3.Connection, name: str, file: str) -> sqlite3.Row:
        if _FID.match(file):
            row = connection.execute(
                "SELECT * FROM files WHERE source = ? AND fid = ?", (name, file)
            ).fetchone()
        else:
            _check_relative(file)
            row = connection.execute(
                "SELECT * FROM files WHERE source = ? AND path = ?", (name, file)
            ).fetchone()
        if row is None:
            if not _FID.match(file):
                # Say why a path is refused (e.g. a symlink) instead of "not indexed".
                try:
                    os.close(_open_under(self._source(name).path, _check_relative(file)))
                except _Refused as exc:
                    raise HistoryError(str(exc)) from exc
            raise HistoryError("no indexed entry at this location")
        return cast(sqlite3.Row, row)

    def read(
        self,
        ref: str,
        *,
        before: int = 2,
        after: int = 2,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> dict[str, Any]:
        """Redacted, size-capped window of entries around a stored location."""

        name, file, offset = split_ref(ref)
        source = self._source(name)
        if not _FID.match(file):
            _check_relative(file)
        cap = max(256, min(int(max_bytes), MAX_READ_BYTES))
        before = max(0, min(int(before), MAX_CONTEXT))
        after = max(0, min(int(after), MAX_CONTEXT))
        with self._connect() as connection:
            file_row = self._file_row(connection, name, file)
            relative = str(file_row["path"])
            parts = _check_relative(relative)
            try:
                descriptor = _open_under(source.path, parts)
            except _Refused as exc:
                raise HistoryError(str(exc)) from exc
            try:
                opened = os.fstat(descriptor)
                if (opened.st_ino, opened.st_dev) != (file_row["ino"], file_row["dev"]):
                    self._drop(connection, name, relative)
                    raise HistoryError("transcript changed since indexing; re-index first")
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
                window = self._window(connection, descriptor, rows, offset, cap, file_row)
            finally:
                os.close(descriptor)
        window.update(
            {
                "ref": make_ref(name, file_row["fid"], offset),
                "session_id": anchor["session_id"],
                "window_id": anchor["window_id"],
            }
        )
        return window

    def _window(
        self,
        connection: sqlite3.Connection,
        descriptor: int,
        rows: Sequence[sqlite3.Row],
        offset: int,
        cap: int,
        file_row: sqlite3.Row,
    ) -> dict[str, Any]:
        jsonl = SUFFIXES.get(PurePosixPath(file_row["path"]).suffix, True)
        entries: list[dict[str, Any]] = []
        used = 0
        truncated = False
        previous_text = ""
        for row in rows:
            # Never read or scan more than one stored entry's length.
            length = min(int(row["length"]), MAX_LINE_BYTES)
            raw = _pread(descriptor, int(row["offset"]), length)
            if _digest(raw) != row["digest"]:
                # Drop the stale file so the next refresh re-indexes it.
                self._drop(connection, row["source"], row["path"])
                connection.commit()
                raise HistoryError("transcript changed since indexing; re-index first")
            line = raw.decode("utf-8", errors="replace")
            record = parse_line(line, jsonl=jsonl)
            text = record.text if record else ""
            if (
                record is None
                or _record_secret(line, record)
                or secret_continues(previous_text, text)
            ):
                previous_text = ""
                text = WITHHELD
            else:
                previous_text = text[-200:] if text.strip() else previous_text
                text = redact(text)
            room = cap - used
            encoded = text.encode("utf-8")
            if len(encoded) > room:
                text = encoded[: max(0, room)].decode("utf-8", errors="ignore")
                truncated = True
            used += len(text.encode("utf-8"))
            entry = self._entry(row, file_row["fid"])
            entry.pop("snippet")
            entry["text"] = text
            entry["anchor"] = row["offset"] == offset
            entries.append(entry)
            if used >= cap:
                truncated = truncated or row is not rows[-1]
                break
        return {"max_bytes": cap, "bytes": used, "truncated": truncated, "entries": entries}


def default_db(vault_root: Path) -> Path:
    return Path(vault_root) / DEFAULT_DB_NAME


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
