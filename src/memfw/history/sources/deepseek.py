"""DeepSeek harness CLI (``dsh``) conversation reader.

Layout under the store root (``~/.dsh/sessions``)::

    <encoded workdir>/session-<uuid>/session.v4.jsonl.zstd
    <encoded workdir>/<uuid>/session.v4.jsonl.zstd
    <encoded workdir>/<...>/session.lock

``session.v4.jsonl.zstd`` is a zstd-compressed, append-only JSONL event log.
The first line is the session header (``type: "session"``, ``id``, ``cwd``,
``createdAt``, ``parentSession`` for sub-agents). Every other line is an event
``{"type", "seq", "time" (epoch ms), "data"}``. Transcript turns are:

* ``user/message`` -- ``data.content`` blocks; ``data.source.kind`` tells the
  user's prompt (``user``) apart from runtime-injected context
  (``time-context``, ``skill-catalog`` ...), which becomes role ``system``.
* ``assistant/message`` -- ``data.message.content`` holds ``text``,
  ``reasoning`` (dropped) and ``tool-call`` blocks (``name`` + JSON-string
  ``arguments``). The raw ``stream`` chunks are ignored.
* ``tool/result`` -- ``data.message.content`` text; ``data.meta.cwd`` when the
  tool ran a command.
* ``system/message`` -- the system prompt.

``tool/call`` events duplicate the assistant's ``tool-call`` blocks and are
skipped. ``offset`` is the byte offset of the line in the *decompressed*
stream, so it is stable while the log grows. The trailing line without a
newline (or a frame still being written) is not yielded.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard

from .base import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
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

AGENT = "deepseek"
SESSION_PREFIX = "session"
_SUFFIXES = (".jsonl.zstd", ".jsonl")
_HEADER = "session"
_USER = "user/message"
_ASSISTANT = "assistant/message"
_TOOL = "tool/result"
_SYSTEM = "system/message"
_TURN_EVENTS = {_USER, _ASSISTANT, _TOOL, _SYSTEM}
# Cheap byte pre-filter: lines that cannot be a transcript turn are not parsed.
_MARKERS = (b'"session"', b"/message", b"tool/result")
# ``user/message`` source kinds the runtime injects; anything else is a person or agent.
_INJECTED_KINDS = {
    "agent-instructions",
    "compact-checkpoint",
    "model-selection",
    "plan-mode",
    "repeat-tool-reminder",
    "runtime-context",
    "skill-catalog",
    "subagent-settled",
    "time-context",
    "tool-goal",
    "tool-jobs",
    "working-directory",
}
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CHUNK = 1024 * 1024


def _is_session_file(path: Path) -> bool:
    return path.name.startswith(SESSION_PREFIX) and path.name.endswith(_SUFFIXES)


def _decoded(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _iter_zstd_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """``(decompressed byte offset, object)`` per complete JSON-object line of a zstd log.

    Mirrors :func:`memfw.history.sources.base.iter_jsonl` for a compressed file:
    ``O_NOFOLLOW``, over-long lines skipped, the unterminated trailing line and a
    truncated trailing frame (still being written) dropped, lines without a
    transcript marker not parsed. Never raises on corrupt data.
    """

    try:
        descriptor = os.open(path, _FILE_FLAGS)
    except OSError:
        return
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            os.close(descriptor)
            return
        handle = os.fdopen(descriptor, "rb")
    except OSError:
        os.close(descriptor)
        return
    with handle:
        try:
            reader = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True)
        except zstandard.ZstdError:
            return
        with reader:
            buffer = b""
            offset = 0
            skipping = False
            while True:
                try:
                    chunk = reader.read(_CHUNK)
                except (zstandard.ZstdError, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        if len(buffer) > MAX_LINE_BYTES:
                            skipping = True
                            offset += len(buffer)
                            buffer = b""
                        break
                    line, buffer = buffer[:newline], buffer[newline + 1 :]
                    start = offset
                    offset += newline + 1
                    if skipping:
                        skipping = False
                        continue
                    if not line.strip() or len(line) > MAX_LINE_BYTES:
                        continue
                    if not any(marker in line for marker in _MARKERS):
                        continue
                    value = _decoded(line)
                    if value is not None:
                        yield start, value


def _events(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    if path.name.endswith(".zstd"):
        return _iter_zstd_jsonl(path)
    return iter_jsonl(path)


def _header(path: Path) -> dict[str, Any]:
    """The session header line, or ``{}`` when the first record is not one."""

    for _offset, event in _events(path):
        return event if event.get("type") == _HEADER else {}
    return {}


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text(content: Any) -> str:
    """Text of the ``text`` blocks only (``reasoning``, ``tool-call``, images dropped)."""

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


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return value
    return value


def _tool_calls(content: Any) -> tuple[ToolCall, ...]:
    if not isinstance(content, list):
        return ()
    return tuple(
        tool_call(block.get("name"), _arguments(block.get("arguments")))
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool-call"
    )


def _role_and_message(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    kind = event.get("type")
    data = event.get("data")
    if kind not in _TURN_EVENTS or not isinstance(data, dict):
        return None
    if kind == _USER:
        source = data.get("source")
        source_kind = source.get("kind") if isinstance(source, dict) else None
        role = "system" if source_kind in _INJECTED_KINDS else "user"
        return role, data
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    role = {_ASSISTANT: "assistant", _TOOL: "tool", _SYSTEM: "system"}[str(kind)]
    return role, message


class DeepSeekReader:
    """Reads DeepSeek harness CLI sessions from ``session.v4.jsonl.zstd`` event logs."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".dsh/sessions",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, _SUFFIXES):
            if not _is_session_file(path):
                continue
            header = _header(path)
            if not header:
                continue
            fallback = path.parent.name.removeprefix("session-")
            yield SessionLocator(
                agent=AGENT,
                path=path,
                key=relative_key(root, path),
                session_id=_str(header.get("id")) or fallback,
                extra={
                    "cwd": _str(header.get("cwd")),
                    "parent_session": _str(header.get("parentSession")),
                },
            )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        session_id = locator.session_id or locator.key
        cwd = locator.extra.get("cwd", "")
        if not isinstance(cwd, str):
            cwd = ""
        turn = 0
        for offset, event in _events(locator.path):
            if offset == 0 and event.get("type") == _HEADER:
                cwd = _str(event.get("cwd")) or cwd
                continue
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
        found = _role_and_message(event)
        if found is None:
            return None
        role, message = found
        content = message.get("content")
        text = _text(content)
        calls = _tool_calls(content) if role == "assistant" else ()
        if not text and not calls:
            return None
        record_cwd = cwd
        data = event.get("data")
        meta = data.get("meta") if isinstance(data, dict) else None
        if role == "tool" and isinstance(meta, dict):
            record_cwd = _str(meta.get("cwd")) or cwd
        return ConversationRecord(
            agent=AGENT,
            session_id=session_id,
            offset=offset,
            turn=0,
            role=role,
            text=text,
            timestamp=iso_timestamp(event.get("time")),
            tool_calls=calls,
            cwd=record_cwd,
        )


READER = DeepSeekReader()
