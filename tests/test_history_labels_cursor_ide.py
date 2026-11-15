"""Cursor IDE sub-agent labels and fork prefix de-duplication (synthetic stores)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import ConversationRecord, SessionLocator, StoreRoot
from memfw.history.sources.cursor_ide import PARSER_VERSION, READER

USER_ROOT = Path("Library/Application Support/Cursor/User")
PARENT = "aaaa0000-0000-4000-8000-000000000001"
CHILD = "aaaa0000-0000-4000-8000-000000000002"
FORK = "aaaa0000-0000-4000-8000-000000000003"
LISTED = "aaaa0000-0000-4000-8000-000000000004"
CWD = "/srv/work/demo"


def _bubble(bubble_id: str, kind: int, text: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "_v": 3,
        "type": kind,
        "bubbleId": bubble_id,
        "text": text,
        "createdAt": "2026-01-02T03:04:00.000Z",
        **extra,
    }


def _tool(bubble_id: str, call_id: str, output: str) -> dict[str, Any]:
    return _bubble(
        bubble_id,
        2,
        toolFormerData={
            "name": "run_terminal_command_v2",
            "toolCallId": call_id,
            "status": "completed",
            "params": json.dumps({"command": "ls"}),
            "result": json.dumps({"output": output}),
        },
    )


def _composer(cid: str, bubbles: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "_v": 18,
        "composerId": cid,
        "createdAt": 1767322800000,
        "fullConversationHeadersOnly": [
            {"bubbleId": b["bubbleId"], "type": b["type"]} for b in bubbles
        ],
        "modelConfig": {"modelName": "default-model"},
        "workspaceIdentifier": {"id": "ws1", "uri": {"scheme": "file", "fsPath": CWD}},
        **extra,
    }


def _info(parent: str, call_id: str) -> dict[str, Any]:
    return {
        "parentComposerId": parent,
        "rootParentConversationId": parent,
        "toolCallId": call_id,
        "subagentType": 3,
        "subagentTypeName": "general-purpose",
        "conversationLengthAtSpawn": 4,
        "toolCallIdHistory": [call_id],
        "additionalData": {},
    }


PARENT_BUBBLES = [
    _bubble("p1", 1, "please fix the build"),
    _bubble("p2", 2, "Looking at it."),
    _tool("p3", "call-ls", "a.txt b.txt"),
    _bubble("p4", 2, "Found the problem."),
    _bubble(
        "p5",
        2,
        toolFormerData={
            "name": "task_v2",
            "toolCallId": "call-task",
            "params": json.dumps({"description": "check", "prompt": "check the tests"}),
            "result": json.dumps({"agentId": CHILD}),
            "additionalData": {"subagentComposerId": CHILD, "status": "success"},
        },
    ),
    _bubble("p6", 2, "All done."),
]

CHILD_BUBBLES = [
    _bubble("c1", 1, "check the tests"),
    _bubble("c2", 2, "Tests pass."),
]

# The fork copies the parent's first four bubbles: the first and the last keep
# their ids, the others get new ids (f2 matches by text only, f3 by tool call
# id); the copied tool output differs slightly (it often does).
FORK_BUBBLES = [
    _bubble("p1", 1, "please fix the build"),
    _bubble("f2", 2, "Looking at it."),
    _tool("f3", "call-ls", "a.txt b.txt (copied)"),
    _bubble("p4", 2, "Found the problem."),
    _tool("f5", "call-own", "c.txt"),
    _bubble("f6", 1, "now write the changelog"),
    _bubble("f7", 2, "Found the problem."),
]

LISTED_BUBBLES = [_bubble("l1", 1, "summarise the logs"), _bubble("l2", 2, "Summary.")]


def _build(home: Path) -> Path:
    root = home / USER_ROOT
    database = root / "globalStorage" / "state.vscdb"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    connection.execute(
        "CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
    )
    composers = {
        PARENT: _composer(PARENT, PARENT_BUBBLES, subagentComposerIds=[CHILD, FORK, LISTED]),
        CHILD: _composer(CHILD, CHILD_BUBBLES, subagentInfo=_info(PARENT, "call-task")),
        FORK: _composer(FORK, FORK_BUBBLES, subagentInfo=_info(PARENT, "call-fork")),
        # No subagentInfo: only the parent's subagentComposerIds links it.
        LISTED: _composer(LISTED, LISTED_BUBBLES),
    }
    bubbles = {PARENT: PARENT_BUBBLES, CHILD: CHILD_BUBBLES, FORK: FORK_BUBBLES}
    bubbles[LISTED] = LISTED_BUBBLES
    for cid, composer in composers.items():
        connection.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{cid}", json.dumps(composer))
        )
        for bubble in bubbles[cid]:
            connection.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                (f"bubbleId:{cid}:{bubble['bubbleId']}", json.dumps(bubble)),
            )
    connection.commit()
    connection.close()
    return root


def _read(root: Path) -> dict[str, list[ConversationRecord]]:
    locators: dict[str, SessionLocator] = {loc.session_id: loc for loc in READER.sessions(root)}
    return {cid: list(READER.read(loc)) for cid, loc in locators.items()}


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION != "1"


def test_task_subagent_is_labelled_with_its_parent(tmp_path: Path) -> None:
    records = _read(_build(tmp_path))
    child = records[CHILD]
    assert [r.text for r in child] == ["check the tests", "Tests pass."]
    assert {r.origin for r in child} == {"subagent"}
    assert {r.parent_session for r in child} == {PARENT}
    parent = records[PARENT]
    assert {r.origin for r in parent} == {""}
    assert {r.parent_session for r in parent} == {""}


def test_parent_list_is_the_fallback_link(tmp_path: Path) -> None:
    listed = _read(_build(tmp_path))[LISTED]
    assert [r.text for r in listed] == ["summarise the logs", "Summary."]
    assert {(r.origin, r.parent_session) for r in listed} == {("subagent", PARENT)}


def test_fork_skips_the_copied_parent_prefix(tmp_path: Path) -> None:
    fork = _read(_build(tmp_path))[FORK]
    # Positions 0-3 copy the parent (by id, text and tool call id); the
    # child's own tool call, task prompt and replies stay, offsets unchanged.
    assert [(r.offset, r.role, r.text) for r in fork] == [
        (8, "assistant", ""),
        (9, "tool", json.dumps({"output": "c.txt"})),
        (10, "user", "now write the changelog"),
        (12, "assistant", "Found the problem."),
    ]
    assert fork[0].tool_calls[0].name == "run_terminal_command_v2"
    assert {(r.origin, r.parent_session) for r in fork} == {("subagent", PARENT)}
    assert [r.turn for r in fork] == [0, 1, 2, 3]
    assert [r.offset for r in _read_after(tmp_path, 9)] == [10, 12]


def _read_after(tmp_path: Path, after: int) -> list[ConversationRecord]:
    root = tmp_path / USER_ROOT
    locator = next(loc for loc in READER.sessions(root) if loc.session_id == FORK)
    return list(READER.read(locator, after=after))


def test_unrelated_subagent_keeps_a_matching_first_message(tmp_path: Path) -> None:
    """A sub-agent sharing no bubble id with its parent is never trimmed."""

    root = _build(tmp_path)
    connection = sqlite3.connect(root / "globalStorage" / "state.vscdb")
    same = [_bubble("x1", 1, "please fix the build"), _bubble("x2", 2, "Looking at it.")]
    other = "aaaa0000-0000-4000-8000-000000000005"
    composer = _composer(other, same, subagentInfo=_info(PARENT, "call-x"))
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{other}", json.dumps(composer))
    )
    for bubble in same:
        connection.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{other}:{bubble['bubbleId']}", json.dumps(bubble)),
        )
    connection.commit()
    connection.close()
    assert [r.offset for r in _read(root)[other]] == [0, 2]


def test_store_keeps_labels_and_one_copy_of_the_prefix(tmp_path: Path) -> None:
    root = _build(tmp_path / "home")
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    store.ingest(READER, [StoreRoot(root)])
    connection = sqlite3.connect(tmp_path / "store" / "conversations.sqlite")
    rows = connection.execute(
        "SELECT session_id, origin, parent_session FROM conversation_turns"
        " WHERE agent = 'cursor-ide' GROUP BY session_id, origin, parent_session"
    ).fetchall()
    labels = {session: (origin, parent) for session, origin, parent in rows}
    assert len(rows) == len(labels) == 4
    assert labels[CHILD] == ("subagent", PARENT)
    assert labels[FORK] == ("subagent", PARENT)
    assert labels[LISTED] == ("subagent", PARENT)
    assert labels[PARENT][0] == "interactive" and labels[PARENT][1] == ""
    copies = connection.execute(
        "SELECT count(*) FROM conversation_turns WHERE agent = 'cursor-ide' AND text = ?",
        ("please fix the build",),
    ).fetchone()[0]
    found = connection.execute(
        "SELECT count(*) FROM conversation_turns WHERE agent = 'cursor-ide' AND text = ?",
        ("Found the problem.",),
    ).fetchone()[0]
    connection.close()
    assert copies == 1  # the fork's copy of the parent's first prompt is not stored
    assert found == 2  # the parent's turn, and the fork's own later reply


def test_own_prompt_repeating_parent_text_is_kept(tmp_path: Path) -> None:
    """Text matches never end the copied prefix: only bubble or tool call ids do."""

    root = _build(tmp_path)
    connection = sqlite3.connect(root / "globalStorage" / "state.vscdb")
    own = [
        _bubble("p1", 1, "please fix the build"),  # copied (same id)
        _bubble("g2", 1, "please fix the build"),  # own prompt, repeats parent text
        _bubble("g3", 2, "Fixing the build now."),
    ]
    other = "aaaa0000-0000-4000-8000-000000000006"
    composer = _composer(other, own, subagentInfo=_info(PARENT, "call-g"))
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{other}", json.dumps(composer))
    )
    for bubble in own:
        connection.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{other}:{bubble['bubbleId']}", json.dumps(bubble)),
        )
    connection.commit()
    connection.close()
    assert [(r.offset, r.text) for r in _read(root)[other]] == [
        (2, "please fix the build"),
        (4, "Fixing the build now."),
    ]
