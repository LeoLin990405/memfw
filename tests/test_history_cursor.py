from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.sources.base import SessionLocator
from memfw.history.sources.cursor import READER

SESSION = "sess-0001"
CWD = "/work/demo"


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
    body += b"\x50" + _varint(7)  # field 10, varint
    body += b"\x2a" + _varint(4) + b"conf"  # field 5, bytes (not a reference)
    return bytes(body)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        self.connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self.latest = ""

    def blob(self, data: bytes) -> str:
        blob_id = hashlib.sha256(data).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)", (blob_id, data)
        )
        return blob_id

    def message(self, message: dict[str, Any]) -> str:
        return self.blob(json.dumps(message).encode())

    def root(self, ids: list[str]) -> str:
        self.latest = self.blob(_root(ids))
        return self.latest

    def close(self) -> None:
        meta = {"agentId": SESSION, "latestRootBlobId": self.latest, "name": "demo"}
        self.connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('0', ?)",
            (json.dumps(meta).encode().hex(),),
        )
        self.connection.commit()
        self.connection.close()


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "you are a demo agent"},
        {"role": "user", "content": [{"type": "text", "text": "hello from user"}], "id": "u1"},
        {
            "role": "assistant",
            "id": "a1",
            "content": [
                {"type": "reasoning", "text": "secret thoughts", "signature": "sig"},
                {"type": "text", "text": "let me look"},
                {
                    "type": "tool-call",
                    "toolCallId": "call-1",
                    "toolName": "Read",
                    "args": {"path": "/work/demo/readme.txt"},
                },
            ],
            "providerOptions": {"cursor": {"modelProviderMessageId": "m1"}},
        },
        {
            "role": "tool",
            "id": "t1",
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": "call-1",
                    "toolName": "Read",
                    "result": "file body " * 1000,
                    "experimental_content": [],
                }
            ],
            "providerOptions": {"cursor": {"highLevelToolCallResult": None}},
        },
        {
            "role": "assistant",
            "id": "a2",
            "content": [{"type": "reasoning", "text": "only thinking", "signature": "s"}],
        },
        {
            "role": "assistant",
            "id": "a3",
            "content": [
                {
                    "type": "tool-call",
                    "toolCallId": "call-2",
                    "toolName": "Shell",
                    "args": {"command": "ls", "description": "list"},
                }
            ],
        },
    ]


def _build(root: Path, session: str = SESSION, cwd: str | None = CWD) -> Path:
    chat = root / "abcdef0123456789" / session
    store = Store(chat / "store.db")
    ids = [store.message(message) for message in _messages()]
    store.root(ids[:2])
    store.root(ids[:4])
    store.root(ids)
    store.close()
    if cwd is not None:
        (chat / "meta.json").write_text(
            json.dumps({"schemaVersion": 1, "createdAtMs": 1, "updatedAtMs": 2, "cwd": cwd})
        )
    return chat / "store.db"


def _locator(root: Path) -> SessionLocator:
    return next(READER.sessions(root))


def test_reader_identity() -> None:
    assert READER.agent == "cursor"
    assert READER.default_roots == (".cursor/chats",)


def test_session_discovery(tmp_path: Path) -> None:
    _build(tmp_path)
    _build(tmp_path, session="sess-0002")
    (tmp_path / "store.db").write_bytes(b"")  # wrong depth
    (tmp_path / "abcdef0123456789" / "notes.txt").write_text("x")
    locators = list(READER.sessions(tmp_path))
    assert [loc.session_id for loc in locators] == ["sess-0001", "sess-0002"]
    assert locators[0].key == "abcdef0123456789/sess-0001/store.db"
    assert locators[0].agent == "cursor"


def test_normalisation(tmp_path: Path) -> None:
    _build(tmp_path)
    records = list(READER.read(_locator(tmp_path)))
    assert [(r.offset, r.turn, r.role) for r in records] == [
        (0, 0, "system"),
        (1, 1, "user"),
        (2, 2, "assistant"),
        (3, 3, "tool"),
        (5, 4, "assistant"),
    ]
    assert records[1].text == "hello from user"
    assistant = records[2]
    assert assistant.text == "let me look"
    assert "secret" not in assistant.text
    assert assistant.tool_calls[0].name == "Read"
    assert "/work/demo/readme.txt" in assistant.tool_calls[0].summary
    assert records[3].text.startswith("file body")
    assert len(records[3].text) <= 64_000  # the store truncates after redaction
    assert records[4].text == ""
    assert records[4].tool_calls[0].name == "Shell"
    assert all(r.cwd == CWD and r.project == "demo" for r in records)
    assert all(r.session_id == SESSION and r.agent == "cursor" for r in records)
    assert all(r.timestamp == "" for r in records)


