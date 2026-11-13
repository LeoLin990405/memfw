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
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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

PARSER_VERSION = "1"

CHAT_FILE = "chat_history.jsonl"
_SKIPPED_TYPES = frozenset({"system", "reasoning"})


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _session_meta(directory: Path) -> dict[str, Any]:
    summary = read_json(directory / "summary.json")
    return summary if isinstance(summary, dict) else {}


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
        project = _str(meta.get("git_root_dir"))
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
            )


READER = GrokReader()
