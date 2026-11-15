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

Labels read from the store (each optional; empty when not recorded):

* ``model`` -- ``message.model`` on assistant messages; other turns get the
  session model from ``llm-call.json`` (``model``), falling back to the index's
  ``extra_data_json.effectiveModel`` (provider prefix such as ``minimax/`` dropped).
* ``project`` -- basename of the index's ``project_workspace_dir``, falling back
  to the basename of ``workspace_dir`` (the cwd).
* ``origin`` -- ``subagent`` (with ``parent_session``) when the index row has a
  ``parent_session_id`` (explore / verifier / worker / background-task sessions
  a conversation spawned); ``automated`` when it has ``session_kind = 'cron'``
  or an ``origin_cron_id``. The store keeps no headless / print-mode marker
  (every turn is ingested with ``source = 'api'``), so nothing else is labelled.
* Injected user turns -- a ``user`` message carrying
  ``hostMetadata.backgroundTaskOrigin`` is a runtime-generated background-task
  notice, not typed text, and is emitted with role ``system``. The
  ``<system-reminder>`` the harness prepends to the first prompt shares one
  message with the typed text (``canonicalTextRange`` marks the typed part), so
  that message stays a ``user`` turn and ingest strips the reminder.

The ``mmx`` CLI (``~/.mmx``) keeps no conversation store, so it has no reader.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

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
from .labels import AUTOMATED, SUBAGENT

PARSER_VERSION = "2"

_MESSAGES = "messages.jsonl"
_MANIFEST = "manifest.json"
_LLM_CALL = "llm-call.json"
_INDEX = Path("sqlite") / "runtime-state.sqlite"
_SESSIONS_TABLE = "local_runtime_sessions"
_ROLES = {"user": "user", "assistant": "assistant", "toolResult": "tool", "custom": "system"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


class _Indexed(NamedTuple):
    """One ``local_runtime_sessions`` row, reduced to the labels the reader uses."""

    cwd: str = ""
    project_dir: str = ""
    model: str = ""
    origin: str = ""
    parent: str = ""


_OPTIONAL_COLUMNS = (
    "workspace_dir",
    "project_workspace_dir",
    "parent_session_id",
    "session_kind",
    "origin_cron_id",
    "extra_data_json",
)


def _effective_model(extra: Any) -> str:
    """``effectiveModel`` from the row's ``extra_data_json``, provider prefix dropped."""

    try:
        data = json.loads(extra) if isinstance(extra, str) and extra else None
    except (ValueError, RecursionError):
        return ""
    model = _str(data.get("effectiveModel")).strip() if isinstance(data, dict) else ""
    return model.rsplit("/", 1)[-1]


def _indexed(row: sqlite3.Row) -> _Indexed:
    keys = set(row.keys())

    def col(name: str) -> str:
        return _str(row[name]).strip() if name in keys else ""

    # Only explicit store fields label a session: a parent link means another
    # session spawned this one; a cron kind / cron origin means a scheduler did.
    parent = col("parent_session_id")
    if parent:
        origin = SUBAGENT
    elif col("session_kind") == "cron" or col("origin_cron_id"):
        origin = AUTOMATED
    else:
        origin = ""
    return _Indexed(
        cwd=col("workspace_dir"),
        project_dir=col("project_workspace_dir"),
        model=_effective_model(row["extra_data_json"] if "extra_data_json" in keys else ""),
        origin=origin,
        parent=parent,
    )


def _index(root: Path, session_id: str | None = None) -> dict[str, _Indexed]:
    """``session_id -> _Indexed`` from the runtime index (columns missing in older
    schemas read as empty)."""

    connection = open_sqlite_readonly(root.parent / _INDEX)
    if connection is None:
        return {}
    found: dict[str, _Indexed] = {}
    try:
        if _SESSIONS_TABLE not in sqlite_tables(connection):
            return {}
        connection.row_factory = sqlite3.Row
        present = {str(r[1]) for r in connection.execute(f"PRAGMA table_info({_SESSIONS_TABLE})")}
        columns = ["session_id", *(c for c in _OPTIONAL_COLUMNS if c in present)]
        query = f"SELECT {', '.join(columns)} FROM {_SESSIONS_TABLE}"
        if session_id is None:
            rows = connection.execute(query).fetchall()
        else:
            rows = connection.execute(query + " WHERE session_id = ?", (session_id,)).fetchall()
        for row in rows:
            key = _str(row["session_id"])
            if key:
                found[key] = _indexed(row)
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return found


def _project(path: str) -> str:
    return PurePosixPath(path).name if path not in {"", "/"} else ""


def _session_model(directory: Path) -> str:
    """The session's configured model from ``llm-call.json`` (absent in old sessions)."""

    data = read_json(directory / _LLM_CALL)
    return _str(data.get("model")).strip() if isinstance(data, dict) else ""


def _injected_user(message: dict[str, Any]) -> bool:
    """True for a ``user`` message the runtime generated (background-task notice)."""

    host = message.get("hostMetadata")
    return isinstance(host, dict) and isinstance(host.get("backgroundTaskOrigin"), dict)


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
        index: dict[str, _Indexed] | None = None
        for path in walk_files(root, (_MESSAGES,)):
            if path.name != _MESSAGES:
                continue
            if index is None:
                index = _index(root)
            session_id = _session_id(path.parent)
            row = index.get(session_id, _Indexed())
            yield SessionLocator(
                agent=self.agent,
                path=path,
                key=relative_key(root, path),
                session_id=session_id,
                extra={
                    "cwd": row.cwd,
                    "project": row.project_dir,
                    "model": row.model,
                    "origin": row.origin,
                    "parent": row.parent,
                    "root": str(root),
                },
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        path = locator.path
        if not regular_file(path):
            return
        session_id = locator.session_id or _session_id(path.parent)
        extra = locator.extra
        row = _Indexed(
            cwd=_str(extra.get("cwd")),
            project_dir=_str(extra.get("project")),
            model=_str(extra.get("model")),
            origin=_str(extra.get("origin")),
            parent=_str(extra.get("parent")),
        )
        if not row.cwd and not row.project_dir:
            root = extra.get("root")
            if isinstance(root, str) and root:
                row = _index(Path(root), session_id).get(session_id, row)
        # project: basename of the project workspace, else of the cwd.
        project = _project(row.project_dir) or _project(row.cwd)
        session = row._replace(project_dir=project, model=_session_model(path.parent) or row.model)
        turn = 0
        for offset, line in iter_jsonl(path):
            record = self._record(line, session_id, offset, turn, session)
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
        session: _Indexed,
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
        if raw_role == "user" and _injected_user(message):
            role = "system"
        content = message.get("content")
        text = _text(content)
        calls = _tool_calls(content) if role == "assistant" else ()
        if not text and not calls:
            return None
        # Assistant messages name the model that answered; other turns take
        # the session's model (llm-call.json, else the index).
        model = session.model
        if role == "assistant":
            model = _str(message.get("model")).strip() or model
        return ConversationRecord(
            agent=self.agent,
            session_id=session_id,
            offset=offset,
            turn=turn,
            role=role,
            text=text,
            timestamp=iso_timestamp(message.get("timestamp")),
            tool_calls=calls,
            cwd=session.cwd,
            project=session.project_dir,
            model=model,
            origin=session.origin,
            parent_session=session.parent,
        )


READER = MiniMaxReader()
