"""Grok CLI conversation reader.

Grok keeps one directory per session under ``~/.grok/sessions``::

    <percent-encoded cwd>/<session id>/chat_history.jsonl
    <percent-encoded cwd>/<session id>/summary.json
    <percent-encoded cwd>/<session id>/events.jsonl

``chat_history.jsonl`` holds the model conversation, one typed object per line:
``system`` (the CLI system prompt, skipped), ``user`` (``content`` is a list of
text blocks; ``synthetic_reason`` marks text the CLI injected, read as
``system``), ``assistant`` (``content`` string plus ``tool_calls`` of
``{id, name, arguments}``), ``tool_result`` (``tool_call_id``, string
``content``), ``reasoning`` (skipped) and ``backend_tool_call`` (server-side
tools, ``kind.tool_type`` / ``kind.action``).

Lines carry no timestamps. ``summary.json`` gives the session's ``cwd``, git
root and ``created_at``; ``events.jsonl`` gives ``tool_completed`` times by
``tool_call_id`` and ``turn_started`` times by conversation message index.
A record without its own time takes the last known one, starting at
``created_at``.

Labels, all from ``summary.json`` (checked on a real store):

* ``project`` is the basename of ``info.cwd`` (the convention of the other
  readers); without a ``summary.json`` the cwd falls back to the decoded
  project directory name.
* ``model`` is the assistant line's ``model_id``; other lines (and assistant
  lines without one) take the last ``model_id`` seen, starting at the
  session's ``current_model_id``.
* ``origin``: ``session_kind`` is ``headless`` for ``grok -p`` / headless
  runs (``automated``) and ``subagent`` / ``subagent_fork`` /
  ``subagent_resume`` for sessions a parent session spawned (``subagent``).
  Interactive TUI sessions carry no ``session_kind`` and stay unlabelled.
  Goal mode (a ``goal/`` directory) is a person's interactive session whose
  worker rounds are the ``subagent`` sessions, so it is not labelled itself.
* ``parent_session`` is ``parent_session_id`` (present on forks and resumes);
  otherwise the parent's ``subagents/<child id>/meta.json``, whose
  ``child_session_id`` names this session and ``parent_session_id`` the
  parent. A child may live under another cwd directory than its parent, so
  every session directory under the root is checked.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from .base import (
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    iter_jsonl,
    read_json,
    relative_key,
    tool_call,
    walk_files,
)
from .labels import AUTOMATED, SUBAGENT

PARSER_VERSION = "2"

CHAT_FILE = "chat_history.jsonl"
_SKIPPED_TYPES = frozenset({"system", "reasoning"})
_KIND_ORIGIN = {
    "headless": AUTOMATED,
    "subagent": SUBAGENT,
    "subagent_fork": SUBAGENT,
    "subagent_resume": SUBAGENT,
}
"""``summary.json`` ``session_kind`` values that say who drove the session.
Unknown or missing kinds (interactive sessions have none) leave it unset."""


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _session_meta(directory: Path) -> dict[str, Any]:
    summary = read_json(directory / "summary.json")
    return summary if isinstance(summary, dict) else {}


def _project(cwd: str) -> str:
    return PurePosixPath(cwd).name if cwd not in {"", "/"} else ""


def _subagent_parent(root: Path, session_id: str) -> str:
    """Parent id from a ``<parent dir>/subagents/<session_id>/meta.json`` under ``root``."""

    if not session_id or not root.is_dir():
        return ""
    for meta_path in root.glob(f"*/*/subagents/{session_id}/meta.json"):
        meta = read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        child = _str(meta.get("child_session_id")) or _str(meta.get("subagent_id"))
        parent = _str(meta.get("parent_session_id")) or meta_path.parent.parent.parent.name
        if child == session_id and parent and parent != session_id:
            return parent
    return ""


def _event_times(directory: Path) -> tuple[dict[str, str], dict[int, str]]:
    """``tool_call_id -> time`` and ``message index -> turn start time``."""

    by_call: dict[str, str] = {}
    by_index: dict[int, str] = {}
    for _offset, event in iter_jsonl(directory / "events.jsonl"):
        stamp = iso_timestamp(event.get("ts"))
        if not stamp:
            continue
        kind = event.get("type")
        if kind == "tool_completed":
            call_id = _str(event.get("tool_call_id"))
            if call_id:
                by_call.setdefault(call_id, stamp)
        elif kind == "turn_started":
            index = event.get("conversation_message_count")
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                by_index.setdefault(index, stamp)
    return by_call, by_index


def _tool_calls(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls: list[ToolCall] = []
    for item in value:
        if isinstance(item, dict):
            calls.append(tool_call(item.get("name"), item.get("arguments")))
    return tuple(calls)


def _backend_call(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, dict):
        return ()
    name = _str(value.get("tool_type")) or "backend_tool"
    return (tool_call(name, value.get("action")),)


class GrokReader:
    """Reads Grok CLI ``chat_history.jsonl`` session files."""

    agent = "grok"
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".grok/sessions",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (CHAT_FILE,)):
            if path.name != CHAT_FILE or path.parent == root:
                continue
            yield SessionLocator(
                agent=self.agent,
                path=path,
                key=relative_key(root, path),
                session_id=path.parent.name,
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        directory = locator.path.parent
        session_id = locator.session_id or directory.name
        meta = _session_meta(directory)
        info = meta.get("info")
        info = info if isinstance(info, dict) else {}
        cwd = _str(info.get("cwd")) or unquote(directory.parent.name)
        project = _project(cwd)
        origin = _KIND_ORIGIN.get(_str(meta.get("session_kind")), "")
        parent = _str(meta.get("parent_session_id"))
        if origin == SUBAGENT and not parent:
            # session dir -> percent-encoded cwd dir -> store root
            parent = _subagent_parent(directory.parent.parent, session_id)
        model = _str(meta.get("current_model_id"))
        by_call, by_index = _event_times(directory)
        last_time = iso_timestamp(meta.get("created_at"))

        turn = 0
        for index, (offset, entry) in enumerate(iter_jsonl(locator.path)):
            kind = entry.get("type")
            role = ""
            text = ""
            calls: tuple[ToolCall, ...] = ()
            stamp = ""
            if kind == "user":
                role = "system" if entry.get("synthetic_reason") else "user"
                text = content_text(entry.get("content"))
                stamp = by_index.get(index, "")
            elif kind == "assistant":
                role = "assistant"
                text = content_text(entry.get("content"))
                calls = _tool_calls(entry.get("tool_calls"))
                model = _str(entry.get("model_id")) or model
            elif kind == "tool_result":
                role = "tool"
                text = content_text(entry.get("content"))
                stamp = by_call.get(_str(entry.get("tool_call_id")), "")
            elif kind == "backend_tool_call":
                role = "assistant"
                calls = _backend_call(entry.get("kind"))
            elif kind in _SKIPPED_TYPES:
                continue
            if stamp:
                last_time = stamp
            if not role or (not text.strip() and not calls):
                continue
            current = turn
            turn += 1
            if offset <= after:
                continue
            yield ConversationRecord(
                agent=self.agent,
                session_id=session_id,
                offset=offset,
                turn=current,
                role=role,
                text=text,
                timestamp=last_time,
                tool_calls=calls,
                cwd=cwd,
                project=project,
                model=model,
                origin=origin,
                parent_session=parent,
            )


READER = GrokReader()
