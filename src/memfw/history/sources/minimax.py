"""MiniMax Agent CLI conversation reader.

The MiniMax CLI (``minimax``) keeps one directory per session under
``~/.minimax/v2/sessions/<yyyy>/<mm>/<dd>/<time>-session_<b64>/``:

* ``manifest.json`` -- ``sessionId``, ``createdAtMs``, ``updatedAtMs``, ``layout``.
* ``messages.jsonl`` -- the append-only message ledger, one
  ``{"message_id", "turn_id", "message": {...}}`` object per line. ``message.role``
  is ``user``, ``assistant``, ``toolResult``, ``custom`` (injected reminders) or
  ``compactionSummary``; ``message.content`` is a string or a list of ``text`` /
  ``thinking`` / ``toolCall`` (``name``, ``arguments``) / ``image`` blocks, and
  ``message.timestamp`` is epoch milliseconds. Lines carrying ``history_artifact``
  are context re-materialised after a compaction, not new turns.

The working directory is not in the session directory; it lives in the
runtime's SQLite index (``~/.minimax/v2/sqlite/runtime-state.sqlite``, table
``local_runtime_sessions``: ``workspace_dir`` / ``project_workspace_dir``),
which is opened read-only and is optional.

The ``mmx`` CLI (``~/.mmx``) keeps no conversation store, so it has no reader.
"""

from __future__ import annotations

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

_MESSAGES = "messages.jsonl"
_MANIFEST = "manifest.json"
_INDEX = Path("sqlite") / "runtime-state.sqlite"
_SESSIONS_TABLE = "local_runtime_sessions"
_ROLES = {"user": "user", "assistant": "assistant", "toolResult": "tool", "custom": "system"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _workspaces(root: Path, session_id: str | None = None) -> dict[str, tuple[str, str]]:
    """``session_id -> (workspace_dir, project_workspace_dir)`` from the runtime index."""

    connection = open_sqlite_readonly(root.parent / _INDEX)
    if connection is None:
        return {}
    found: dict[str, tuple[str, str]] = {}
    try:
        if _SESSIONS_TABLE not in sqlite_tables(connection):
            return {}
        query = f"SELECT session_id, workspace_dir, project_workspace_dir FROM {_SESSIONS_TABLE}"
        if session_id is None:
            rows = connection.execute(query).fetchall()
        else:
            rows = connection.execute(query + " WHERE session_id = ?", (session_id,)).fetchall()
        for row in rows:
            key = _str(row[0])
            if key:
                found[key] = (_str(row[1]), _str(row[2]))
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return found


def _session_id(directory: Path) -> str:
    manifest = read_json(directory / _MANIFEST)
    if isinstance(manifest, dict):
        value = _str(manifest.get("sessionId")).strip()
        if value:
            return value
    return directory.name


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return content_text(blocks).strip()


def _tool_calls(content: Any) -> tuple[ToolCall, ...]:
    if not isinstance(content, list):
        return ()
    return tuple(
        tool_call(block.get("name"), block.get("arguments"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    )


class MiniMaxReader:
    """Reader for the MiniMax Agent CLI v2 session ledger."""

    agent = "minimax"
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".minimax/v2/sessions",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        workspaces: dict[str, tuple[str, str]] | None = None
        for path in walk_files(root, (_MESSAGES,)):
            if path.name != _MESSAGES:
                continue
            if workspaces is None:
                workspaces = _workspaces(root)
            session_id = _session_id(path.parent)
            cwd, project = workspaces.get(session_id, ("", ""))
            yield SessionLocator(
                agent=self.agent,
                path=path,
                key=relative_key(root, path),
                session_id=session_id,
                extra={"cwd": cwd, "project": project, "root": str(root)},
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        path = locator.path
        if not regular_file(path):
            return
        session_id = locator.session_id or _session_id(path.parent)
        cwd = _str(locator.extra.get("cwd"))
        project = _str(locator.extra.get("project"))
        if not cwd and not project:
            root = locator.extra.get("root")
            if isinstance(root, str) and root:
                cwd, project = _workspaces(Path(root), session_id).get(session_id, ("", ""))
        turn = 0
        for offset, line in iter_jsonl(path):
            record = self._record(line, session_id, offset, turn, cwd, project)
            if record is None:
                continue
            turn += 1
            if offset > after:
                yield record

    def _record(
        self,
        line: dict[str, Any],
        session_id: str,
        offset: int,
        turn: int,
        cwd: str,
        project: str,
    ) -> ConversationRecord | None:
        if "history_artifact" in line:
            return None
        message = line.get("message")
        if not isinstance(message, dict):
            return None
        raw_role = _str(message.get("role"))
        role = _ROLES.get(raw_role)
        if role is None:
            return None
        if raw_role == "custom" and message.get("display") is not True:
            return None
        content = message.get("content")
        text = _text(content)
        calls = _tool_calls(content) if role == "assistant" else ()
        if not text and not calls:
            return None
        return ConversationRecord(
            agent=self.agent,
            session_id=session_id,
            offset=offset,
            turn=turn,
            role=role,
            text=text,
            timestamp=iso_timestamp(message.get("timestamp")),
            tool_calls=calls,
            cwd=cwd,
            project=project,
        )


READER = MiniMaxReader()
