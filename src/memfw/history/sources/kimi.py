"""Kimi CLI (``kimi-code``) conversation reader.

Layout under the store root (``~/.kimi-code/sessions``)::

    <workdir>/session_<uuid>/state.json             id, cwd, createdAt ...
    <workdir>/session_<uuid>/agents/<agent>/wire.jsonl

``wire.jsonl`` is an append-only event log. Only ``agent.message.appended``
events are transcript turns; each carries ``message.message`` (``role``,
``content`` blocks, ``toolCalls``) and ``message.meta.source`` (``input`` for
the user's prompt, ``llm`` for model output, ``tool`` for tool results,
``notify`` for runtime notifications). ``think`` blocks are reasoning and are
dropped. The ``main`` agent is the session itself; every other agent directory
(``agent-0`` ...) is a sub-agent and becomes its own session,
``<session id>:<agent>``.
"""

from __future__ import annotations

import os
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
    read_json,
    regular_file,
    relative_key,
    tool_call,
)

PARSER_VERSION = "1"

AGENT = "kimi"
MAIN_AGENT = "main"
WIRE_FILE = "wire.jsonl"
MESSAGE_EVENT = "agent.message.appended"
_ROLES = {"user", "assistant", "tool", "system"}
_SOURCE_ROLES = {"notify": "system"}


def _subdirs(directory: Path) -> list[Path]:
    """Real (non-symlink) child directories, sorted by name."""

    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return []
    return [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]


def _session_meta(session_dir: Path) -> tuple[str, str]:
    """``(session id, cwd)`` from ``state.json``; the directory name when it is missing."""

    state = read_json(session_dir / "state.json")
    session_id = ""
    cwd = ""
    if isinstance(state, dict):
        if isinstance(state.get("id"), str):
            session_id = state["id"].strip()
        if isinstance(state.get("cwd"), str):
            cwd = state["cwd"]
    return session_id or session_dir.name, cwd


def _text(content: Any) -> str:
    """Text of the ``text`` blocks only (``think`` reasoning is dropped)."""

    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    blocks = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return content_text(blocks).strip()


def _tool_calls(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, list):
        return ()
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict):
            calls.append(tool_call(function.get("name"), function.get("arguments")))
        else:
            calls.append(tool_call(item.get("name"), item.get("arguments")))
    return tuple(calls)


class KimiReader:
    """Reads Kimi CLI sessions from ``wire.jsonl`` event logs."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".kimi-code/sessions",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        if not root.is_dir() or root.is_symlink():
            return
        for workdir in _subdirs(root):
            for session_dir in _subdirs(workdir):
                if not session_dir.name.startswith("session_"):
                    continue
                agents_dir = session_dir / "agents"
                if agents_dir.is_symlink() or not agents_dir.is_dir():
                    continue
                session_id, cwd = _session_meta(session_dir)
                for agent_dir in _subdirs(agents_dir):
                    wire = agent_dir / WIRE_FILE
                    if not regular_file(wire):
                        continue
                    name = agent_dir.name
                    yield SessionLocator(
                        agent=AGENT,
                        path=wire,
                        key=relative_key(root, wire),
                        session_id=session_id if name == MAIN_AGENT else f"{session_id}:{name}",
                        extra={"cwd": cwd, "agent_id": name},
                    )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        session_id = locator.session_id or locator.key
        cwd = locator.extra.get("cwd", "")
        if not isinstance(cwd, str):
            cwd = ""
        turn = 0
        for offset, event in iter_jsonl(locator.path):
            record = self._record(event, session_id, offset, cwd)
            if record is None:
                continue
            record.turn = turn
            turn += 1
            if offset > after:
                yield record

    @staticmethod
    def _record(
        event: dict[str, Any], session_id: str, offset: int, cwd: str
    ) -> ConversationRecord | None:
        if event.get("type") != MESSAGE_EVENT:
            return None
        envelope = event.get("message")
        if not isinstance(envelope, dict):
            return None
        message = envelope.get("message")
        if not isinstance(message, dict):
            return None
        meta = envelope.get("meta")
        source = meta.get("source") if isinstance(meta, dict) else None
        role = message.get("role")
        if isinstance(source, str) and source in _SOURCE_ROLES:
            role = _SOURCE_ROLES[source]
        if not isinstance(role, str) or role not in _ROLES:
            return None
        text = _text(message.get("content"))
        calls = _tool_calls(message.get("toolCalls")) if role == "assistant" else ()
        if not text and not calls:
            return None
        return ConversationRecord(
            agent=AGENT,
            session_id=session_id,
            offset=offset,
            turn=0,
            role=role,
            text=text,
            timestamp=iso_timestamp(event.get("time")),
            tool_calls=calls,
            cwd=cwd,
        )


READER = KimiReader()
