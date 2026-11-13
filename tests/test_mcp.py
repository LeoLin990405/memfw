"""Tests for the generic local memfw MCP server."""

from __future__ import annotations

import json
from pathlib import Path

from memfw.core.repository import Repository
from memfw.mcp.server import TOOLS, Gateway


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(title="greenhouse", body="The greenhouse sensor reports 18 degrees.")
    repository.add(title="lab", body="The lab bench uses a 12V supply.")
    return root


def test_mcp_tool_list() -> None:
    names = {tool["name"] for tool in TOOLS}
    assert names == {
        "memory_search",
        "memory_get",
        "memory_remember",
        "memory_related",
        "memory_context",
        "memory_status",
        "history_list",
        "history_search",
        "history_read",
        "conversations_search",
        "conversations_list",
        "conversations_get",
        "conversations_handoff",
    }


def test_mcp_search_and_get(tmp_path: Path) -> None:
    gateway = Gateway(_vault(tmp_path))
    found = gateway.search("greenhouse", limit=5)
    assert found["results"]
    memory_id = found["results"][0]["id"]
    document = gateway.get(memory_id)
    assert "greenhouse" in document["body"]


def test_mcp_remember_is_staging_only(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    gateway = Gateway(root)
    before = len(Repository(root).list())
    staged = gateway.remember("A new candidate", title="candidate note")
    assert staged["authority"] is False
    relative = Path(staged["staged_candidate"])
    assert not relative.is_absolute()
    assert (root / relative).is_file()
    assert len(Repository(root).list()) == before


def test_mcp_status(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    status = Gateway(root).status()
    assert status["memories"] == 2
    assert status["log"]["event_count"] >= 2


def test_mcp_initialize_includes_instructions(capsys):
    Gateway("/tmp/does-not-need-to-exist").handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    payload = json.loads(capsys.readouterr().out)
    instructions = payload["result"]["instructions"]
    assert "memory_search" in instructions
    assert "human" in instructions
