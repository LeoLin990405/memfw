from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.stepfun import READER


def _write(path: Path, entries: list[Any], *, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def _header(session_id: str = "sess-0001", **extra: Any) -> dict[str, Any]:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "cwd": "/work/demo",
        **extra,
    }


def _message(
    role: str, content: Any, ts: str | None = "2026-01-01T00:00:01.000Z", **msg: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "message",
        "id": "e1",
        "parentId": "e0",
        "message": {"role": role, "content": content, "timestamp": 1767225600000, **msg},
    }
    if ts is not None:
        entry["timestamp"] = ts
    return entry


def _session(root: Path, name: str = "2026-01-01T00-00-00-000Z_sess-0001.jsonl") -> Path:
    return _write(
        root / "--work-demo--" / name,
        [
            _header(),
            {
                "type": "model_change",
                "id": "m",
                "parentId": None,
                "timestamp": "x",
                "provider": "p",
                "modelId": "m",
            },
            {
                "type": "thinking_level_change",
                "id": "t",
                "parentId": "m",
                "timestamp": "x",
                "thinkingLevel": "high",
            },
            {
                "type": "custom",
                "customType": "state",
                "data": {"version": 1},
                "id": "c",
                "parentId": "t",
                "timestamp": "x",
            },
            _message("user", [{"type": "text", "text": "hello from user"}]),
            _message(
                "assistant",
                [
                    {"type": "thinking", "thinking": "secret plan", "thinkingSignature": "sig"},
                    {"type": "text", "text": "hello from assistant"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "read",
                        "arguments": {"path": "/work/demo/a.txt"},
                    },
                ],
                api="a",
                provider="p",
                model="m",
                stopReason="toolUse",
            ),
            _message(
                "toolResult",
                [{"type": "text", "text": "file contents"}],
                toolCallId="call-1",
                toolName="read",
                isError=False,
            ),
            _message(
                "assistant",
                [
                    {
                        "type": "toolCall",
                        "id": "call-2",
                        "name": "bash",
                        "arguments": {"command": "ls"},
                    }
                ],
            ),
            _message(
                "assistant",
                [{"type": "thinking", "thinking": "only thoughts", "thinkingSignature": "s"}],
            ),
            {
                "type": "custom_message",
                "customType": "goal",
                "content": "continue the goal",
                "display": True,
                "details": {"goalId": "g1", "iteration": 1},
                "id": "cm",
                "parentId": "e1",
                "timestamp": "2026-01-01T00:00:05.000Z",
            },
            {
                "type": "compaction",
                "id": "k",
                "parentId": "cm",
                "timestamp": "x",
                "summary": "summary text",
            },
            _message("user", "plain string content", ts=None),
        ],
    )


def _locator(path: Path, root: Path) -> SessionLocator:
    found = [s for s in READER.sessions(root) if s.path == path]
    assert len(found) == 1
    return found[0]


def test_reader_identity() -> None:
    assert READER.agent == "stepfun"
    assert READER.default_roots == (".stepcode/agent/sessions",)


def test_session_discovery(tmp_path: Path) -> None:
    main = _session(tmp_path)
    sub = _write(
        tmp_path / "--work-demo--" / "2026-01-01T00-00-01-000Z_subagent-sess-0002.jsonl",
        [
            _header("sess-0002", origin="subagent", parentSessionId="sess-0001"),
            _message("user", "sub task"),
        ],
    )
    _write(tmp_path / "--work-demo--" / "not-a-session.jsonl", [{"type": "message"}])
    (tmp_path / "--work-demo--" / "tool-output").mkdir()
    (tmp_path / "--work-demo--" / "tool-output" / "tool-abc.txt").write_text("out")
    sessions = {s.path: s for s in READER.sessions(tmp_path)}
    assert set(sessions) == {main, sub}
    assert sessions[main].session_id == "sess-0001"
    assert sessions[main].key == "--work-demo--/2026-01-01T00-00-00-000Z_sess-0001.jsonl"
    assert sessions[main].agent == "stepfun"
    assert sessions[sub].session_id == "sess-0002"
    assert sessions[sub].extra == {"parent_session_id": "sess-0001"}


def test_normalisation(tmp_path: Path) -> None:
    path = _session(tmp_path)
    records = list(READER.read(_locator(path, tmp_path)))
    assert [r.role for r in records] == ["user", "assistant", "tool", "assistant", "system", "user"]
    assert [r.turn for r in records] == [0, 1, 2, 3, 4, 5]
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    user, assistant, tool, tool_only, custom, plain = records
    assert user.text == "hello from user"
    assert user.timestamp == "2026-01-01T00:00:01.000Z"
    assert user.cwd == "/work/demo"
    assert user.session_id == "sess-0001"
    assert user.agent == "stepfun"
    assert assistant.text == "hello from assistant"
    assert "secret plan" not in assistant.text
    assert [c.name for c in assistant.tool_calls] == ["read"]
    assert "/work/demo/a.txt" in assistant.tool_calls[0].summary
    assert tool.text == "file contents" and tool.tool_calls == ()
    assert tool_only.text == "" and [c.name for c in tool_only.tool_calls] == ["bash"]
    assert custom.text == "continue the goal"
    assert plain.text == "plain string content"
    assert plain.timestamp == "2026-01-01T00:00:00Z"  # message epoch-ms fallback
    assert all("summary text" not in r.text and "only thoughts" not in r.text for r in records)


def test_after_resume(tmp_path: Path) -> None:
    path = _session(tmp_path)
    locator = _locator(path, tmp_path)
    records = list(READER.read(locator))
    resumed = list(READER.read(locator, after=records[2].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in records[2:]]
    assert [r.turn for r in resumed] == [2, 3, 4, 5]
    assert list(READER.read(locator, after=records[-1].offset)) == []


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "--work-demo--" / "2026-01-01T00-00-00-000Z_sess-0003.jsonl",
        [
            _header("sess-0003"),
            "{not json",
            "[1, 2, 3]",
            {"type": "message", "message": "not a dict"},
            {"type": "message", "message": {"role": "user", "content": 42}},
            {"type": "message", "message": {"role": "alien", "content": "x"}},
            _message(
                "assistant",
                [{"type": "toolCall", "name": None, "arguments": None}, "loose text", 7],
            ),
            _message("user", "still fine"),
        ],
        tail='{"type": "message", "message": {"role": "user", "content": "partial',
    )
    records = list(READER.read(_locator(path, tmp_path)))
    assert [(r.role, r.text) for r in records] == [
        ("assistant", "loose text"),
        ("user", "still fine"),
    ]
    assert records[0].tool_calls[0].name == "tool"


def test_bash_execution_message(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "d" / "2026-01-01T00-00-00-000Z_sess-0004.jsonl",
        [_header("sess-0004"), _message("bashExecution", None, command="echo hi", output="hi")],
    )
    (record,) = READER.read(_locator(path, tmp_path))
    assert record.role == "tool" and record.text == "hi"
    assert record.tool_calls[0].name == "bash" and "echo hi" in record.tool_calls[0].summary


def test_unknown_layout_yields_nothing(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    _write(tmp_path / "x" / "other.jsonl", [{"role": "user", "content": "hello"}])
    (tmp_path / "x" / "empty.jsonl").write_text("")
    assert list(READER.sessions(tmp_path)) == []
    bogus = SessionLocator("stepfun", tmp_path / "x" / "other.jsonl", key="x/other.jsonl")
    assert list(READER.read(bogus)) == []
    missing = SessionLocator("stepfun", tmp_path / "nope.jsonl", key="nope.jsonl")
    assert list(READER.read(missing)) == []
