"""OpenCode CLI conversation reader.

OpenCode keeps its sessions under ``~/.local/share/opencode``. Three layouts
are recognised, all read-only:

* ``opencode*.db`` with ``session_v2`` / ``session_message`` tables (current):
  one row per message with ``type`` (``user``, ``assistant``, ``synthetic``,
  ``idle`` ...), a per-session ``seq`` and a JSON ``data`` column. User rows
  carry ``text``; assistant rows carry ``content`` blocks (``text``,
  ``reasoning``, ``tool`` with ``name`` and ``state.input``). The session row
  holds the working ``directory`` and ``project_id``.
* ``opencode*.db`` with ``session`` / ``message`` / ``part`` tables (older
  SQLite): message ``data`` holds ``role`` and ``time``; part ``data`` holds a
  typed part (``text``, ``reasoning``, ``tool`` ...).
* ``storage/session/**/<id>.json`` with ``storage/message/<session>/*.json``
  and ``storage/part/<message>/*.json`` (JSON file store).

One record per message. ``offset`` is ``seq`` for the current SQLite layout
and the message's position (ordered by creation time, then id) otherwise.
Reasoning blocks and tool output are not included.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from .base import (
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    open_sqlite_readonly,
    read_json,
    regular_file,
    relative_key,
    sqlite_tables,
    tool_call,
    walk_files,
)

PARSER_VERSION = "1"

_AGENT = "opencode"
_V2 = "v2"
_V1 = "v1"
_JSON = "json"
_ROLES = {"user", "assistant", "system", "tool"}
_MAX_DATA_CHARS = 16 * 1024 * 1024
_Row = tuple[int, str, str, tuple[ToolCall, ...], str]
"""``(offset, role, text, tool calls, timestamp)`` of one message."""


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str) or len(value) > _MAX_DATA_CHARS:
        return {}
    try:
        return _obj(json.loads(value))
    except (ValueError, RecursionError):
        return {}


def _created(data: dict[str, Any]) -> Any:
    return _obj(data.get("time")).get("created")


def _project_name(name: Any, worktree: Any) -> str:
    if isinstance(name, str) and name.strip():
        return name.strip()
    if isinstance(worktree, str) and worktree.strip() not in {"", "/"}:
        return PurePosixPath(worktree.strip()).name
    return ""


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info(`{table}`)").fetchall()
    return {str(row[1]) for row in rows}


def _has(connection: sqlite3.Connection, table: str, needed: set[str]) -> bool:
    return needed <= _columns(connection, table)


def _blocks_to_turn(blocks: Iterable[Any]) -> tuple[str, tuple[ToolCall, ...]]:
    """Text and tool calls of OpenCode content blocks / parts."""

    texts: list[dict[str, Any]] = []
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("ignored") is True:
            continue
        kind = _str(block.get("type"))
        if kind == "text":
            texts.append({"type": "text", "text": _str(block.get("text"))})
        elif kind == "tool":
            name = block.get("name") or block.get("tool")
            calls.append(tool_call(name, _obj(block.get("state")).get("input")))
    return content_text(texts).strip(), tuple(calls)


def _records(
    locator: SessionLocator,
    rows: Iterable[_Row],
    after: int,
    cwd: str,
    project: str,
) -> Iterator[ConversationRecord]:
    turn = 0
    for offset, role, text, calls, timestamp in rows:
        if role not in _ROLES or (not text and not calls):
            continue
        if offset > after:
            yield ConversationRecord(
                agent=_AGENT,
                session_id=locator.session_id,
                offset=offset,
                turn=turn,
                role=role,
                text=text,
                timestamp=timestamp,
                tool_calls=calls,
                cwd=cwd,
                project=project,
            )
        turn += 1


# -- current SQLite layout (session_v2 / session_message) ------------------------


def _v2_row(kind: str, seq: int, data: dict[str, Any], row_time: Any) -> _Row:
    timestamp = iso_timestamp(_created(data)) or iso_timestamp(row_time)
    if kind == "user":
        return seq, "user", _str(data.get("text")).strip(), (), timestamp
    if kind == "assistant":
        content = data.get("content")
        text, calls = _blocks_to_turn(content if isinstance(content, list) else ())
        return seq, "assistant", text, calls, timestamp
    if kind == "synthetic":
        metadata = _obj(data.get("metadata"))
        role = "tool" if "shellID" in metadata or "jobID" in metadata else "system"
        return seq, role, _str(data.get("text")).strip(), (), timestamp
    return seq, "", "", (), timestamp


def _v2_session(connection: sqlite3.Connection, session_id: str) -> tuple[str, str]:
    row = connection.execute(
        "SELECT directory, project_id FROM session_v2 WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return "", ""
    cwd = _str(row["directory"])
    project = ""
    if "project" in sqlite_tables(connection) and _has(
        connection, "project", {"id", "name", "worktree"}
    ):
        found = connection.execute(
            "SELECT name, worktree FROM project WHERE id = ?", (row["project_id"],)
        ).fetchone()
        if found is not None:
            project = _project_name(found["name"], found["worktree"])
    return cwd, project


def _read_v2(
    connection: sqlite3.Connection, locator: SessionLocator, after: int
) -> Iterator[ConversationRecord]:
    cwd, project = _v2_session(connection, locator.session_id)
    cursor = connection.execute(
        "SELECT type, seq, time_created, data FROM session_message "
        "WHERE session_id = ? ORDER BY seq",
        (locator.session_id,),
    )

    def rows() -> Iterator[_Row]:
        for row in cursor:
            seq = row["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                continue
            yield _v2_row(_str(row["type"]), seq, _loads(row["data"]), row["time_created"])

    yield from _records(locator, rows(), after, cwd, project)


# -- older SQLite layout (session / message / part) ------------------------------


def _read_v1(
    connection: sqlite3.Connection, locator: SessionLocator, after: int
) -> Iterator[ConversationRecord]:
    cwd = ""
    if _has(connection, "session", {"id", "directory"}):
        found = connection.execute(
            "SELECT directory FROM session WHERE id = ?", (locator.session_id,)
        ).fetchone()
        cwd = _str(found["directory"]) if found is not None else ""
    messages = connection.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created, id",
        (locator.session_id,),
    ).fetchall()
    parts: dict[str, list[dict[str, Any]]] = {}
    for part in connection.execute(
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY id",
        (locator.session_id,),
    ):
        parts.setdefault(_str(part["message_id"]), []).append(_loads(part["data"]))

    def rows() -> Iterator[_Row]:
        for index, message in enumerate(messages):
            data = _loads(message["data"])
            text, calls = _blocks_to_turn(parts.get(_str(message["id"]), ()))
            timestamp = iso_timestamp(_created(data)) or iso_timestamp(message["time_created"])
            yield index, _str(data.get("role")), text, calls, timestamp

    yield from _records(locator, rows(), after, cwd, "")


# -- JSON file store (storage/session, storage/message, storage/part) ------------


def _sorted_json(directory: Path) -> list[dict[str, Any]]:
    items: list[tuple[Any, str, dict[str, Any]]] = []
    if not directory.is_dir() or directory.is_symlink():
        return []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json" or not regular_file(path):
            continue
        data = _obj(read_json(path))
        if not data:
            continue
        created = _created(data)
        order = (
            created if isinstance(created, (int, float)) and not isinstance(created, bool) else 0
        )
        items.append((order, _str(data.get("id")) or path.stem, data))
    items.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in items]


def _read_json_store(locator: SessionLocator, after: int) -> Iterator[ConversationRecord]:
    storage = Path(str(locator.extra["storage"]))
    info = _obj(read_json(locator.path))
    cwd = _str(info.get("directory"))
    project = ""
    project_id = _str(info.get("projectID"))
    if project_id and "/" not in project_id and ".." not in project_id:
        found = _obj(read_json(storage / "project" / f"{project_id}.json"))
        project = _project_name(found.get("name"), found.get("worktree"))
    messages = _sorted_json(storage / "message" / locator.session_id)

    def rows() -> Iterator[_Row]:
        for index, message in enumerate(messages):
            message_id = _str(message.get("id"))
            safe = message_id and "/" not in message_id and ".." not in message_id
            parts = _sorted_json(storage / "part" / message_id) if safe else []
            text, calls = _blocks_to_turn(parts)
            yield index, _str(message.get("role")), text, calls, iso_timestamp(_created(message))

    message_cwd = ""
    for message in messages:
        message_cwd = _str(_obj(message.get("path")).get("cwd"))
        if message_cwd:
            break
    yield from _records(locator, rows(), after, cwd or message_cwd, project)


# -- reader ----------------------------------------------------------------------


class OpenCodeReader:
    """Reads OpenCode sessions from its SQLite database or JSON file store."""

    agent = _AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (".local/share/opencode",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        if not root.is_dir() or root.is_symlink():
            return
        for database in sorted(root.glob("opencode*.db")):
            if regular_file(database):
                yield from self._db_sessions(root, database)
        storage = root / "storage"
        for path in walk_files(storage / "session", (".json",)):
            info = _obj(read_json(path))
            session_id = _str(info.get("id")) or path.stem
            if session_id and "/" not in session_id and ".." not in session_id:
                yield SessionLocator(
                    _AGENT,
                    path,
                    relative_key(root, path),
                    session_id,
                    {"layout": _JSON, "storage": str(storage)},
                )

    def _db_sessions(self, root: Path, database: Path) -> Iterator[SessionLocator]:
        connection = open_sqlite_readonly(database)
        if connection is None:
            return
        key = relative_key(root, database)
        found: list[tuple[str, str]] = []
        with closing(connection):
            try:
                tables = sqlite_tables(connection)
                query = ""
                layout = ""
                if {"session_v2", "session_message"} <= tables and _has(
                    connection, "session_v2", {"id", "time_created"}
                ):
                    query = "SELECT id FROM session_v2 ORDER BY time_created, id"
                    layout = _V2
                elif {"session", "message", "part"} <= tables and _has(
                    connection, "session", {"id", "time_created"}
                ):
                    query = "SELECT id FROM session ORDER BY time_created, id"
                    layout = _V1
                if query:
                    for row in connection.execute(query):
                        session_id = row[0]
                        if isinstance(session_id, str) and session_id:
                            found.append((session_id, layout))
            except sqlite3.Error:
                return
        for session_id, layout in found:
            yield SessionLocator(
                _AGENT, database, f"{key}#{session_id}", session_id, {"layout": layout}
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        layout = locator.extra.get("layout", "")
        if not layout:
            layout = _JSON if locator.path.suffix == ".json" else self._detect(locator.path)
        if layout == _JSON:
            if "storage" not in locator.extra:
                return
            try:
                yield from _read_json_store(locator, after)
            except (OSError, ValueError, IndexError):
                return
            return
        if layout not in {_V1, _V2}:
            return
        connection = open_sqlite_readonly(locator.path)
        if connection is None:
            return
        with closing(connection):
            try:
                records = list(
                    _read_v2(connection, locator, after)
                    if layout == _V2
                    else _read_v1(connection, locator, after)
                )
            except sqlite3.Error:
                return
        yield from records

    @staticmethod
    def _detect(path: Path) -> str:
        connection = open_sqlite_readonly(path)
        if connection is None:
            return ""
        with closing(connection):
            try:
                tables = sqlite_tables(connection)
            except sqlite3.Error:
                return ""
        if {"session_v2", "session_message"} <= tables:
            return _V2
        if {"session", "message", "part"} <= tables:
            return _V1
        return ""


READER = OpenCodeReader()
