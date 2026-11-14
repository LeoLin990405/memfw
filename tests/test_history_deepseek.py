"""DeepSeek harness CLI (``dsh``) reader on synthetic ``session.v4.jsonl.zstd`` logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import zstandard

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.deepseek import READER

T0 = 1_760_000_000_000  # epoch ms


def _line(value: Any) -> bytes:
    return json.dumps(value).encode() + b"\n"


def _header(session_id: str = "sess-0001", cwd: str = "/work/demo") -> dict[str, Any]:
    return {
        "type": "session",
        "version": 4,
        "id": session_id,
        "cwd": cwd,
        "createdAt": T0,
        "delegationDepth": 0,
        "isSeeded": False,
    }


def _event(kind: str, seq: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "seq": seq, "time": T0 + seq * 1000, "data": data}


def _user(seq: int, text: str, kind: str = "user") -> dict[str, Any]:
    return _event(
        "user/message",
        seq,
        {
            "id": f"m{seq}",
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "source": {"kind": kind},
        },
    )


def _assistant(seq: int, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return _event(
        "assistant/message",
        seq,
        {
            "turn": 0,
            "step": 0,
            "message": {"id": f"m{seq}", "role": "assistant", "content": blocks},
            "stream": [{"type": "delta", "time": T0, "chunk": {"text": "streamed noise"}}],
        },
    )


def _tool_result(seq: int, text: str, cwd: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "turn": 0,
        "step": 0,
        "message": {
            "id": f"m{seq}",
            "role": "tool",
            "toolCallId": "call-1",
            "isError": False,
            "content": [{"type": "text", "text": text}],
        },
    }
    if cwd is not None:
        data["meta"] = {"cwd": cwd}
    return _event("tool/result", seq, data)


def _events() -> list[dict[str, Any]]:
    return [
        _header(),
        _event(
            "system/message",
            1,
            {
                "message": {
                    "id": "m1",
                    "role": "system",
                    "content": [{"type": "text", "text": "you are a demo agent"}],
                }
            },
        ),
        _user(2, "current time is noon", kind="time-context"),
        _user(3, "hello from user"),
        _event("step/start", 4, {"turn": 0, "step": 0}),
        _assistant(
            5,
            [
                {"type": "reasoning", "text": "secret reasoning"},
                {"type": "text", "text": "hello from assistant"},
                {
                    "type": "tool-call",
                    "id": "call-1",
                    "name": "shell",
                    "arguments": json.dumps({"command": "ls -la"}),
                },
            ],
        ),
        _event(
            "tool/call",
            6,
            {"callId": "call-1", "name": "shell", "arguments": "{}", "turn": 0, "step": 0},
        ),
        _tool_result(7, "file-a\nfile-b", cwd="/work/demo/sub"),
        _assistant(
            8,
            [
                {"type": "reasoning", "text": "more reasoning"},
                {"type": "tool-call", "id": "call-2", "name": "read", "arguments": "not json"},
            ],
        ),
        _assistant(9, [{"type": "reasoning", "text": "only reasoning"}]),
        _user(10, "thanks, bye"),
    ]


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))
    return path


def _store(tmp_path: Path, events: list[dict[str, Any]] | None = None) -> Path:
    root = tmp_path / "sessions"
    payload = b"".join(_line(event) for event in (events or _events()))
    _write(root / "--work-demo--" / "session-sess-0001" / "session.v4.jsonl.zstd", payload)
    return root


def _only(root: Path) -> SessionLocator:
    sessions = list(READER.sessions(root))
    assert len(sessions) == 1
    return sessions[0]


def test_reader_identity() -> None:
    assert READER.agent == "deepseek"
    assert READER.default_roots == (".dsh/sessions",)


def test_session_discovery(tmp_path: Path) -> None:
    root = _store(tmp_path)
    second = _header(session_id="sess-0002", cwd="/work/other")
    _write(root / "--work-other--" / "sess-0002" / "session.v4.jsonl.zstd", _line(second))
    (root / "--work-demo--" / "session-sess-0001" / "session.lock").write_text("")
    _write(root / "--work-demo--" / "notes" / "other.jsonl.zstd", _line(_header("sess-x")))

    sessions = sorted(READER.sessions(root), key=lambda s: s.session_id)
    assert [s.session_id for s in sessions] == ["sess-0001", "sess-0002"]
    assert sessions[0].agent == "deepseek"
    assert sessions[0].key == "--work-demo--/session-sess-0001/session.v4.jsonl.zstd"
    assert sessions[0].extra["cwd"] == "/work/demo"
    assert sessions[1].extra["cwd"] == "/work/other"


def test_records_are_normalised(tmp_path: Path) -> None:
    records = list(READER.read(_only(_store(tmp_path))))
    assert [r.role for r in records] == [
        "system",
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert [r.turn for r in records] == list(range(7))
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and offsets[0] > 0
    assert all(r.agent == "deepseek" and r.session_id == "sess-0001" for r in records)

    system, injected, user, assistant, tool, tool_only, last = records
    assert system.text == "you are a demo agent"
    assert injected.text == "current time is noon"
    assert user.text == "hello from user"
    assert user.timestamp == "2025-10-09T08:53:23Z"
    assert user.cwd == "/work/demo"

    assert assistant.text == "hello from assistant"
    assert "reasoning" not in assistant.text and "streamed" not in assistant.text
    assert [c.name for c in assistant.tool_calls] == ["shell"]
    assert "ls -la" in assistant.tool_calls[0].summary

    assert tool.text == "file-a\nfile-b"
    assert tool.cwd == "/work/demo/sub"
    assert tool.tool_calls == ()

    assert tool_only.text == ""
    assert [c.name for c in tool_only.tool_calls] == ["read"]
    assert "not json" in tool_only.tool_calls[0].summary
    assert last.text == "thanks, bye"


def test_after_resumes(tmp_path: Path) -> None:
    locator = _only(_store(tmp_path))
    records = list(READER.read(locator))
    resumed = list(READER.read(locator, after=records[3].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in records[3:]]
    assert [r.turn for r in resumed] == [3, 4, 5, 6]
    assert list(READER.read(locator, after=records[-1].offset)) == []


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    payload = b"".join(
        [
            _line(_header()),
            b"{not json\n",
            b"[1, 2, 3]\n",
            _line({"type": "user/message", "data": "oops"}),
            _line({"type": "assistant/message", "data": {"message": "oops"}}),
            _line({"type": "user/message", "time": "bad", "data": {"content": 42}}),
            _line(_user(5, "survivor")),
            b'{"type": "user/message", "data": {"content": "partial',  # still being written
        ]
    )
    _write(root / "w" / "session-a" / "session.v4.jsonl.zstd", payload)
    records = list(READER.read(_only(root)))
    assert [r.text for r in records] == ["survivor"]


def test_truncated_frame_and_garbage_do_not_raise(tmp_path: Path) -> None:
    root = _store(tmp_path)
    path = next(root.rglob("session.v4.jsonl.zstd"))
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    locator = SessionLocator(agent="deepseek", path=path, key="k", session_id="sess-0001")
    list(READER.read(locator))  # must not raise

    path.write_bytes(b"definitely not zstd")
    assert list(READER.read(locator)) == []
    assert list(READER.sessions(root)) == []


def test_headerless_file_is_not_a_session(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write(root / "w" / "session-a" / "session.v4.jsonl.zstd", _line(_user(1, "hello from user")))
    assert list(READER.sessions(root)) == []


def test_empty_or_unknown_layout_yields_nothing(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list(READER.sessions(empty)) == []
    (empty / "random.txt").write_text("hello")
    (empty / "session.json").write_text("{}")
    assert list(READER.sessions(empty)) == []


def test_symlinked_session_is_ignored(tmp_path: Path) -> None:
    root = _store(tmp_path)
    real = next(root.rglob("session.v4.jsonl.zstd"))
    link_dir = root / "--linked--" / "session-link"
    link_dir.mkdir(parents=True)
    (link_dir / "session.v4.jsonl.zstd").symlink_to(real)
    assert [s.session_id for s in READER.sessions(root)] == ["sess-0001"]
