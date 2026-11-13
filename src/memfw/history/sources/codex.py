"""Codex rollout reader (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``).

Lines are ``{"timestamp", "type", "payload"}``. ``session_meta`` names the
session and its ``cwd`` (``turn_context`` may move the cwd);
``response_item`` messages, function calls and their outputs are the turns.
``event_msg`` lines repeat the same messages for the UI and are skipped, as
is ``reasoning``. The transcript index (:mod:`memfw.history.index`) is
unaffected by this reader.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import (
    TOOL_OUTPUT_CHARS,
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

AGENT = "codex"
_ROLES = {"user": "user", "assistant": "assistant", "developer": "system", "system": "system"}
_CALLS = {"function_call", "custom_tool_call", "local_shell_call"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _meta(path: Path) -> tuple[str, str]:
    """``(session id, cwd)`` from the leading ``session_meta`` line."""

    for _offset, raw in iter_jsonl(path):
        payload = raw.get("payload")
        if raw.get("type") == "session_meta" and isinstance(payload, dict):
            return _str(payload.get("id")), _str(payload.get("cwd"))
        break
    return "", ""


class CodexReader:
    """Reads Codex rollout JSONL files."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".codex/sessions", ".codex/archived_sessions")

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (".jsonl",)):
            session_id, _cwd = _meta(path)
            yield SessionLocator(AGENT, path, relative_key(root, path), session_id or path.stem)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        session_id, cwd = _meta(locator.path)
        session_id = session_id or locator.session_id
        turn = 0
        model = ""
        for offset, raw in iter_jsonl(locator.path):
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue
            if raw.get("type") == "turn_context":
                cwd = _str(payload.get("cwd")) or cwd
                model = _str(payload.get("model")) or model
                continue
            if raw.get("type") != "response_item":
                continue
            item = _str(payload.get("type"))
            role, text = "", ""
            calls: tuple[ToolCall, ...] = ()
            if item == "message":
                role = _ROLES.get(_str(payload.get("role")), "")
                text = content_text(payload.get("content"))
            elif item in _CALLS:
                role = "assistant"
                arguments = payload.get("arguments", payload.get("input", payload.get("action")))
                calls = (tool_call(payload.get("name") or item, arguments),)
            elif item in _OUTPUTS:
                role = "tool"
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content", output.get("output"))
                text = content_text(output)[:TOOL_OUTPUT_CHARS]
            if not role or (not text.strip() and not calls):
                continue
            index = turn
            turn += 1
            if offset <= after:
                continue
            yield ConversationRecord(
                agent=AGENT,
                session_id=session_id,
                offset=offset,
                turn=index,
                role=role,
                text=text,
                timestamp=iso_timestamp(raw.get("timestamp")),
                tool_calls=calls,
                cwd=cwd,
                model=model,
            )


READER = CodexReader()
