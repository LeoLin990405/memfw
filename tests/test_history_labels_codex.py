"""Codex reader session labels: origin, parent session, model and project.

Every fixture is synthetic and written under ``tmp_path``; the session_meta
shapes mirror the Codex rollout format (keys and enum values only).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources import READERS
from memfw.history.sources.codex import PARSER_VERSION

CWD = "/home/user/work/demo-repo"


def _meta(**payload: Any) -> dict[str, Any]:
    base = {"id": "thread-1", "cwd": CWD, "cli_version": "0.0.0", "originator": "codex-tui"}
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": base | payload}


def _message(role: str, text: str, stamp: str = "2026-01-01T00:00:01Z") -> dict[str, Any]:
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "timestamp": stamp,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
    }


def _turn_context(model: str = "gpt-test") -> dict[str, Any]:
    return {
        "timestamp": "2026-01-01T00:00:02Z",
        "type": "turn_context",
        "payload": {"cwd": CWD, "model": model},
    }


def _rows(meta: dict[str, Any]) -> list[dict[str, Any]]:
    # Real rollouts put the instructions and first prompt before turn_context.
    return [
        meta,
        _message("developer", "instructions"),
        _message("user", "please fix the bug"),
        _turn_context(),
        _message("assistant", "done"),
    ]


def _write(path: Path, rows: list[dict[str, Any]], mode: str = "w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _read(tmp_path: Path, meta: dict[str, Any]) -> list[Any]:
    root = tmp_path / "sessions"
    _write(root / "2026" / "01" / "01" / "rollout-1.jsonl", _rows(meta))
    reader = READERS["codex"]
    return [r for loc in reader.sessions(root) for r in reader.read(loc)]


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_subagent_thread_spawn(tmp_path: Path) -> None:
    spawn = {"parent_thread_id": "thread-parent", "depth": 1, "agent_nickname": "n"}
    records = _read(
        tmp_path,
        _meta(
            id="thread-child",
            originator="codex_exec",  # inherited from the parent process
            source={"subagent": {"thread_spawn": spawn}},
            thread_source="subagent",
            parent_thread_id="thread-parent",
        ),
    )
    assert records
    assert {r.origin for r in records} == {"subagent"}
    assert {r.parent_session for r in records} == {"thread-parent"}


def test_subagent_parent_falls_back_to_forked_from(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(source={"subagent": {}}, forked_from_id="thread-origin"))
    assert {(r.origin, r.parent_session) for r in records} == {("subagent", "thread-origin")}


def test_guardian_review_is_automated(tmp_path: Path) -> None:
    records = _read(
        tmp_path,
        _meta(
            originator="Codex Desktop",
            source={"subagent": {"other": "guardian"}},
            thread_source="guardian_review",
            parent_thread_id="thread-reviewed",
        ),
    )
    assert {(r.origin, r.parent_session) for r in records} == {("automated", "thread-reviewed")}


def test_exec_is_automated(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(source="exec", originator="codex_exec"))
    assert {(r.origin, r.parent_session) for r in records} == {("automated", "")}


def test_exec_originator_without_source_is_automated(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(originator="codex_exec"))
    assert {r.origin for r in records} == {"automated"}


def test_conductor_native_is_automated(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(source="vscode", originator="conductor-native"))
    assert {r.origin for r in records} == {"automated"}


def test_interactive_cli_left_unlabelled(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(source="cli", originator="codex-tui", thread_source="user"))
    assert records
    assert {(r.origin, r.parent_session) for r in records} == {("", "")}


def test_model_and_project_filled_before_turn_context(tmp_path: Path) -> None:
    records = _read(tmp_path, _meta(source="cli"))
    # The instructions and first prompt precede turn_context in the file.
    assert [r.role for r in records] == ["system", "user", "assistant"]
    assert {r.model for r in records} == {"gpt-test"}
    assert {r.project for r in records} == {"demo-repo"}
    assert {r.cwd for r in records} == {CWD}


def test_incremental_read_keeps_labels(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = _write(root / "rollout-1.jsonl", _rows(_meta(source={"subagent": {}})))
    reader = READERS["codex"]
    (locator,) = list(reader.sessions(root))
    last = max(r.offset for r in reader.read(locator))
    _write(path, [_message("assistant", "more", "2026-01-01T00:00:09Z")], mode="a")
    tail = list(reader.read(locator, after=last))
    assert [r.text for r in tail] == ["more"]
    assert tail[0].origin == "subagent"
    assert tail[0].model == "gpt-test"


def test_store_origin_column_with_incremental_append(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = _write(
        root / "2026" / "01" / "01" / "rollout-1.jsonl",
        _rows(_meta(source="exec", originator="codex_exec")),
    )
    store = ConversationStore(tmp_path / "data" / "conv.sqlite")
    reader = READERS["codex"]
    assert store.ingest(reader, [root]).records_new == 3
    _write(path, [_message("assistant", "appended", "2026-01-01T00:00:09Z")], mode="a")
    assert store.ingest(reader, [root]).records_new == 1
    connection = sqlite3.connect(store.db)
    try:
        rows = connection.execute(
            "SELECT text, origin, model, project FROM conversation_turns ORDER BY offset"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 4
    assert rows[-1][0] == "appended"
    assert {row[1] for row in rows} == {"automated"}
    assert {row[2] for row in rows} == {"gpt-test"}
    assert {row[3] for row in rows} == {"demo-repo"}
