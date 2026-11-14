from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.sources import default_roots, get_reader
from memfw.history.sources.antigravity import READER
from memfw.history.sources.base import SessionLocator

CONVERSATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
WORKSPACE = "/work/zztester/work/demo"


def _write(path: Path, rows: list[Any], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def _step(index: int, kind: str, source: str, **fields: Any) -> dict[str, Any]:
    return {
        "step_index": index,
        "source": source,
        "type": kind,
        "status": "DONE",
        "created_at": f"2026-01-02T03:04:{index:02d}Z",
        **fields,
    }


def _full_steps() -> list[Any]:
    return [
        _step(0, "USER_INPUT", "USER_EXPLICIT", content="please list the files"),
        _step(1, "CONVERSATION_HISTORY", "SYSTEM", content="boilerplate"),
        _step(
            2,
            "PLANNER_RESPONSE",
            "MODEL",
            content="Listing now.",
            thinking="secret thinking",
            tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": WORKSPACE}}],
        ),
        _step(3, "LIST_DIRECTORY", "SYSTEM", content="a.txt\nb.txt"),
        "{not json",
        _step(4, "CORTEX_STEP_TYPE_RUN_COMMAND", "CORTEX_STEP_SOURCE_SYSTEM", content=""),
        _step(5, "USER_INPUT", "USER_IMPLICIT", content="auto context"),
        _step(6, "ERROR_MESSAGE", "SYSTEM", content="quota exceeded"),
        _step(
            7,
            "PLANNER_RESPONSE",
            "MODEL",
            content="Done: two files.",
            tool_calls=[
                {"function": {"name": "notify", "arguments": '{"Message": "hi"}'}},
                "bad",
            ],
        ),
        ["not", "an", "object"],
    ]


def _store(home: Path, *, with_db: bool = True) -> Path:
    root = home / ".gemini" / "antigravity"
    logs = root / "brain" / CONVERSATION / ".system_generated" / "logs"
    full = _write(logs / "transcript_full.jsonl", _full_steps())
    compact = _full_steps()
    compact[0] = _step(0, "USER_INPUT", "USER_EXPLICIT", content="please li…")
    compact[0]["truncated_fields"] = ["content"]
    # A step present only in the compact file (its full line was unreadable).
    compact.append(_step(8, "USER_INPUT", "USER_EXPLICIT", content="thanks"))
    _write(logs / "transcript.jsonl", compact)
    (root / "brain" / CONVERSATION / "task.md").write_text("# task", encoding="utf-8")
    (root / "brain" / "tempmediaStorage").mkdir()
    (root / "conversations").mkdir()
    (root / "conversations" / f"{CONVERSATION}.pb").write_bytes(os.urandom(64))
    if with_db:
        connection = sqlite3.connect(root / "conversation_summaries.db")
        connection.execute(
            "CREATE TABLE conversation_summaries (conversation_id text PRIMARY KEY,"
            " title text, workspace_uris text, project_id text, parent_conversation_id text)"
        )
        connection.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
            (CONVERSATION, "t", json.dumps([f"file://{WORKSPACE}"]), "proj-1", ""),
        )
        connection.commit()
        connection.close()
    return full


def test_registered_with_aliases(tmp_path: Path) -> None:
    for name in ("antigravity", "agy", "antigravity-cli", "antigravity-ide", "jetski"):
        assert get_reader(name) is READER
    assert READER.append_only is False
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)
    assert default_roots(READER, tmp_path) == [tmp_path / ".gemini" / "antigravity-cli"]


def test_sessions_and_records(tmp_path: Path) -> None:
    full = _store(tmp_path)
    [root] = default_roots(READER, tmp_path)
    [locator] = list(READER.sessions(root))
    assert locator.path == full
    assert locator.session_id == CONVERSATION
    assert locator.key == f"brain/{CONVERSATION}/.system_generated/logs/transcript_full.jsonl"
    records = list(READER.read(locator))
    assert [(r.offset, r.role) for r in records] == [
        (0, "user"),
        (2, "assistant"),
        (3, "tool"),
        (5, "system"),
        (6, "system"),
        (7, "assistant"),
        (8, "user"),
    ]
    assert [r.turn for r in records] == list(range(7))
    user, plan, tool, _implicit, error, done, thanks = records
    assert user.text == "please list the files"  # the full line wins
    assert user.timestamp == "2026-01-02T03:04:00Z"
    assert user.cwd == WORKSPACE and user.project == "proj-1"
    assert plan.text == "Listing now."
    assert plan.tool_calls[0].name == "list_dir" and WORKSPACE in plan.tool_calls[0].summary
    assert tool.text == "a.txt\nb.txt"
    assert error.text == "quota exceeded"
    assert [c.name for c in done.tool_calls] == ["notify"]
    assert "hi" in done.tool_calls[0].summary
    assert thanks.text == "thanks"
    joined = " ".join(r.text for r in records)
    assert "secret" not in joined and "boilerplate" not in joined


def test_incremental_after(tmp_path: Path) -> None:
    _store(tmp_path)
    [locator] = list(READER.sessions(tmp_path / ".gemini" / "antigravity"))
    later = list(READER.read(locator, after=3))
    assert [r.offset for r in later] == [5, 6, 7, 8]
    assert [r.turn for r in later] == [3, 4, 5, 6]


def test_compact_only_and_no_database(tmp_path: Path) -> None:
    root = tmp_path / "agy"
    logs = root / "brain" / "conv-2" / ".system_generated" / "logs"
    path = _write(
        logs / "transcript.jsonl",
        [
            {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "no index"},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "ok"},
        ],
    )
    (root / "conversation_summaries.db").write_bytes(b"not a database")
    [locator] = list(READER.sessions(root))
    assert locator.path == path and locator.extra["cwd"] == ""
    assert [(r.offset, r.text) for r in READER.read(locator)] == [(0, "no index"), (1, "ok")]


def test_malformed_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    logs = root / "brain" / "conv-3" / ".system_generated" / "logs"
    _write(logs / "transcript_full.jsonl", ["{oops", "[]", '"s"'], tail='{"step_index": 9')
    [locator] = list(READER.sessions(root))
    assert list(READER.read(locator)) == []
    assert list(READER.sessions(tmp_path / "absent")) == []
    missing = SessionLocator("antigravity", tmp_path / "x" / "transcript.jsonl", key="x")
    assert list(READER.read(missing)) == []
    # Symlinked conversation directories are not followed.
    os.symlink(root / "brain" / "conv-3", root / "brain" / "conv-link")
    assert [loc.session_id for loc in READER.sessions(root)] == ["conv-3"]


def test_opaque_stores_never_read(tmp_path: Path, monkeypatch: Any) -> None:
    _store(tmp_path, with_db=False)
    gemini = tmp_path / ".gemini"
    (gemini / "oauth_creds.json").write_text('{"zz": "fake"}', encoding="utf-8")
    from memfw.history.sources import base as base_module

    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(base_module.os, "open", spy)
    for root in default_roots(READER, tmp_path):
        for locator in READER.sessions(root):
            list(READER.read(locator))
    assert opened
    assert not [p for p in opened if p.endswith(".pb") or "oauth_creds" in p or "task.md" in p]
