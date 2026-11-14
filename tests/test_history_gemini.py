from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from memfw.history.conversations import ConversationStore
from memfw.history.sources import base as base_module
from memfw.history.sources import default_roots, get_reader
from memfw.history.sources.base import SessionLocator
from memfw.history.sources.gemini import READER

PROJECT = "/work/zztester/work/demo"
SESSION = "11111111-2222-4333-8444-555555555555"


def _write(path: Path, rows: list[Any], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
    return path


def _header(**extra: Any) -> dict[str, Any]:
    return {
        "sessionId": SESSION,
        "projectHash": "0" * 64,
        "startTime": "2026-01-02T03:04:00.000Z",
        "lastUpdated": "2026-01-02T03:04:00.000Z",
        "kind": "main",
        **extra,
    }


def _rows() -> list[Any]:
    return [
        _header(),
        {
            "id": "u1",
            "timestamp": "2026-01-02T03:04:05.000Z",
            "type": "user",
            "content": [{"text": "list the files"}],
        },
        {"$set": {"lastUpdated": "2026-01-02T03:04:05.000Z"}},
        {
            "id": "g1",
            "timestamp": "2026-01-02T03:04:06.000Z",
            "type": "gemini",
            "content": "",
            "model": "fake-gemini-pro",
            "toolCalls": [
                {
                    "id": "call-1",
                    "name": "list_directory",
                    "args": {"path": PROJECT},
                    "status": "executing",
                }
            ],
            "thoughts": [{"subject": "plan", "description": "secret thinking"}],
        },
        # Re-recording the same id replaces the message (now with its result).
        {
            "id": "g1",
            "timestamp": "2026-01-02T03:04:06.000Z",
            "type": "gemini",
            "content": "",
            "model": "fake-gemini-pro",
            "toolCalls": [
                {
                    "id": "call-1",
                    "name": "list_directory",
                    "args": {"path": PROJECT},
                    "status": "success",
                    "result": [
                        {
                            "functionResponse": {
                                "id": "call-1",
                                "name": "list_directory",
                                "response": {"output": "a.txt\nb.txt"},
                            }
                        }
                    ],
                }
            ],
            "tokens": {"input": 10, "output": 2, "total": 12},
        },
        "{not json",
        {
            "id": "g2",
            "timestamp": "2026-01-02T03:04:07.000Z",
            "type": "gemini",
            "content": [{"text": "hidden", "thought": True}, {"text": "two files"}],
            "model": "fake-gemini-pro",
        },
        {"id": "i1", "timestamp": "2026-01-02T03:04:08.000Z", "type": "info", "content": "note"},
        {"id": "x1", "timestamp": "2026-01-02T03:04:09.000Z", "type": "mystery", "content": "?"},
        ["not", "an", "object"],
        {"id": "g3", "timestamp": "2026-01-02T03:04:10.000Z", "type": "gemini", "content": "  "},
    ]


def _store(home: Path) -> Path:
    project = home / ".gemini" / "tmp" / "demo-slug"
    project.mkdir(parents=True)
    (project / ".project_root").write_text(PROJECT + "\n", encoding="utf-8")
    (project / "logs.json").write_text("[]", encoding="utf-8")
    return _write(project / "chats" / "session-2026-01-02T03-04-11111111.jsonl", _rows())


def test_registered_with_alias() -> None:
    assert get_reader("gemini") is READER
    assert get_reader("gemini-cli") is READER
    assert READER.append_only is False


def test_sessions_and_records(tmp_path: Path) -> None:
    path = _store(tmp_path)
    [root] = default_roots(READER, tmp_path)
    [locator] = list(READER.sessions(root))
    assert locator.path == path
    assert locator.key == "demo-slug/chats/session-2026-01-02T03-04-11111111.jsonl"
    records = list(READER.read(locator))
    assert [r.role for r in records] == ["user", "assistant", "tool", "assistant", "system"]
    assert [r.turn for r in records] == [0, 1, 2, 3, 4]
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    user, call, result, reply, info = records
    assert user.text == "list the files"
    assert user.session_id == SESSION
    assert user.cwd == PROJECT and user.project == "demo-slug"
    assert user.timestamp == "2026-01-02T03:04:05.000Z"
    assert call.text == "" and call.tool_calls[0].name == "list_directory"
    assert PROJECT in call.tool_calls[0].summary
    assert call.model == "fake-gemini-pro"
    assert result.offset == call.offset + 1
    assert result.text == "a.txt\nb.txt"
    assert reply.text == "two files"
    assert "secret" not in " ".join(r.text for r in records)
    assert info.text == "note"


def test_incremental_after(tmp_path: Path) -> None:
    _store(tmp_path)
    [locator] = list(READER.sessions(tmp_path / ".gemini" / "tmp"))
    records = list(READER.read(locator))
    later = list(READER.read(locator, after=records[1].offset))
    assert [r.offset for r in later] == [r.offset for r in records[2:]]
    assert [r.turn for r in later] == [2, 3, 4]


def test_rewind_patch_and_set(tmp_path: Path) -> None:
    rows = [
        _header(),
        {"id": "u1", "timestamp": "t1", "type": "user", "content": "first"},
        {"id": "g1", "timestamp": "t2", "type": "gemini", "content": "draft"},
        {"id": "u2", "timestamp": "t3", "type": "user", "content": "dropped"},
        {"$rewindTo": "u2"},
        {"$patch": {"id": "g1", "content": "final"}},
        {"id": "u3", "timestamp": "t4", "type": "user", "content": "again"},
        {"$patch": {"removeIds": ["nope"]}},
    ]
    path = _write(tmp_path / "p" / "chats" / "session-x.jsonl", rows)
    [locator] = list(READER.sessions(tmp_path))
    assert locator.path == path
    assert [r.text for r in READER.read(locator)] == ["first", "final", "again"]

    reset = rows + [{"$set": {"messages": [{"id": "s1", "type": "user", "content": "compressed"}]}}]
    _write(path, reset)
    assert [r.text for r in READER.read(locator)] == ["compressed"]


def test_legacy_json_and_subagent(tmp_path: Path) -> None:
    chats = tmp_path / "abc123" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-2025-01-01T00-00-aaaa.json").write_text(
        json.dumps(
            {
                "sessionId": "legacy-1",
                "projectHash": "abc123",
                "messages": [
                    {"id": "a", "timestamp": "t", "type": "user", "content": "old hello"},
                    {"id": "b", "timestamp": "t", "type": "gemini", "content": "old reply"},
                ],
            }
        ),
        encoding="utf-8",
    )
    _write(
        chats / SESSION / "sub-1.jsonl",
        [
            _header(sessionId="sub-1", kind="subagent", directories=["/work/zztester/sub"]),
            {"id": "m", "timestamp": "t", "type": "user", "content": "sub task"},
        ],
    )
    (chats / "notes.txt").write_text("ignored", encoding="utf-8")
    (chats / "other.jsonl").write_text("{}\n", encoding="utf-8")
    locators = {loc.key: loc for loc in READER.sessions(tmp_path)}
    assert sorted(locators) == [
        f"abc123/chats/{SESSION}/sub-1.jsonl",
        "abc123/chats/session-2025-01-01T00-00-aaaa.json",
    ]
    legacy = list(READER.read(locators["abc123/chats/session-2025-01-01T00-00-aaaa.json"]))
    assert [(r.role, r.text, r.offset) for r in legacy] == [
        ("user", "old hello", 0),
        ("assistant", "old reply", 2),
    ]
    assert legacy[0].session_id == "legacy-1"
    sub = locators[f"abc123/chats/{SESSION}/sub-1.jsonl"]
    assert sub.extra["parent_id"] == SESSION
    [record] = list(READER.read(sub))
    assert record.session_id == "sub-1" and record.cwd == "/work/zztester/sub"


