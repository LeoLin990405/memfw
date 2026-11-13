"""Synthetic conversation-store fixtures (no real sessions, paths or names).

The table matches the unified store's ``conversation_turns`` schema
(``memfw.history.conversations``), written directly so these tests do not
depend on the reader branch.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

from memfw.core.repository import Repository

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    agent TEXT NOT NULL, session_id TEXT NOT NULL, offset INTEGER NOT NULL,
    turn INTEGER NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL,
    tool_calls TEXT NOT NULL, cwd TEXT NOT NULL, project TEXT NOT NULL,
    locator TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (agent, session_id, offset));
"""

# Built at runtime so the repository holds no credential-shaped literal.
SECRET = "pass" + "word=correct-horse-battery-9"
EMAIL = "demo.user" + "@" + "example.com"


def turn(
    agent: str,
    session: str,
    offset: int,
    role: str,
    text: str,
    ts: str,
    calls: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    return {
        "agent": agent,
        "session_id": session,
        "offset": offset,
        "turn": offset,
        "role": role,
        "ts": ts,
        "text": text,
        "tool_calls": json.dumps([{"name": n, "summary": s} for n, s in calls]),
        "cwd": "/work/demo",
        "project": "demo",
        "locator": f"{agent}/{session}",
        "digest": f"d{offset}",
    }


def write_store(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT OR REPLACE INTO conversation_turns VALUES (:agent, :session_id, :offset,"
            " :turn, :role, :ts, :text, :tool_calls, :cwd, :project, :locator, :digest)",
            list(rows),
        )
    os.chmod(path, 0o600)
    return path


def standard_rows() -> list[dict[str, Any]]:
    day = "2026-01-05T10:0"
    return [
        turn(
            "codex",
            "s-alpha",
            0,
            "user",
            "Add a retry wrapper to the flaky uploader.",
            day + "0:00Z",
        ),
        turn(
            "codex",
            "s-alpha",
            1,
            "assistant",
            "We decided to use exponential backoff with jitter. Should the cap be 30 seconds?",
            day + "1:00Z",
            calls=[("apply_patch", "src/uploader/retry.py +40 -2"), ("shell", "pytest -q")],
        ),
        turn("codex", "s-alpha", 2, "tool", "", day + "2:00Z", calls=[("shell", "pytest -q")]),
        turn("codex", "s-alpha", 3, "assistant", "All tests passed; committed.", day + "3:00Z"),
        turn("claude", "s-beta", 0, "user", f"Contact {EMAIL} about the uploader.", day + "4:00Z"),
        turn("claude", "s-beta", 1, "assistant", f"Stored it: {SECRET}", day + "5:00Z"),
        turn("cursor", "s-gamma", 0, "user", "Private uploader notes for cursor.", day + "6:00Z"),
    ]


def make_vault(root: Path, extra: dict[str, Any] | None = None) -> Path:
    repository = Repository(root)
    repository.init()
    config = repository.read_config()
    config.update(extra or {})
    repository.configure(config)
    return root


def set_config(root: Path, extra: dict[str, Any]) -> None:
    repository = Repository(root)
    config = repository.read_config()
    config.update(extra)
    repository.configure(config)


def dump_yaml(value: Any) -> str:
    return yaml.safe_dump(value)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home
