"""Kimi desktop app (``Kimi.app``) conversation reader.

The desktop app's agent conversations run on an embedded copy of the Kimi
CLI kernel, which keeps its own home inside the app's data directory::

    ~/Library/Application Support/kimi-desktop/daimon-share/daimon/
        runtime/kimi-code/home/sessions           <- store root
            <workdir>/conv-<id>/state.json        id, cwd, custom.sessionKind ...
            <workdir>/conv-<id>/agents/<agent>/wire.jsonl
            <workdir>/ctitle-<id>/...             title-generation side sessions

``wire.jsonl`` uses the same event format as the Kimi CLI (see
:mod:`.kimi`): ``agent.message.appended`` events are the transcript, and
``llm.request`` events name the model that produced the following assistant
messages. The app's own index (``agents/main/sessions/hosted-logical/
conversations.sqlite``) only points at these session directories, and its
search index (``search-index/global-search``) is a derived copy of message
text; neither is read. Plain chat with the web assistant is stored server-side
and is not available locally.

Sessions are the directories under a workdir that have an ``agents``
directory. Title-generation sessions (``custom.sessionKind`` of
``conversation-title``, or a ``ctitle-`` directory name) are internal
boilerplate and skipped. As in the CLI store, ``main`` is the conversation and
every other agent directory is a sub-agent session ``<session id>:<agent>``.
Offsets are byte offsets in ``wire.jsonl``, which is append-only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import (
    ConversationRecord,
    SessionLocator,
    iter_jsonl,
    read_json,
    regular_file,
    relative_key,
)
from .kimi import MAIN_AGENT, WIRE_FILE, KimiReader, _subdirs

PARSER_VERSION = "1"

AGENT = "kimi-desktop"
STORE_ROOT = (
    "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions"
)
TITLE_KIND = "conversation-title"
TITLE_PREFIX = "ctitle-"
MODEL_EVENTS = {"llm.request", "usage.record"}


def _session_meta(session_dir: Path) -> tuple[str, str, str]:
    """``(session id, cwd, session kind)`` from ``state.json``; the directory name as id."""

    state = read_json(session_dir / "state.json")
    session_id = cwd = kind = ""
    if isinstance(state, dict):
        if isinstance(state.get("id"), str):
            session_id = state["id"].strip()
        if isinstance(state.get("cwd"), str):
            cwd = state["cwd"]
        custom = state.get("custom")
        if isinstance(custom, dict) and isinstance(custom.get("sessionKind"), str):
            kind = custom["sessionKind"]
    return session_id or session_dir.name, cwd, kind


class KimiDesktopReader:
    """Reads Kimi desktop app conversations from the embedded kernel's ``wire.jsonl`` logs."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = True
    default_roots: tuple[str, ...] = (STORE_ROOT,)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        if not root.is_dir() or root.is_symlink():
            return
        for workdir in _subdirs(root):
            for session_dir in _subdirs(workdir):
                if session_dir.name.startswith((".", TITLE_PREFIX)):
                    continue
                agents_dir = session_dir / "agents"
                if agents_dir.is_symlink() or not agents_dir.is_dir():
                    continue
                session_id, cwd, kind = _session_meta(session_dir)
                if kind == TITLE_KIND:
                    continue
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
        model = ""
        for offset, event in iter_jsonl(locator.path):
            if event.get("type") in MODEL_EVENTS:
                model = _model(event) or model
                continue
            record = KimiReader._record(event, session_id, offset, cwd)
            if record is None:
                continue
            record.agent = AGENT
            if record.role == "assistant":
                record.model = model
            record.turn = turn
            turn += 1
            if offset > after:
                yield record


def _model(event: dict[str, Any]) -> str:
    value = event.get("model")
    return value.strip() if isinstance(value, str) else ""


READER = KimiDesktopReader()
