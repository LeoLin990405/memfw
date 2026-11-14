"""Google Antigravity conversation reader (desktop app, IDE and ``agy`` CLI).

Every Antigravity surface runs the same agent server, which keeps an "app
data dir" under ``~/.gemini`` (``antigravity`` for the app, ``antigravity-cli``
for ``agy``, ``antigravity-ide`` for the IDE)::

    conversations/<conversation id>.pb          trajectory store (opaque, not read)
    implicit/<id>.pb                            opaque, not read
    conversation_summaries.db                   SQLite: one row per conversation
    brain/<conversation id>/                    task artifacts (markdown, media)
    brain/<conversation id>/.system_generated/logs/transcript.jsonl
    brain/<conversation id>/.system_generated/logs/transcript_full.jsonl

The ``.pb`` stores are not plain protobuf (they do not parse as protobuf wire
format and are near-random bytes) and are left alone. The transcripts are
plain JSON Lines the agent writes for itself: one object per trajectory
*step*, with ``step_index``, ``source`` (``USER_EXPLICIT``, ``USER_IMPLICIT``,
``MODEL``, ``SYSTEM`` ...), ``type`` (``USER_INPUT``, ``PLANNER_RESPONSE``,
``RUN_COMMAND``, ``VIEW_FILE`` ...), ``status``, ``created_at`` (ISO-8601),
``content``, ``thinking`` (dropped), ``tool_calls``, ``media`` and, in the
compact file only, ``truncated_fields``. Enum values may also carry their
``CORTEX_STEP_TYPE_`` / ``CORTEX_STEP_SOURCE_`` prefix.

``transcript.jsonl`` truncates large fields; ``transcript_full.jsonl`` does
not. Both are read and merged by ``step_index`` (the full line wins), so a
step whose full line is too large to read still comes through truncated.
``USER_INPUT`` steps are ``user`` (``system`` when the source is implicit),
``PLANNER_RESPONSE`` / ``NOTIFY_USER`` steps are ``assistant`` with their tool
calls, ``SYSTEM_MESSAGE`` / ``ERROR_MESSAGE`` steps are ``system``, context
bookkeeping steps are skipped, and every other step is a tool step whose
``content`` is its output (``tool``). The offset is ``step_index`` (the line
number when a step has none). ``conversation_summaries.db`` supplies the
workspace (``cwd``) and project of a conversation when it can be opened.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .base import (
    TOOL_OUTPUT_CHARS,
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    iter_jsonl,
    open_sqlite_readonly,
    regular_file,
    relative_key,
    sqlite_tables,
    tool_call,
)

PARSER_VERSION = "1"

AGENT = "antigravity"
BRAIN_DIR = "brain"
LOGS_DIR = Path(".system_generated") / "logs"
COMPACT_FILE = "transcript.jsonl"
FULL_FILE = "transcript_full.jsonl"
SUMMARIES_DB = "conversation_summaries.db"

_TYPE_PREFIX = "CORTEX_STEP_TYPE_"
_SOURCE_PREFIX = "CORTEX_STEP_SOURCE_"
_ASSISTANT_TYPES = frozenset({"PLANNER_RESPONSE", "NOTIFY_USER"})
_SYSTEM_TYPES = frozenset({"SYSTEM_MESSAGE", "ERROR_MESSAGE"})
_SKIPPED_TYPES = frozenset(
    {
        "CHECKPOINT",
        "CONVERSATION_HISTORY",
        "EPHEMERAL_MESSAGE",
        "KI_INSERTION",
        "DIRECTORY_RULES",
        "SUGGESTED_RESPONSES",
        "KNOWLEDGE_ARTIFACTS",
        "RETRIEVE_MEMORY",
        "UNSPECIFIED",
    }
)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _enum(value: Any, prefix: str) -> str:
    text = _str(value).strip().upper()
    return text[len(prefix) :] if text.startswith(prefix) else text


def _uri_path(uri: str) -> str:
    """Local path of a ``file://`` URI (other strings pass through)."""

    if uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri


def _workspaces(value: Any) -> list[str]:
    """Workspace paths of a ``workspace_uris`` column (a JSON list or separated URIs)."""

    if not isinstance(value, str) or not value.strip():
        return []
    items: list[Any]
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, str):
        items = [parsed]
    else:
        items = value.replace(",", "\n").split()
    return [_uri_path(item.strip()) for item in items if isinstance(item, str) and item.strip()]


