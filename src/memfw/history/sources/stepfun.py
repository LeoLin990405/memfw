"""StepFun ``stepcode`` CLI conversation reader.

Sessions live under ``~/.stepcode/agent/sessions/<encoded-cwd>/`` as one JSONL
file per session (``<timestamp>_<id>.jsonl``; subagent runs use
``<timestamp>_subagent-<id>.jsonl``). The first line is a ``session`` header
(``id``, ``cwd``, ``timestamp``, optional ``parentSessionId``); later lines are
entries with a ``type``. Conversation turns are ``message`` entries whose
``message.role`` is ``user``, ``assistant`` or ``toolResult``; assistant
content is a list of ``text`` / ``thinking`` / ``toolCall`` blocks.
``custom_message`` entries are extension-injected prompts. Other entry types
(model / thinking-level changes, ``custom`` state, ``compaction``) carry no
conversation turn and are skipped. ``tool-output/*.txt`` side files are not read.
"""

from __future__ import annotations

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
    relative_key,
    tool_call,
    walk_files,
)

PARSER_VERSION = "1"

_ROLES = {"user": "user", "assistant": "assistant", "toolResult": "tool"}
_SKIPPED_BLOCKS = {"toolCall", "thinking", "redacted_thinking", "reasoning", "image"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _header(path: Path) -> dict[str, Any] | None:
    for _offset, entry in iter_jsonl(path):
        return entry if entry.get("type") == "session" else None
    return None


def _session_id_from_name(path: Path) -> str:
    stem = path.stem
    return stem.split("_", 1)[1] if "_" in stem else stem


def _text(content: Any) -> str:
    if isinstance(content, list):
        content = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") in _SKIPPED_BLOCKS)
        ]
    return content_text(content).strip()


def _tool_calls(content: Any) -> tuple[ToolCall, ...]:
    if not isinstance(content, list):
        return ()
    return tuple(
        tool_call(block.get("name"), block.get("arguments"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    )


def _turn(entry: dict[str, Any]) -> tuple[str, str, tuple[ToolCall, ...], Any] | None:
    """``(role, text, tool_calls, fallback timestamp)`` for a conversational entry."""

    kind = entry.get("type")
    if kind == "custom_message":
        return "system", _text(entry.get("content")), (), None
    if kind != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    raw_role = _str(message.get("role"))
    content = message.get("content")
    stamp = message.get("timestamp")
    if raw_role == "bashExecution":
        call = tool_call("bash", {"command": message.get("command")})
        return "tool", _str(message.get("output")).strip(), (call,), stamp
    role = _ROLES.get(raw_role)
    if role is None:
        return None
    calls = _tool_calls(content) if role == "assistant" else ()
    return role, _text(content), calls, stamp


class StepfunReader:
    """Reader for StepFun ``stepcode`` JSONL session files."""

    agent = "stepfun"
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".stepcode/agent/sessions",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (".jsonl",)):
            header = _header(path)
            if header is None:
                continue
            session_id = _str(header.get("id")) or _session_id_from_name(path)
            extra: dict[str, Any] = {}
            parent = _str(header.get("parentSessionId"))
            if parent:
                extra["parent_session_id"] = parent
            yield SessionLocator(
                self.agent,
                path,
                key=relative_key(root, path),
                session_id=session_id,
                extra=extra,
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        session_id = locator.session_id or _session_id_from_name(locator.path)
        cwd = ""
        turn = 0
        first = True
        for offset, entry in iter_jsonl(locator.path):
            if first:
                first = False
                if entry.get("type") != "session":
                    return
                session_id = locator.session_id or _str(entry.get("id")) or session_id
                cwd = _str(entry.get("cwd"))
                continue
            try:
                parsed = _turn(entry)
            except Exception:  # malformed entry: skip, never raise
                continue
            if parsed is None:
                continue
            role, text, calls, fallback = parsed
            if not text and not calls:
                continue
            index = turn
            turn += 1
            if offset <= after:
                continue
            yield ConversationRecord(
                agent=self.agent,
                session_id=session_id,
                offset=offset,
                turn=index,
                role=role,
                text=text,
                timestamp=iso_timestamp(entry.get("timestamp")) or iso_timestamp(fallback),
                tool_calls=calls,
                cwd=cwd,
            )


READER = StepfunReader()
