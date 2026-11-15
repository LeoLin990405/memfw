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
so it stays stable while the chat grows. Reasoning blocks are excluded and
tool output is capped. A user message flagged
``providerOptions.cursor.isSummary`` (a generated context summary) is
reported with role ``system``.

Session labels (see :func:`_session_labels` and :func:`_timestamps`):

* ``cwd``: ``meta.json`` ``cwd``; when ``meta.json`` is missing or has none
  (older chats), the ``Workspace Path:`` line of the first ``<user_info>``
  block in a user message. ``project`` is the cwd's basename.
* ``timestamp``: message blobs have no time field, but cursor-agent prefixes
  each user prompt with ``<timestamp>Tuesday, Nov 11, 2026, 9:05 PM
  (UTC-8)</timestamp>`` (minute precision, local time plus offset). A user
  turn takes its own tag; every later turn takes the latest tag before it.
  Turns before the first tag take the chat's creation time: ``meta.json``
  ``createdAtMs``, else the store meta's ``createdAt``, else (documented
  last resort) the ``store.db`` file mtime, which is the last write time.
* ``origin`` / ``parent_session``: the store meta's ``subagentInfo``
  (``parentAgentId``) marks a chat a ``task`` agent spawned: ``subagent``,
  with the parent chat's id. Nothing in the store tells a headless
  (``--print``) run from an interactive one (``mode`` is ``default`` and
  ``isRunEverything`` is a permission setting either way), so ``automated``
  is never set here; the ingest step's dispatch-prompt check covers Ticket
  workers.
* ``model``: the store meta's ``lastUsedModel`` when it names a model; the
  ``default`` / ``auto`` placeholders are not models and are left empty.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
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
from .labels import SUBAGENT

PARSER_VERSION = "2"

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


