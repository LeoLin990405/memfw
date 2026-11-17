"""Synthetic sessions for the handoff tests, written through the real store.

Each session goes through :class:`memfw.history.conversations.ConversationStore`
(write-time redaction included) from an in-memory reader. No real sessions,
paths, names or hosts: work trees are ``/work/demo``-style paths.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import (
    ConversationRecord,
    SessionLocator,
    StoreRoot,
    tool_call,
)

# Assembled at runtime so the repository holds no credential-shaped literal.
SECRET = "pass" + "word=correct-horse-battery-9"
EMAIL = "demo.user" + "@" + "example.com"

# Per agent: (edit tool name, argument dict) for one public edit tool of that CLI.
EDITS: dict[str, tuple[str, dict[str, Any]]] = {
    "claude": (
        "Edit",
        {"file_path": "/work/demo/src/app.py", "old_string": "a", "new_string": "b"},
    ),
    "codex": ("apply_patch", {"input": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b"}),
    "kimi": ("WriteFile", {"path": "/work/demo/src/app.py", "content": "x"}),
    "kimi-desktop": ("WriteFile", {"path": "/work/demo/src/app.py", "content": "x"}),
    "grok": ("edit_file", {"path": "/work/demo/src/app.py", "content": "x"}),
    "cursor": ("edit_file", {"target_file": "/work/demo/src/app.py", "code_edit": "x"}),
    "cursor-ide": ("edit_file", {"target_file": "/work/demo/src/app.py", "code_edit": "x"}),
    "deepseek": ("write_file", {"path": "/work/demo/src/app.py", "content": "x"}),
    "minimax": ("write_file", {"path": "/work/demo/src/app.py", "content": "x"}),
    "glm": ("write_file", {"path": "/work/demo/src/app.py", "content": "x"}),
    "stepfun": ("write_file", {"path": "/work/demo/src/app.py", "content": "x"}),
    "opencode": ("edit", {"filePath": "/work/demo/src/app.py", "oldString": "a"}),
    "gemini": ("replace", {"file_path": "/work/demo/src/app.py", "old_string": "a"}),
    "antigravity": ("write_to_file", {"TargetFile": "/work/demo/src/app.py", "CodeContent": "x"}),
    "qwen": ("edit", {"file_path": "/work/demo/src/app.py", "old_string": "a"}),
}
EXPECTED_FILE = {"codex": "src/app.py"}


class ListReader:
    """A reader over in-memory records for one agent (drives the real store)."""

    parser_version = "3"
    append_only = True
    default_roots: tuple[str, ...] = ()

    def __init__(self, agent: str, sessions: dict[str, list[ConversationRecord]]) -> None:
        self.agent = agent
        self.data = sessions

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for session_id in self.data:
            yield SessionLocator(self.agent, root / f"{session_id}.jsonl", session_id, session_id)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        return (r for r in self.data[locator.session_id] if r.offset > after)


def record(
    agent: str,
    session: str,
    offset: int,
    role: str,
    text: str,
    *,
    calls: Sequence[tuple[str, Any]] = (),
    cwd: str = "/work/demo",
    project: str = "demo",
    minute: int | None = None,
) -> ConversationRecord:
    stamp = f"2026-01-02T10:{(offset if minute is None else minute) % 60:02d}:00Z"
    return ConversationRecord(
        agent,
        session,
        offset,
        offset,
        role,
        text,
        timestamp=stamp,
        tool_calls=tuple(tool_call(name, args) for name, args in calls),
        cwd=cwd,
        project=project,
    )


def standard_session(agent: str, session: str = "s1", *, cwd: str = "/work/demo") -> list[Any]:
    """Goal, an edit, a read, tool output, a secret turn and a closing exchange."""

    edit_name, edit_args = EDITS.get(agent, ("write_file", {"path": "/work/demo/src/app.py"}))
    return [
        record(agent, session, 0, "user", "Add a retry flag to the uploader CLI.", cwd=cwd),
        record(
            agent,
            session,
            1,
            "assistant",
            "Editing the CLI module.",
            calls=[(edit_name, edit_args), ("read_file", {"path": "/work/demo/README.md"})],
            cwd=cwd,
        ),
        record(agent, session, 2, "tool", "tool output that must not appear", cwd=cwd),
        record(agent, session, 3, "user", f"use {SECRET} for the test server", cwd=cwd),
        record(agent, session, 4, "assistant", f"Done. Ping {EMAIL} when merged.", cwd=cwd),
        record(agent, session, 5, "user", "Now update the docs.", cwd=cwd),
    ]


def make_store(tmp_path: Path, sessions: dict[str, dict[str, list[ConversationRecord]]]) -> Path:
    """Ingest ``{agent: {session: records}}`` into a store under ``tmp_path``."""

    db = tmp_path / "data" / "conversations.sqlite"
    store = ConversationStore(db)
    root = tmp_path / "roots"
    root.mkdir(exist_ok=True)
    for agent, by_session in sessions.items():
        store.ingest(ListReader(agent, by_session), [StoreRoot(root, "worker")])
    return db


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME (and Path.home) pointed at a temp directory."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
