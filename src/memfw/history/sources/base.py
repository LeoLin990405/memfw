"""Shared contract for per-CLI conversation readers.

A reader knows one worker CLI's local session store. It finds sessions under
a root (``sessions``) and turns one session into normalised
:class:`ConversationRecord` values (``read``). Readers only read: they never
write to, lock, or migrate the store they look at. Text in a record is the
raw, unredacted value; redaction happens once, when the ingest step writes it
(see :mod:`memfw.history.conversations`).

``offset`` is a non-negative integer that grows with position inside one
session (a byte offset for JSONL, a message index or row order for JSON and
SQLite stores). Ingest resumes with ``read(locator, after=last_offset - 1)``
so the last record is re-read (it may still have been streaming) and
everything earlier is skipped; rows are keyed by (agent, session_id, offset).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..parse import _blocks, render

MAX_LINE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
TOOL_SUMMARY_CHARS = 4000
"""Readers keep this much of a tool rendering; the store redacts it, then cuts to 200."""
TOOL_OUTPUT_CHARS = 64_000
"""Readers keep this much of a tool output; the store redacts first, then truncates."""
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation, summarised: name plus a short rendering of its input."""

    name: str
    summary: str = ""


@dataclass
class ConversationRecord:
    """One normalised turn. ``text`` is unredacted and never stored as-is."""

    agent: str
    session_id: str
    offset: int
    turn: int
    role: str
    text: str
    timestamp: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    cwd: str = ""
    project: str = ""
    model: str = ""
    """Model that produced the turn, when the store records it."""


@dataclass(frozen=True)
class SessionLocator:
    """Where one session lives. ``key`` is stable and relative to the reader root."""

    agent: str
    path: Path
    key: str
    session_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class StoreRoot:
    """One root a reader is pointed at, and whether it is an interactive or Worker home."""

    path: Path
    origin: str = "interactive"

    @property
    def label(self) -> str:
        """Opaque, stable id of the root: locators never store the real path."""

        return hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:12]


class Reader(Protocol):
    """A worker CLI's conversation reader."""

    agent: str
    """Registry name (``kimi``, ``glm`` ...)."""
    default_roots: tuple[str, ...]
    """Store roots relative to the home directory, tried in order."""
    parser_version: str
    """Bumped when the reader's output for the same input changes."""
    append_only: bool
    """True when offsets are byte offsets of an append-only log. False (SQLite,
    message positions in a rewritten snapshot) makes ingest re-read the whole
    session on every change and drop offsets no longer present."""

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        """Every session under ``root``; nothing when the layout is not recognised."""
        ...

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        """Records of one session with ``offset > after``, in offset order."""
        ...


# -- helpers shared by readers ---------------------------------------------------


def iso_timestamp(value: Any) -> str:
    """ISO-8601 UTC for epoch seconds / milliseconds / nanoseconds, or a passthrough string."""

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e17:
            seconds /= 1e9
        elif seconds > 1e14:
            seconds /= 1e6
        elif seconds > 1e11:
            seconds /= 1e3
        try:
            return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.replace(".", "", 1).isdigit():
            try:
                return iso_timestamp(float(stripped))
            except ValueError:
                return ""
        return stripped
    return ""


def content_text(content: Any) -> str:
    """Text of a message content value: a string or a list of typed blocks."""

    return _blocks(content)


def tool_call(name: Any, arguments: Any = None) -> ToolCall:
    """A :class:`ToolCall` with a capped, decoded rendering of the arguments."""

    label = name if isinstance(name, str) and name else "tool"
    summary = render(arguments) if arguments is not None else ""
    if len(summary) > TOOL_SUMMARY_CHARS:
        summary = summary[: TOOL_SUMMARY_CHARS - 1] + "…"
    return ToolCall(label, summary)


def regular_file(path: Path) -> bool:
    """True for a regular file that is not a symlink."""

    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def walk_files(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Regular files under ``root`` with one of ``suffixes``; symlinks are never followed."""

    if not root.is_dir() or root.is_symlink():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if not (base / name).is_symlink())
        for filename in sorted(filenames):
            path = base / filename
            if filename.endswith(suffixes) and regular_file(path):
                yield path


def read_bytes(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Whole file (``O_NOFOLLOW``), or ``b""`` when missing, not regular, or over ``limit``."""

    try:
        descriptor = os.open(path, _FILE_FLAGS)
    except OSError:
        return b""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> Any:
    """Parsed JSON file, or ``None`` when unreadable or invalid."""

    data = read_bytes(path)
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None


def iter_jsonl(path: Path, *, after: int = -1) -> Iterator[tuple[int, dict[str, Any]]]:
    """``(byte offset, object)`` for each complete JSON-object line starting after ``after``.

    The trailing line without a newline (still being written) is not yielded.
    Lines over ``MAX_LINE_BYTES`` and lines that are not JSON objects are skipped.
    """

    try:
        descriptor = os.open(path, _FILE_FLAGS)
    except OSError:
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return
        position = 0
        buffer = b""
        offset = 0
        skipping = False
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, position)
            if not chunk:
                return
            position += len(chunk)
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > MAX_LINE_BYTES:
                        skipping = True
                        offset += len(buffer)
                        buffer = b""
                    break
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                start = offset
                offset += newline + 1
                if skipping:
                    skipping = False
                    continue
                if start <= after or not line.strip() or len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    value = json.loads(line.decode("utf-8", errors="replace"))
                except (ValueError, RecursionError):
                    continue
                if isinstance(value, dict):
                    yield start, value
    finally:
        os.close(descriptor)


def open_sqlite_readonly(path: Path) -> sqlite3.Connection | None:
    """Read-only connection to a SQLite store, or ``None`` when it cannot be opened.

    ``mode=ro`` never creates or migrates the database. A store that is
    being written to is read as of its last commit.
    """

    if not regular_file(path):
        return None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error:
        return None
    return connection


def sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    """Names of the tables in a database."""

    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def relative_key(root: Path, path: Path) -> str:
    """POSIX path of ``path`` relative to ``root`` (the locator key)."""

    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name
