"""Codex rollout reader (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``).

Lines are ``{"timestamp", "type", "payload"}``. ``session_meta`` names the
session and its ``cwd`` (``turn_context`` may move the cwd);
``response_item`` messages, function calls and their outputs are the turns.
``event_msg`` lines repeat the same messages for the UI and are skipped, as
is ``reasoning``. The transcript index (:mod:`memfw.history.index`) is
unaffected by this reader.

Session labels come from the ``session_meta`` payload (always the first
line; it is re-read on every ``read``, so an incremental read from a later
offset still labels its records):

* ``subagent``: ``source`` is a dict ``{"subagent": {"thread_spawn": {...}}}``
  (a thread another Codex thread spawned; ``thread_source == "subagent"``).
  ``parent_session`` is ``source.subagent.thread_spawn.parent_thread_id``,
  falling back to the top-level ``parent_thread_id`` and then
  ``forked_from_id``.
* ``automated``: the auto-review guardian (``source`` is
  ``{"subagent": {"other": "guardian"}}`` or ``thread_source ==
  "guardian_review"``; ``parent_session`` is ``parent_thread_id``, the thread
  it reviews); a headless ``codex exec`` run (``source == "exec"``, or, when
  ``source`` is missing, ``originator == "codex_exec"``); and a thread the
  Conductor native runtime drives (``originator == "conductor-native"``).
* anything else (``source`` ``"cli"`` / ``"vscode"`` with originator
  ``codex-tui`` / ``Codex Desktop``, or rollouts too old to carry these
  fields) leaves ``origin`` empty: the root's origin applies.

A sub-agent's ``originator`` is its parent process's (often ``codex_exec``),
so ``source`` is checked before ``originator``. ``model`` is the latest
``turn_context.model``; the opening turns (instructions, the first prompt)
come before the first ``turn_context``, so they take the model of the first
``turn_context`` in the file. ``project`` is the last component of ``cwd``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
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
    relative_key,
    tool_call,
    walk_files,
)
from .labels import AUTOMATED, SUBAGENT

PARSER_VERSION = "2"

AGENT = "codex"
_ROLES = {"user": "user", "assistant": "assistant", "developer": "system", "system": "system"}
_CALLS = {"function_call", "custom_tool_call", "local_shell_call"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output"}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


_MODEL_SCAN_LINES = 400
"""How far ``_meta`` looks for the first ``turn_context`` (it is near the top)."""


@dataclass(frozen=True)
class _Meta:
    session_id: str = ""
    cwd: str = ""
    origin: str = ""
    parent: str = ""
    model: str = ""


def _labels(payload: dict[str, Any]) -> tuple[str, str]:
    """``(origin, parent session)`` from a ``session_meta`` payload (see module doc)."""

    source = payload.get("source")
    thread_source = _str(payload.get("thread_source"))
    parent = _str(payload.get("parent_thread_id")) or _str(payload.get("forked_from_id"))
    sub = source.get("subagent") if isinstance(source, dict) else None
    guardian = isinstance(sub, dict) and sub.get("other") == "guardian"
    if guardian or thread_source == "guardian_review":
        return AUTOMATED, parent
    if sub is not None or thread_source == "subagent":
        spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
        if isinstance(spawn, dict):
            parent = _str(spawn.get("parent_thread_id")) or parent
        return SUBAGENT, parent
    originator = _str(payload.get("originator"))
    if source == "exec" or (source is None and originator == "codex_exec"):
        return AUTOMATED, parent
    if originator == "conductor-native":
        return AUTOMATED, parent
    return "", parent


def _meta(path: Path, *, model: bool = False) -> _Meta:
    """Session id, cwd and labels from the leading ``session_meta`` line.

    With ``model``, also the model of the first ``turn_context`` within
    ``_MODEL_SCAN_LINES`` lines.
    """

    meta = _Meta()
    for index, (_offset, raw) in enumerate(iter_jsonl(path)):
        payload = raw.get("payload")
        if index == 0:
            if raw.get("type") != "session_meta" or not isinstance(payload, dict):
                break
            origin, parent = _labels(payload)
            meta = _Meta(_str(payload.get("id")), _str(payload.get("cwd")), origin, parent)
            if not model:
                break
            continue
        if index >= _MODEL_SCAN_LINES:
            break
        if raw.get("type") == "turn_context" and isinstance(payload, dict):
            found = _str(payload.get("model"))
            if found:
                return _Meta(meta.session_id, meta.cwd, meta.origin, meta.parent, found)
    return meta


def _project(cwd: str) -> str:
    return PurePosixPath(cwd).name if cwd not in {"", "/"} else ""


class CodexReader:
    """Reads Codex rollout JSONL files."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (".codex/sessions", ".codex/archived_sessions")

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (".jsonl",)):
            session_id = _meta(path).session_id
            yield SessionLocator(AGENT, path, relative_key(root, path), session_id or path.stem)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        meta = _meta(locator.path, model=True)
        session_id = meta.session_id or locator.session_id
        cwd = meta.cwd
        turn = 0
        model = meta.model
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
                project=_project(cwd),
                model=model,
                origin=meta.origin,
                parent_session=meta.parent,
            )


READER = CodexReader()
