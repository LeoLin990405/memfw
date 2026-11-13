from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.glm import READER

BASE_MS = 1_700_000_000_000
SESSION = "sess-0001"
CHILD = "sess-0002"


# -- SQLite (ZCode app CLI) fixtures ---------------------------------------------


def _make_db(root: Path) -> Path:
    database = root / "db" / "db.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE session (
            id text primary key, project_id text not null, parent_id text,
            directory text not null, path text, title text not null,
            time_created integer not null, time_updated integer not null,
            time_deleted integer
        );
        CREATE TABLE message (
            id text primary key, session_id text not null,
            time_created integer not null, time_updated integer not null,
            data text not null, sequence integer
        );
        CREATE TABLE part (
            id text primary key, message_id text not null, session_id text not null,
            time_created integer not null, time_updated integer not null,
            data text not null, sequence integer
        );
        """
    )
    sessions = [
        (SESSION, "proj", None, "/work/demo", "/work/demo", "demo", BASE_MS, BASE_MS, None),
        (CHILD, "proj", SESSION, "/work/demo", "/work/demo", "child", BASE_MS + 1, BASE_MS, None),
        ("sess-gone", "proj", None, "/work/old", "/work/old", "old", BASE_MS + 2, BASE_MS, BASE_MS),
    ]
    connection.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)", sessions)

    messages: list[tuple[str, str, int, dict[str, Any] | str, list[dict[str, Any] | str]]] = [
        (
            "m0",
            SESSION,
            0,
            {
                "role": "user",
                "time": {"created": BASE_MS},
                "semantics": {"kind": "user_prompt"},
                "contextSnapshot": {"envInfo": {"cwd": "/work/demo/sub"}},
            },
            [{"type": "text", "text": "hello from user", "time": {"start": BASE_MS}}],
        ),
        (
            "m1",
            SESSION,
            1,
            {
                "role": "assistant",
                "time": {"created": BASE_MS + 1000, "completed": BASE_MS + 2000},
                "semantics": {"kind": "assistant_response"},
                "path": {"cwd": "/work/demo", "root": "/work/demo"},
            },
            [
                {"type": "step-start"},
                {"type": "reasoning", "text": "secret thoughts"},
                {"type": "text", "text": "hello from assistant"},
                {
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "Bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls"},
                        "output": "file-a",
                    },
                },
                {
                    "type": "tool",
                    "callID": "call-2",
                    "tool": "Read",
                    "state": {"status": "error", "input": {"path": "x"}, "error": "boom"},
                },
                {"type": "step-finish", "reason": "tool-calls"},
            ],
        ),
        (
            "m2",
            SESSION,
            2,
            {
                "role": "user",
                "time": {"created": BASE_MS + 3000},
                "semantics": {"kind": "system_reminder"},
            },
            [{"type": "text", "text": "reminder noise", "synthetic": True}],
        ),
        (
            "m3",
            SESSION,
            3,
            {"role": "assistant", "time": {"created": BASE_MS + 4000}},
            [{"type": "reasoning", "text": "only thinking"}, {"type": "step-finish"}],
        ),
        ("m4", SESSION, 4, "{not json", []),
        (
            "m5",
            SESSION,
            5,
            {
                "role": "user",
                "time": {"created": BASE_MS + 5000},
                "semantics": {"kind": "background_notification"},
            },
            [{"type": "text", "text": "task finished", "synthetic": True}, "{bad part"],
        ),
        (
            "m6",
            SESSION,
            6,
            {
                "role": "assistant",
                "semantics": {"kind": "assistant_response"},
                "path": {"cwd": "/work/demo"},
            },
            [
                {
                    "type": "tool",
                    "tool": "Write",
                    "state": {"status": "running", "input": {"path": "y"}},
                }
            ],
        ),
        (
            "c0",
            CHILD,
            0,
            {"role": "user", "time": {"created": BASE_MS}, "semantics": {"kind": "user_prompt"}},
            [{"type": "text", "text": "child prompt"}],
        ),
    ]
    part_id = 0
    for message_id, session_id, sequence, data, parts in messages:
        raw = data if isinstance(data, str) else json.dumps(data)
        connection.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?)",
            (message_id, session_id, BASE_MS + sequence * 1000, BASE_MS, raw, sequence),
        )
        for index, part in enumerate(parts):
            part_id += 1
            connection.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
                (
                    f"p{part_id:03d}",
                    message_id,
                    session_id,
                    BASE_MS,
                    BASE_MS,
                    part if isinstance(part, str) else json.dumps(part),
                    index,
                ),
            )
    connection.commit()
    connection.close()
    return database


def _locator(root: Path, session_id: str) -> SessionLocator:
    return next(loc for loc in READER.sessions(root) if loc.session_id == session_id)


def test_reader_identity() -> None:
    assert READER.agent == "glm"
    assert READER.default_roots == (".zcode/cli", ".zcode-cli/sessions")


def test_sqlite_session_discovery(tmp_path: Path) -> None:
    database = _make_db(tmp_path)
    locators = list(READER.sessions(tmp_path))
    assert [loc.session_id for loc in locators] == [SESSION, CHILD]
    assert all(loc.path == database and loc.agent == "glm" for loc in locators)
    assert locators[0].key == f"db/db.sqlite#{SESSION}"
    assert locators[0].extra["cwd"] == "/work/demo"
    assert locators[1].extra["parent_id"] == SESSION


def test_sqlite_records_normalised(tmp_path: Path) -> None:
    _make_db(tmp_path)
    records = list(READER.read(_locator(tmp_path, SESSION)))
    assert [(r.offset, r.role) for r in records] == [
        (0, "user"),
        (2, "assistant"),
        (3, "tool"),
        (10, "system"),
        (12, "assistant"),
    ]
    assert [r.turn for r in records] == [0, 1, 2, 3, 4]
    user, assistant, tool, system, tool_only = records
    assert user.text == "hello from user"
    assert user.cwd == "/work/demo/sub"
    assert user.timestamp == "2023-11-14T22:13:20Z"
    assert assistant.text == "hello from assistant"
    assert "secret" not in assistant.text
    assert [c.name for c in assistant.tool_calls] == ["Bash", "Read"]
    assert "ls" in assistant.tool_calls[0].summary
    assert assistant.cwd == "/work/demo" and assistant.project == "/work/demo"
    assert assistant.timestamp == "2023-11-14T22:13:21Z"
    assert tool.text == "file-a\nboom"
    assert not tool.tool_calls
    assert system.text == "task finished"
    assert tool_only.text == "" and [c.name for c in tool_only.tool_calls] == ["Write"]
    # no time in the JSON: the row's time_created is used
    assert tool_only.timestamp == "2023-11-14T22:13:26Z"
    assert all(r.session_id == SESSION and r.agent == "glm" for r in records)


def test_sqlite_resume_after(tmp_path: Path) -> None:
    _make_db(tmp_path)
    locator = _locator(tmp_path, SESSION)
    full = list(READER.read(locator))
    resumed = list(READER.read(locator, after=2))
    assert [r.offset for r in resumed] == [3, 10, 12]
    assert [r.turn for r in resumed] == [r.turn for r in full if r.offset > 2]
    assert list(READER.read(locator, after=12)) == []


def test_sqlite_child_session(tmp_path: Path) -> None:
    _make_db(tmp_path)
    records = list(READER.read(_locator(tmp_path, CHILD)))
    assert [(r.role, r.text, r.cwd) for r in records] == [("user", "child prompt", "/work/demo")]


def test_sqlite_unknown_schema_yields_nothing(tmp_path: Path) -> None:
    database = tmp_path / "db" / "db.sqlite"
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE other (id text)")
    connection.commit()
    connection.close()
    assert list(READER.sessions(tmp_path)) == []
    (tmp_path / "db" / "db.sqlite").write_bytes(b"not a database")
    assert list(READER.sessions(tmp_path)) == []
    locator = SessionLocator("glm", database, "db/db.sqlite#x", "x", {"store": "sqlite"})
    assert list(READER.read(locator)) == []


# -- JSON snapshot (clean-room zcode-cli) fixtures ----------------------------------


def _event(kind: str, ms: int, **payload: Any) -> dict[str, Any]:
    return {
        "ts": "2023-11-14T22:13:20.000Z",
        "type": "event",
        "event": {
            "type": kind,
            "seq": 1,
            "sessionId": SESSION,
            "timestamp": ms,
            "payload": payload,
        },
    }


def _make_json_store(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "version": 1,
        "meta": {
            "id": SESSION,
            "cwd": "/work/demo",
            "model": "m",
            "title": "t",
            "createdAt": "2023-11-14T22:00:00.000Z",
            "updatedAt": "2023-11-14T23:00:00.000Z",
            "version": 1,
        },
        "messages": [
            {"role": "user", "content": "hello from user"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "hello from assistant"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "/work/demo"}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t2", "content": ""}],
            },
            "not a message",
            {"role": "user", "content": "second prompt"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t3", "name": "Read", "input": {"path": "a"}}
                ],
            },
        ],
    }
    path = root / f"sess_{SESSION}.json"
    path.write_text(json.dumps(snapshot))
    lines = [
        json.dumps(_event("session.created", BASE_MS, cwd="/work/demo")),
        "{broken",
        json.dumps(_event("turn.started", BASE_MS + 1000, input="x", turnNumber=1)),
        json.dumps(_event("model.streaming", BASE_MS + 1500, delta="d")),
        json.dumps(_event("turn.started", BASE_MS + 60_000, input="y", turnNumber=2)),
    ]
    (root / f"sess_{SESSION}.jsonl").write_text("\n".join(lines) + "\n")
    (root / f"sess_{SESSION}").mkdir()
    (root / "sess_broken.json").write_text("{not json")
    (root / "sess_other.json").write_text(json.dumps({"meta": {}, "messages": "nope"}))
    (root / "notes.json").write_text(json.dumps({"messages": []}))
    return path


def test_json_session_discovery(tmp_path: Path) -> None:
    path = _make_json_store(tmp_path)
    locators = list(READER.sessions(tmp_path))
    assert [(loc.session_id, loc.path, loc.key) for loc in locators] == [
        (SESSION, path, f"sess_{SESSION}.json")
    ]


def test_json_records_normalised(tmp_path: Path) -> None:
    _make_json_store(tmp_path)
    records = list(READER.read(_locator(tmp_path, SESSION)))
    assert [(r.offset, r.turn, r.role) for r in records] == [
        (0, 0, "user"),
        (1, 1, "assistant"),
        (2, 2, "tool"),
        (5, 3, "user"),
        (6, 4, "assistant"),
    ]
    user, assistant, tool, second, tool_only = records
    assert user.text == "hello from user"
    assert user.timestamp == "2023-11-14T22:13:21Z"
    assert assistant.text == "hello from assistant"
    assert [c.name for c in assistant.tool_calls] == ["Bash"]
    assert "pwd" in assistant.tool_calls[0].summary
    assert assistant.timestamp == "2023-11-14T22:13:21Z"
    assert tool.text == "/work/demo"
    assert second.timestamp == "2023-11-14T22:14:20Z"
    assert tool_only.text == "" and [c.name for c in tool_only.tool_calls] == ["Read"]
    assert all(r.cwd == "/work/demo" and r.session_id == SESSION for r in records)


def test_json_resume_after(tmp_path: Path) -> None:
    _make_json_store(tmp_path)
    locator = _locator(tmp_path, SESSION)
    resumed = list(READER.read(locator, after=2))
    assert [(r.offset, r.turn) for r in resumed] == [(5, 3), (6, 4)]


def test_json_without_event_log_uses_created_at(tmp_path: Path) -> None:
    _make_json_store(tmp_path)
    (tmp_path / f"sess_{SESSION}.jsonl").unlink()
    records = list(READER.read(_locator(tmp_path, SESSION)))
    assert {r.timestamp for r in records} == {"2023-11-14T22:00:00.000Z"}


def test_empty_and_unknown_layouts(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    assert list(READER.sessions(tmp_path)) == []
    (tmp_path / "random.txt").write_text("x")
    assert list(READER.sessions(tmp_path)) == []
    missing = SessionLocator("glm", tmp_path / "sess_x.json", "sess_x.json", "x")
    assert list(READER.read(missing)) == []