def test_malformed_and_missing(tmp_path: Path) -> None:
    chats = tmp_path / "p" / "chats"
    _write(chats / "session-bad.jsonl", ["{oops", "[1, 2]", '"text"'], tail='{"id": "partial"')
    (chats / "session-bad.json").write_text("{not json", encoding="utf-8")
    for locator in READER.sessions(tmp_path):
        assert list(READER.read(locator)) == []
    missing = SessionLocator("gemini", tmp_path / "nope.jsonl", key="nope.jsonl")
    assert list(READER.read(missing)) == []
    assert list(READER.sessions(tmp_path / "absent")) == []
    link = tmp_path / "linked"
    os.symlink(chats.parent, link)
    assert all(not loc.key.startswith("linked/") for loc in READER.sessions(tmp_path))


def test_credential_files_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gemini = tmp_path / ".gemini"
    _store(tmp_path)
    for name in ("oauth_creds.json", "google_accounts.json"):
        (gemini / name).write_text('{"zz": "fake"}', encoding="utf-8")
        # Even a credential file dropped inside chats/ is skipped.
        (gemini / "tmp" / "demo-slug" / "chats" / name).write_text("{}", encoding="utf-8")
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
    assert not [p for p in opened if "oauth_creds" in p or "google_accounts" in p]
    bogus = SessionLocator("gemini", gemini / "oauth_creds.json", key="oauth_creds.json")
    assert list(READER.read(bogus)) == []
    assert not [p for p in opened if "oauth_creds" in p]


