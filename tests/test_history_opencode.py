"""OpenCode reader: synthetic SQLite (current and older) and JSON file stores."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.opencode import READER

T0 = 1_700_000_000_000


def _v2_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL, name TEXT);
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
            title TEXT, time_created INTEGER NOT NULL
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO project VALUES ('prj-1', '/work/demo', NULL)")
    connection.execute(
        "INSERT INTO session_v2 VALUES ('sess-0001', 'prj-1', '/work/demo', 't', ?)", (T0,)
    )
    connection.execute(
        "INSERT INTO session_v2 VALUES ('sess-0002', 'prj-1', '/work/other', 't', ?)", (T0 + 5,)
    )
    rows: list[tuple[str, int, Any]] = [
        ("user", 1, {"text": "hello from user", "files": [], "time": {"created": T0}}),
        (
            "assistant",
            2,
            {
                "agent": "build",
                "content": [
                    {"type": "reasoning", "text": "secret thoughts", "state": {}},
                    {"type": "text", "text": "hello from assistant"},
                ],
                "time": {"created": T0 + 1000, "completed": T0 + 2000},
            },
        ),
        (
            "assistant",
            3,
            {
                "content": [
                    {
                        "type": "tool",
                        "id": "call-1",
                        "name": "bash",
                        "executed": True,
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls /work/demo"},
                            "content": [{"type": "text", "text": "tool output text"}],
                        },
                    }
                ],
                "time": {"created": T0 + 3000},
            },
        ),
        ("idle", 4, {"outcome": "done", "time": {"created": T0 + 4000}}),
        ("user", 5, "{not json"),
        ("synthetic", 6, {"text": "job finished", "metadata": {"shellID": "sh-1"}, "time": {}}),
        ("synthetic", 7, {"text": "instructions loaded", "metadata": {"instruction": {}}}),
        ("assistant", 8, {"content": "not-a-list"}),
        ("user", 9, {"text": "second question", "time": {"created": T0 + 9000}}),
    ]
    for kind, seq, data in rows:
        payload = data if isinstance(data, str) else json.dumps(data)
        connection.execute(
            "INSERT INTO session_message VALUES (?, 'sess-0001', ?, ?, ?, ?, ?)",
            (f"m{seq}", kind, seq, T0 + seq * 1000, T0, payload),
        )
    connection.execute(
        "INSERT INTO session_message VALUES ('x1', 'sess-0002', 'user', 1, ?, ?, ?)",
        (T0, T0, json.dumps({"text": "other session"})),
    )
    connection.commit()
    connection.close()


def _v1_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, time_created INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT);
        """
    )
    connection.execute("INSERT INTO session VALUES ('sess-0100', '/work/demo', ?)", (T0,))
    connection.execute(
        "INSERT INTO message VALUES ('msg-a', 'sess-0100', ?, ?)",
        (T0, json.dumps({"role": "user", "time": {"created": T0}})),
    )
    connection.execute(
        "INSERT INTO message VALUES ('msg-b', 'sess-0100', ?, ?)",
        (T0 + 1, json.dumps({"role": "assistant", "time": {"created": T0 + 1}})),
    )
    connection.execute("INSERT INTO message VALUES ('msg-c', 'sess-0100', ?, 'garbage')", (T0 + 2,))
    parts = [
        ("p1", "msg-a", {"type": "text", "text": "hello from user"}),
        ("p2", "msg-b", {"type": "reasoning", "text": "hidden"}),
        ("p3", "msg-b", {"type": "text", "text": "hello from assistant"}),
        ("p4", "msg-b", {"type": "tool", "tool": "read", "state": {"input": {"path": "a.txt"}}}),
    ]
    for part_id, message_id, data in parts:
        connection.execute(
            "INSERT INTO part VALUES (?, ?, 'sess-0100', ?)",
            (part_id, message_id, json.dumps(data)),
        )
    connection.commit()
    connection.close()


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _json_store(root: Path) -> None:
    storage = root / "storage"
    _write(
        storage / "session" / "prj-1" / "sess-0200.json",
        {
            "id": "sess-0200",
            "projectID": "prj-1",
            "directory": "/work/demo",
            "time": {"created": T0},
        },
    )
    _write(storage / "project" / "prj-1.json", {"id": "prj-1", "worktree": "/work/demo"})
    messages = storage / "message" / "sess-0200"
    _write(messages / "msg-1.json", {"id": "msg-1", "role": "user", "time": {"created": T0}})
    _write(
        messages / "msg-2.json", {"id": "msg-2", "role": "assistant", "time": {"created": T0 + 1}}
    )
    _write(messages / "msg-3.json", "{broken")
    parts = storage / "part"
    _write(
        parts / "msg-1" / "prt-1.json", {"id": "prt-1", "type": "text", "text": "hello from user"}
    )
    _write(parts / "msg-2" / "prt-1.json", {"id": "prt-1", "type": "reasoning", "text": "hidden"})
    _write(
        parts / "msg-2" / "prt-2.json",
        {"id": "prt-2", "type": "tool", "tool": "bash", "state": {"input": {"command": "pwd"}}},
    )


