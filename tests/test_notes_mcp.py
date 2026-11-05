from __future__ import annotations

import json
from pathlib import Path

from memfw.notes.index import NotesIndex
from memfw.notes.mcp import NotesGateway, handle_request


def test_notes_mcp_search_get_status(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# Corpus\n\nKWIC concordance.\n", encoding="utf-8")
    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    gateway = NotesGateway(root, db)

    initialized = handle_request(gateway, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialized and initialized["result"]["serverInfo"]["name"] == "memfw-notes"
    listed = handle_request(gateway, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed and {tool["name"] for tool in listed["result"]["tools"]} == {
        "notes_search",
        "notes_get",
        "notes_status",
    }
    searched = handle_request(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "notes_search", "arguments": {"query": "corpus"}},
        },
    )
    payload = json.loads(searched["result"]["content"][0]["text"])
    assert payload[0]["path"] == "a.md"
    fetched = handle_request(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "notes_get", "arguments": {"path": "a.md"}},
        },
    )
    assert "KWIC" in fetched["result"]["content"][0]["text"]
