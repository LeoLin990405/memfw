"""Claude reader session labels: sub-agents, headless runs, cwd/project/model fill.

Every fixture is synthetic and written under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources import READERS
from memfw.history.sources.base import ConversationRecord, StoreRoot
from memfw.history.sources.claude import PARSER_VERSION

PARENT = "11111111-2222-3333-4444-555555555555"
CWD = "/home/user/work/demo"


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _rows(session: str, **extra: Any) -> list[dict[str, Any]]:
    base = {"sessionId": session, "cwd": CWD} | extra
    return [
        base | {"type": "user", "message": {"role": "user", "content": "please list files"}},
        base
        | {
            "type": "assistant",
            "message": {"role": "assistant", "model": "claude-test-1", "content": "done"},
        },
    ]


def _read(root: Path) -> list[ConversationRecord]:
    reader = READERS["claude"]
    return [record for locator in reader.sessions(root) for record in reader.read(locator)]


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION != "1"


def test_subagent_file_is_labelled_with_parent(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    rows = _rows(PARENT, isSidechain=True, agentId="a0b1c2", entrypoint="cli")
    _jsonl(root / "-home-user-work-demo" / PARENT / "subagents" / "agent-a0b1c2.jsonl", rows)
    records = _read(root)
    assert len(records) == 2
    assert all(r.session_id == "agent-a0b1c2" for r in records)
    assert all(r.origin == "subagent" for r in records)
    assert all(r.parent_session == PARENT for r in records)


def test_subagent_path_alone_labels_and_links(tmp_path: Path) -> None:
    # No isSidechain / sessionId on the lines: the path shape still decides.
    root = tmp_path / "projects"
    rows = [{k: v for k, v in row.items() if k != "sessionId"} for row in _rows(PARENT)]
    path = root / "-p" / PARENT / "subagents" / "workflows" / "wf_1" / "agent-x9.jsonl"
    _jsonl(path, rows)
    records = _read(root)
    assert {(r.origin, r.parent_session) for r in records} == {("subagent", PARENT)}


def test_sidechain_line_labels_without_subagent_path(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _jsonl(root / "-p" / "side-1.jsonl", _rows(PARENT, isSidechain=True))
    records = _read(root)
    assert {(r.origin, r.parent_session) for r in records} == {("subagent", PARENT)}


def test_sdk_entrypoint_is_automated(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _jsonl(root / "-p" / "sess-a.jsonl", _rows("sess-a", entrypoint="sdk-cli"))
    _jsonl(root / "-p" / "sess-b.jsonl", _rows("sess-b", entrypoint="sdk-ts"))
    _jsonl(root / "-p" / "sess-c.jsonl", _rows("sess-c", entrypoint="cli"))
    _jsonl(root / "-p" / "sess-d.jsonl", _rows("sess-d", entrypoint="claude-desktop"))
    _jsonl(root / "-p" / "sess-e.jsonl", _rows("sess-e"))
    origins = {r.session_id: r.origin for r in _read(root)}
    assert origins == {
        "sess-a": "automated",
        "sess-b": "automated",
        "sess-c": "",
        "sess-d": "",
        "sess-e": "",
    }


def test_cwd_project_and_model_fill(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    rows = [
        # A request-blob line has no cwd: it inherits the last one seen.
        {"type": "user", "cwd": CWD, "message": {"role": "user", "content": "start"}},
        {"type": "api-request-blob", "message": {"role": "user", "content": "copy"}},
        {"type": "user", "cwd": CWD + "/sub", "message": {"role": "user", "content": "cd'd"}},
        {
            "type": "assistant",
            "cwd": CWD + "/sub",
            "message": {"role": "assistant", "model": "<synthetic>", "content": "local note"},
        },
        {
            "type": "assistant",
            "cwd": CWD + "/sub",
            "message": {"role": "assistant", "model": "claude-test-1", "content": "real"},
        },
    ]
    _jsonl(root / "-p" / "sess-f.jsonl", rows)
    records = _read(root)
    assert [r.cwd for r in records] == [CWD, CWD, CWD + "/sub", CWD + "/sub", CWD + "/sub"]
    assert {r.project for r in records} == {CWD.rstrip("/").rsplit("/", 1)[-1]}
    assert [r.model for r in records] == ["", "", "", "", "claude-test-1"]


def test_ingest_stores_origin_and_parent(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _jsonl(
        root / "-p" / PARENT / "subagents" / "agent-a0.jsonl",
        _rows(PARENT, isSidechain=True, agentId="a0"),
    )
    _jsonl(root / "-p" / "sess-h.jsonl", _rows("sess-h", entrypoint="sdk-cli"))
    _jsonl(root / "-p" / "sess-i.jsonl", _rows("sess-i", entrypoint="cli"))
    store = ConversationStore(tmp_path / "c.db")
    store.ingest(READERS["claude"], [StoreRoot(root, "interactive")])
    with sqlite3.connect(store.db) as connection:
        stored = set(
            connection.execute(
                "SELECT DISTINCT session_id, origin, parent_session, project <> ''"
                " FROM conversation_turns"
            )
        )
    assert stored == {
        ("agent-a0", "subagent", PARENT, 1),
        ("sess-h", "automated", "", 1),
        ("sess-i", "interactive", "", 1),
    }
