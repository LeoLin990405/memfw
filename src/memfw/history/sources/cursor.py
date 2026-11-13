"""cursor-agent CLI conversation reader.

cursor-agent keeps one SQLite store per chat under
``~/.cursor/chats/<workspace hash>/<chat id>/store.db``, next to a
``meta.json`` that holds the chat's working directory (``cwd``) and its
``createdAtMs`` / ``updatedAtMs``. The store is content addressed:

* ``blobs(id TEXT PRIMARY KEY, data BLOB)``: ``id`` is the hex digest of
  ``data``. Message blobs are JSON objects ``{"role", "content", "id"?,
  "providerOptions"?}`` (``system`` / ``user`` content is a string or ``text``
  blocks; ``assistant`` content holds ``text``, ``reasoning`` and
  ``tool-call`` blocks with ``toolName`` / ``args``; ``tool`` content holds
  ``tool-result`` blocks with ``toolName`` / ``result``). Other blobs are
  protobuf. A *root* blob lists the conversation's message blob ids, in
  order, as repeated field 1 (32 raw bytes each); every turn writes a new
  root, so later roots usually extend earlier ones.
* ``meta(key, value)``: one row whose value is hex-encoded JSON naming the
  ``latestRootBlobId``.

The session is the union of every root's message list, merged in root
insertion (rowid) order: a root that extends the previous one appends its new
tail; any other root (after a summary or a rewind) appends the messages that
were not seen before. ``offset`` is the message's index in that merged list,
so it stays stable while the chat grows. Messages carry no timestamps, so
``timestamp`` is empty. Reasoning blocks are excluded and tool output is
capped. A user message flagged ``providerOptions.cursor.isSummary`` (a
generated context summary) is reported with role ``system``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from .base import (
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    open_sqlite_readonly,
    read_json,
    regular_file,
    relative_key,
    sqlite_tables,
    tool_call,
    walk_files,
)

PARSER_VERSION = "1"

_AGENT = "cursor"
_STORE = "store.db"
_ROLES = {"user", "assistant", "tool", "system"}
_DIGEST_BYTES = 32
_MAX_BLOB_BYTES = 16 * 1024 * 1024
_MAX_TOOL_TEXT = 64_000  # the store redacts, then truncates


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def _is_json(data: bytes) -> bool:
    return data.lstrip()[:1] == b"{"


# -- minimal protobuf wire walker (stdlib only) ---------------------------------


def _varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if index >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, index


def _root_refs(data: bytes) -> list[str]:
    """Hex ids in repeated field 1 of a protobuf root blob; ``[]`` when not a root."""

    refs: list[str] = []
    index = 0
    try:
        while index < len(data):
            key, index = _varint(data, index)
            field_number, wire = key >> 3, key & 7
            if field_number == 0:
                return []
            if wire == 0:
                _, index = _varint(data, index)
            elif wire == 1:
                index += 8
            elif wire == 5:
                index += 4
            elif wire == 2:
                length, index = _varint(data, index)
                value = data[index : index + length]
                if len(value) != length:
                    return []
                index += length
                if field_number == 1:
                    if length != _DIGEST_BYTES:
                        return []
                    refs.append(value.hex())
            else:
                return []
    except ValueError:
        return []
    if index != len(data):
        return []
    return refs


# -- store layout ------------------------------------------------------------------


def _is_store(connection: sqlite3.Connection) -> bool:
    if "blobs" not in sqlite_tables(connection):
        return False
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(`blobs`)")}
    return {"id", "data"} <= columns


def _merged_ids(connection: sqlite3.Connection) -> list[str]:
    """Message blob ids of the whole chat, in stable conversation order."""

    json_ids: set[str] = set()
    roots: list[list[str]] = []
    for row in connection.execute("SELECT id, data FROM blobs ORDER BY rowid"):
        blob_id = row[0]
        data = _bytes(row[1])
        if not isinstance(blob_id, str) or not data or len(data) > _MAX_BLOB_BYTES:
            continue
        if _is_json(data):
            json_ids.add(blob_id)
            continue
        refs = _root_refs(data)
        if refs:
            roots.append(refs)
    merged: list[str] = []
    seen: set[str] = set()
    previous: list[str] = []
    for refs in roots:
        if not all(ref in json_ids for ref in refs):
            continue
        if refs[: len(previous)] == previous:
            new = refs[len(previous) :]
        else:
            common = 0
            for left, right in zip(previous, refs, strict=False):
                if left != right:
                    break
                common += 1
            new = [ref for ref in refs[common:] if ref not in seen]
        merged.extend(new)
        seen.update(new)
        previous = refs
    return merged


def _message(connection: sqlite3.Connection, blob_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT data FROM blobs WHERE id = ?", (blob_id,)).fetchone()
    if row is None:
        return {}
    data = _bytes(row[0])
    if not data or len(data) > _MAX_BLOB_BYTES:
        return {}
    try:
        return _obj(json.loads(data.decode("utf-8", errors="replace")))
    except (ValueError, RecursionError):
        return {}


def _turn(message: dict[str, Any]) -> tuple[str, str, tuple[ToolCall, ...]]:
    """``(role, text, tool calls)`` of one message blob."""

    role = _str(message.get("role"))
    if role not in _ROLES:
        return "", "", ()
    if _obj(_obj(message.get("providerOptions")).get("cursor")).get("isSummary") is True:
        role = "system"
    content = message.get("content")
    if isinstance(content, str):
        return role, content.strip(), ()
    if not isinstance(content, list):
        return role, "", ()
    texts: list[dict[str, Any]] = []
    calls: list[ToolCall] = []
    for block in content:
        if isinstance(block, str):
            texts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        kind = _str(block.get("type"))
        if kind == "text":
            texts.append({"type": "text", "text": _str(block.get("text"))})
        elif kind == "tool-call":
            calls.append(tool_call(block.get("toolName"), block.get("args")))
        elif kind == "tool-result":
            result = block.get("result")
            text = result if isinstance(result, str) else content_text(result)
            texts.append({"type": "text", "text": text[:_MAX_TOOL_TEXT]})
    return role, content_text(texts).strip(), tuple(calls)


def _chat_meta(store: Path) -> tuple[str, str]:
    cwd = _str(_obj(read_json(store.parent / "meta.json")).get("cwd")).strip()
    project = PurePosixPath(cwd).name if cwd not in {"", "/"} else ""
    return cwd, project


class CursorReader:
    """Reader for cursor-agent ``chats/<hash>/<id>/store.db`` stores."""

    agent = _AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (".cursor/chats",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (_STORE,)):
            if path.name != _STORE:
                continue
            key = relative_key(root, path)
            if len(PurePosixPath(key).parts) != 3:
                continue
            yield SessionLocator(agent=_AGENT, path=path, key=key, session_id=path.parent.name)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        if not regular_file(locator.path):
            return
        connection = open_sqlite_readonly(locator.path)
        if connection is None:
            return
        with closing(connection):
            try:
                if not _is_store(connection):
                    return
                ids = _merged_ids(connection)
            except sqlite3.Error:
                return
            cwd, project = _chat_meta(locator.path)
            session_id = locator.session_id or locator.path.parent.name
            turn = 0
            for offset, blob_id in enumerate(ids):
                try:
                    message = _message(connection, blob_id)
                except sqlite3.Error:
                    return
                role, text, calls = _turn(message)
                if not role or (not text and not calls):
                    continue
                if offset > after:
                    yield ConversationRecord(
                        agent=_AGENT,
                        session_id=session_id,
                        offset=offset,
                        turn=turn,
                        role=role,
                        text=text,
                        tool_calls=calls,
                        cwd=cwd,
                        project=project,
                    )
                turn += 1


READER = CursorReader()