def test_after_resume(tmp_path: Path) -> None:
    _build(tmp_path)
    locator = _locator(tmp_path)
    assert [r.offset for r in READER.read(locator, after=2)] == [3, 5]
    tail = list(READER.read(locator, after=4))
    assert [(r.offset, r.turn) for r in tail] == [(5, 4)]
    assert list(READER.read(locator, after=5)) == []


def test_summary_root_keeps_offsets_stable(tmp_path: Path) -> None:
    chat = tmp_path / "abcdef0123456789" / SESSION
    store = Store(chat / "store.db")
    system = store.message({"role": "system", "content": "sys"})
    first = store.message({"role": "user", "content": "first question"})
    answer = store.message({"role": "assistant", "content": [{"type": "text", "text": "a1"}]})
    store.root([system, first, answer])
    summary = store.message(
        {
            "role": "user",
            "content": "summary of earlier work",
            "providerOptions": {"cursor": {"isSummary": True}},
        }
    )
    again = store.message({"role": "user", "content": "next question"})
    store.root([system, summary, answer, again])
    store.close()
    records = list(READER.read(_locator(tmp_path)))
    assert [(r.offset, r.role, r.text) for r in records] == [
        (0, "system", "sys"),
        (1, "user", "first question"),
        (2, "assistant", "a1"),
        (3, "system", "summary of earlier work"),
        (4, "user", "next question"),
    ]


def test_repeated_message_in_growing_root(tmp_path: Path) -> None:
    chat = tmp_path / "abcdef0123456789" / SESSION
    store = Store(chat / "store.db")
    go = store.message({"role": "user", "content": "go on"})
    reply = store.message({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    store.root([go, reply])
    store.root([go, reply, go, reply])
    store.close()
    records = list(READER.read(_locator(tmp_path)))
    assert [r.offset for r in records] == [0, 1, 2, 3]
    assert [r.turn for r in records] == [0, 1, 2, 3]


def test_malformed_blobs_skipped(tmp_path: Path) -> None:
    chat = tmp_path / "abcdef0123456789" / SESSION
    store = Store(chat / "store.db")
    good = store.message({"role": "user", "content": "hello from user"})
    broken = store.blob(b'{"role": "user", "content": ')
    unknown = store.message({"role": "narrator", "content": "nope"})
    no_role = store.message({"palette": {}, "space": 1})
    store.blob(b"\x0b\xff\xff")  # undecodable protobuf
    store.blob(b"")
    missing = "ab" * 32
    store.root([good, missing])  # references a missing blob: not a usable root
    store.root([good, broken, unknown, no_role])
    store.close()
    records = list(READER.read(_locator(tmp_path)))
    assert [(r.offset, r.turn, r.text) for r in records] == [(0, 0, "hello from user")]


def test_missing_meta_json(tmp_path: Path) -> None:
    _build(tmp_path, cwd=None)
    records = list(READER.read(_locator(tmp_path)))
    assert records
    assert all(r.cwd == "" and r.project == "" for r in records)


def test_unknown_layouts_yield_nothing(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "missing")) == []
    chat = tmp_path / "abcdef0123456789"
    (chat / "sess-0003").mkdir(parents=True)
    (chat / "sess-0003" / "store.db").write_bytes(b"not a database")
    (chat / "sess-0004").mkdir()
    other = sqlite3.connect(chat / "sess-0004" / "store.db")
    other.execute("CREATE TABLE something (x TEXT)")
    other.commit()
    other.close()
    empty = Store(chat / "sess-0005" / "store.db")
    empty.close()
    locators = list(READER.sessions(tmp_path))
    assert len(locators) == 3
    for locator in locators:
        assert list(READER.read(locator)) == []
    gone = SessionLocator(agent="cursor", path=tmp_path / "nope" / "store.db", key="x")
    assert list(READER.read(gone)) == []
