"""Qwen Code reader: synthetic ``~/.qwen/projects`` stores only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from memfw.history.sources import default_roots, get_reader
from memfw.history.sources.labels import AUTOMATED, SUBAGENT
from memfw.history.sources.qwen import READER

SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ARCHIVED = "11111111-2222-4333-8444-555555555555"
WORKSPACE = "/work/demo"
PROJECT = "-work-demo"
# Assembled at runtime so the repository holds no credential-shaped literal.
SECRET = "pass" + "word=correct-horse-battery-9"


def _record(kind: str, index: int, **fields: Any) -> dict[str, Any]:
    return {
        "uuid": f"u{index}",
        "parentUuid": f"u{index - 1}" if index else None,
        "sessionId": SESSION,
        "timestamp": f"2026-01-02T03:04:{index:02d}.000Z",
        "type": kind,
        "cwd": WORKSPACE,
        "version": "0.0.0-test",
        **fields,
    }


def _user(index: int, text: str, **fields: Any) -> dict[str, Any]:
    return _record(
        "user",
        index,
        provenance="real_user",
        message={"role": "user", "parts": [{"text": text}]},
        **fields,
    )


def _records() -> list[Any]:
    return [
        _record(
            "system",
            0,
            subtype="session_source",
            provenance="system",
            systemPayload={"sourceType": "default"},
        ),
        _user(1, "please list the files"),
        _record(
            "assistant",
            2,
            provenance="assistant_output",
            model="qwen-test-model",
            message={
                "role": "model",
                "parts": [
                    {"text": "private reasoning", "thought": True},
                    {"text": "Listing now."},
                    {"functionCall": {"name": "list_directory", "args": {"path": WORKSPACE}}},
                ],
            },
        ),
        _record(
            "tool_result",
            3,
            provenance="tool_result",
            message={
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "list_directory",
                            "response": {"output": f"a.txt\nb.txt\n{SECRET}"},
                        }
                    }
                ],
            },
            toolCallResult={"callId": "c1", "status": "success", "resultDisplay": "2 files"},
        ),
        "{not json",
        _record("system", 4, subtype="ui_telemetry", provenance="system", systemPayload={"x": 1}),
        _record(
            "user",
            5,
            subtype="notification",
            provenance="system",
            message={"role": "user", "parts": [{"text": "background task finished"}]},
        ),
        _record(
            "tool_result",
            6,
            provenance="tool_result",
            message={
                "role": "user",
                "parts": [{"functionResponse": {"name": "x", "response": {}}}],
            },
            toolCallResult={"resultDisplay": "shown only"},
        ),
        _record("assistant", 7, message={"role": "model", "parts": [{"text": "  "}]}),
        _record(
            "assistant",
            8,
            model="qwen-test-model",
            message={"role": "model", "parts": [{"text": "Done: two files."}]},
        ),
        ["not", "an", "object"],
    ]


def _write(path: Path, rows: list[Any], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def _store(home: Path) -> Path:
    root = home / ".qwen" / "projects"
    project = root / PROJECT
    chat = _write(project / "chats" / f"{SESSION}.jsonl", _records())
    (project / "chats" / f"{SESSION}.runtime.json").write_text("{}", encoding="utf-8")
    (project / "chats" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    _write(project / "chats" / "archive" / f"{ARCHIVED}.jsonl", [_user(0, "old question")])
    _write(
        project / "subagents" / SESSION / "agent-explore1.jsonl",
        [_user(0, "explore the repo"), _record("assistant", 1, message={"parts": "found it"})],
    )
    (project / "subagents" / SESSION / "agent-explore1.meta.json").write_text(
        "{}", encoding="utf-8"
    )
    (home / ".qwen" / "settings.json").write_text('{"zz": "fake"}', encoding="utf-8")
    return chat


def _at(path: Path, index: int) -> int:
    """Byte offset of line ``index`` of ``path``."""

    offset = 0
    for number, line in enumerate(path.read_bytes().split(b"\n")):
        if number == index:
            return offset
        offset += len(line) + 1
    raise AssertionError(index)


def _main(root: Path) -> Any:
    return next(loc for loc in READER.sessions(root) if loc.session_id == SESSION)


def test_registered_with_alias(tmp_path: Path) -> None:
    assert get_reader("qwen") is READER
    assert get_reader("Qwen-Code") is READER
    assert default_roots(READER, tmp_path) == []
    _store(tmp_path)
    assert default_roots(READER, tmp_path) == [tmp_path / ".qwen" / "projects"]


def test_sessions_layout(tmp_path: Path) -> None:
    _store(tmp_path)
    [root] = default_roots(READER, tmp_path)
    locators = list(READER.sessions(root))
    assert [(loc.key, loc.session_id, loc.extra["parent_id"]) for loc in locators] == [
        (f"{PROJECT}/chats/{ARCHIVED}.jsonl", ARCHIVED, ""),
        (f"{PROJECT}/chats/{SESSION}.jsonl", SESSION, ""),
        (f"{PROJECT}/subagents/{SESSION}/agent-explore1.jsonl", "agent-explore1", SESSION),
    ]


def test_records(tmp_path: Path) -> None:
    chat = _store(tmp_path)
    [root] = default_roots(READER, tmp_path)
    locator = _main(root)
    records = list(READER.read(locator))
    assert [(r.offset, r.turn, r.role) for r in records] == [
        (_at(chat, 1), 0, "user"),
        (_at(chat, 2), 1, "assistant"),
        (_at(chat, 3), 2, "tool"),
        (_at(chat, 6), 3, "system"),
        (_at(chat, 7), 4, "tool"),
        (_at(chat, 9), 5, "assistant"),
    ]
    user, assistant, tool, notice, display, done = records
    assert user.text == "please list the files"
    assert user.cwd == WORKSPACE and user.project == "demo"
    assert user.timestamp == "2026-01-02T03:04:01.000Z"
    assert user.origin == "" and user.parent_session == ""
    assert assistant.text == "Listing now."
    assert "private reasoning" not in " ".join(r.text for r in records)
    assert [(c.name, c.summary) for c in assistant.tool_calls] == [
        ("list_directory", f"path: {WORKSPACE}")
    ]
    assert assistant.model == "qwen-test-model" and user.model == ""
    assert tool.text.startswith("a.txt\nb.txt")
    assert notice.text == "background task finished"
    assert display.text == "shown only"
    assert done.text == "Done: two files."
    assert all(r.session_id == SESSION and r.agent == "qwen" for r in records)


def test_incremental_after(tmp_path: Path) -> None:
    chat = _store(tmp_path)
    [root] = default_roots(READER, tmp_path)
    locator = _main(root)
    later = list(READER.read(locator, after=_at(chat, 3)))
    assert [(r.role, r.turn) for r in later] == [("system", 3), ("tool", 4), ("assistant", 5)]


def test_subagent_and_automated_labels(tmp_path: Path) -> None:
    _store(tmp_path)
    root = tmp_path / ".qwen" / "projects"
    sub = [loc for loc in READER.sessions(root) if loc.extra["parent_id"]]
    [record_user, record_assistant] = list(READER.read(sub[0]))
    assert record_user.origin == SUBAGENT and record_user.parent_session == SESSION
    assert record_assistant.text == "found it"

    scripted = _write(
        root / PROJECT / "chats" / "99999999-8888-4777-8666-555555555555.jsonl",
        [
            _record("system", 0, subtype="session_source", systemPayload={"sourceType": "script"}),
            _user(1, "run the nightly check"),
        ],
    )
    [locator] = [loc for loc in READER.sessions(root) if loc.path == scripted]
    [record] = list(READER.read(locator))
    assert record.origin == AUTOMATED


def test_malformed_missing_and_partial(tmp_path: Path) -> None:
    root = tmp_path / ".qwen" / "projects"
    assert list(READER.sessions(root)) == []
    chat = _write(
        root / PROJECT / "chats" / f"{SESSION}.jsonl",
        [
            _record("user", 0, message="not a dict"),
            _record("user", 1, message={"parts": [{"text": 5}, None, {"functionCall": 3}]}),
            _record("mystery", 2, message={"parts": [{"text": "skipped"}]}),
            _user(3, "kept"),
        ],
        tail=json.dumps(_user(4, "still streaming")),
    )
    [locator] = list(READER.sessions(root))
    assert [r.text for r in READER.read(locator)] == ["kept"]
    chat.unlink()
    assert list(READER.read(locator)) == []


def test_symlinked_project_and_files_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "chats" / f"{SESSION}.jsonl", [_user(0, "outside")])
    root = tmp_path / "home" / ".qwen" / "projects"
    root.mkdir(parents=True)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    chats = root / PROJECT / "chats"
    chats.mkdir(parents=True)
    (chats / f"{ARCHIVED}.jsonl").symlink_to(outside / "chats" / f"{SESSION}.jsonl")
    assert list(READER.sessions(root)) == []


def test_other_qwen_files_never_opened(tmp_path: Path, monkeypatch: Any) -> None:
    _store(tmp_path)
    from memfw.history.sources import base as base_module

    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(base_module.os, "open", spy)
    for root in default_roots(READER, tmp_path):
        for locator in READER.sessions(root):
            list(READER.read(locator))
    assert opened
    assert all(path.endswith(".jsonl") for path in opened)
    assert not [p for p in opened if "settings" in p or "meta.json" in p or "notes" in p]


def test_ingest_round_trip_redacts(tmp_path: Path) -> None:
    from memfw.history.conversations import ConversationStore
    from memfw.history.sources.base import StoreRoot

    chat = _store(tmp_path / "home")
    [root] = default_roots(READER, tmp_path / "home")
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [StoreRoot(root)])
    assert counts.sessions_seen == 3 and counts.sessions_failed == 0
    assert counts.records_new == 6 + 1 + 2
    stored = store.turns("qwen", SESSION)
    assert [t["role"] for t in stored] == [
        "user",
        "assistant",
        "tool",
        "system",
        "tool",
        "assistant",
    ]
    assert SECRET not in json.dumps(stored)
    assert "correct-horse-battery-9" not in (
        tmp_path / "store" / "conversations.sqlite"
    ).read_bytes().decode("utf-8", errors="replace")
    again = store.ingest(READER, [StoreRoot(root)])
    assert again.sessions_unchanged == 3 and again.records_new == 0

    # A rewrite that drops the last record is reconciled on the full re-read.
    _write(chat, _records()[:-2])
    third = store.ingest(READER, [StoreRoot(root)])
    assert third.sessions_read == 1
    assert [t["role"] for t in store.turns("qwen", SESSION)] == [
        "user",
        "assistant",
        "tool",
        "system",
        "tool",
    ]


def _texts(store: Any) -> list[str]:
    return [t["text"] for t in store.turns("qwen", SESSION)]


def test_archive_and_unarchive_keep_one_copy(tmp_path: Path) -> None:
    """Repro A: archive (a rename), then unarchive with a rewritten, shifted file."""

    from memfw.history.conversations import ConversationStore
    from memfw.history.sources.base import StoreRoot

    root = tmp_path / ".qwen" / "projects"
    active = _write(root / PROJECT / "chats" / f"{SESSION}.jsonl", [_user(0, "hello")])
    archived = root / PROJECT / "chats" / "archive" / f"{SESSION}.jsonl"
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    store.ingest(READER, [StoreRoot(root)])
    assert _texts(store) == ["hello"]

    archived.parent.mkdir(parents=True)
    active.rename(archived)
    [locator] = list(READER.sessions(root))
    assert locator.path == archived and locator.key == f"{PROJECT}/chats/{SESSION}.jsonl"
    store.ingest(READER, [StoreRoot(root)])
    assert _texts(store) == ["hello"]

    archived.rename(active)
    # Rewritten on unarchive: a new first line shifts every offset.
    _write(
        active, [_record("system", 0, subtype="custom_title"), _user(1, "hello"), _user(2, "hi")]
    )
    store.ingest(READER, [StoreRoot(root)])
    assert _texts(store) == ["hello", "hi"]
    store.ingest(READER, [StoreRoot(root)])
    assert _texts(store) == ["hello", "hi"]


def test_both_copies_present_reads_the_active_one(tmp_path: Path) -> None:
    """Repro B: an active and an archived copy that differ never mix."""

    from memfw.history.conversations import ConversationStore
    from memfw.history.sources.base import StoreRoot

    root = tmp_path / ".qwen" / "projects"
    chats = root / PROJECT / "chats"
    _write(chats / "archive" / f"{SESSION}.jsonl", [_user(0, "old copy"), _user(1, "old reply")])
    active = _write(chats / f"{SESSION}.jsonl", [_user(0, "new copy")])
    [locator] = list(READER.sessions(root))
    assert locator.path == active
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [StoreRoot(root)])
    assert counts.sessions_seen == 1
    assert _texts(store) == ["new copy"]
