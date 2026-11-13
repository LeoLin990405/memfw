"""ZCode CLI (Zhipu GLM) conversation reader.

Two local stores are understood; each root is recognised by its layout.

**ZCode app CLI** (root ``~/.zcode/cli``)::

    db/db.sqlite      tables session, message, part

``session`` rows carry ``id``, ``directory`` (the cwd), ``parent_id`` (set for
sub-agent sessions) and ``time_deleted``. ``message`` rows carry
``session_id``, ``sequence`` (0-based order inside the session),
``time_created`` (epoch ms) and a JSON ``data`` column with ``role``,
``time.created``, ``semantics.kind`` (``user_prompt``, ``assistant_response``,
``system_reminder`` ...), ``path.cwd`` / ``path.root`` (assistant) and
``contextSnapshot.envInfo.cwd`` (user). ``part`` rows hold the content of a
message, ordered by ``sequence``: ``text`` parts, ``reasoning`` parts (dropped),
``tool`` parts (``tool``, ``state.input``, ``state.output`` / ``state.error``)
and bookkeeping parts (``step-start``, ``timeline`` ...; ignored).

Every message ``i`` (row order inside its session) maps to offset ``2 * i``;
the tool results gathered from its ``tool`` parts become a separate ``tool``
record at ``2 * i + 1``. Runtime reminders (``system_reminder``,
``todo_reminder``) are boilerplate and skipped; other hidden runtime user
messages (background notifications, compaction summaries) become ``system``.

**Clean-room ``zcode-cli``** (root ``~/.zcode-cli/sessions``)::

    sess_<id>.json    {"version", "meta": {id, cwd, createdAt ...}, "messages": [...]}
    sess_<id>.jsonl   event log ({"ts", "type": "event", "event": {...}})

``messages`` are Anthropic-style ``{"role", "content"}`` values (a string or
``text`` / ``tool_use`` / ``tool_result`` blocks); the offset is the message
index. Messages have no timestamps of their own: the ``turn.started`` events of
the sibling ``.jsonl`` log line up one-to-one with the user's prompts, so each
message takes the start time of the turn it belongs to.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import (
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    iter_jsonl,
    open_sqlite_readonly,
    read_json,
    regular_file,
    relative_key,
    sqlite_tables,
    tool_call,
    walk_files,
)

PARSER_VERSION = "1"

AGENT = "glm"
DB_FILE = "db/db.sqlite"
SESSION_PREFIX = "sess_"
_TABLES = {"session", "message", "part"}
_SKIPPED_KINDS = {"system_reminder", "todo_reminder"}
_PROMPT_KINDS = {"user_prompt", ""}
_TOOL_RESULT_CHARS = 64_000  # the store redacts, then truncates


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _loads(value: Any) -> dict[str, Any]:
    """A JSON object column, or ``{}``."""

    if not isinstance(value, (str, bytes)):
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return _dict(parsed)


# -- ZCode app CLI (SQLite) -------------------------------------------------------


def _tool_output(state: dict[str, Any]) -> str:
    output = state.get("output")
    if isinstance(output, str) and output:
        return output[:_TOOL_RESULT_CHARS]
    error = state.get("error")
    if isinstance(error, str) and error:
        return error[:_TOOL_RESULT_CHARS]
    return ""


def _message_records(
    message: dict[str, Any],
    parts: list[dict[str, Any]],
    *,
    session_id: str,
    index: int,
    row_time: Any,
    cwd: str,
) -> list[ConversationRecord]:
    """Zero, one or two records (message, then its tool results) for one message row."""

    raw_role = _str(message.get("role"))
    kind = _str(_dict(message.get("semantics")).get("kind"))
    if kind in _SKIPPED_KINDS:
        return []
    if raw_role == "user":
        role = "user" if kind in _PROMPT_KINDS else "system"
        env = _dict(_dict(message.get("contextSnapshot")).get("envInfo"))
        cwd = _str(env.get("cwd")) or cwd
        project = ""
    elif raw_role == "assistant":
        role = "assistant"
        path = _dict(message.get("path"))
        cwd = _str(path.get("cwd")) or cwd
        project = _str(path.get("root"))
    elif raw_role == "system":
        role = "system"
        project = ""
    else:
        return []
    timestamp = iso_timestamp(_dict(message.get("time")).get("created")) or iso_timestamp(row_time)

    texts: list[str] = []
    calls: list[ToolCall] = []
    outputs: list[str] = []
    for part in parts:
        part_type = _str(part.get("type"))
        if part_type == "text":
            text = _str(part.get("text")).strip()
            if text:
                texts.append(text)
        elif part_type == "tool":
            state = _dict(part.get("state"))
            calls.append(tool_call(part.get("tool"), state.get("input")))
            output = _tool_output(state)
            if output:
                outputs.append(output)

    records: list[ConversationRecord] = []
    text = "\n".join(texts)
    if text or calls:
        records.append(
            ConversationRecord(
                agent=AGENT,
                session_id=session_id,
                offset=2 * index,
                turn=0,
                role=role,
                text=text,
                timestamp=timestamp,
                tool_calls=tuple(calls),
                cwd=cwd,
                project=project,
            )
        )
    if outputs:
        records.append(
            ConversationRecord(
                agent=AGENT,
                session_id=session_id,
                offset=2 * index + 1,
                turn=0,
                role="tool",
                text="\n".join(outputs),
                timestamp=timestamp,
                cwd=cwd,
                project=project,
            )
        )
    return records


def _sqlite_sessions(root: Path) -> Iterator[SessionLocator]:
    database = root / DB_FILE
    if not regular_file(database):
        return
    connection = open_sqlite_readonly(database)
    if connection is None:
        return
    try:
        if not _TABLES <= sqlite_tables(connection):
            return
        rows = connection.execute("SELECT * FROM session ORDER BY time_created, id").fetchall()
    except sqlite3.Error:
        return
    finally:
        connection.close()
    for row in rows:
        keys = row.keys()
        session_id = _str(row["id"]) if "id" in keys else ""
        if not session_id or ("time_deleted" in keys and row["time_deleted"] is not None):
            continue
        yield SessionLocator(
            agent=AGENT,
            path=database,
            key=f"{relative_key(root, database)}#{session_id}",
            session_id=session_id,
            extra={
                "store": "sqlite",
                "cwd": _str(row["directory"]) if "directory" in keys else "",
                "parent_id": _str(row["parent_id"]) if "parent_id" in keys else "",
            },
        )


def _sqlite_read(locator: SessionLocator, after: int) -> Iterator[ConversationRecord]:
    session_id = locator.session_id
    if not session_id:
        return
    cwd = _str(locator.extra.get("cwd"))
    connection = open_sqlite_readonly(locator.path)
    if connection is None:
        return
    try:
        messages = connection.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY sequence, time_created, id",
            (session_id,),
        ).fetchall()
        part_rows = connection.execute(
            "SELECT message_id, data FROM part WHERE session_id = ?"
            " ORDER BY message_id, sequence, time_created, id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return
    finally:
        connection.close()

    parts: dict[str, list[dict[str, Any]]] = {}
    for row in part_rows:
        data = _loads(row["data"])
        if data:
            parts.setdefault(_str(row["message_id"]), []).append(data)

    turn = 0
    for index, row in enumerate(messages):
        keys = row.keys()
        message = _loads(row["data"]) if "data" in keys else {}
        if not message:
            continue
        records = _message_records(
            message,
            parts.get(_str(row["id"]) if "id" in keys else "", []),
            session_id=session_id,
            index=index,
            row_time=row["time_created"] if "time_created" in keys else None,
            cwd=cwd,
        )
        for record in records:
            record.turn = turn
            turn += 1
            if record.offset > after:
                yield record


# -- clean-room zcode-cli (JSON snapshots) ------------------------------------------


def _turn_starts(log: Path) -> list[str]:
    """ISO start times of the ``turn.started`` events in a session event log."""

    starts: list[str] = []
    if not regular_file(log):
        return starts
    for _offset, line in iter_jsonl(log):
        event = _dict(line.get("event"))
        if event.get("type") != "turn.started":
            continue
        starts.append(iso_timestamp(event.get("timestamp")) or iso_timestamp(line.get("ts")))
    return starts


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _json_sessions(root: Path) -> Iterator[SessionLocator]:
    for path in walk_files(root, (".json",)):
        if not path.name.startswith(SESSION_PREFIX) or path.parent != root:
            continue
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        meta = _dict(data.get("meta"))
        session_id = _str(meta.get("id")).strip() or path.stem
        yield SessionLocator(
            agent=AGENT,
            path=path,
            key=relative_key(root, path),
            session_id=session_id,
            extra={"store": "json", "cwd": _str(meta.get("cwd"))},
        )


def _json_read(locator: SessionLocator, after: int) -> Iterator[ConversationRecord]:
    data = read_json(locator.path)
    if not isinstance(data, dict):
        return
    messages = data.get("messages")
    if not isinstance(messages, list):
        return
    meta = _dict(data.get("meta"))
    session_id = locator.session_id or _str(meta.get("id")) or locator.path.stem
    cwd = _str(meta.get("cwd")) or _str(locator.extra.get("cwd"))
    starts = _turn_starts(locator.path.with_suffix(".jsonl"))
    fallback = iso_timestamp(meta.get("createdAt"))
    prompt = -1
    turn = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        blocks = _blocks(message.get("content"))
        raw_role = _str(message.get("role"))
        texts = [block for block in blocks if block.get("type") == "text"]
        results = [block for block in blocks if block.get("type") == "tool_result"]
        uses = [block for block in blocks if block.get("type") == "tool_use"]
        if raw_role == "user" and texts:
            prompt += 1
        if raw_role == "user":
            role = "user" if texts or not results else "tool"
            text = content_text(texts) if texts else content_text(results)
        elif raw_role in {"assistant", "system"}:
            role = raw_role
            text = content_text(texts)
        else:
            continue
        calls = tuple(tool_call(block.get("name"), block.get("input")) for block in uses)
        text = text.strip()
        if not text and not calls:
            continue
        timestamp = starts[prompt] if 0 <= prompt < len(starts) else fallback
        record = ConversationRecord(
            agent=AGENT,
            session_id=session_id,
            offset=index,
            turn=turn,
            role=role,
            text=text,
            timestamp=timestamp,
            tool_calls=calls,
            cwd=cwd,
        )
        turn += 1
        if index > after:
            yield record


class GlmReader:
    """Reads ZCode CLI sessions (SQLite app store or ``zcode-cli`` JSON snapshots)."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (".zcode/cli", ".zcode-cli/sessions")

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        if not root.is_dir() or root.is_symlink():
            return
        yield from _sqlite_sessions(root)
        yield from _json_sessions(root)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        store = locator.extra.get("store")
        if store is None:
            store = "json" if locator.path.suffix == ".json" else "sqlite"
        try:
            if store == "sqlite":
                yield from _sqlite_read(locator, after)
            elif store == "json":
                yield from _json_read(locator, after)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
            return


READER = GlmReader()
