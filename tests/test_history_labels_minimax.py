"""MiniMax reader labels: model, project, injected user turns and session origin.

Synthetic v2 session ledgers plus a synthetic runtime index with the columns
the real ``local_runtime_sessions`` table carries.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import ConversationRecord, StoreRoot
from memfw.history.sources.labels import AUTOMATED, SUBAGENT
from memfw.history.sources.minimax import PARSER_VERSION, READER

T0 = 1_762_400_000_000
NOTICE = "<background-task-finished>task bg_1 completed</background-task-finished>"
REMINDER = "<system-reminder>\nUse the tools you are given.\n</system-reminder>\n"


def _line(i: int, message: dict[str, Any]) -> dict[str, Any]:
    return {"message_id": f"msg-{i:04d}", "turn_id": "turn-0001", "message": message}


def _ledger(*, notice: bool = False) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": REMINDER + "fix the parser"}],
            "timestamp": T0,
            "canonicalTextRange": {"startOffset": len(REMINDER), "endOffset": len(REMINDER) + 14},
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "provider": "demo",
            "model": "Demo-Model-A",
            "timestamp": T0 + 1000,
        },
    ]
    if notice:
        msgs.append(
            {
                "role": "user",
                "content": NOTICE,
                "timestamp": T0 + 2000,
                "hostMetadata": {
                    "backgroundTaskOrigin": {
                        "kind": "background-task-terminal",
                        "taskIds": ["bg_1"],
                        "observedTerminalCount": 1,
                    }
                },
            }
        )
    return [_line(i, m) for i, m in enumerate(msgs)]


def _session(root: Path, rel: str, session_id: str, lines: list[Any], *, model: str = "") -> Path:
    directory = root / rel
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"sessionId": session_id}))
    if model:
        (directory / "llm-call.json").write_text(
            json.dumps({"schemaVersion": 1, "provider": "demo", "model": model})
        )
    path = directory / "messages.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _index(root: Path, rows: list[dict[str, Any]]) -> None:
    db = root.parent / "sqlite"
    db.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db / "runtime-state.sqlite")
    conn.execute(
        "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY,"
        " record_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,"
        " agent_name TEXT, session_kind TEXT NOT NULL DEFAULT 'unknown',"
        " origin_cron_id TEXT, parent_session_id TEXT, workspace_dir TEXT,"
        " project_workspace_dir TEXT, extra_data_json TEXT NOT NULL DEFAULT '{}')"
    )
    for row in rows:
        values = {"record_json": "{}", "updated_at_ms": 0, **row}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO local_runtime_sessions ({cols}) VALUES ({marks})", tuple(values.values())
        )
    conn.commit()
    conn.close()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / ".minimax" / "v2" / "sessions"
    root.mkdir(parents=True)
    return root


def _read(root: Path) -> list[ConversationRecord]:
    [loc] = list(READER.sessions(root))
    return list(READER.read(loc))


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_assistant_model_from_message(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    records = _read(root)
    assert records[1].role == "assistant"
    assert records[1].model == "Demo-Model-A"


def test_session_model_from_llm_call_on_user_turns(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger(), model="Demo-Model-B")
    records = _read(root)
    assert (records[0].role, records[0].model) == ("user", "Demo-Model-B")
    assert records[1].model == "Demo-Model-A"


def test_session_model_falls_back_to_index_effective_model(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    extra = json.dumps({"effectiveModel": "demo/Demo-Model-C", "origin": "user"})
    _index(root, [{"session_id": "mvs_0001", "extra_data_json": extra}])
    assert _read(root)[0].model == "Demo-Model-C"


def test_project_is_basename_of_project_workspace(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    _index(
        root,
        [
            {
                "session_id": "mvs_0001",
                "workspace_dir": "/home/user/code/demo-app/src",
                "project_workspace_dir": "/home/user/code/demo-app",
            }
        ],
    )
    records = _read(root)
    assert {(r.cwd, r.project) for r in records} == {("/home/user/code/demo-app/src", "demo-app")}


def test_project_falls_back_to_cwd_basename(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    _index(root, [{"session_id": "mvs_0001", "workspace_dir": "/home/user/code/other"}])
    assert {r.project for r in _read(root)} == {"other"}


def test_background_task_notice_is_system(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger(notice=True))
    records = _read(root)
    assert [r.role for r in records] == ["user", "assistant", "system"]
    assert records[2].text.startswith("<background-task-finished>")


def test_system_reminder_first_prompt_stays_user_and_unchanged(tmp_path: Path) -> None:
    # Regression guard (passes on the old reader too): the reminder shares one
    # message with the typed text, so the role and text are left alone.
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    first = _read(root)[0]
    assert first.role == "user"
    assert first.text == (REMINDER + "fix the parser").strip()


def test_parent_session_marks_subagent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0002", _ledger())
    _index(
        root,
        [
            {"session_id": "mvs_0001", "session_kind": "conversation", "agent_name": "lead"},
            {
                "session_id": "mvs_0002",
                "session_kind": "task",
                "agent_name": "worker",
                "parent_session_id": "mvs_0001",
            },
        ],
    )
    records = _read(root)
    assert {(r.origin, r.parent_session) for r in records} == {(SUBAGENT, "mvs_0001")}


def test_cron_session_is_automated(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "a", "mvs_0001", _ledger())
    _session(root, "b", "mvs_0002", _ledger())
    _index(
        root,
        [
            {"session_id": "mvs_0001", "session_kind": "cron"},
            {"session_id": "mvs_0002", "session_kind": "task", "origin_cron_id": "cron_1"},
        ],
    )
    for loc in READER.sessions(root):
        assert {(r.origin, r.parent_session) for r in READER.read(loc)} == {(AUTOMATED, "")}


def test_plain_conversation_has_no_origin(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0001", _ledger())
    _index(root, [{"session_id": "mvs_0001", "session_kind": "conversation"}])
    assert {(r.origin, r.parent_session) for r in _read(root)} == {("", "")}


def test_store_keeps_model_project_and_origin(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _session(root, "s", "mvs_0002", _ledger(), model="Demo-Model-B")
    _index(
        root,
        [
            {
                "session_id": "mvs_0002",
                "workspace_dir": "/home/user/code/demo-app",
                "project_workspace_dir": "/home/user/code/demo-app",
                "parent_session_id": "mvs_0001",
            }
        ],
    )
    store = ConversationStore(tmp_path / "conversations.sqlite")
    store.ingest(READER, [StoreRoot(root)])
    turns = store.turns("minimax", "mvs_0002")
    assert [(t["role"], t["model"]) for t in turns] == [
        ("user", "Demo-Model-B"),
        ("assistant", "Demo-Model-A"),
    ]
    assert {t["project"] for t in turns} == {"demo-app"}
    assert {t["origin"] for t in turns} == {SUBAGENT}
    assert {t["parent_session"] for t in turns} == {"mvs_0001"}
