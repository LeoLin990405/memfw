from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources import default_roots, get_reader
from memfw.history.sources.base import SessionLocator, StoreRoot
from memfw.history.sources.cursor_ide import READER

USER_ROOT = Path("Library/Application Support/Cursor/User")
CHAT = "c0ffee00-0000-4000-8000-000000000001"
PAGED = "c0ffee00-0000-4000-8000-000000000002"
LEGACY = "c0ffee00-0000-4000-8000-000000000003"
CWD = "/home/zztester/work/demo-app"


class Kv:
    """A synthetic Cursor ``state.vscdb``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        for table in ("ItemTable", "cursorDiskKV"):
            self.connection.execute(
                f"CREATE TABLE {table} (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
            )

    def put(self, key: str, value: Any) -> None:
        raw = value if isinstance(value, (str, bytes)) else json.dumps(value)
        self.connection.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, raw))

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()


def _header(bubble_id: str, kind: int) -> dict[str, Any]:
    return {"bubbleId": bubble_id, "type": kind, "grouping": {"isRenderable": True}}


def _bubble(bubble_id: str, kind: int, text: str = "", minute: int = 0, **extra: Any) -> dict:
    return {
        "_v": 3,
        "type": kind,
        "bubbleId": bubble_id,
        "text": text,
        "createdAt": f"2026-01-02T03:{minute:02d}:00.000Z",
        **extra,
    }


def _composer(cid: str, headers: list[dict[str, Any]] | None, **extra: Any) -> dict[str, Any]:
    return {
        "_v": 18,
        "composerId": cid,
        "createdAt": 1767322800000,
        "fullConversationHeadersOnly": headers or [],
        "conversationMap": {},
        "modelConfig": {"modelName": "default-model", "maxMode": False},
        **extra,
    }


def _build(home: Path) -> Path:
    root = home / USER_ROOT
    kv = Kv(root / "globalStorage" / "state.vscdb")
    bubbles = [
        _bubble("b1", 1, "please list the files", 1, modelInfo={"modelName": "model-x"}),
        _bubble("b2", 2, "", 2, thinking={"text": "private reasoning"}),
        _bubble(
            "b3/t1:call-0001-1",
            2,
            "",
            3,
            toolFormerData={
                "name": "run_terminal_command_v2",
                "status": "completed",
                "params": json.dumps({"command": "ls -la"}),
                "result": json.dumps({"output": "alpha.txt beta.txt"}),
            },
        ),
        _bubble("b4/s:0", 2, "There are two files.", 4),
        _bubble("b5", 1, "<auto-continue/>", 5, isSimulatedMsg=True, simulatedMsgReason=3),
        _bubble(
            "b6",
            2,
            "",
            6,
            toolFormerData={"name": "read_file_v2", "status": "error", "rawArgs": "{bad json"},
        ),
    ]
    kv.put(
        f"composerData:{CHAT}",
        _composer(
            CHAT,
            [_header(b["bubbleId"], b["type"]) for b in bubbles],
            workspaceIdentifier={"id": "ws1", "uri": {"scheme": "file", "fsPath": CWD}},
        ),
    )
    for bubble in bubbles:
        kv.put(f"bubbleId:{CHAT}:{bubble['bubbleId']}", bubble)
    kv.put(f"bubbleId:{CHAT}:orphan", _bubble("orphan", 1, "rewound away", 9))

    # A long chat: headers paged out; workspace known by id only.
    kv.put(
        f"composerData:{PAGED}",
        _composer(PAGED, None, workspaceIdentifier={"id": "ws2"}, modelConfig={}),
    )
    kv.put(f"composerConvHeaders:{PAGED}:000001", [_header("p3", 1)])
    kv.put(f"composerConvHeaders:{PAGED}:000000", [_header("p1", 1), _header("p2", 2)])
    kv.put(f"bubbleId:{PAGED}:p1", _bubble("p1", 1, "first", 1))
    kv.put(f"bubbleId:{PAGED}:p2", _bubble("p2", 2, "second", 2))
    kv.put(f"bubbleId:{PAGED}:p3", _bubble("p3", 1, "third", 3))

    # Very old layout: bubbles inline in composerData.conversation.
    kv.put(
        f"composerData:{LEGACY}",
        {
            "composerId": LEGACY,
            "conversation": [
                _bubble("l1", 1, "old question", 1),
                _bubble("l2", 2, "old answer", 2),
            ],
        },
    )
    kv.close()
    ws2 = root / "workspaceStorage" / "ws2"
    ws2.mkdir(parents=True)
    (ws2 / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/zztester/work/other%20app"})
    )
    return root


def _locators(root: Path) -> dict[str, SessionLocator]:
    return {locator.session_id: locator for locator in READER.sessions(root)}


def test_registry_alias_and_default_root(tmp_path: Path) -> None:
    assert get_reader("cursor-app") is READER
    assert get_reader("cursor-ide") is READER
    assert get_reader("cursor") is not READER
    root = _build(tmp_path)
    assert root in default_roots(READER, tmp_path)


def test_enumerates_composers(tmp_path: Path) -> None:
    locators = _locators(_build(tmp_path))
    assert set(locators) == {CHAT, PAGED, LEGACY}
    for cid, locator in locators.items():
        assert locator.agent == "cursor-ide"
        assert locator.key == f"globalStorage/state.vscdb#{cid}"
        assert not locator.key.startswith("/")


def test_order_roles_tools_and_metadata(tmp_path: Path) -> None:
    records = list(READER.read(_locators(_build(tmp_path))[CHAT]))
    assert [(r.offset, r.role) for r in records] == [
        (0, "user"),
        (4, "assistant"),
        (5, "tool"),
        (6, "assistant"),
        (8, "system"),
        (10, "assistant"),
    ]
    assert [r.turn for r in records] == list(range(6))
    assert records[0].text == "please list the files"
    assert records[0].timestamp == "2026-01-02T03:01:00.000Z"
    call = records[1].tool_calls[0]
    assert call.name == "run_terminal_command_v2"
    assert "ls -la" in call.summary
    assert "alpha.txt" in records[2].text and records[2].tool_calls == ()
    assert records[3].text == "There are two files."
    assert records[5].tool_calls[0].name == "read_file_v2"
    assert "{bad json" in records[5].tool_calls[0].summary
    joined = " ".join(r.text for r in records)
    assert "private reasoning" not in joined and "rewound away" not in joined
    assert {r.cwd for r in records} == {CWD}
    assert {r.project for r in records} == {"demo-app"}
    assert records[0].model == "model-x" and records[3].model == "model-x"
    assert records[2].model == ""


def test_paged_headers_and_workspace_mapping(tmp_path: Path) -> None:
    records = list(READER.read(_locators(_build(tmp_path))[PAGED]))
    assert [(r.offset, r.role, r.text) for r in records] == [
        (0, "user", "first"),
        (2, "assistant", "second"),
        (4, "user", "third"),
    ]
    assert records[0].cwd == "/home/zztester/work/other app"
    assert records[0].project == "other app"
    assert records[0].model == ""


def test_legacy_inline_conversation(tmp_path: Path) -> None:
    records = list(READER.read(_locators(_build(tmp_path))[LEGACY]))
    assert [(r.role, r.text) for r in records] == [
        ("user", "old question"),
        ("assistant", "old answer"),
    ]


def test_incremental_after_offset(tmp_path: Path) -> None:
    locator = _locators(_build(tmp_path))[CHAT]
    full = list(READER.read(locator))
    tail = list(READER.read(locator, after=5))
    assert [r.offset for r in tail] == [6, 8, 10]
    assert [(r.offset, r.turn) for r in tail] == [(r.offset, r.turn) for r in full[3:]]
    assert list(READER.read(locator, after=10)) == []


def test_growth_keeps_offsets(tmp_path: Path) -> None:
    root = _build(tmp_path)
    locator = _locators(root)[PAGED]
    before = {r.offset: r.text for r in READER.read(locator)}
    connection = sqlite3.connect(root / "globalStorage" / "state.vscdb")
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"composerConvHeaders:{PAGED}:000002", json.dumps([_header("p4", 2)])),
    )
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"bubbleId:{PAGED}:p4", json.dumps(_bubble("p4", 2, "fourth", 4))),
    )
    connection.commit()
    connection.close()
    after = {r.offset: r.text for r in READER.read(locator)}
    assert after == {**before, 6: "fourth"}


def test_missing_and_non_cursor_stores(tmp_path: Path) -> None:
    assert list(READER.sessions(tmp_path / "nowhere")) == []
    ghost = SessionLocator("cursor-ide", tmp_path / "missing.vscdb", "x#y", CHAT)
    assert list(READER.read(ghost)) == []
    other = tmp_path / "other" / "globalStorage" / "state.vscdb"
    other.parent.mkdir(parents=True)
    sqlite3.connect(other).execute("CREATE TABLE t (a)").connection.commit()
    assert list(READER.sessions(tmp_path / "other")) == []
    corrupt = tmp_path / "corrupt" / "globalStorage" / "state.vscdb"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a database at all" * 100)
    assert list(READER.sessions(tmp_path / "corrupt")) == []
    bad = SessionLocator("cursor-ide", corrupt, "x#y", CHAT)
    assert list(READER.read(bad)) == []


def test_malformed_values_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / USER_ROOT
    kv = Kv(root / "globalStorage" / "state.vscdb")
    kv.put("composerData:broken", "{not json")
    kv.put("composerData:../escape", _composer("x", [_header("a", 1)]))
    kv.put(
        f"composerData:{CHAT}",
        _composer(CHAT, [_header("ok", 1), _header("bad", 2), {"nope": 1}, _header("gone", 2)]),
    )
    kv.put(f"bubbleId:{CHAT}:ok", _bubble("ok", 1, "fine", 1))
    kv.put(f"bubbleId:{CHAT}:bad", b"\xff\xfe{garbage")
    kv.put(f"composerConvHeaders:{PAGED}:000000", "[not json")
    kv.put(f"composerData:{PAGED}", _composer(PAGED, None))
    kv.close()
    locators = _locators(root)
    assert set(locators) == {"broken", CHAT, PAGED}
    assert list(READER.read(locators["broken"])) == []
    assert list(READER.read(locators[PAGED])) == []
    assert [(r.offset, r.text) for r in READER.read(locators[CHAT])] == [(0, "fine")]


def test_reads_through_wal_without_writing(tmp_path: Path) -> None:
    root = _build(tmp_path)
    database = root / "globalStorage" / "state.vscdb"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"bubbleId:{LEGACY}:unused", json.dumps(_bubble("unused", 1, "x"))),
    )
    writer.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:c0ffee00-0000-4000-8000-000000000009",
            json.dumps(
                _composer(
                    "c0ffee00-0000-4000-8000-000000000009",
                    [_header("w1", 1)],
                )
            ),
        ),
    )
    writer.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:c0ffee00-0000-4000-8000-000000000009:w1",
            json.dumps(_bubble("w1", 1, "only in the wal")),
        ),
    )
    writer.commit()
    before = database.stat().st_mtime_ns, database.stat().st_size
    locators = _locators(root)
    records = list(READER.read(locators["c0ffee00-0000-4000-8000-000000000009"]))
    assert [r.text for r in records] == ["only in the wal"]
    assert (database.stat().st_mtime_ns, database.stat().st_size) == before
    writer.close()


def test_ingest_into_store(tmp_path: Path) -> None:
    root = _build(tmp_path / "home")
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [StoreRoot(root)])
    assert counts.sessions_seen == 3
    assert counts.records_new == 6 + 3 + 2
    again = store.ingest(READER, [StoreRoot(root)])
    assert again.sessions_unchanged == 3 and again.records_new == 0
