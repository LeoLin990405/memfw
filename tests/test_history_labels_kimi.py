"""Kimi CLI reader labels: project, model, origin and parent session (synthetic store)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import SessionLocator
from memfw.history.sources.kimi import PARSER_VERSION, READER

SESSION = "session_0000bbbb-0000-0000-0000-000000000002"
BASE_MS = 1_700_000_000_000
CWD = "/srv/work/demo-project"


def _message(role: str, source: str, text: str) -> dict[str, Any]:
    return {
        "type": "agent.message.appended",
        "time": BASE_MS,
        "message": {
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
            "meta": {"source": source, "model": {"provider": "agent-loop", "model": "agent-loop"}},
        },
    }


def _profile(alias: str) -> dict[str, Any]:
    return {"type": "profile.bind", "agentId": "main", "modelAlias": alias, "time": BASE_MS}


def _request(model: str, alias: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "llm.request", "agentId": "main", "model": model}
    if alias is not None:
        event["modelAlias"] = alias
    return event


def _runtime(runtime_id: str) -> dict[str, Any]:
    return {"type": "runtime.set_binding", "runtimeId": runtime_id, "agentId": "main"}


def _write(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _store(
    tmp_path: Path,
    main: list[dict[str, Any]],
    subs: dict[str, list[dict[str, Any]]] | None = None,
    agents: dict[str, Any] | None = None,
) -> Path:
    root = tmp_path / ".kimi-code" / "sessions"
    session_dir = root / "wd_demo_0123456789ab" / SESSION
    session_dir.mkdir(parents=True)
    state: dict[str, Any] = {"id": SESSION, "cwd": CWD, "createdAt": BASE_MS}
    if agents is not None:
        state["agents"] = agents
    (session_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _write(session_dir / "agents" / "main" / "wire.jsonl", main)
    for name, lines in (subs or {}).items():
        _write(session_dir / "agents" / name / "wire.jsonl", lines)
    return root


def _locator(root: Path, agent_id: str = "main") -> SessionLocator:
    return next(loc for loc in READER.sessions(root) if loc.extra.get("agent_id") == agent_id)


def _plain() -> list[dict[str, Any]]:
    return [
        {"type": "metadata", "protocol_version": "1.5"},
        _runtime("local"),
        _profile("kimi-code/model-a"),
        _message("user", "input", "first question"),
        _request("model-a", "kimi-code/model-a"),
        _message("assistant", "llm", "first answer"),
        _request("model-b", None),
        _message("assistant", "llm", "second answer"),
    ]


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_project_is_cwd_basename(tmp_path: Path) -> None:
    records = list(READER.read(_locator(_store(tmp_path, _plain()))))
    assert records and all(r.cwd == CWD and r.project == "demo-project" for r in records)


def test_model_from_profile_and_requests_not_placeholder(tmp_path: Path) -> None:
    records = list(READER.read(_locator(_store(tmp_path, _plain()))))
    assert [r.model for r in records] == [
        "kimi-code/model-a",
        "kimi-code/model-a",
        "model-b",
    ]
    assert all(r.origin == "" and r.parent_session == "" for r in records)


def test_acp_runtime_is_automated_on_incremental_reads(tmp_path: Path) -> None:
    lines = _plain()
    lines.insert(2, _runtime(f"acp:{SESSION}"))
    locator = _locator(_store(tmp_path, lines))
    records = list(READER.read(locator))
    assert records and all(r.origin == "automated" for r in records)
    resumed = list(READER.read(locator, after=records[0].offset))
    assert [r.origin for r in resumed] == ["automated", "automated"]
    assert [r.model for r in resumed] == ["kimi-code/model-a", "model-b"]


def test_subagent_from_state_agents(tmp_path: Path) -> None:
    sub_lines = [_profile("kimi-code/model-s"), _message("user", "input", "sub prompt")]
    agents = {
        "main": {"type": "main"},
        "agent-0": {"type": "sub", "parentAgentId": "main"},
        "agent-1": {"type": "sub", "parentAgentId": "agent-0"},
    }
    root = _store(tmp_path, _plain(), {"agent-0": sub_lines, "agent-1": sub_lines}, agents)
    (first,) = READER.read(_locator(root, "agent-0"))
    assert (first.origin, first.parent_session) == ("subagent", SESSION)
    assert first.session_id == f"{SESSION}:agent-0"
    assert first.model == "kimi-code/model-s" and first.project == "demo-project"
    (nested,) = READER.read(_locator(root, "agent-1"))
    assert (nested.origin, nested.parent_session) == ("subagent", f"{SESSION}:agent-0")
    main = list(READER.read(_locator(root)))
    assert all(r.origin == "" and r.parent_session == "" for r in main)


def test_store_keeps_model_project_origin(tmp_path: Path) -> None:
    lines = _plain()
    lines.insert(2, _runtime(f"acp:{SESSION}"))
    agents = {"main": {"type": "main"}, "agent-0": {"type": "sub", "parentAgentId": "main"}}
    sub_lines = [_profile("kimi-code/model-s"), _message("user", "input", "sub prompt")]
    root = _store(tmp_path, lines, {"agent-0": sub_lines}, agents)
    store = ConversationStore(tmp_path / "data" / "conversations.sqlite")
    store.ingest(READER, [root])
    main = store.turns("kimi", SESSION)
    assert [(t["model"], t["project"], t["origin"]) for t in main] == [
        ("kimi-code/model-a", "demo-project", "automated"),
        ("kimi-code/model-a", "demo-project", "automated"),
        ("model-b", "demo-project", "automated"),
    ]
    (sub,) = store.turns("kimi", f"{SESSION}:agent-0")
    assert (sub["origin"], sub["parent_session"], sub["model"]) == (
        "subagent",
        SESSION,
        "kimi-code/model-s",
    )
