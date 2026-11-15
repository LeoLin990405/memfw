"""Grok reader labels: model, project, origin and parent session (synthetic store)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import ConversationRecord
from memfw.history.sources.grok import PARSER_VERSION, READER

CWD = "/srv/work/demo"
CWD_DIR = "%2Fhome%2Fuser%2Fwork%2Fdemo"
OTHER_DIR = "%2Fhome%2Fuser%2Fwork%2Fother"


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _rows() -> list[Any]:
    return [
        {"type": "system", "content": "fake system prompt"},
        {"type": "user", "content": [{"type": "text", "text": "do the thing"}], "prompt_index": 0},
        {"type": "assistant", "content": "first answer", "model_id": "demo-model-b"},
        {"type": "user", "content": [{"type": "text", "text": "and more"}], "prompt_index": 1},
        {"type": "assistant", "content": "second answer"},
    ]


def _session(
    root: Path,
    session_id: str = "sess-0001",
    *,
    cwd_dir: str = CWD_DIR,
    summary: dict[str, Any] | None = None,
) -> Path:
    directory = root / cwd_dir / session_id
    directory.mkdir(parents=True)
    _write_jsonl(directory / "chat_history.jsonl", _rows())
    base: dict[str, Any] = {
        "info": {"id": session_id, "cwd": CWD},
        "created_at": "2026-01-02T03:04:05.000000Z",
        "current_model_id": "demo-model-a",
    }
    base.update(summary or {})
    (directory / "summary.json").write_text(json.dumps(base), encoding="utf-8")
    return directory


def _read(root: Path, session_id: str = "sess-0001") -> list[ConversationRecord]:
    (locator,) = [loc for loc in READER.sessions(root) if loc.session_id == session_id]
    return list(READER.read(locator))


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_model_from_message_and_session(tmp_path: Path) -> None:
    _session(tmp_path)
    records = _read(tmp_path)
    # user before any assistant line: session current_model_id; later lines: last model_id
    assert [r.model for r in records] == [
        "demo-model-a",
        "demo-model-b",
        "demo-model-b",
        "demo-model-b",
    ]


def test_project_is_cwd_basename(tmp_path: Path) -> None:
    _session(tmp_path, summary={"git_root_dir": "/srv/work"})
    assert {r.project for r in _read(tmp_path)} == {"demo"}


def test_headless_session_is_automated(tmp_path: Path) -> None:
    _session(tmp_path, summary={"session_kind": "headless"})
    records = _read(tmp_path)
    assert {r.origin for r in records} == {"automated"}
    assert {r.parent_session for r in records} == {""}


def test_interactive_session_is_unlabelled(tmp_path: Path) -> None:
    directory = _session(tmp_path)
    (directory / "goal").mkdir()
    (directory / "goal" / "state.json").write_text('{"status": "complete"}', encoding="utf-8")
    assert {r.origin for r in _read(tmp_path)} == {""}


def test_subagent_parent_from_parent_meta(tmp_path: Path) -> None:
    parent = _session(tmp_path, "sess-parent")
    meta_dir = parent / "subagents" / "sess-child"
    meta_dir.mkdir(parents=True)
    (meta_dir / "meta.json").write_text(
        json.dumps(
            {
                "subagent_id": "sess-child",
                "child_session_id": "sess-child",
                "parent_session_id": "sess-parent",
            }
        ),
        encoding="utf-8",
    )
    # the child lives under another cwd directory than its parent
    _session(tmp_path, "sess-child", cwd_dir=OTHER_DIR, summary={"session_kind": "subagent"})
    records = _read(tmp_path, "sess-child")
    assert {r.origin for r in records} == {"subagent"}
    assert {r.parent_session for r in records} == {"sess-parent"}
    assert {r.origin for r in _read(tmp_path, "sess-parent")} == {""}


def test_subagent_fork_parent_from_summary(tmp_path: Path) -> None:
    _session(
        tmp_path,
        summary={"session_kind": "subagent_fork", "parent_session_id": "sess-parent"},
    )
    records = _read(tmp_path)
    assert {r.origin for r in records} == {"subagent"}
    assert {r.parent_session for r in records} == {"sess-parent"}


def test_labels_on_incremental_read(tmp_path: Path) -> None:
    _session(tmp_path, summary={"session_kind": "headless"})
    (locator,) = list(READER.sessions(tmp_path))
    first = list(READER.read(locator))
    later = list(READER.read(locator, after=first[1].offset))
    assert later and {r.origin for r in later} == {"automated"}
    assert {r.model for r in later} == {"demo-model-b"}
    assert {r.project for r in later} == {"demo"}


def test_store_keeps_origin_model_project(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _session(root, summary={"session_kind": "subagent", "parent_session_id": "sess-parent"})
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [root])
    assert counts.sessions_failed == 0
    turns = store.turns("grok", "sess-0001")
    assert turns
    assert {t["origin"] for t in turns} == {"subagent"}
    assert {t["parent_session"] for t in turns} == {"sess-parent"}
    assert {t["model"] for t in turns} == {"demo-model-a", "demo-model-b"}
    assert {t["project"] for t in turns} == {"demo"}
