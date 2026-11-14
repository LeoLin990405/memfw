"""Cursor IDE (Cursor.app) conversation reader.

This is the desktop editor, not the ``cursor-agent`` CLI (see :mod:`.cursor`).
The app keeps its chat ("composer") data in VS Code style key/value SQLite
stores under ``~/Library/Application Support/Cursor/User`` on macOS
(``~/.config/Cursor/User`` on Linux):

* ``globalStorage/state.vscdb``, table ``cursorDiskKV(key TEXT UNIQUE, value
  BLOB)``, values are JSON:

  * ``composerData:<composerId>``: one per conversation. ``createdAt`` (epoch
    ms), ``modelConfig.modelName``, ``workspaceIdentifier`` (``{id, uri:
    {fsPath, path, scheme ...}}``) and the ordered message list, either inline
    as ``fullConversationHeadersOnly`` (``[{bubbleId, type, createdAt ...}]``)
    or, for longer chats, paged out to ``composerConvHeaders:<composerId>:
    <000000 ...>`` (each page a JSON list of the same headers). Very old
    versions inline whole bubbles as ``conversation``.
  * ``bubbleId:<composerId>:<bubbleId>``: one message. ``type`` 1 is a user
    message, 2 an assistant message; ``text``, ``createdAt`` (ISO-8601),
    ``modelInfo.modelName`` (on user turns), ``isSimulatedMsg`` (a message the
    app injected on the user's behalf), ``thinking`` (reasoning, not read) and
    ``toolFormerData`` (``{name, params | rawArgs, result, status}``) for a tool
    call and its output in the same bubble.

  Bubbles that no header references (rewound or edited turns) are not read.
  ``agentKv:blob:*`` (the agent runtime's content addressed state),
  ``checkpointId:*`` and the derived ``conversation-search.db`` index are not
  read either.
* ``workspaceStorage/<id>/workspace.json``: ``{"folder": "file:///..."}`` for
  the workspace ``<id>``; used for the working directory when the composer's
  ``workspaceIdentifier`` carries only the id. The per-workspace
  ``state.vscdb`` holds no conversation content in current versions.

Each header is one position ``i``: the bubble is reported at ``offset = 2 * i``
and, when it is a tool call with output, that output (role ``tool``) at
``2 * i + 1``. Offsets follow the app's own message order, so they stay put
while a chat grows; a rewind rewrites the list, which a full re-read
(``append_only = False``) reconciles. Every database is opened read-only;
a missing, locked or corrupt store, or a malformed value, is skipped.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from .base import (
    TOOL_OUTPUT_CHARS,
    ConversationRecord,
    SessionLocator,
    ToolCall,
    iso_timestamp,
    open_sqlite_readonly,
    read_json,
    regular_file,
    sqlite_tables,
    tool_call,
)

PARSER_VERSION = "1"

_AGENT = "cursor-ide"
_GLOBAL_DB = PurePosixPath("globalStorage/state.vscdb")
_WORKSPACES = "workspaceStorage"
_TABLE = "cursorDiskKV"
_COMPOSER = "composerData:"
_PAGES = "composerConvHeaders:"
_BUBBLE = "bubbleId:"
_MAX_VALUE_BYTES = 16 * 1024 * 1024
_MAX_PAGES = 10_000
_USER = 1
_ASSISTANT = 2
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=+-]{0,199}$")

log = logging.getLogger(__name__)


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _loads(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE_BYTES:
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _safe_id(value: Any) -> str:
    text = _str(value)
    return text if _SAFE_ID.match(text) and ".." not in text else ""


def _upper(prefix: str) -> str:
    """Exclusive upper bound of the keys starting with ``prefix`` (ends with ``:``)."""

    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _value(connection: sqlite3.Connection, key: str) -> Any:
    """Parsed JSON value of ``key``; ``None`` when absent, too large or malformed."""

    row = connection.execute(
        f"SELECT CASE WHEN length(value) > ? THEN NULL ELSE value END FROM {_TABLE} WHERE key = ?",
        (_MAX_VALUE_BYTES, key),
    ).fetchone()
    return _loads(row[0]) if row is not None else None


def _is_store(connection: sqlite3.Connection) -> bool:
    if _TABLE not in sqlite_tables(connection):
        return False
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info(`{_TABLE}`)")}
    return {"key", "value"} <= columns


# -- conversation layout ---------------------------------------------------------


def _headers(connection: sqlite3.Connection, composer: dict[str, Any], cid: str) -> list[Any]:
    """The composer's ordered message list: headers (dicts with ``bubbleId``) or bubbles."""

    inline = composer.get("fullConversationHeadersOnly")
    if isinstance(inline, list) and inline:
        return inline
    prefix = f"{_PAGES}{cid}:"
    pages: list[Any] = []
    rows = connection.execute(
        f"SELECT key FROM {_TABLE} WHERE key >= ? AND key < ? ORDER BY key LIMIT ?",
        (prefix, _upper(prefix), _MAX_PAGES),
    ).fetchall()
    for row in rows:
        page = _value(connection, str(row[0]))
        if not isinstance(page, list):
            log.debug("cursor-ide: skipping a malformed header page")
            continue
        pages.extend(page)
    if pages:
        return pages
    legacy = composer.get("conversation")
    return legacy if isinstance(legacy, list) else []


def _bubble(connection: sqlite3.Connection, cid: str, header: Any) -> dict[str, Any]:
    entry = _obj(header)
    if "type" in entry and ("text" in entry or "toolFormerData" in entry):
        return entry  # a legacy inline bubble
    bubble_id = _str(entry.get("bubbleId"))
    if not bubble_id:
        return {}
    return _obj(_value(connection, f"{_BUBBLE}{cid}:{bubble_id}"))


