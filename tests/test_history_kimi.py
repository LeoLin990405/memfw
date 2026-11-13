from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.kimi import READER

SESSION = "session_0000aaaa-0000-0000-0000-000000000001"
BASE_MS = 1_700_000_000_000


def _message(role: str, source: str, content: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    inner: dict[str, Any] = {"role": role, "content": content, **extra}
    return {
        "type": "agent.message.appended",
        "kind": "event",
        "time": BASE_MS,
        "message": {"message": inner, "meta": {"source": source}},
    }


def _lines() -> list[Any]:
    return [
        {"type": "metadata", "protocol_version": "1.5", "created_at": BASE_MS},
        {
            "type": "turn.prompt",
            "agentId": "main",
            "time": BASE_MS,
            "input": [{"type": "text", "text": "dup"}],
        },
        _message("user", "input", [{"type": "text", "text": "hello from user"}]),
        {
            "type": "context.append_loop_event",
            "agentId": "main",
            "time": BASE_MS,
            "event": {"type": "step.begin"},
        },
        _message(
            "assistant",
            "llm",
            [{"type": "think", "think": "secret reasoning", "reasoningKey": "r"}],
            toolCalls=[
                {
                    "type": "function",
                    "id": "call-1",
                    "function": {
                        "name": "Shell",
                        "arguments": json.dumps({"command": "ls /work/demo"}),
                    },
                }
            ],
        ),
        _message("tool", "tool", [{"type": "text", "text": "file-a\nfile-b"}], toolCallId="call-1"),
        "not json at all",
        {"type": "agent.message.appended", "message": "broken"},
        _message("assistant", "llm", [{"type": "think", "think": "only thinking"}]),
        _message("user", "notify", [{"type": "text", "text": "task finished"}]),
        _message(
            "assistant",
            "llm",
            [{"type": "think", "think": "hmm"}, {"type": "text", "text": "hello from assistant"}],
            toolCalls=[],
        ),
        _message("weird", "llm", [{"type": "text", "text": "unknown role"}]),
    ]


def _write_wire(path: Path, lines: list[Any], *, partial: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    body = "\n".join(rendered) + "\n"
    if partial is not None:
        body += partial
    path.write_text(body, encoding="utf-8")


def _store(tmp_path: Path) -> Path:
    root = tmp_path / ".kimi-code" / "sessions"
    session_dir = root / "wd_demo_0123456789ab" / SESSION
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(
        json.dumps({"id": SESSION, "version": 1, "cwd": "/work/demo", "createdAt": BASE_MS}),
        encoding="utf-8",
    )
    _write_wire(
        session_dir / "agents" / "main" / "wire.jsonl", _lines(), partial='{"type": "agent.mess'
    )
    _write_wire(
        session_dir / "agents" / "agent-0" / "wire.jsonl",
        [_message("user", "input", [{"type": "text", "text": "sub task prompt"}])],
    )
    (session_dir / "notify").mkdir()
    (session_dir / "notify" / "state.json").write_text('{"enabled": true}', encoding="utf-8")
    return root


def _main(root: Path) -> SessionLocator:
    return next(loc for loc in READER.sessions(root) if loc.extra.get("agent_id") == "main")


def test_reader_identity() -> None:
    assert READER.agent == "kimi"
    assert READER.default_roots == (".kimi-code/sessions",)


def test_session_discovery(tmp_path: Path) -> None:
    root = _store(tmp_path)
    locators = list(READER.sessions(root))
    assert [loc.key for loc in locators] == [
        f"wd_demo_0123456789ab/{SESSION}/agents/agent-0/wire.jsonl",
        f"wd_demo_0123456789ab/{SESSION}/agents/main/wire.jsonl",
    ]
    assert {loc.session_id for loc in locators} == {SESSION, f"{SESSION}:agent-0"}
    assert all(loc.agent == "kimi" for loc in locators)


def test_normalisation(tmp_path: Path) -> None:
    records = list(READER.read(_main(_store(tmp_path))))
    assert [r.role for r in records] == ["user", "assistant", "tool", "system", "assistant"]
    assert [r.turn for r in records] == [0, 1, 2, 3, 4]
    assert records[0].text == "hello from user"
    assert records[0].timestamp == "2023-11-14T22:13:20Z"
    assert all(r.cwd == "/work/demo" and r.session_id == SESSION for r in records)
    tool_turn = records[1]
    assert tool_turn.text == ""
    assert [c.name for c in tool_turn.tool_calls] == ["Shell"]
    assert tool_turn.tool_calls[0].summary == "command: ls /work/demo"
    assert records[2].text == "file-a\nfile-b"
    assert records[4].text == "hello from assistant"
    assert all("reasoning" not in r.text and "thinking" not in r.text for r in records)
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)


def test_after_resume(tmp_path: Path) -> None:
    locator = _main(_store(tmp_path))
    records = list(READER.read(locator))
    resumed = list(READER.read(locator, after=records[2].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in records[2:]]
    assert [r.turn for r in resumed] == [2, 3, 4]
    assert list(READER.read(locator, after=records[-1].offset)) == []


def test_subagent_session(tmp_path: Path) -> None:
    root = _store(tmp_path)
    sub = next(loc for loc in READER.sessions(root) if loc.extra.get("agent_id") == "agent-0")
    records = list(READER.read(sub))
    assert [(r.role, r.text, r.session_id) for r in records] == [
        ("user", "sub task prompt", f"{SESSION}:agent-0")
    ]


def test_missing_state_falls_back_to_directory_name(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_wire(root / "wd_x" / SESSION / "agents" / "main" / "wire.jsonl", _lines()[:3])
    (locator,) = READER.sessions(root)
    assert locator.session_id == SESSION
    (record,) = READER.read(locator)
    assert record.cwd == "" and record.text == "hello from user"


def test_empty_or_unknown_layout(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    unknown = tmp_path / "unknown"
    (unknown / "wd_x" / "not-a-session").mkdir(parents=True)
    (unknown / "wd_x" / "loose.jsonl").write_text("{}\n", encoding="utf-8")
    (unknown / "wd_x" / "session_y").mkdir()
    assert list(READER.sessions(unknown)) == []
    garbage = tmp_path / "garbage" / "wire.jsonl"
    garbage.parent.mkdir()
    garbage.write_bytes(b"\x00\xff not json\n[1, 2]\n")
    locator = SessionLocator(agent="kimi", path=garbage, key="wire.jsonl", session_id="sess-0001")
    assert list(READER.read(locator)) == []
    missing = SessionLocator(agent="kimi", path=tmp_path / "nope.jsonl", key="nope.jsonl")
    assert list(READER.read(missing)) == []
