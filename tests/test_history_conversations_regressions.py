"""Regression tests for the conversation store's ingest step (synthetic fixtures)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from memfw.history import conversations
from memfw.history.conversations import BUSY_RETRIES, LABELS_VERSION, ConversationStore
from memfw.history.sources import gemini
from memfw.history.sources.base import (
    ConversationRecord,
    SessionLocator,
    SessionTooLarge,
    StoreRoot,
)
from memfw.history.sources.claude import READER as CLAUDE


def _line(session: str, uuid: str, role: str, text: str, ts: str = "") -> str:
    raw: dict[str, object] = {
        "sessionId": session,
        "uuid": uuid,
        "cwd": "/w",
        "message": {"role": role, "content": text},
    }
    if ts:
        raw["timestamp"] = ts
    return json.dumps(raw) + "\n"


def _labels_version(db: Path) -> str | None:
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = 'labels_version:claude'"
        ).fetchone()
    return None if row is None else str(row[0])


def test_lone_surrogate_is_stored_as_replacement_char(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    for index in range(3):
        (root / "p" / f"good{index}.jsonl").write_text(_line(f"good{index}", "a", "user", "hi"))
    # json.dumps escapes the lone surrogate, as a JS writer that cut a UTF-16 pair does.
    (root / "p" / "bad.jsonl").write_text(
        _line("bad", "a", "user", "hello") + _line("bad", "b", "assistant", "half \ud83d cut")
    )
    store = ConversationStore(tmp_path / "conv.sqlite")
    first = store.ingest(CLAUDE, [StoreRoot(root)])
    assert first.sessions_failed == 0
    assert first.extra["relabelled"] is True
    assert _labels_version(tmp_path / "conv.sqlite") == LABELS_VERSION
    turns = store.turns("claude", "bad")
    assert [t["text"] for t in turns] == ["hello", "half � cut"]
    second = store.ingest(CLAUDE, [StoreRoot(root)])
    assert second.sessions_unchanged == 4 and second.sessions_read == 0


def test_same_session_in_two_roots_keeps_the_other_copy(tmp_path: Path) -> None:
    a = tmp_path / "home-a" / "projects"
    b = tmp_path / "home-b" / "projects"
    for root in (a, b):
        (root / "p").mkdir(parents=True)
    lines = [
        _line("S", str(i), "user" if i % 2 == 0 else "assistant", f"turn {i}") for i in range(6)
    ]
    (a / "p" / "S.jsonl").write_text("".join(lines))
    (b / "p" / "S.jsonl").write_text("".join(lines))
    store = ConversationStore(tmp_path / "conv.sqlite")
    roots = [StoreRoot(a), StoreRoot(b, "worker")]
    store.ingest(CLAUDE, roots)
    assert len(store.turns("claude", "S")) == 6

    os.remove(a / "p" / "S.jsonl")
    (a / "p" / "S.jsonl").write_text("".join(lines[:2]))
    counts = store.ingest(CLAUDE, roots)
    assert counts.records_dropped == 0
    assert len(store.turns("claude", "S")) == 6

    with open(b / "p" / "S.jsonl", "a", encoding="utf-8") as handle:
        handle.write(_line("S", "9", "user", "turn 6"))
    store.ingest(CLAUDE, roots)
    assert len(store.turns("claude", "S")) == 7

    # Once neither copy holds a turn any more, it goes.
    os.remove(b / "p" / "S.jsonl")
    (b / "p" / "S.jsonl").write_text("".join(lines[:2]))
    counts = store.ingest(CLAUDE, roots)
    assert counts.records_dropped == 5
    assert len(store.turns("claude", "S")) == 2


def test_emptied_session_is_not_busy_forever(tmp_path: Path) -> None:
    root = tmp_path / "projects" / "-w-p"
    root.mkdir(parents=True)
    for n in range(3):
        (root / f"s{n}.jsonl").write_text(
            _line(f"s{n}", "a", "user", f"hello {n}") + _line(f"s{n}", "b", "assistant", "hi")
        )
    db = tmp_path / "c.sqlite"
    store = ConversationStore(db)
    store.ingest(CLAUDE, [tmp_path / "projects"])
    (root / "s0.jsonl").write_text("")
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE conversation_meta SET value = '1' WHERE key LIKE 'labels_%'")
    busy = [store.ingest(CLAUDE, [tmp_path / "projects"]) for _ in range(BUSY_RETRIES)]
    assert [c.sessions_busy for c in busy[:-1]] == [1] * (BUSY_RETRIES - 1)
    assert busy[-1].sessions_busy == 0 and busy[-1].sessions_stale == 1
    assert _labels_version(db) == LABELS_VERSION
    # Stored rows are kept; the next ingest has nothing to do.
    assert len(store.turns("claude", "s0")) == 2
    after = store.ingest(CLAUDE, [tmp_path / "projects"])
    assert after.sessions_busy == 0 and after.sessions_read == 0
    # New content in the file is read in full again.
    (root / "s0.jsonl").write_text(_line("s0", "z", "user", "fresh"))
    store.ingest(CLAUDE, [tmp_path / "projects"])
    assert [t["text"] for t in store.turns("claude", "s0")] == ["fresh"]


def test_gemini_bad_patch_line_does_not_stall_the_session(tmp_path: Path) -> None:
    chats = tmp_path / "gm" / "p" / "chats"
    chats.mkdir(parents=True)
    log = chats / "session-x.jsonl"
    lines: list[dict[str, object]] = [
        {"sessionId": "s1", "kind": "main"},
        {"id": "m1", "type": "user", "content": "hello"},
    ]
    log.write_text("\n".join(map(json.dumps, lines)) + "\n")
    store = ConversationStore(tmp_path / "conv.sqlite")
    store.ingest(gemini.READER, [StoreRoot(tmp_path / "gm")])
    lines += [
        {"$patch": {"id": "m1", "updates": 1}},
        {"id": "m2", "type": "user", "content": "new"},
    ]
    log.write_text("\n".join(map(json.dumps, lines)) + "\n")
    counts = store.ingest(gemini.READER, [StoreRoot(tmp_path / "gm")])
    assert counts.sessions_busy == 0 and counts.sessions_failed == 0
    assert [t["text"] for t in store.turns("gemini", "s1")] == ["hello", "new"]


class _FakeReader:
    agent = "fake"
    default_roots: tuple[str, ...] = ()
    parser_version = "1"
    append_only = False

    def __init__(self, key: str = "s.json") -> None:
        self.key = key
        self.records: list[ConversationRecord] = []
        self.too_large = False

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        yield SessionLocator(self.agent, root / "s.json", self.key, "s1")

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        if self.too_large:
            raise SessionTooLarge("over the limit")
        yield from (r for r in self.records if r.offset > after)


def _record(offset: int, text: str, ts: str = "") -> ConversationRecord:
    return ConversationRecord("fake", "s1", offset, offset, "user", text, timestamp=ts)


def test_session_over_size_limit_keeps_rows_and_is_not_busy(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text("{}")
    reader = _FakeReader()
    reader.records = [_record(0, "hello"), _record(1, "more")]
    store = ConversationStore(tmp_path / "conv.sqlite")
    store.ingest(reader, [tmp_path])
    (tmp_path / "s.json").write_text("{ bigger }")
    reader.too_large = True
    for _ in range(2):
        counts = store.ingest(reader, [tmp_path])
        assert counts.sessions_busy == 0 and counts.sessions_failed == 0
    assert counts.sessions_too_large == 0 and counts.sessions_unchanged == 1
    assert len(store.turns("fake", "s1")) == 2


def test_timestamps_with_offset_are_stored_as_utc(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text("{}")
    reader = _FakeReader()
    reader.records = [
        _record(0, "a", "2026-11-16T09:00:00+08:00"),
        _record(1, "b", "2026-11-16T01:30:00.250Z"),
        _record(2, "c", "2026-11-16T02:00:00"),
    ]
    store = ConversationStore(tmp_path / "conv.sqlite")
    store.ingest(reader, [tmp_path])
    assert [t["ts"] for t in store.turns("fake", "s1")] == [
        "2026-11-16T01:00:00Z",
        "2026-11-16T01:30:00.250Z",
        "2026-11-16T02:00:00",
    ]


def test_non_utf8_locator_key_is_hashed(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text("{}")
    reader = _FakeReader(key=os.fsdecode(b"caf\xe9.json"))  # surrogateescape on POSIX
    reader.records = [_record(0, "hello")]
    store = ConversationStore(tmp_path / "conv.sqlite")
    counts = store.ingest(reader, [tmp_path])
    assert counts.sessions_failed == 0 and counts.records_new == 1


def test_schema_1_store_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "conv.sqlite"
    (tmp_path / "s.json").write_text("{}")
    reader = _FakeReader()
    reader.records = [_record(0, "hello"), _record(1, "more")]
    store = ConversationStore(db)
    store.ingest(reader, [tmp_path])
    # Turn the store back into a schema-1 one: no ownership table.
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE conversation_turn_locators")
        connection.execute("UPDATE conversation_meta SET value = '1' WHERE key = 'schema_version'")
    counts = store.ingest(reader, [tmp_path])
    assert counts.sessions_read == 1 and counts.records_duplicate == 2
    with sqlite3.connect(db) as connection:
        owned = connection.execute("SELECT COUNT(*) FROM conversation_turn_locators").fetchone()
        version = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert owned[0] == 2 and version[0] == conversations.SCHEMA_VERSION


def _roots(*paths: Path) -> list[StoreRoot]:
    return [StoreRoot(path) for path in paths]


def _v1_downgrade(db: Path) -> None:
    """Rewrite a fresh store into the schema-1 shape (no ownership table)."""

    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE conversation_turn_locators")
        connection.execute("UPDATE conversation_meta SET value = '1' WHERE key = 'schema_version'")


def _owner_rows(db: Path) -> int:
    with sqlite3.connect(db) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM conversation_turn_locators").fetchone()[0]
        )


def test_migration_failure_rolls_back_and_retries(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "projects" / "p"
    home.mkdir(parents=True)
    (home / "S.jsonl").write_text(
        "".join(
            _line("S", str(i), "user" if i % 2 == 0 else "assistant", f"turn {i}") for i in range(4)
        )
    )
    db = tmp_path / "conv.sqlite"
    store = ConversationStore(db)
    store.ingest(CLAUDE, _roots(tmp_path / "projects"))
    _v1_downgrade(db)

    real = sqlite3.connect
    armed = {"on": True}

    def failing(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real(*args, **kwargs)  # type: ignore[arg-type]
        if armed["on"]:
            armed["on"] = False

            def authorizer(action: int, arg1: str | None, *_rest: object) -> int:
                if action == sqlite3.SQLITE_INSERT and arg1 == "conversation_turn_locators":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(conversations.sqlite3, "connect", failing)
    try:
        ConversationStore(db).ingest(CLAUDE, _roots(tmp_path / "projects"))
    except sqlite3.Error:
        pass
    else:  # pragma: no cover
        raise AssertionError("migration should have failed")
    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'conversation_turn_locators'"
        ).fetchone()
    assert version == "1" and has_table is None  # fully rolled back

    monkeypatch.undo()
    # the file now shrinks: a half-migrated store would lose or keep turns wrongly
    ConversationStore(db).ingest(CLAUDE, _roots(tmp_path / "projects"))
    assert _owner_rows(db) == 4
    assert len(ConversationStore(db).turns("claude", "S")) == 4
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute(
                "SELECT value FROM conversation_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "2"
        )


def test_owner_rows_repaired_on_open(tmp_path: Path) -> None:
    home = tmp_path / "projects" / "p"
    home.mkdir(parents=True)
    (home / "S.jsonl").write_text(
        "".join(_line("S", str(i), "user", f"turn {i}") for i in range(3))
    )
    db = tmp_path / "conv.sqlite"
    ConversationStore(db).ingest(CLAUDE, _roots(tmp_path / "projects"))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "DELETE FROM conversation_turn_locators"
            " WHERE offset = (SELECT MIN(offset) FROM conversation_turns)"
        )
        connection.execute(
            "INSERT INTO conversation_turn_locators VALUES ('claude', 'GONE', 9, 'x')"
        )
    assert _owner_rows(db) == 3  # 2 + 1 orphan
    ConversationStore(db).turns("claude", "S")  # any open repairs
    assert _owner_rows(db) == 3
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turn_locators WHERE session_id = 'GONE'"
            ).fetchone()[0]
            == 0
        )


def test_repair_skips_consistent_and_readonly_stores(tmp_path: Path) -> None:
    home = tmp_path / "projects" / "p"
    home.mkdir(parents=True)
    (home / "S.jsonl").write_text(
        "".join(_line("S", str(i), "user", f"turn {i}") for i in range(2))
    )
    db = tmp_path / "conv.sqlite"
    ConversationStore(db).ingest(CLAUDE, _roots(tmp_path / "projects"))
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO conversation_turn_locators VALUES ('claude','GONE',9,'x')")
    # a connection that cannot write: the repair is skipped, not an error
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA query_only = ON")
        ConversationStore._repair_owners(connection)
    assert _owner_rows(db) == 3
    ConversationStore(db).turns("claude", "S")
    assert _owner_rows(db) == 2
    before = db.stat().st_mtime_ns
    ConversationStore(db).turns("claude", "S")  # consistent: nothing written
    assert db.stat().st_mtime_ns == before
