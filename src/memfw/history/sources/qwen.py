"""Qwen Code CLI conversation reader (``~/.qwen/projects/<sanitised cwd>/...``).

Qwen Code keeps one directory per project under ``~/.qwen/projects``, named
after the project root with every non-alphanumeric character replaced by
``-``::

    <project>/chats/<session uuid>.jsonl                 one session, a record log
    <project>/chats/archive/<session uuid>.jsonl         archived sessions
    <project>/chats/<session uuid>.runtime.json, .pr.json, .worktree.json (not read)
    <project>/subagents/<parent session id>/agent-<agent id>.jsonl
    <project>/subagents/<parent session id>/agent-<agent id>.meta.json  (not read)

Everything else in ``~/.qwen`` (``settings.json``, OAuth and installation
files, ``tmp/``, ``debug/``) is never opened.

Each line of a session file is one *chat record*: ``uuid``, ``parentUuid``,
``sessionId``, ``timestamp`` (ISO-8601), ``type`` (``user``, ``assistant``,
``tool_result`` or ``system``), ``subtype``, ``provenance``, ``cwd``,
``version`` and, for the first three types, a Gemini-style ``message``
``{"role", "parts"}``. Parts are ``{"text"}`` (``"thought": true`` marks
reasoning, which is dropped), ``{"functionCall": {"name", "args"}}`` and
``{"functionResponse": {"name", "response"}}``. Assistant records carry
``model``; tool results may carry ``toolCallResult.resultDisplay``.

* ``user`` records are ``user`` turns when their ``provenance`` is absent or
  ``real_user``; notifications, cron prompts and goal-runtime messages
  (``provenance`` ``system``, ``goal_runtime`` ...) are ``system`` turns.
* ``assistant`` records are ``assistant`` turns; function calls become tool calls.
* ``tool_result`` records are ``tool`` turns: the function response output
  (``output``, ``error``, ``content`` or ``llmContent``), else the result display.
* ``system`` records (titles, telemetry, compression snapshots, slash commands)
  are bookkeeping and skipped, except a ``session_source`` record whose
  ``sourceType`` is ``script`` or ``scheduled_task``, which labels the
  session ``automated``.

Files under ``subagents/`` are labelled ``subagent`` with the directory name
as ``parent_session`` (their session id is the file stem, ``agent-<id>``).
Offsets are byte offsets of the record lines. The CLI appends, but its
maintenance step may rewrite a file, so the session is re-read in full on
every change (``append_only`` is false).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from .base import (
    TOOL_OUTPUT_CHARS,
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    iter_jsonl,
    regular_file,
    relative_key,
    tool_call,
)
from .labels import AUTOMATED, SUBAGENT

PARSER_VERSION = "1"

AGENT = "qwen"
CHATS_DIR = "chats"
ARCHIVE_DIR = "archive"
SUBAGENTS_DIR = "subagents"
SESSION_FILE = re.compile(r"^[0-9a-fA-F-]{32,36}\.jsonl$")
AGENT_FILE = re.compile(r"^agent-[^/]+\.jsonl$")
_USER_PROVENANCE = frozenset({"", "real_user"})
_AUTOMATED_SOURCES = frozenset({"script", "scheduled_task"})
_RESPONSE_KEYS = ("output", "error", "content", "llmContent")


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _real_dirs(path: Path) -> list[Path]:
    if not path.is_dir() or path.is_symlink():
        return []
    return sorted(entry for entry in path.iterdir() if entry.is_dir() and not entry.is_symlink())


def _files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    return sorted(
        entry for entry in directory.iterdir() if pattern.match(entry.name) and regular_file(entry)
    )


def _parts(message: Any) -> list[Any]:
    parts = _dict(message).get("parts")
    if isinstance(parts, list):
        return parts
    if isinstance(parts, (dict, str)):
        return [parts]
    return []


def _split(message: Any) -> tuple[str, tuple[ToolCall, ...]]:
    """Visible text and function calls of a ``message``; thoughts dropped."""

    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in _parts(message):
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict) or part.get("thought") is True:
            continue
        call = part.get("functionCall")
        if isinstance(call, dict):
            calls.append(tool_call(call.get("name"), call.get("args")))
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(text for text in texts if text.strip()), tuple(calls)


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    for key in _RESPONSE_KEYS:
        value = response.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else content_text(value)
        if text.strip():
            return text
    return ""


def _tool_output(raw: dict[str, Any]) -> str:
    pieces: list[str] = []
    for part in _parts(raw.get("message")):
        if isinstance(part, dict):
            response = _dict(part.get("functionResponse")).get("response")
            text = _response_text(response) if response is not None else _str(part.get("text"))
        else:
            text = _str(part)
        if text.strip():
            pieces.append(text.strip())
    if not pieces:
        display = _dict(raw.get("toolCallResult")).get("resultDisplay")
        if isinstance(display, str) and display.strip():
            pieces.append(display.strip())
    return "\n".join(pieces)


class QwenReader:
    """Reads Qwen Code ``chats/`` and ``subagents/`` session logs."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (".qwen/projects",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for project_dir in _real_dirs(root):
            chats = project_dir / CHATS_DIR
            for directory in (chats, chats / ARCHIVE_DIR):
                for path in _files(directory, SESSION_FILE):
                    yield SessionLocator(
                        agent=self.agent,
                        path=path,
                        key=relative_key(root, path),
                        session_id=path.stem,
                        extra={"parent_id": ""},
                    )
            for parent in _real_dirs(project_dir / SUBAGENTS_DIR):
                for path in _files(parent, AGENT_FILE):
                    yield SessionLocator(
                        agent=self.agent,
                        path=path,
                        key=relative_key(root, path),
                        session_id=path.stem,
                        extra={"parent_id": parent.name},
                    )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        path = locator.path
        if not regular_file(path):
            return
        parent = _str(locator.extra.get("parent_id"))
        session_id = locator.session_id or path.stem
        origin = SUBAGENT if parent else ""
        cwd = project = ""
        turn = 0
        for offset, raw in iter_jsonl(path):
            try:
                kind = _str(raw.get("type"))
                seen_cwd = _str(raw.get("cwd"))
                if seen_cwd:
                    cwd = seen_cwd
                    project = project or PurePosixPath(seen_cwd).name
                if kind == "system":
                    payload = _dict(raw.get("systemPayload"))
                    if (
                        not origin
                        and raw.get("subtype") == "session_source"
                        and _str(payload.get("sourceType")) in _AUTOMATED_SOURCES
                    ):
                        origin = AUTOMATED
                    continue
                calls: tuple[ToolCall, ...] = ()
                if kind == "user":
                    provenance = _str(raw.get("provenance"))
                    role = "user" if provenance in _USER_PROVENANCE else "system"
                    text, _calls = _split(raw.get("message"))
                elif kind == "assistant":
                    role = "assistant"
                    text, calls = _split(raw.get("message"))
                elif kind == "tool_result":
                    role = "tool"
                    text = _tool_output(raw)[:TOOL_OUTPUT_CHARS]
                else:
                    continue
                text = text.strip()
            except (TypeError, ValueError, AttributeError, RecursionError):
                continue  # one malformed record never hides the rest of the session
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
                timestamp=iso_timestamp(raw.get("timestamp")),
                tool_calls=calls,
                cwd=cwd,
                project=project,
                model=_str(raw.get("model")) if role == "assistant" else "",
                origin=origin,
                parent_session=parent,
            )


READER = QwenReader()
