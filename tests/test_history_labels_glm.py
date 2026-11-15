"""Model, project and origin labels of the glm (ZCode CLI) reader. Synthetic fixtures only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import ConversationRecord
from memfw.history.sources.glm import PARSER_VERSION, READER

BASE_MS = 1_700_000_000_000
PARENT = "sess-parent"
CWD = "/home/user/work/demo"


def _make_db(root: Path, sessions: list[tuple[str, str | None, str]]) -> Path:
    """A ZCode app store; ``sessions`` holds (id, parent_id, task_type)."""

    database = root / "db" / "db.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE session (
            id text primary key, project_id text not null, parent_id text,
            directory text not null, title text not null,
            time_created integer not null, time_updated integer not null,
            task_type text not null default 'interactive', time_deleted integer
        );
        CREATE TABLE message (
            id text primary key, session_id text not null,
            time_created integer not null, time_updated integer not null,
            data text not null, sequence integer
        );
        CREATE TABLE part (
            id text primary key, message_id text not null, session_id text not null,
            time_created integer not null, time_updated integer not null,
            data text not null, sequence integer
        );
        """
    )
    for index, (session_id, parent, task_type) in enumerate(sessions):
        connection.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, "proj", parent, CWD, "t", BASE_MS + index, BASE_MS, task_type, None),
        )
        messages: list[tuple[dict[str, Any], str]] = [
            (
                {
                    "role": "user",
                    "time": {"created": BASE_MS},
                    "semantics": {"kind": "user_prompt"},
                    "modelSelection": {"providerId": "acct:plan", "modelId": "demo-large"},
                    "model": {"providerID": "acct:plan", "modelID": "demo-large"},
                    "contextSnapshot": {"envInfo": {"cwd": CWD + "/sub"}},
                },
                "a prompt",
            ),
            (
                {
                    "role": "assistant",
                    "time": {"created": BASE_MS + 1000},
                    "semantics": {"kind": "assistant_response"},
                    "modelId": "demo-flash",
                    "providerId": "acct:plan",
                    "path": {"cwd": CWD + "/sub", "root": CWD},
                },
                "an answer",
            ),
        ]
        for sequence, (data, text) in enumerate(messages):
            message_id = f"{session_id}-m{sequence}"
            connection.execute(
                "INSERT INTO message VALUES (?,?,?,?,?,?)",
                (message_id, session_id, BASE_MS + sequence, BASE_MS, json.dumps(data), sequence),
            )
            connection.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
                (
                    f"{message_id}-p0",
                    message_id,
                    session_id,
                    BASE_MS,
                    BASE_MS,
                    json.dumps({"type": "text", "text": text}),
                    0,
                ),
            )
    connection.commit()
    connection.close()
    return database


def _read(root: Path, session_id: str, after: int = -1) -> list[ConversationRecord]:
    locator = next(loc for loc in READER.sessions(root) if loc.session_id == session_id)
    return list(READER.read(locator, after=after))


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_sqlite_model_from_message_fields(tmp_path: Path) -> None:
    _make_db(tmp_path, [("sess-a", None, "interactive")])
    user, assistant = _read(tmp_path, "sess-a")
    assert user.model == "demo-large"  # modelSelection.modelId
    assert assistant.model == "demo-flash"  # modelId


def test_sqlite_project_is_session_directory_basename(tmp_path: Path) -> None:
    _make_db(tmp_path, [("sess-a", None, "interactive")])
    records = _read(tmp_path, "sess-a")
    assert [r.project for r in records] == ["demo", "demo"]
    assert [r.cwd for r in records] == [CWD + "/sub", CWD + "/sub"]


def test_sqlite_subagent_child_origin_and_parent(tmp_path: Path) -> None:
    _make_db(
        tmp_path,
        [
            (PARENT, None, "interactive"),
            ("sess-child", PARENT, "subagent_child"),
            ("sess-wf", PARENT, "nested_workflow_child"),
        ],
    )
    assert {(r.origin, r.parent_session) for r in _read(tmp_path, PARENT)} == {("", "")}
    for session_id in ("sess-child", "sess-wf"):
        records = _read(tmp_path, session_id)
        assert {(r.origin, r.parent_session) for r in records} == {("subagent", PARENT)}
    # incremental re-read: later records carry the label too
    resumed = _read(tmp_path, "sess-child", after=0)
    assert resumed and {r.origin for r in resumed} == {"subagent"}


def test_sqlite_side_chat_links_parent_without_origin(tmp_path: Path) -> None:
    sessions = [(PARENT, None, "interactive"), ("sess-side", PARENT, "selection_side_chat")]
    _make_db(tmp_path, sessions)
    records = _read(tmp_path, "sess-side")
    assert {(r.origin, r.parent_session) for r in records} == {("", PARENT)}


def test_json_snapshot_model_and_project(tmp_path: Path) -> None:
    snapshot = {
        "version": 1,
        "meta": {"id": "sess-j", "cwd": CWD, "model": "demo-large", "surface": "terminal"},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
    }
    (tmp_path / "sess_sess-j.json").write_text(json.dumps(snapshot))
    records = _read(tmp_path, "sess-j")
    assert [(r.model, r.project, r.origin) for r in records] == [("demo-large", "demo", "")] * 2


def test_store_keeps_glm_labels(tmp_path: Path) -> None:
    root = tmp_path / "zcode"
    _make_db(root, [(PARENT, None, "interactive"), ("sess-child", PARENT, "subagent_child")])
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [root])
    assert counts.sessions_failed == 0
    turns = store.turns("glm", "sess-child")
    assert turns
    assert {t["origin"] for t in turns} == {"subagent"}
    assert {t["parent_session"] for t in turns} == {PARENT}
    assert {t["model"] for t in turns} == {"demo-large", "demo-flash"}
    assert {t["project"] for t in turns} == {"demo"}
    parent_turns = store.turns("glm", PARENT)
    assert {t["origin"] for t in parent_turns} == {"interactive"}
