"""Regressions: handoff goal skips injected turns, redacted cwd skips git with a note."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from memfw.handoff.pack import (
    NOTE_GIT_REDACTED_CWD,
    NOTE_GIT_UNAVAILABLE,
    build_pack,
    render_markdown,
)
from memfw.handoff.schema import PackSchemaError, validate_pack
from memfw.handoff.source import load_session

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    agent TEXT NOT NULL, session_id TEXT NOT NULL, offset INTEGER NOT NULL,
    turn INTEGER NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL,
    tool_calls TEXT NOT NULL, cwd TEXT NOT NULL, project TEXT NOT NULL, model TEXT NOT NULL,
    origin TEXT NOT NULL, withheld INTEGER NOT NULL, parser_version TEXT NOT NULL,
    locator TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (agent, session_id, offset));
"""
INJECTED = (
    "<system-reminder>Contents of the project instructions: always use pnpm;"
    " private infra notes</system-reminder>"
)
REDACTED_CWD = "/Users/[REDACTED:user]/projects/app"


def row(offset: int, role: str, text: str, ts: str, cwd: str) -> dict[str, Any]:
    return {
        "agent": "claude",
        "session_id": "s1",
        "offset": offset,
        "turn": offset,
        "role": role,
        "ts": ts,
        "text": text,
        "tool_calls": json.dumps([]),
        "cwd": cwd,
        "project": "demo",
        "model": "m",
        "origin": "interactive",
        "withheld": 0,
        "parser_version": "1",
        "locator": "claude/s1",
        "digest": f"d{offset}",
    }


def write_store(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT OR REPLACE INTO conversation_turns VALUES (:agent, :session_id, :offset,"
            " :turn, :role, :ts, :text, :tool_calls, :cwd, :project, :model, :origin,"
            " :withheld, :parser_version, :locator, :digest)",
            list(rows),
        )
    path.chmod(0o600)
    return path


def _store(tmp_path: Path, cwd: str = REDACTED_CWD) -> Path:
    return write_store(
        tmp_path / "store.sqlite",
        [
            row(0, "user", INJECTED, "2026-11-16T09:00:00+08:00", cwd),
            row(1, "user", "Fix the login redirect bug", "2026-11-16T01:00:01Z", cwd),
            row(2, "assistant", "Looking at auth.py", "2026-11-16T01:00:02Z", cwd),
        ],
    )


class Runner:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        raise FileNotFoundError("git")  # as if git were not installed


def test_goal_skips_harness_injected_first_user_turn(tmp_path: Path) -> None:
    pack = build_pack(_store(tmp_path), "claude", "s1", git=False)
    assert pack["system"]["goal"] == "Fix the login redirect bug"
    assert pack["system"]["goal_source"] == "first_user_message"
    assert "pnpm" not in pack["system"]["goal"]


def test_goal_none_when_only_injected(tmp_path: Path) -> None:
    db = write_store(
        tmp_path / "store.sqlite", [row(0, "user", INJECTED, "2026-11-16T01:00:00Z", "/w")]
    )
    pack = build_pack(db, "claude", "s1", git=False)
    assert pack["system"]["goal"] == ""
    assert pack["system"]["goal_source"] == "none"


def test_redacted_cwd_skips_git_and_says_why(tmp_path: Path) -> None:
    runner = Runner()
    pack = build_pack(_store(tmp_path), "claude", "s1", git=True, git_runner=runner)
    assert runner.calls == []
    assert pack["system"]["git"] is None
    assert pack["system"]["notes"] == [NOTE_GIT_REDACTED_CWD]
    validate_pack(pack)
    assert f"- note: {NOTE_GIT_REDACTED_CWD}" in render_markdown(pack)


def test_explicit_cwd_overrides_redacted_store_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = Runner()
    pack = build_pack(_store(tmp_path), "claude", "s1", git=True, cwd=repo, git_runner=runner)
    assert runner.calls  # git was attempted for the given work tree
    assert pack["system"]["notes"] == [NOTE_GIT_UNAVAILABLE]


def test_no_notes_field_when_git_not_requested(tmp_path: Path) -> None:
    pack = build_pack(_store(tmp_path), "claude", "s1", git=False)
    assert "notes" not in pack["system"]


def test_notes_schema_is_additive_and_typed(tmp_path: Path) -> None:
    pack = build_pack(_store(tmp_path), "claude", "s1", git=False)
    validate_pack(pack)  # without notes
    pack["system"]["notes"] = ["fine"]
    validate_pack(pack)
    pack["system"]["notes"] = [1]
    with pytest.raises(PackSchemaError):
        validate_pack(pack)


def test_session_bounds_normalise_offset_timestamps(tmp_path: Path) -> None:
    session = load_session(_store(tmp_path), "claude", "s1")
    assert session is not None
    # 09:00+08:00 is 01:00Z: the earliest turn, not the latest (text order says otherwise).
    assert session.started_at == "2026-11-16T01:00:00Z"
    assert session.updated_at == "2026-11-16T01:00:02Z"
    assert session.tail[0].ts == "2026-11-16T01:00:00Z"
