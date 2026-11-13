"""MiniMax CLI conversation reader, against synthetic v2 session ledgers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.minimax import READER

T0 = 1_762_400_000_000


def _assistant(content: list[dict[str, Any]], ts: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "api": "demo-api",
        "provider": "demo",
        "model": "demo-model",
        "stopReason": "stop",
        "timestamp": ts,
        "responseId": "resp-0001",
        "usage": {"input": 1, "output": 1, "totalTokens": 2},
    }


def _lines() -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello from user"}],
            "timestamp": T0,
            "canonicalTextRange": {"startOffset": 0, "endOffset": 15},
        },
        _assistant(
            [
                {"type": "thinking", "thinking": "secret plan", "thinkingSignature": "sig"},
                {"type": "text", "text": "hello from assistant"},
            ],
            T0 + 1000,
        ),
        _assistant(
            [
                {
                    "type": "toolCall",
                    "id": "call-0001",
                    "name": "bash",
                    "arguments": {"command": "ls /work/demo", "description": "list"},
                }
            ],
            T0 + 2000,
        ),
        {
            "role": "toolResult",
            "toolCallId": "call-0001",
            "toolName": "bash",
            "content": [{"type": "text", "text": "file-a\nfile-b"}],
            "details": {"status": "completed"},
            "isError": False,
            "timestamp": T0 + 3000,
        },
        {
            "role": "custom",
            "customType": "todo_reminder",
            "content": "hidden reminder",
            "display": False,
            "timestamp": T0 + 3500,
        },
        {"role": "compactionSummary", "summary": "earlier stuff", "timestamp": T0 + 3600},
        {"role": "user", "content": "second question", "timestamp": T0 + 4000},
    ]
    out = [
        {"message_id": f"msg-{i:04d}", "turn_id": "turn-0001", "message": m}
        for i, m in enumerate(msgs)
    ]
    out.insert(
        6,
        {
            "message_id": "msg-hist",
            "turn_id": "turn-0002",
            "message": {"role": "user", "content": [{"type": "text", "text": "replayed"}]},
            "history_artifact": {"schemaVersion": 1, "generation": 1},
        },
    )
    return out


def _write_session(
    root: Path, rel: str, session_id: str, lines: list[Any], *, tail: str = ""
) -> Path:
    directory = root / rel
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "sessionId": session_id,
                "createdAtMs": T0,
                "updatedAtMs": T0,
                "source": "local-runtime",
                "layout": "v2-final-dated-session",
                "paths": {},
            }
        )
    )
    body = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    path = directory / "messages.jsonl"
    path.write_text(body + tail)
    (directory / "user-message-locators.jsonl").write_text("{}\n")
    return path


def _write_index(root: Path, rows: list[tuple[str, str, str]]) -> None:
    db = root.parent / "sqlite"
    db.mkdir(parents=True)
    conn = sqlite3.connect(db / "runtime-state.sqlite")
    conn.execute(
        "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, record_json TEXT,"
        " updated_at_ms INTEGER, workspace_dir TEXT, project_workspace_dir TEXT)"
    )
    conn.executemany(
        "INSERT INTO local_runtime_sessions VALUES (?, '{}', 0, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / ".minimax" / "v2" / "sessions"
    root.mkdir(parents=True)
    return root


def test_reader_identity() -> None:
    assert READER.agent == "minimax"
    assert READER.default_roots == (".minimax/v2/sessions",)


def test_sessions_discovered_with_ids_and_cwd(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_session(root, "2026/11/01/10-00-00-000-session_a", "mvs_0001", _lines())
    _write_session(root, "2026/11/02/11-00-00-000-session_b", "mvs_0002", _lines())
    snap = root / "2026/11/01/10-00-00-000-session_a/snapshots"
    snap.mkdir()
    (snap / "g000000000000--ctx_x.jsonl").write_text("{}\n")
    _write_index(root, [("mvs_0001", "/work/demo", "/work")])

    found = list(READER.sessions(root))
    assert [s.session_id for s in found] == ["mvs_0001", "mvs_0002"]
    assert found[0].key == "2026/11/01/10-00-00-000-session_a/messages.jsonl"
    assert found[0].extra["cwd"] == "/work/demo"
    assert found[0].extra["project"] == "/work"
    assert found[1].extra["cwd"] == ""


def test_session_id_falls_back_to_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _write_session(root, "2026/11/01/x-session_c", "", _lines())
    (path.parent / "manifest.json").write_text("not json")
    [loc] = list(READER.sessions(root))
    assert loc.session_id == "x-session_c"


def test_read_normalises_records(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_session(root, "2026/11/01/s", "mvs_0001", _lines())
    _write_index(root, [("mvs_0001", "/work/demo", "/work")])
    [loc] = list(READER.sessions(root))
    records = list(READER.read(loc))

    assert [r.role for r in records] == ["user", "assistant", "assistant", "tool", "user"]
    assert [r.turn for r in records] == [0, 1, 2, 3, 4]
    assert [r.text for r in records] == [
        "hello from user",
        "hello from assistant",
        "",
        "file-a\nfile-b",
        "second question",
    ]
    assert "secret plan" not in "".join(r.text for r in records)
    assert records[2].tool_calls[0].name == "bash"
    assert "/work/demo" in records[2].tool_calls[0].summary
    assert records[0].timestamp.startswith("2025-11-06T")
    assert records[0].timestamp.endswith("Z")
    assert all(r.session_id == "mvs_0001" and r.agent == "minimax" for r in records)
    assert all(r.cwd == "/work/demo" and r.project == "/work" for r in records)
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and offsets[0] == 0


def test_read_looks_up_cwd_without_extra(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _write_session(root, "2026/11/01/s", "mvs_0001", _lines())
    _write_index(root, [("mvs_0001", "/work/demo", "/work")])
    loc = SessionLocator("minimax", path, "k", "mvs_0001", {"root": str(root)})
    assert {r.cwd for r in READER.read(loc)} == {"/work/demo"}
    bare = SessionLocator("minimax", path, "k", "mvs_0001")
    assert {r.cwd for r in READER.read(bare)} == {""}


def test_display_custom_message_is_system(tmp_path: Path) -> None:
    root = _root(tmp_path)
    line = {
        "message_id": "m",
        "turn_id": "t",
        "message": {"role": "custom", "content": "shown note", "display": True, "timestamp": T0},
    }
    _write_session(root, "s", "mvs_0003", [line])
    [loc] = list(READER.sessions(root))
    [rec] = list(READER.read(loc))
    assert (rec.role, rec.text) == ("system", "shown note")


def test_after_resume(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_session(root, "s", "mvs_0001", _lines())
    [loc] = list(READER.sessions(root))
    full = list(READER.read(loc))
    resumed = list(READER.read(loc, after=full[2].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in full[2:]]
    assert [r.turn for r in resumed] == [2, 3, 4]
    assert list(READER.read(loc, after=full[-1].offset)) == []


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    lines: list[Any] = [
        "not json at all",
        '["a", "list"]',
        {"message": "a string"},
        {"message": {"role": "user", "content": 42}},
        {"message": {"role": "mystery", "content": "x"}},
        {"message": {"role": "assistant", "content": [None, 3, {"type": "toolCall"}]}},
        {
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "ok"}],
                "timestamp": "x",
            }
        },
    ]
    _write_session(root, "s", "mvs_0001", lines, tail='{"message": {"role": "user", "con')
    [loc] = list(READER.sessions(root))
    records = list(READER.read(loc))
    assert [(r.role, r.text) for r in records] == [("assistant", ""), ("user", "ok")]
    assert records[0].tool_calls[0].name == "tool"
    assert records[1].timestamp == "x"


def test_broken_index_is_ignored(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write_session(root, "s", "mvs_0001", _lines())
    (root.parent / "sqlite").mkdir()
    (root.parent / "sqlite" / "runtime-state.sqlite").write_bytes(b"garbage" * 100)
    [loc] = list(READER.sessions(root))
    assert loc.extra["cwd"] == ""
    assert len(list(READER.read(loc))) == 5


def test_empty_and_unknown_layouts(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    root = _root(tmp_path)
    assert list(READER.sessions(root)) == []
    (root / "other.jsonl").write_text('{"message": {"role": "user", "content": "x"}}\n')
    (root / "user-messages.jsonl").write_text("{}\n")
    assert list(READER.sessions(root)) == []
    gone = SessionLocator("minimax", root / "nope" / "messages.jsonl", "k", "mvs_x")
    assert list(READER.read(gone)) == []
