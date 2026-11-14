"""Claude Code conversation reader (``~/.claude/projects/<encoded cwd>/<session>.jsonl``).

Each line carries ``sessionId``, ``uuid``, ``timestamp``, ``cwd`` and a
``message`` whose ``content`` is a string or typed blocks. ``tool_use`` blocks
become tool calls, a user message made only of ``tool_result`` blocks is a
``tool`` turn, and thinking blocks are dropped. The transcript index
(:mod:`memfw.history.index`) is unaffected by this reader.
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

AGENT = "claude"
_ROLES = {"user", "assistant", "system"}


def _split(content: Any) -> tuple[str, tuple[ToolCall, ...], bool]:
    """``(text, tool calls, tool-result only)`` of a message content value."""

    if not isinstance(content, list):
        return content_text(content), (), False
    calls: list[ToolCall] = []
    kept: list[Any] = []
    results = 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append(tool_call(block.get("name"), block.get("input")))
            continue
        if isinstance(block, dict) and block.get("type") == "tool_result":
            results += 1
        kept.append(block)
    only_results = bool(kept) and results == len(kept)
    return content_text(kept), tuple(calls), only_results


class ClaudeReader:
    """Reads Claude Code session JSONL files."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".claude/projects",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (".jsonl",)):
            yield SessionLocator(AGENT, path, relative_key(root, path), path.stem)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        turn = 0
        for offset, raw in iter_jsonl(locator.path):
            message = raw.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role") if isinstance(message.get("role"), str) else ""
            if role not in _ROLES:
                continue
            text, calls, only_results = _split(message.get("content"))
            if not text.strip() and not calls:
                continue
            index = turn
            turn += 1
            if offset <= after:
                continue
            # The file names the session: a resumed or forked file repeats an
            # earlier sessionId on its copied lines, which must not collide.
            cwd = raw.get("cwd")
            model = message.get("model")
            yield ConversationRecord(
                agent=AGENT,
                session_id=locator.session_id,
                offset=offset,
                turn=index,
                role="tool" if only_results and role == "user" else role,
                text=text,
                timestamp=iso_timestamp(raw.get("timestamp")),
                tool_calls=calls,
                cwd=cwd if isinstance(cwd, str) else "",
                model=model if isinstance(model, str) else "",
            )


READER = ClaudeReader()
