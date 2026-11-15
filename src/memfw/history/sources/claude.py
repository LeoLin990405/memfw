"""Claude Code conversation reader (``~/.claude/projects/<encoded cwd>/<session>.jsonl``).

Each line carries ``sessionId``, ``uuid``, ``timestamp``, ``cwd`` and a
``message`` whose ``content`` is a string or typed blocks. ``tool_use`` blocks
become tool calls, a user message made only of ``tool_result`` blocks is a
``tool`` turn, and thinking blocks are dropped. The transcript index
(:mod:`memfw.history.index`) is unaffected by this reader.

Session labels (set on every record of the session by ``_Session``):

* ``subagent``: a Task/Agent-tool sub-agent. Claude Code writes those to
  ``<project>/<parent sessionId>/subagents/agent-<agentId>.jsonl`` (workflow
  agents one level deeper, ``subagents/workflows/<wf>/agent-*.jsonl``) and
  marks each line ``isSidechain: true`` with an ``agentId``. Either the path
  shape or one sidechain line labels the file. ``parent_session`` is the
  lines' ``sessionId`` (the parent's id, unlike the file stem); fallback: the
  directory name right above ``subagents``.
* ``automated``: started headless (``claude -p`` / Agent SDK). Lines carry
  ``entrypoint``; observed values are ``cli`` (interactive terminal),
  ``claude-desktop`` (desktop app) and ``sdk-cli`` (print mode); the SDKs
  report ``sdk-py`` / ``sdk-ts``. Any ``sdk-*`` value labels the session.
  No ``entrypoint`` (older files) leaves the origin empty, so the root's applies.

``cwd`` falls back to the last ``cwd`` seen in the file (request-blob lines
have none); ``project`` is the last path part of the first ``cwd`` in the
file, the directory the session started in (the convention every reader
uses). ``model`` is
the assistant ``message.model``; the ``<synthetic>`` placeholder Claude
writes on locally generated messages is not a model and is dropped.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath
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
from .labels import AUTOMATED, SUBAGENT

PARSER_VERSION = "2"

AGENT = "claude"
_ROLES = {"user", "assistant", "system"}
_SUBAGENT_DIR = "subagents"
_SYNTHETIC_MODEL = "<synthetic>"


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _path_parent(locator: SessionLocator) -> str | None:
    """Parent session id when the file sits under ``<parent>/subagents/``, else ``None``."""

    parts = Path(locator.key).parts
    if not locator.path.stem.startswith("agent-") or _SUBAGENT_DIR not in parts[:-1]:
        return None
    index = parts.index(_SUBAGENT_DIR)
    return parts[index - 1] if index >= 1 else ""


class _Session:
    """Labels and fallbacks accumulated over one file's lines (never downgraded)."""

    def __init__(self, locator: SessionLocator) -> None:
        self.session_id = locator.session_id
        parent = _path_parent(locator)
        self.origin = SUBAGENT if parent is not None else ""
        self.parent = parent or ""
        self.cwd = ""
        self.project = ""

    def see(self, raw: dict[str, Any]) -> None:
        cwd = _str(raw.get("cwd"))
        if cwd:
            self.cwd = cwd
            self.project = self.project or PurePosixPath(cwd).name
        if raw.get("isSidechain") is True:
            self.origin = SUBAGENT
        elif not self.origin and _str(raw.get("entrypoint")).startswith("sdk"):
            self.origin = AUTOMATED
        if self.origin == SUBAGENT:
            session = _str(raw.get("sessionId"))
            if session and session != self.session_id:
                self.parent = session


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
        session = _Session(locator)
        for offset, raw in iter_jsonl(locator.path):
            session.see(raw)
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
            model = _str(message.get("model"))
            yield ConversationRecord(
                agent=AGENT,
                session_id=locator.session_id,
                offset=offset,
                turn=index,
                role="tool" if only_results and role == "user" else role,
                text=text,
                timestamp=iso_timestamp(raw.get("timestamp")),
                tool_calls=calls,
                cwd=_str(raw.get("cwd")) or session.cwd,
                project=session.project,
                model="" if model == _SYNTHETIC_MODEL else model,
                origin=session.origin,
                parent_session=session.parent,
            )


READER = ClaudeReader()
