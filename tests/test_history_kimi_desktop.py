"""Kimi desktop app reader: synthetic fixtures only (fake user ``zztester``)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from memfw.history.sources import get_reader, store_roots
from memfw.history.sources.base import SessionLocator
from memfw.history.sources.kimi_desktop import READER, STORE_ROOT

CONV = "conv-00000000000000000000aaaa"
TITLE = "ctitle-00000000-0000-0000-0000-00000000bbbb"
WORKDIR = "wd_00-00-00-00000000_000000000000"
BASE_MS = 1_700_000_000_000
CWD = "/work/zztester-demo"


def _message(
    role: str, source: str, content: list[dict[str, Any]], *, time: int = BASE_MS, **extra: Any
) -> dict[str, Any]:
    inner: dict[str, Any] = {"role": role, "content": content, **extra}
    return {
        "type": "agent.message.appended",
        "kind": "event",
        "time": time,
        "message": {"message": inner, "meta": {"source": source, "promptId": "p-1"}},
    }


def _llm(model: str) -> dict[str, Any]:
    return {"type": "llm.request", "agentId": "main", "provider": "fake", "model": model}


def _lines() -> list[Any]:
    return [
        {"type": "metadata", "protocol_version": "2", "created_at": BASE_MS},
        {"type": "turn.prompt", "agentId": "main", "input": [{"type": "text", "text": "dup"}]},
        _message("user", "input", [{"type": "text", "text": "hello from zztester"}]),
        _llm("fake-model-a"),
        _message(
            "assistant",
            "llm",
            [{"type": "think", "think": "secret reasoning"}],
            time=BASE_MS + 1000,
            toolCalls=[
                {
                    "type": "function",
                    "id": "call-1",
                    "function": {"name": "Shell", "arguments": json.dumps({"command": "ls"})},
                }
            ],
        ),
        {"type": "usage.record", "agentId": "main", "model": "fake-model-a", "usage": {}},
        _message("tool", "tool", [{"type": "text", "text": "file-a"}], toolCallId="call-1"),
        "not json at all",
        {"type": "agent.message.appended", "message": "broken"},
        _llm("fake-model-b"),
        _message("assistant", "llm", [{"type": "text", "text": "answer one"}]),
        _message("user", "notify", [{"type": "text", "text": "subagent finished"}]),
        _message("weird", "llm", [{"type": "text", "text": "unknown role"}]),
        _message("assistant", "llm", [{"type": "text", "text": "answer two"}]),
    ]


def _write_wire(path: Path, lines: list[Any], *, partial: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n"
    path.write_text(body + (partial or ""), encoding="utf-8")


def _state(session_dir: Path, session_id: str, kind: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "id": session_id,
        "version": 2,
        "cwd": CWD,
        "custom": {"owner": "daimon", "sessionKind": kind},
        "createdAt": BASE_MS,
    }
    (session_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _app(home: Path) -> Path:
    """A synthetic app data tree, credential-store look-alikes included."""

    app = home / "Library" / "Application Support" / "kimi-desktop"
    (app / "bridge-store").mkdir(parents=True)
    (app / "bridge-store" / "token-store.json").write_text('{"token": "fake"}', encoding="utf-8")
    (app / "Cookies").write_bytes(b"fake cookie db")
    (app / "Local State").write_text("{}", encoding="utf-8")
    root = home / STORE_ROOT
    conv = root / WORKDIR / CONV
    _state(conv, CONV, "conversation")
    _write_wire(conv / "agents" / "main" / "wire.jsonl", _lines(), partial='{"type": "agent.mes')
    _write_wire(
        conv / "agents" / "agent-0" / "wire.jsonl",
        [_llm("fake-sub-model"), _message("assistant", "llm", [{"type": "text", "text": "sub"}])],
    )
    (conv / "agents" / "main" / "token.json").write_text('{"token": "fake"}', encoding="utf-8")
    title = root / WORKDIR / TITLE
    _state(title, TITLE, "conversation-title")
    _write_wire(title / "agents" / "main" / "wire.jsonl", _lines()[:3])
    hidden = root / WORKDIR / "conv-hidden-title"
    _state(hidden, "conv-hidden-title", "conversation-title")
    _write_wire(hidden / "agents" / "main" / "wire.jsonl", _lines()[:3])
    (root / ".index-cache").mkdir()
    (root / ".index-cache" / "scan.json").write_text("{}", encoding="utf-8")
    return root


def _main(root: Path) -> SessionLocator:
    return next(loc for loc in READER.sessions(root) if loc.extra.get("agent_id") == "main")


def test_identity_and_registry() -> None:
    assert READER.agent == "kimi-desktop"
    assert READER.append_only is True
    assert get_reader("kimi-app") is READER
    assert get_reader("Kimi-Desktop") is READER
    assert all(not root.startswith("/") for root in READER.default_roots)


def test_default_root_under_home_override(tmp_path: Path) -> None:
    root = _app(tmp_path / "zztester")
    assert [item.path for item in store_roots(READER, tmp_path / "zztester")] == [root.resolve()]
    assert store_roots(READER, tmp_path / "nobody") == []


def test_enumeration_skips_title_sessions(tmp_path: Path) -> None:
    root = _app(tmp_path)
    locators = list(READER.sessions(root))
    assert [loc.key for loc in locators] == [
        f"{WORKDIR}/{CONV}/agents/agent-0/wire.jsonl",
        f"{WORKDIR}/{CONV}/agents/main/wire.jsonl",
    ]
    assert [loc.session_id for loc in locators] == [f"{CONV}:agent-0", CONV]
    assert all(loc.agent == "kimi-desktop" for loc in locators)


def test_ordering_roles_and_model(tmp_path: Path) -> None:
    records = list(READER.read(_main(_app(tmp_path))))
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
    assert records[0].text == "hello from zztester"
    assert records[0].timestamp == "2023-11-14T22:13:20Z"
    assert records[1].timestamp == "2023-11-14T22:13:21Z"
    assert [c.name for c in records[1].tool_calls] == ["Shell"]
    assert records[2].text == "file-a"
    assert [r.model for r in records] == [
        "",
        "fake-model-a",
        "",
        "fake-model-b",
        "",
        "fake-model-b",
    ]
    assert all(r.agent == "kimi-desktop" and r.cwd == CWD for r in records)
    assert all(r.session_id == CONV for r in records)
    assert all("reasoning" not in r.text and "unknown role" not in r.text for r in records)


def test_incremental_after(tmp_path: Path) -> None:
    locator = _main(_app(tmp_path))
    records = list(READER.read(locator))
    resumed = list(READER.read(locator, after=records[3].offset - 1))
    assert [r.offset for r in resumed] == [r.offset for r in records[3:]]
    assert [r.turn for r in resumed] == [3, 4, 5]
    assert resumed[0].model == "fake-model-b"
    assert list(READER.read(locator, after=records[-1].offset)) == []


def test_appended_line_is_picked_up(tmp_path: Path) -> None:
    locator = _main(_app(tmp_path))
    last = list(READER.read(locator))[-1].offset
    with locator.path.open("a", encoding="utf-8") as handle:
        handle.write('sage.appended"}\n')  # completes the partial line: still not a valid event
        handle.write(
            json.dumps(_message("user", "input", [{"type": "text", "text": "more"}])) + "\n"
        )
    (record,) = READER.read(locator, after=last)
    assert (record.role, record.text, record.turn) == ("user", "more", 6)


def test_subagent_session(tmp_path: Path) -> None:
    sub = next(loc for loc in READER.sessions(_app(tmp_path)) if loc.extra["agent_id"] == "agent-0")
    (record,) = READER.read(sub)
    assert (record.role, record.text, record.model) == ("assistant", "sub", "fake-sub-model")
    assert record.session_id == f"{CONV}:agent-0"


def test_malformed_and_missing(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    root = tmp_path / "sessions"
    broken = root / WORKDIR / "conv-broken"
    (broken / "agents" / "main").mkdir(parents=True)
    (broken / "state.json").write_text("{not json", encoding="utf-8")
    (broken / "agents" / "main" / "wire.jsonl").write_bytes(b"\x00\xff garbage\n[1, 2]\n")
    (root / WORKDIR / "conv-no-agents").mkdir()
    (root / WORKDIR / "loose.jsonl").write_text("{}\n", encoding="utf-8")
    (locator,) = READER.sessions(root)
    assert locator.session_id == "conv-broken" and locator.extra["cwd"] == ""
    assert list(READER.read(locator)) == []
    gone = SessionLocator(agent="kimi-desktop", path=tmp_path / "nope.jsonl", key="nope.jsonl")
    assert list(READER.read(gone)) == []


def test_symlinked_session_is_not_followed(tmp_path: Path) -> None:
    root = _app(tmp_path)
    outside = tmp_path / "outside" / "conv-outside"
    _write_wire(outside / "agents" / "main" / "wire.jsonl", _lines()[:3])
    os.symlink(outside, root / WORKDIR / "conv-outside")
    assert all("conv-outside" not in loc.key for loc in READER.sessions(root))


def test_credential_files_are_never_opened(tmp_path: Path, monkeypatch: Any) -> None:
    root = _app(tmp_path)
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    for locator in READER.sessions(root):
        list(READER.read(locator))
    assert opened
    names = {Path(path).name for path in opened}
    assert names <= {"state.json", "wire.jsonl"}
    assert not any("token" in path.lower() or "cookie" in path.lower() for path in opened)