def test_default_roots_and_agent() -> None:
    assert READER.agent == "opencode"
    assert READER.default_roots == (".local/share/opencode",)


def test_v2_sessions_discovered_one_per_session(tmp_path: Path) -> None:
    _v2_db(tmp_path / "opencode.db")
    locators = list(READER.sessions(tmp_path))
    assert [loc.session_id for loc in locators] == ["sess-0001", "sess-0002"]
    assert locators[0].key == "opencode.db#sess-0001"
    assert all(loc.path == tmp_path / "opencode.db" for loc in locators)


def test_v2_normalisation(tmp_path: Path) -> None:
    _v2_db(tmp_path / "opencode.db")
    locator = next(iter(READER.sessions(tmp_path)))
    records = list(READER.read(locator))
    assert [(r.offset, r.turn, r.role) for r in records] == [
        (1, 0, "user"),
        (2, 1, "assistant"),
        (3, 2, "assistant"),
        (6, 3, "tool"),
        (7, 4, "system"),
        (9, 5, "user"),
    ]
    first, second, tool_turn = records[0], records[1], records[2]
    assert first.text == "hello from user"
    assert first.timestamp == "2023-11-14T22:13:20Z"
    assert first.cwd == "/work/demo" and first.project == "demo"
    assert second.text == "hello from assistant"
    assert "secret" not in second.text
    assert tool_turn.text == ""
    assert [c.name for c in tool_turn.tool_calls] == ["bash"]
    assert "ls /work/demo" in tool_turn.tool_calls[0].summary
    assert all("tool output" not in r.text for r in records)
    assert records[3].timestamp  # falls back to the row time


def test_v2_after_resume(tmp_path: Path) -> None:
    _v2_db(tmp_path / "opencode.db")
    locator = next(iter(READER.sessions(tmp_path)))
    resumed = list(READER.read(locator, after=2))
    assert [(r.offset, r.turn) for r in resumed] == [(3, 2), (6, 3), (7, 4), (9, 5)]
    assert list(READER.read(locator, after=9)) == []


def test_v1_sqlite_layout(tmp_path: Path) -> None:
    _v1_db(tmp_path / "opencode.db")
    locators = list(READER.sessions(tmp_path))
    assert [loc.key for loc in locators] == ["opencode.db#sess-0100"]
    records = list(READER.read(locators[0]))
    assert [(r.offset, r.role, r.text) for r in records] == [
        (0, "user", "hello from user"),
        (1, "assistant", "hello from assistant"),
    ]
    assert records[1].tool_calls[0].name == "read"
    assert records[0].cwd == "/work/demo"
    assert list(READER.read(locators[0], after=0))[0].offset == 1


def test_json_store_layout(tmp_path: Path) -> None:
    _json_store(tmp_path)
    locators = list(READER.sessions(tmp_path))
    assert [(loc.key, loc.session_id) for loc in locators] == [
        ("storage/session/prj-1/sess-0200.json", "sess-0200")
    ]
    records = list(READER.read(locators[0]))
    assert [(r.offset, r.turn, r.role, r.text) for r in records] == [
        (0, 0, "user", "hello from user"),
        (1, 1, "assistant", ""),
    ]
    assert records[1].tool_calls[0].name == "bash"
    assert records[0].cwd == "/work/demo" and records[0].project == "demo"
    assert records[0].timestamp.startswith("2023-11-14")
    assert [r.offset for r in READER.read(locators[0], after=0)] == [1]


def test_empty_and_unknown_layouts(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    assert list(READER.sessions(tmp_path)) == []
    (tmp_path / "opencode.db").write_bytes(b"not a database at all")
    assert list(READER.sessions(tmp_path)) == []
    other = tmp_path / "other"
    other.mkdir()
    connection = sqlite3.connect(other / "opencode.db")
    connection.execute("CREATE TABLE unrelated (x)")
    connection.commit()
    connection.close()
    assert list(READER.sessions(other)) == []
    ghost = SessionLocator("opencode", other / "opencode.db", "opencode.db#x", "x")
    assert list(READER.read(ghost)) == []


def test_store_is_not_modified(tmp_path: Path) -> None:
    database = tmp_path / "opencode.db"
    _v2_db(database)
    before = database.read_bytes()
    for locator in READER.sessions(tmp_path):
        list(READER.read(locator))
    assert database.read_bytes() == before
    assert not (tmp_path / "opencode.db-wal").exists()
