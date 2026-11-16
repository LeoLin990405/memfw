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
import logging
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..parse import _blocks, render

_LOG = logging.getLogger(__name__)

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
    origin: str = ""
    """Who drove the session, when the reader can tell from the store:
    ``automated`` or ``subagent`` (see :mod:`memfw.history.sources.labels`).
    Empty means "nothing recorded": the root's origin applies. Ingest applies
    the strongest value seen in a session to all of that session's turns."""
    parent_session: str = ""
    """Session id this one was spawned or forked from, when the store links them."""


@dataclass(frozen=True)
class SessionLocator:
    """Where one session lives. ``key`` is stable and relative to the reader root."""

    agent: str
    path: Path
    key: str
    session_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    fingerprint: str | None = field(default=None, compare=False, hash=False)
    """Optional change marker of this one session. When set, ingest compares it
    instead of the file's size/mtime/inode, so a store holding many sessions
    (one SQLite file) re-reads only the sessions whose marker changed. ``None``
    (the default) keeps the file-stat check."""


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
    session on every change and drop offsets no longer present. What counts
    as a change is the file's stat, or :attr:`SessionLocator.fingerprint`
    when the reader sets one."""

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        """Every session under ``root``; nothing when the layout is not recognised."""
        ...

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        """Records of one session with ``offset > after``, in offset order."""
        ...


# -- helpers shared by readers ---------------------------------------------------


class SessionTooLarge(Exception):
    """Raised by ``Reader.read`` when a session's file is over the size limit.

    Distinct from "nothing readable": ingest keeps what it stored before and
    does not retry the session until its file changes.
    """


def utc_timestamp(value: str) -> str:
    """ISO-8601 text with a UTC offset normalised to ``...Z``; anything else unchanged.

    Stored timestamps are compared as text, so ``2026-01-01T09:00:00+08:00``
    becomes ``2026-01-01T01:00:00Z``. Naive or unparseable values are returned
    stripped but otherwise as given (their zone is unknown).
    """

    stripped = value.strip() if isinstance(value, str) else ""
    if not stripped or stripped.endswith(("Z", "z")) and "+" not in stripped[10:]:
        return stripped[:-1] + "Z" if stripped.endswith("z") else stripped
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError:
        return stripped
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return stripped
    try:
        moment = parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return stripped
    out = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        if moment.microsecond % 1000 == 0:
            out += f".{moment.microsecond // 1000:03d}"
        else:
            out += f".{moment.microsecond:06d}"
    return out + "Z"


def iso_timestamp(value: Any) -> str:
    """ISO-8601 UTC for epoch seconds / milliseconds / nanoseconds, or a normalised string.

    Never raises: out-of-range numbers (``1e400``, huge integers) give ``""``.
    """

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds != seconds or seconds in (float("inf"), float("-inf")):
                return ""
            if seconds > 1e17:
                seconds /= 1e9
            elif seconds > 1e14:
                seconds /= 1e6
            elif seconds > 1e11:
                seconds /= 1e3
            return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.replace(".", "", 1).isdigit():
            try:
                return iso_timestamp(float(stripped))
            except (OverflowError, ValueError):
                return ""
        return utc_timestamp(stripped)
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


def read_bytes(path: Path, limit: int = MAX_FILE_BYTES, *, strict: bool = False) -> bytes:
    """Whole file (``O_NOFOLLOW``), or ``b""`` when missing, not regular, or over ``limit``.

    With ``strict`` a file over ``limit`` raises :class:`SessionTooLarge` instead,
    so a session reader can tell "too large" from "empty".
    """

    try:
        descriptor = os.open(path, _FILE_FLAGS)
    except OSError:
        return b""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return b""
        if info.st_size > limit:
            if strict:
                raise SessionTooLarge(f"{info.st_size} bytes > {limit}")
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


def read_json(path: Path, *, strict: bool = False) -> Any:
    """Parsed JSON file, or ``None`` when unreadable or invalid.

    With ``strict`` a file over the size limit raises :class:`SessionTooLarge`.
    """

    data = read_bytes(path, strict=strict)
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None


_BOM = b"\xef\xbb\xbf"


def iter_lines(chunks: Iterable[bytes]) -> Iterator[tuple[int, bytes]]:
    """``(byte offset, line)`` for each complete line of a byte stream given in chunks.

    The newline is not part of ``line``. A first line starting with a UTF-8
    BOM has it removed (its offset stays 0). The trailing line without a
    newline (still being written) is not yielded, nor are lines over
    ``MAX_LINE_BYTES`` (their bytes still count toward later offsets).
    Each chunk is scanned with a moving index, so splitting costs linear time.
    """

    tail = b""
    base = 0
    """Byte offset of ``tail[0]`` (of the next chunk when ``tail`` is empty)."""
    skipping = False
    for chunk in chunks:
        if not chunk:
            continue
        data = tail + chunk if tail else chunk
        index = 0
        while True:
            newline = data.find(b"\n", index)
            if newline < 0:
                break
            start = base + index
            if skipping:
                skipping = False
            elif newline - index <= MAX_LINE_BYTES:
                line = data[index:newline]
                if start == 0 and line.startswith(_BOM):
                    line = line[len(_BOM) :]
                yield start, line
            index = newline + 1
        if len(data) - index > MAX_LINE_BYTES:
            # An over-long line without its newline yet: drop what is buffered
            # and skip the rest of it when the newline arrives.
            skipping = True
            base += len(data)
            tail = b""
        else:
            tail = data[index:]
            base += index


def iter_jsonl(path: Path, *, after: int = -1) -> Iterator[tuple[int, dict[str, Any]]]:
    """``(byte offset, object)`` for each complete JSON-object line starting after ``after``.

    The trailing line without a newline (still being written) is not yielded.
    Lines over ``MAX_LINE_BYTES`` and lines that are not JSON objects are skipped.
    A UTF-8 BOM before the first line is ignored.
    """

    try:
        descriptor = os.open(path, _FILE_FLAGS)
    except OSError:
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return

        def chunks() -> Iterator[bytes]:
            position = 0
            while True:
                chunk = os.pread(descriptor, 1024 * 1024, position)
                if not chunk:
                    return
                position += len(chunk)
                yield chunk

        for start, line in iter_lines(chunks()):
            if start <= after or not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                yield start, value
    finally:
        os.close(descriptor)


def _relative(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` (no home directory in logs); the bare name otherwise."""

    try:
        return f"{root.name}/{path.relative_to(root)}"
    except ValueError:
        return path.name