def _ingest(tmp_path: Path, root: Path) -> list[dict[str, Any]]:
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [root])
    assert counts.sessions_failed == 0
    return store.turns("gemini", SESSION)


def _msg(key: str, kind: str, text: str) -> dict[str, Any]:
    return {"id": key, "timestamp": "2026-01-02T03:04:05.000Z", "type": kind, "content": text}


def test_header_and_set_lists_store_every_message(tmp_path: Path) -> None:
    # One line can record several messages (a header or ``$set`` list); each needs its
    # own offset or the store keeps only the last one.
    header = [_msg("a", "user", "alpha"), _msg("b", "gemini", "beta")]
    rows: list[Any] = [_header(messages=header)]
    path = _write(tmp_path / "home" / "p" / "chats" / "session-four.jsonl", rows)
    [locator] = list(READER.sessions(tmp_path / "home"))
    before = {r.text: r.offset for r in READER.read(locator)}
    assert len(set(before.values())) == 2

    # ``$set.messages`` replaces the list; here it re-lists the header's two and adds two.
    listed = header + [
        _msg("c", "user", "gamma"),
        {
            **_msg("d", "gemini", "delta"),
            "toolCalls": [{"id": "t", "name": "read_file", "args": {}, "resultDisplay": "ok"}],
        },
    ]
    rows.append({"$set": {"messages": listed}})
    _write(path, rows)
    records = list(READER.read(locator))
    offsets = [r.offset for r in records]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    # Messages already recorded keep their offsets as the log grows.
    assert {r.text: r.offset for r in records if r.text in before} == before
    stored = _ingest(tmp_path, tmp_path / "home")
    assert [t["text"] for t in stored] == ["alpha", "beta", "gamma", "delta", "ok"]


def test_set_list_without_header_messages(tmp_path: Path) -> None:
    rows = [
        _header(),
        _msg("u1", "user", "dropped by set"),
        {"$set": {"messages": [_msg("s1", "user", "one"), _msg("s2", "gemini", "two")]}},
        _msg("u2", "user", "three"),
    ]
    _write(tmp_path / "home" / "p" / "chats" / "session-set.jsonl", rows)
    stored = _ingest(tmp_path, tmp_path / "home")
    assert [t["text"] for t in stored] == ["one", "two", "three"]


def test_order_ids_reorder_is_not_reflected(tmp_path: Path) -> None:
    rows = [
        _header(),
        _msg("u1", "user", "first"),
        _msg("g1", "gemini", "second"),
        {"$patch": {"orderIds": ["g1", "u1"]}},
    ]
    _write(tmp_path / "p" / "chats" / "session-order.jsonl", rows)
    [locator] = list(READER.sessions(tmp_path))
    records = list(READER.read(locator))
    # Records follow recording (offset) order; the reorder is documented as ignored.
    assert [r.text for r in records] == ["first", "second"]
    assert [r.offset for r in records] == sorted(r.offset for r in records)


def test_rewind_to_unknown_id_is_a_no_op(tmp_path: Path) -> None:
    rows = [
        _header(),
        _msg("u1", "user", "kept"),
        _msg("g1", "gemini", "also kept"),
        {"$rewindTo": "no-such-id"},
    ]
    _write(tmp_path / "p" / "chats" / "session-rewind.jsonl", rows)
    [locator] = list(READER.sessions(tmp_path))
    assert [r.text for r in READER.read(locator)] == ["kept", "also kept"]