def _arguments(data: dict[str, Any]) -> Any:
    for field_name in ("params", "rawArgs"):
        raw = data.get(field_name)
        if isinstance(raw, str) and raw.strip():
            parsed = _loads(raw)
            return parsed if parsed is not None else raw
        if isinstance(raw, (dict, list)):
            return raw
    return None


def _tool(bubble: dict[str, Any]) -> tuple[tuple[ToolCall, ...], str]:
    """``(tool calls, tool output)`` of a bubble's ``toolFormerData``."""

    data = _obj(bubble.get("toolFormerData"))
    if not data:
        return (), ""
    name = _str(data.get("name")) or "tool"
    call = tool_call(name, _arguments(data))
    result = data.get("result")
    if isinstance(result, str):
        output = result
    elif result is None:
        output = ""
    else:
        output = json.dumps(result, ensure_ascii=False)
    return (call,), output.strip()[:TOOL_OUTPUT_CHARS]


def _workspace_path(identifier: Any, workspaces: Path) -> str:
    ident = _obj(identifier)
    uri = _obj(ident.get("uri"))
    if _str(uri.get("scheme")) in {"", "file"}:
        path = _str(uri.get("fsPath")) or _str(uri.get("path"))
        if path:
            return path
    workspace_id = _safe_id(ident.get("id"))
    if not workspace_id:
        return ""
    info = _obj(read_json(workspaces / workspace_id / "workspace.json"))
    folder = _str(info.get("folder"))
    parsed = urlparse(folder)
    if parsed.scheme == "file" and parsed.path:
        return unquote(parsed.path)
    return ""


def _project(cwd: str) -> str:
    return PurePosixPath(cwd).name if cwd not in {"", "/"} else ""


def _timestamp(bubble: dict[str, Any], header: dict[str, Any]) -> str:
    for value in (
        bubble.get("createdAt"),
        header.get("createdAt"),
        bubble.get("startedAtMs"),
        header.get("startedAtMs"),
    ):
        stamp = iso_timestamp(value)
        if stamp:
            return stamp
    return ""


# -- reader ----------------------------------------------------------------------


class CursorIdeReader:
    """Reads Cursor.app composer conversations from ``globalStorage/state.vscdb``."""

    agent = _AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (
        "Library/Application Support/Cursor/User",
        ".config/Cursor/User",
    )

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        database = root / _GLOBAL_DB
        if not regular_file(database):
            return
        connection = open_sqlite_readonly(database)
        if connection is None:
            log.debug("cursor-ide: global store cannot be opened (locked, missing or corrupt)")
            return
        found: list[str] = []
        with closing(connection):
            try:
                if not _is_store(connection):
                    log.debug("cursor-ide: global store has no %s table", _TABLE)
                    return
                for row in connection.execute(
                    f"SELECT key FROM {_TABLE} WHERE key >= ? AND key < ? ORDER BY key",
                    (_COMPOSER, _upper(_COMPOSER)),
                ):
                    cid = _safe_id(_str(row[0])[len(_COMPOSER) :])
                    if cid:
                        found.append(cid)
            except sqlite3.Error as error:
                log.debug("cursor-ide: listing composers failed: %s", error)
                return
        key = _GLOBAL_DB.as_posix()
        for cid in found:
            yield SessionLocator(_AGENT, database, f"{key}#{cid}", cid)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        cid = _safe_id(locator.session_id)
        if not cid or not regular_file(locator.path):
            return
        connection = open_sqlite_readonly(locator.path)
        if connection is None:
            log.debug("cursor-ide: store cannot be opened (locked, missing or corrupt)")
            return
        with closing(connection):
            try:
                records = list(self._records(connection, locator, cid, after))
            except sqlite3.Error as error:
                log.debug("cursor-ide: reading a composer failed: %s", error)
                return
        yield from records

    @staticmethod
    def _records(
        connection: sqlite3.Connection, locator: SessionLocator, cid: str, after: int
    ) -> Iterator[ConversationRecord]:
        if not _is_store(connection):
            return
        composer = _obj(_value(connection, f"{_COMPOSER}{cid}"))
        if not composer:
            log.debug("cursor-ide: composer value missing or malformed")
            return
        root = locator.path.parent.parent
        cwd = _workspace_path(composer.get("workspaceIdentifier"), root / _WORKSPACES)
        project = _project(cwd)
        model = _str(_obj(composer.get("modelConfig")).get("modelName")).strip()
        turn = 0
        for index, header in enumerate(_headers(connection, composer, cid)):
            bubble = _bubble(connection, cid, header)
            if not bubble:
                continue
            kind = bubble.get("type")
            picked = _str(_obj(bubble.get("modelInfo")).get("modelName")).strip()
            if picked:
                model = picked
            if kind == _USER:
                role = "system" if bubble.get("isSimulatedMsg") is True else "user"
            elif kind == _ASSISTANT:
                role = "assistant"
            else:
                continue
            text = _str(bubble.get("text")).strip()
            calls, output = _tool(bubble) if role == "assistant" else ((), "")
            timestamp = _timestamp(bubble, _obj(header))
            turns: list[tuple[int, str, str, tuple[ToolCall, ...]]] = []
            if text or calls:
                turns.append((2 * index, role, text, calls))
            if output:
                turns.append((2 * index + 1, "tool", output, ()))
            for offset, turn_role, turn_text, turn_calls in turns:
                if offset > after:
                    yield ConversationRecord(
                        agent=_AGENT,
                        session_id=cid,
                        offset=offset,
                        turn=turn,
                        role=turn_role,
                        text=turn_text,
                        timestamp=timestamp,
                        tool_calls=turn_calls,
                        cwd=cwd,
                        project=project,
                        model=model if turn_role != "tool" else "",
                    )
                turn += 1


READER = CursorIdeReader()