def inside(root: Path, path: Path) -> bool:
    """True when ``path``, with every symlink resolved, lies under ``root`` (also resolved).

    Guards fixed sub-paths (``globalStorage/state.vscdb``, ``db/db.sqlite`` ...)
    whose intermediate directories could be symlinks pointing out of the root.
    """

    try:
        resolved = path.resolve()
        base = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved == base or resolved.is_relative_to(base):
        return True
    _LOG.warning(
        "skipping %s: it resolves outside the store root (a symlinked store directory "
        "is not read)",
        _relative(path, root),
    )
    return False


def open_sqlite_readonly(path: Path) -> sqlite3.Connection | None:
    """Read-only connection to a SQLite store, or ``None`` when it cannot be opened.

    ``mode=ro`` never creates or migrates the database. A store that is
    being written to is read as of its last commit. A WAL database whose
    ``-wal``/``-shm`` sidecars are gone (the App closed cleanly) would still
    get them created next to it by a ``mode=ro`` open, so when no sidecar
    (nor a rollback ``-journal``) exists it is opened ``immutable=1``, which
    touches nothing on disk.
    """

    if not regular_file(path):
        return None
    resolved = path.resolve()
    sidecars = ("-wal", "-shm", "-journal")
    quiet = not any(os.path.lexists(f"{resolved}{suffix}") for suffix in sidecars)
    query = "mode=ro&immutable=1" if quiet else "mode=ro"
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?{query}", uri=True, timeout=5)
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
