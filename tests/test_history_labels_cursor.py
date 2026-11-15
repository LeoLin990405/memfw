"""cursor-agent CLI session labels: cwd, timestamps, model, subagent origin.

Fixtures are synthetic stores in the cursor-agent ``chats/<hash>/<id>/store.db``
layout (content-addressed ``blobs`` plus a hex-JSON ``meta`` row).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import SessionLocator
from memfw.history.sources.cursor import PARSER_VERSION, READER
from memfw.history.sources.labels import SUBAGENT

SESSION = "chat-0001"
PARENT = "chat-0000"
WORKSPACE = "/srv/work/demo-repo"
USER_INFO = (
    "<user_info>\nOS Version: linux 6.0\n\nShell: bash\n\n"
    f"Workspace Path: {WORKSPACE}\n\nIs directory a git repo: Yes\n\n"
    "Today's date: Tuesday Nov 10, 2026\n</user_info>"
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _root(ids: list[str]) -> bytes:
    body = bytearray()
    for blob_id in ids:
        raw = bytes.fromhex(blob_id)
        body += b"\x0a" + _varint(len(raw)) + raw
    return bytes(body)


def _build(
    root: Path,
    messages: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    chat_meta: dict[str, Any] | None = None,
    session: str = SESSION,
) -> Path:
    chat = root / "0123456789abcdef" / session
    chat.mkdir(parents=True)
    path = chat / "store.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    def blob(data: bytes) -> str:
        blob_id = hashlib.sha256(data).hexdigest()
        connection.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (blob_id, data))
        return blob_id

    ids = [blob(json.dumps(message).encode()) for message in messages]
    latest = blob(_root(ids))
    value = {"agentId": session, "latestRootBlobId": latest, "name": "demo", **(meta or {})}
    connection.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(value).encode().hex(),))
    connection.commit()
    connection.close()
    if chat_meta is not None:
        (chat / "meta.json").write_text(json.dumps(chat_meta))
    return path


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _read(root: Path) -> list[Any]:
    locator: SessionLocator = next(READER.sessions(root))
    return list(READER.read(locator))


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "you are a demo agent"},
        _user(
            f"{USER_INFO}\n<timestamp>Tuesday, Nov 10, 2026, 9:05 PM (UTC-8)</timestamp>\n"
            "<user_query>first ask</user_query>"
        ),
        _assistant("first answer"),
        _user(
            "<timestamp>Wednesday, Nov 11, 2026, 10:30 AM (UTC+5:30)</timestamp>\n"
            "<user_query>second ask</user_query>"
        ),
        _assistant("second answer"),
    ]


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION != "1"


def test_cwd_from_user_info_when_meta_json_missing(tmp_path: Path) -> None:
    # Old code: cwd came only from meta.json, so it was empty here.
    _build(tmp_path, _conversation(), chat_meta=None)
    records = _read(tmp_path)
    assert records
    assert all(r.cwd == WORKSPACE for r in records)
    assert all(r.project == "demo-repo" for r in records)


def test_meta_json_cwd_wins_over_user_info(tmp_path: Path) -> None:
    _build(tmp_path, _conversation(), chat_meta={"cwd": "/srv/other", "createdAtMs": 0})
    assert {r.cwd for r in _read(tmp_path)} == {"/srv/other"}


def test_timestamp_tags_carried_forward(tmp_path: Path) -> None:
    # Old code: every timestamp was "".
    created_ms = 1_794_300_000_000
    _build(tmp_path, _conversation(), chat_meta={"cwd": WORKSPACE, "createdAtMs": created_ms})
    records = _read(tmp_path)
    stamps = {r.role + ":" + r.text[-12:]: r.timestamp for r in records}
    assert records[0].role == "system"
    assert records[0].timestamp == "2026-11-10T08:40:00Z"  # createdAtMs, before any tag
    assert [r.timestamp for r in records[1:]] == [
        "2026-11-11T05:05:00Z",  # 9:05 PM at UTC-8
        "2026-11-11T05:05:00Z",  # assistant inherits the prompt's time
        "2026-11-11T05:00:00Z",  # 10:30 AM at UTC+5:30
        "2026-11-11T05:00:00Z",
    ], stamps


def test_timestamp_falls_back_to_store_created_then_mtime(tmp_path: Path) -> None:
    untagged = [_user("plain ask"), _assistant("plain answer")]
    _build(tmp_path / "a", untagged, meta={"createdAt": 1_794_300_000_000})
    assert {r.timestamp for r in _read(tmp_path / "a")} == {"2026-11-10T08:40:00Z"}

    store = _build(tmp_path / "b", untagged)
    os.utime(store, (1_794_400_000, 1_794_400_000))
    assert {r.timestamp for r in _read(tmp_path / "b")} == {"2026-11-11T12:26:40Z"}


def test_model_from_last_used_model(tmp_path: Path) -> None:
    _build(tmp_path / "a", _conversation(), meta={"lastUsedModel": "model-x-1"})
    assert {r.model for r in _read(tmp_path / "a")} == {"model-x-1"}
    _build(tmp_path / "b", _conversation(), meta={"lastUsedModel": "default"})
    assert {r.model for r in _read(tmp_path / "b")} == {""}


def test_subagent_origin_and_parent(tmp_path: Path) -> None:
    info = {
        "parentAgentId": PARENT,
        "rootParentAgentId": PARENT,
        "toolCallId": "tool-1",
        "typeName": "generalPurpose",
    }
    _build(tmp_path / "a", _conversation(), meta={"subagentInfo": info})
    records = _read(tmp_path / "a")
    assert {(r.origin, r.parent_session) for r in records} == {(SUBAGENT, PARENT)}

    # Run-everything / default mode say nothing about who drove the chat.
    _build(tmp_path / "b", _conversation(), meta={"mode": "default", "isRunEverything": True})
    assert {(r.origin, r.parent_session) for r in _read(tmp_path / "b")} == {("", "")}


def test_ingest_stores_ts_and_cwd(tmp_path: Path) -> None:
    root = tmp_path / "chats"
    _build(root, _conversation(), chat_meta=None)
    store = ConversationStore(tmp_path / "data" / "conv.sqlite")
    store.ingest(READER, [root])
    turns = store.turns("cursor", SESSION)
    assert [t["role"] for t in turns] == ["system", "user", "assistant", "user", "assistant"]
    assert all(t["cwd"] == WORKSPACE and t["project"] == "demo-repo" for t in turns)
    assert [t["ts"] for t in turns][1:] == [
        "2026-11-11T05:05:00Z",
        "2026-11-11T05:05:00Z",
        "2026-11-11T05:00:00Z",
        "2026-11-11T05:00:00Z",
    ]
    assert turns[0]["ts"]  # store.db mtime fallback
