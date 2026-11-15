"""Session labels at ingest: origin merge, dispatch markers, re-label pass, intent."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import ExtractiveBackend
from memfw.conversations.policy import SharePolicy
from memfw.conversations.worker import Budgets, ConversationWorker, WorkState
from memfw.history.conversations import ConversationStore
from memfw.history.sources.base import (
    ConversationRecord,
    SessionLocator,
    StoreRoot,
    iter_jsonl,
    relative_key,
    walk_files,
)
from memfw.history.sources.labels import merge_origin, prompt_origin, strip_injected
from tests.conversation_fixtures import turn, write_store

REMINDER = "<system-reminder>\nYou have tools. Be careful.\n</system-reminder>"
TICKET = "Ticket delivery contract - Complete this Ticket's task yourself.\n\nTask: fix it."


class JsonlReader:
    """A minimal append-only reader: one ``<session>.jsonl`` per session."""

    agent = "fake"
    default_roots = ("fake",)
    parser_version = "1"
    append_only = True

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        for path in walk_files(root, (".jsonl",)):
            yield SessionLocator(self.agent, path, relative_key(root, path), path.stem)

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        for index, (offset, raw) in enumerate(iter_jsonl(locator.path, after=after)):
            yield ConversationRecord(
                agent=self.agent,
                session_id=locator.session_id,
                offset=offset,
                turn=index,
                role=str(raw["role"]),
                text=str(raw["text"]),
                origin=str(raw.get("origin", "")),
                parent_session=str(raw.get("parent", "")),
            )


READER = JsonlReader()


def _write(root: Path, session: str, lines: list[dict[str, Any]], *, append: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{session}.jsonl").open("a" if append else "w") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def _origins(db: Path) -> dict[str, set[str]]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT session_id, origin FROM conversation_turns").fetchall()
    out: dict[str, set[str]] = {}
    for session, origin in rows:
        out.setdefault(session, set()).add(origin)
    return out


# -- helpers -----------------------------------------------------------------


def test_prompt_origin_detects_dispatch_markers() -> None:
    assert prompt_origin(TICKET) == "automated"
    assert prompt_origin(f"<user_query>\n{TICKET}\n</user_query>") == "automated"
    assert prompt_origin("You review one work line of a parallel dispatch. Reply") == "automated"
    assert prompt_origin("<goal_round n='2'>keep going</goal_round>") == "automated"
    assert prompt_origin("please fix the flaky uploader") == ""
    assert prompt_origin("") == ""


def test_merge_origin_precedence() -> None:
    assert merge_origin("interactive") == "interactive"
    assert merge_origin("interactive", "", "automated") == "automated"
    assert merge_origin("worker", "automated") == "worker"
    assert merge_origin("worker", "subagent") == "subagent"
    assert merge_origin("interactive", "subagent", "automated") == "subagent"
    assert merge_origin("interactive", "bogus") == "interactive"


def test_strip_injected_removes_system_reminder() -> None:
    assert strip_injected(f"{REMINDER}\n\nreal question here").strip() == "real question here"


# -- ingest ------------------------------------------------------------------


def test_reader_origin_and_parent_are_stored_for_the_whole_session(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(
        root,
        "child",
        [
            {"role": "user", "text": "sub task", "origin": "subagent", "parent": "parent-1"},
            {"role": "assistant", "text": "done", "parent": "parent-1"},
        ],
    )
    db = tmp_path / "store.sqlite"
    ConversationStore(db).ingest(READER, [StoreRoot(root)])
    assert _origins(db) == {"child": {"subagent"}}
    with sqlite3.connect(db) as connection:
        parents = {
            r[0] for r in connection.execute("SELECT parent_session FROM conversation_turns")
        }
    assert parents == {"parent-1"}


def test_dispatch_marker_behind_a_system_reminder_labels_automated(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(
        root,
        "ticket",
        [
            {"role": "user", "text": REMINDER},
            {"role": "user", "text": TICKET},
            {"role": "assistant", "text": "ok"},
        ],
    )
    _write(root, "human", [{"role": "user", "text": "fix my uploader please"}])
    db = tmp_path / "store.sqlite"
    ConversationStore(db).ingest(READER, [StoreRoot(root)])
    assert _origins(db) == {"ticket": {"automated"}, "human": {"interactive"}}


def test_marker_only_in_a_later_prompt_does_not_label(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(
        root,
        "human",
        [
            {"role": "user", "text": "look at this ticket text for me"},
            {"role": "user", "text": TICKET},
        ],
    )
    db = tmp_path / "store.sqlite"
    ConversationStore(db).ingest(READER, [StoreRoot(root)])
    assert _origins(db) == {"human": {"interactive"}}


def test_worker_root_keeps_worker_unless_subagent(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(root, "w1", [{"role": "user", "text": TICKET}])
    _write(root, "w2", [{"role": "user", "text": "child", "origin": "subagent"}])
    db = tmp_path / "store.sqlite"
    ConversationStore(db).ingest(READER, [StoreRoot(root, "worker")])
    assert _origins(db) == {"w1": {"worker"}, "w2": {"subagent"}}


def test_incremental_read_keeps_the_session_label(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(root, "ticket", [{"role": "user", "text": TICKET}, {"role": "assistant", "text": "a"}])
    db = tmp_path / "store.sqlite"
    store = ConversationStore(db)
    store.ingest(READER, [StoreRoot(root)])
    _write(
        root,
        "ticket",
        [{"role": "user", "text": "continue"}, {"role": "assistant", "text": "b"}],
        append=True,
    )
    counts = store.ingest(READER, [StoreRoot(root)])
    assert counts.records_new == 2
    assert _origins(db) == {"ticket": {"automated"}}


def test_existing_store_is_relabelled_at_the_next_ingest(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    _write(root, "ticket", [{"role": "user", "text": TICKET}, {"role": "assistant", "text": "a"}])
    db = tmp_path / "store.sqlite"
    store = ConversationStore(db)
    store.ingest(READER, [StoreRoot(root)])
    # Simulate a store written before labels: no parent column, no labels
    # version, every turn "interactive", cursors up to date.
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE conversation_turns SET origin = 'interactive'")
        connection.execute("ALTER TABLE conversation_turns DROP COLUMN parent_session")
        connection.execute("DELETE FROM conversation_meta WHERE key LIKE 'labels_version:%'")
    counts = ConversationStore(db).ingest(READER, [StoreRoot(root)])
    assert counts.sessions_read == 1 and counts.extra["relabelled"] is True
    assert _origins(db) == {"ticket": {"automated"}}
    with sqlite3.connect(db) as connection:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(conversation_turns)")}
    assert "parent_session" in columns
    # Done once: the next ingest skips the unchanged session again.
    again = ConversationStore(db).ingest(READER, [StoreRoot(root)])
    assert again.sessions_unchanged == 1 and again.extra["relabelled"] is False


# -- summary intent ----------------------------------------------------------


def test_intent_skips_an_injected_first_user_turn(tmp_path: Path) -> None:
    day = "2026-01-05T10:00:0"
    rows = [
        turn("minimax", "s-mm", 0, "user", REMINDER, day + "0Z"),
        turn("minimax", "s-mm", 1, "user", f"{REMINDER}\nAdd a retry wrapper.", day + "1Z"),
        turn("minimax", "s-mm", 2, "assistant", "Done, tests pass.", day + "2Z"),
    ]
    store = tmp_path / "data" / "conversations.sqlite"
    write_store(store, rows)
    state = WorkState(tmp_path / "data" / "work.sqlite")
    worker = ConversationWorker(
        SqliteStoreSource(store),
        state,
        backend=ExtractiveBackend(),
        policy=SharePolicy.from_config({}),
        budgets=Budgets(),
        now=lambda: datetime(2026, 1, 6, 12, 0, tzinfo=UTC),
    )
    worker.run()
    with sqlite3.connect(tmp_path / "data" / "work.sqlite") as connection:
        intents = [r[0] for r in connection.execute("SELECT intent FROM episodes")]
    assert intents == ["Add a retry wrapper."]