def _summaries(root: Path) -> dict[str, dict[str, str]]:
    """``conversation_id -> {cwd, project, parent_id}`` from the summaries database."""

    database = root / SUMMARIES_DB
    if not regular_file(database):
        return {}
    connection = open_sqlite_readonly(database)
    if connection is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        if "conversation_summaries" not in sqlite_tables(connection):
            return {}
        for row in connection.execute("SELECT * FROM conversation_summaries"):
            keys = row.keys()
            conversation = _str(row["conversation_id"]) if "conversation_id" in keys else ""
            if not conversation:
                continue
            spaces = _workspaces(row["workspace_uris"]) if "workspace_uris" in keys else []
            out[conversation] = {
                "cwd": spaces[0] if spaces else "",
                "project": _str(row["project_id"]) if "project_id" in keys else "",
                "parent_id": (
                    _str(row["parent_conversation_id"]) if "parent_conversation_id" in keys else ""
                ),
            }
    except sqlite3.Error:
        return out
    finally:
        connection.close()
    return out


def _steps(logs: Path) -> list[tuple[int, dict[str, Any]]]:
    """``(offset, step)`` pairs of a conversation, compact lines overlaid by full ones."""

    merged: dict[int, dict[str, Any]] = {}
    for name in (COMPACT_FILE, FULL_FILE):
        path = logs / name
        if not regular_file(path):
            continue
        for line, (_position, step) in enumerate(iter_jsonl(path)):
            index = step.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                index = line
            merged[index] = step
    return sorted(merged.items())


def _tool_calls(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        source = function if isinstance(function, dict) else item
        arguments = next(
            (source[key] for key in ("args", "arguments", "input", "parameters") if key in source),
            None,
        )
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                pass
        calls.append(tool_call(source.get("name") or item.get("name"), arguments))
    return tuple(calls)


def _text(value: Any) -> str:
    if isinstance(value, (dict, list)) and not isinstance(value, str):
        rendered = content_text(value)
        return rendered if rendered.strip() else ""
    return _str(value)


class AntigravityReader:
    """Reads Antigravity ``brain/<id>/.system_generated/logs`` transcripts."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (
        ".gemini/antigravity",
        ".gemini/antigravity-cli",
        ".gemini/antigravity-ide",
    )

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        brain = root / BRAIN_DIR
        if not brain.is_dir() or brain.is_symlink():
            return
        meta = _summaries(root)
        for directory in sorted(brain.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            logs = directory / LOGS_DIR
            if logs.is_symlink() or (logs.parent).is_symlink() or not logs.is_dir():
                continue
            # The fingerprinted file: the full transcript grows with every step.
            path = next(
                (logs / name for name in (FULL_FILE, COMPACT_FILE) if regular_file(logs / name)),
                None,
            )
            if path is None:
                continue
            extra = {"cwd": "", "project": "", "parent_id": ""}
            extra.update(meta.get(directory.name, {}))
            yield SessionLocator(
                agent=self.agent,
                path=path,
                key=relative_key(root, path),
                session_id=directory.name,
                extra=extra,
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        logs = locator.path.parent
        session_id = locator.session_id or logs.parent.parent.name
        cwd = _str(locator.extra.get("cwd"))
        project = _str(locator.extra.get("project"))
        try:
            steps = _steps(logs)
        except (OSError, ValueError, TypeError, RecursionError):
            return
        turn = 0
        for offset, step in steps:
            kind = _enum(step.get("type"), _TYPE_PREFIX)
            source = _enum(step.get("source"), _SOURCE_PREFIX)
            if kind in _SKIPPED_TYPES:
                continue
            calls: tuple[ToolCall, ...] = ()
            if kind == "USER_INPUT":
                role = "user" if source in {"", "USER_EXPLICIT", "USER"} else "system"
            elif kind in _ASSISTANT_TYPES:
                role = "assistant"
                calls = _tool_calls(step.get("tool_calls"))
            elif kind in _SYSTEM_TYPES or (not kind and source.startswith("SYSTEM")):
                role = "system"
            elif kind:
                role = "tool"
            else:
                continue
            text = _text(step.get("content")).strip()
            if role == "tool":
                text = text[:TOOL_OUTPUT_CHARS]
                if not text:
                    continue
            if not text and not calls:
                continue
            record = ConversationRecord(
                agent=self.agent,
                session_id=session_id,
                offset=offset,
                turn=turn,
                role=role,
                text=text,
                timestamp=iso_timestamp(step.get("created_at")),
                tool_calls=calls,
                cwd=cwd,
                project=project,
                model=_str(step.get("model")) or _str(step.get("model_name")),
            )
            turn += 1
            if offset > after:
                yield record


READER = AntigravityReader()
