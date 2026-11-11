"""MCP tools for the transcript layer: history_list, history_search, history_read."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.mcp.server import TOOLS, Gateway

URL_CREDS = "https://" + "bot" + ":" + "s3cretPassXYZ" + "@git.invalid/repo"


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s1.jsonl").write_text(
        _line(session_id="s1", ts="2026-02-01T00:00:00Z", message="Tune the greenhouse fan curve")
        + _line(session_id="s1", ts="2026-02-01T00:00:01Z", message=f"clone {URL_CREDS}")
        + _line(
            session_id="s1",
            ts="2026-02-01T00:00:02Z",
            message="Fan curve tuned; owner is ops@example.org",
        ),
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    repository = Repository(vault)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    return vault, logs


def _call(gateway: Gateway, name: str, arguments: dict[str, Any], capsys: Any) -> dict[str, Any]:
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    payload = json.loads(capsys.readouterr().out)
    return dict(payload["result"])


def test_history_tools_are_listed_with_schemas() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    assert tools["history_search"]["inputSchema"]["required"] == ["query"]
    assert tools["history_read"]["inputSchema"]["required"] == ["ref"]
    assert "read-only" in tools["history_list"]["description"]
    for name in ("history_list", "history_search", "history_read"):
        assert tools[name]["description"]


def test_history_tools_over_protocol(tmp_path: Path, capsys: Any) -> None:
    vault, _logs = _setup(tmp_path)
    gateway = Gateway(vault)

    listed = _call(gateway, "history_list", {}, capsys)
    assert listed["isError"] is False
    body = json.loads(listed["content"][0]["text"])
    assert [row["session_id"] for row in body["sessions"]] == ["s1"]
    assert body["stats"]["skipped_secret"] == 1

    found = _call(gateway, "history_search", {"query": "fan curve"}, capsys)
    hits = json.loads(found["content"][0]["text"])["results"]
    assert len(hits) == 2
    assert all("ops@example.org" not in hit["snippet"] for hit in hits)

    ref = hits[0]["ref"]
    read = _call(gateway, "history_read", {"ref": ref, "before": 5, "after": 5}, capsys)
    window = json.loads(read["content"][0]["text"])
    text = json.dumps(window)
    assert "s3cretPass" not in text and "ops@example.org" not in text
    assert len(window["entries"]) == 2

    detail = _call(gateway, "history_list", {"session": "s1"}, capsys)
    assert len(json.loads(detail["content"][0]["text"])["entries"]) == 2


def test_history_read_refusals_over_protocol(tmp_path: Path, capsys: Any) -> None:
    vault, logs = _setup(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_line(session_id="x", message="not for you"), encoding="utf-8")
    os.symlink(outside, logs / "link.jsonl")
    gateway = Gateway(vault)
    escaped = _call(gateway, "history_read", {"ref": "logs:../outside.jsonl@0"}, capsys)
    assert escaped["isError"] is True
    assert "outside" in escaped["content"][0]["text"]
    linked = _call(gateway, "history_read", {"ref": "logs:link.jsonl@0"}, capsys)
    assert linked["isError"] is True
    assert "symlink" in linked["content"][0]["text"]
    unknown = _call(gateway, "history_read", {"ref": "other:s1.jsonl@0"}, capsys)
    assert unknown["isError"] is True


def test_history_tools_need_configured_sources(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    Repository(vault).init()
    with pytest.raises(ValueError, match="history_sources"):
        Gateway(vault).history_list()


def test_history_index_lives_with_derived_data(tmp_path: Path) -> None:
    vault, _logs = _setup(tmp_path)
    Gateway(vault).history_search("greenhouse")
    assert (vault / ".memfw-history.sqlite").is_file()
    assert not (vault / "memories").exists() or not any(
        path.name.endswith(".jsonl") for path in (vault / "memories").iterdir()
    )
