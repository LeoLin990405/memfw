from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.grok import READER

SESSION = "sess-0001"
PROJECT_DIR = "%2Fwork%2Fdemo"


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chat_rows() -> list[Any]:
    return [
        {"type": "system", "content": "you are a fake cli"},
        {
            "type": "user",
            "content": [{"type": "text", "text": "hello from user"}],
            "prompt_index": 0,
        },
        {
            "type": "reasoning",
            "id": "rs-1",
            "summary": [{"type": "summary_text", "text": "secret thinking"}],
            "encrypted_content": "xxx",
            "status": "completed",
        },
        {
            "type": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "name": "read_file", "arguments": '{"path": "/work/demo/a.txt"}'}
            ],
            "model_id": "fake-model",
            "model_fingerprint": "fp",
            "reasoning_effort": "high",
        },
        {"type": "tool_result", "tool_call_id": "call-1", "content": "file body"},
        "{not json",
        {"type": "assistant", "content": "hello from assistant", "model_id": "fake-model"},
        {
            "type": "user",
            "content": [{"type": "text", "text": "injected reminder"}],
            "synthetic_reason": "system_reminder",
        },
        {
            "type": "backend_tool_call",
            "kind": {"tool_type": "web_search", "action": {"type": "search", "query": "q"}},
        },
        {"type": "assistant", "content": "   "},
        {"type": "mystery", "content": "ignored"},
        ["not", "an", "object"],
    ]


def _make_store(root: Path) -> Path:
    session_dir = root / PROJECT_DIR / SESSION
    session_dir.mkdir(parents=True)
    _write_jsonl(session_dir / "chat_history.jsonl", _chat_rows())
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": SESSION, "cwd": "/work/demo"},
                "created_at": "2026-01-02T03:04:05.000000Z",
                "git_root_dir": "/work/demo",
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "ts": "2026-01-02T03:04:06.000Z",
                "type": "turn_started",
                "conversation_message_count": 1,
            },
            {"ts": "2026-01-02T03:04:07.000Z", "type": "tool_completed", "tool_call_id": "call-1"},
            "garbage",
        ],
    )
    (root / PROJECT_DIR / "prompt_history.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "session_search.sqlite").write_bytes(b"")
    return session_dir


def _locator(root: Path) -> SessionLocator:
    (locator,) = list(READER.sessions(root))
    return locator


def test_reader_identity() -> None:
    assert READER.agent == "grok"
    assert READER.default_roots == (".grok/sessions",)


def test_session_discovery(tmp_path: Path) -> None:
    session_dir = _make_store(tmp_path)
    locator = _locator(tmp_path)
    assert locator.agent == "grok"
    assert locator.session_id == SESSION
    assert locator.path == session_dir / "chat_history.jsonl"
    assert locator.key == f"{PROJECT_DIR}/{SESSION}/chat_history.jsonl"


def test_read_normalises_records(tmp_path: Path) -> None:
    _make_store(tmp_path)
    records = list(READER.read(_locator(tmp_path)))
    assert [r.role for r in records] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "system",
        "assistant",
    ]
    assert [r.turn for r in records] == list(range(6))
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    user, call, result, reply, reminder, backend = records
    assert user.text == "hello from user"
    assert user.timestamp == "2026-01-02T03:04:06.000Z"
    assert call.text == ""
    assert call.tool_calls[0].name == "read_file"
    assert "/work/demo/a.txt" in call.tool_calls[0].summary
    assert call.timestamp == "2026-01-02T03:04:06.000Z"
    assert result.text == "file body"
    assert result.timestamp == "2026-01-02T03:04:07.000Z"
    assert reply.text == "hello from assistant"
    assert reminder.text == "injected reminder"
    assert backend.tool_calls[0].name == "web_search"
    for record in records:
        assert record.agent == "grok"
        assert record.session_id == SESSION
        assert record.cwd == "/work/demo"
        assert record.project == "/work/demo"
        assert "secret thinking" not in record.text
        assert "fake cli" not in record.text


def test_after_resumes(tmp_path: Path) -> None:
    _make_store(tmp_path)
    locator = _locator(tmp_path)
    records = list(READER.read(locator))
    resumed = list(READER.read(locator, after=records[2].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in records[2:]]
    assert [r.turn for r in resumed] == [2, 3, 4, 5]
    assert list(READER.read(locator, after=records[-1].offset)) == []


def test_missing_metadata_falls_back(tmp_path: Path) -> None:
    session_dir = tmp_path / PROJECT_DIR / SESSION
    session_dir.mkdir(parents=True)
    _write_jsonl(
        session_dir / "chat_history.jsonl",
        [{"type": "user", "content": [{"type": "text", "text": "hello from user"}]}],
    )
    (session_dir / "summary.json").write_text("{broken", encoding="utf-8")
    (record,) = list(READER.read(_locator(tmp_path)))
    assert record.cwd == "/work/demo"
    assert record.project == ""
    assert record.timestamp == ""


def test_partial_trailing_line_ignored(tmp_path: Path) -> None:
    session_dir = tmp_path / PROJECT_DIR / SESSION
    session_dir.mkdir(parents=True)
    good = json.dumps({"type": "assistant", "content": "hello from assistant"})
    (session_dir / "chat_history.jsonl").write_text(
        good + '\n{"type": "assistant", "con', encoding="utf-8"
    )
    assert [r.text for r in READER.read(_locator(tmp_path))] == ["hello from assistant"]


def test_empty_or_unknown_layout(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    assert list(READER.sessions(tmp_path)) == []
    (tmp_path / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    assert list(READER.sessions(tmp_path)) == []
    ghost = SessionLocator(agent="grok", path=tmp_path / "nope" / "chat_history.jsonl", key="nope")
    assert list(READER.read(ghost)) == []