_TIMESTAMP_TAG = re.compile(r"<timestamp>\s*(.*?)\s*</timestamp>", re.S)
_TIMESTAMP_TEXT = re.compile(
    r"(?:[A-Za-z]+,\s*)?([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),\s*(\d{4}),\s*"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*"
    r"\(\s*(?:UTC|GMT)\s*(?:([+-])(\d{1,2})(?::?(\d{2}))?)?\s*\)"
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_USER_INFO = re.compile(r"<user_info>(.*?)</user_info>", re.S)
_WORKSPACE_PATH = re.compile(r"^[ \t]*Workspace Path:[ \t]*(\S[^\r\n]*?)[ \t]*$", re.M)
_MODEL_PLACEHOLDERS = {"", "default", "auto"}


@dataclass(frozen=True)
class _Labels:
    cwd: str = ""
    project: str = ""
    created: str = ""
    model: str = ""
    origin: str = ""
    parent_session: str = ""


def _store_meta(connection: sqlite3.Connection) -> dict[str, Any]:
    """The store's ``meta`` row: hex-encoded JSON (``agentId``, ``createdAt`` ...)."""

    try:
        rows = connection.execute("SELECT value FROM meta").fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        value = row[0]
        raw = value.decode("ascii", errors="replace") if isinstance(value, bytes) else value
        if not isinstance(raw, str):
            continue
        try:
            decoded = _obj(json.loads(bytes.fromhex(raw.strip()).decode("utf-8")))
        except (ValueError, RecursionError):
            continue
        if decoded:
            return decoded
    return {}


def _tag_timestamp(text: str) -> str:
    """ISO-8601 UTC of a user prompt's ``<timestamp>`` tag; ``""`` when absent or unparsable."""

    tag = _TIMESTAMP_TAG.search(text)
    if tag is None:
        return ""
    match = _TIMESTAMP_TEXT.search(tag.group(1))
    if match is None:
        return ""
    month_name, day, year, hour, minute, second, meridiem, sign, off_h, off_m = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return ""
    hours = int(hour)
    if meridiem:
        if not 1 <= hours <= 12:
            return ""
        hours = hours % 12 + (12 if meridiem.lower() == "pm" else 0)
    offset = timedelta(hours=int(off_h or 0), minutes=int(off_m or 0))
    if sign == "-":
        offset = -offset
    try:
        local = datetime(
            int(year),
            month,
            int(day),
            hours,
            int(minute),
            int(second or 0),
            tzinfo=timezone(offset),
        )
    except (ValueError, OverflowError):
        return ""
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _workspace_path(text: str) -> str:
    """``Workspace Path:`` of a ``<user_info>`` block in ``text``; ``""`` when none."""

    for block in _USER_INFO.finditer(text):
        match = _WORKSPACE_PATH.search(block.group(1))
        if match:
            return match.group(1).strip()
    return ""


def _mtime(path: Path) -> str:
    try:
        return iso_timestamp(path.stat().st_mtime)
    except OSError:
        return ""


def _session_labels(store: Path, meta: dict[str, Any], turns: list[tuple[str, str]]) -> _Labels:
    """Session-wide labels from ``meta.json``, the store meta and the user turns."""

    chat = _obj(read_json(store.parent / "meta.json"))
    # cwd: meta.json first; older chats have no meta.json, so fall back to the
    # Workspace Path the first user turn's <user_info> block records.
    cwd = _str(chat.get("cwd")).strip()
    if not cwd:
        for role, text in turns:
            if role == "user":
                cwd = _workspace_path(text)
                if cwd:
                    break
    project = PurePosixPath(cwd).name if cwd not in {"", "/"} else ""
    # Creation time for turns before the first <timestamp> tag: meta.json
    # createdAtMs, then the store meta's createdAt (epoch ms), then the store
    # file's mtime. The mtime is the last write, not the start: a fallback only.
    created = iso_timestamp(chat.get("createdAtMs")) or iso_timestamp(meta.get("createdAt"))
    if not created:
        created = _mtime(store)
    model = _str(meta.get("lastUsedModel")).strip()
    if model.lower() in _MODEL_PLACEHOLDERS:
        model = ""
    # subagentInfo is written only for chats a task agent spawned; its
    # parentAgentId is the parent chat's id (its directory name).
    subagent = _obj(meta.get("subagentInfo"))
    parent = _str(subagent.get("parentAgentId")).strip()
    origin = SUBAGENT if subagent else ""
    return _Labels(cwd, project, created, model, origin, parent)


def _timestamps(turns: list[tuple[str, str]], created: str) -> list[str]:
    """Per-turn timestamps: a user turn's tag, carried forward; ``created`` before the first."""

    out: list[str] = []
    current = created
    for role, text in turns:
        if role == "user":
            current = _tag_timestamp(text) or current
        out.append(current)
    return out


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
                meta = _store_meta(connection)
                # Every message is parsed up front: cwd and the carried
                # timestamp depend on turns that may lie before ``after``.
                parsed = [
                    (offset, _turn(_message(connection, blob_id)))
                    for offset, blob_id in enumerate(ids)
                ]
            except sqlite3.Error:
                return
            kept = [(offset, turn) for offset, turn in parsed if turn[0] and (turn[1] or turn[2])]
            texts = [(role, text) for _, (role, text, _) in kept]
            labels = _session_labels(locator.path, meta, texts)
            stamps = _timestamps(texts, labels.created)
            session_id = locator.session_id or locator.path.parent.name
            for turn, ((offset, (role, text, calls)), stamp) in enumerate(
                zip(kept, stamps, strict=True)
            ):
                if offset > after:
                    yield ConversationRecord(
                        agent=_AGENT,
                        session_id=session_id,
                        offset=offset,
                        turn=turn,
                        role=role,
                        text=text,
                        timestamp=stamp,
                        tool_calls=calls,
                        cwd=labels.cwd,
                        project=labels.project,
                        model=labels.model,
                        origin=labels.origin,
                        parent_session=labels.parent_session,
                    )


READER = CursorReader()
