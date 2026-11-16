"""Regressions: share handoff tail/goal, timezone-safe ``since``/ordering, idle without ts.

Synthetic stores only, built in ``tmp_path`` with raw rows so the stored
timestamp text is exactly what an older ingest may have written.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import ExtractiveBackend
from memfw.conversations.policy import SharePolicy
from memfw.conversations.share import HANDOFF_MAX_TURNS, ConversationShare
from memfw.conversations.store import (
    SessionInfo,
    Turn,
    goal_text,
    ts_display,
    ts_key,
)
from memfw.conversations.worker import Budgets, ConversationWorker, WorkState

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    agent TEXT NOT NULL, session_id TEXT NOT NULL, offset INTEGER NOT NULL,
    turn INTEGER NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL,
    tool_calls TEXT NOT NULL, cwd TEXT NOT NULL, project TEXT NOT NULL, model TEXT NOT NULL,
    origin TEXT NOT NULL, withheld INTEGER NOT NULL, parser_version TEXT NOT NULL,
    locator TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (agent, session_id, offset));
"""
INJECTED = (
    "<system-reminder>Contents of the project instructions: always use pnpm;"
    " private infra notes</system-reminder>"
)


def row(
    agent: str, session: str, offset: int, role: str, text: str, ts: str, cwd: str = "/work/demo"
) -> dict[str, Any]:
    return {
        "agent": agent,
        "session_id": session,
        "offset": offset,
        "turn": offset,
        "role": role,
        "ts": ts,
        "text": text,
        "tool_calls": json.dumps([]),
        "cwd": cwd,
        "project": "demo",
        "model": "m",
        "origin": "interactive",
        "withheld": 0,
        "parser_version": "1",
        "locator": f"{agent}/{session}",
        "digest": f"d{offset}",
    }


def write_store(path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT OR REPLACE INTO conversation_turns VALUES (:agent, :session_id, :offset,"
            " :turn, :role, :ts, :text, :tool_calls, :cwd, :project, :model, :origin,"
            " :withheld, :parser_version, :locator, :digest)",
            list(rows),
        )
    path.chmod(0o600)
    return path


class ListSource:
    """A source over an in-memory turn list (position-based paging, like the store)."""

    def __init__(self, turns: Sequence[Turn]) -> None:
        self.items = list(turns)
        self.calls: list[tuple[int, int]] = []

    def available(self) -> bool:
        return True

    def find_session(
        self, session_id: str, apps: Sequence[str] | None, exclude: Sequence[str] = ()
    ) -> list[SessionInfo]:
        return [SessionInfo("claude", "s1", len(self.items), 0, len(self.items) - 1)]

    def turns(self, app: str, session_id: str, *, start: int = 0, limit: int = 200) -> list[Turn]:
        self.calls.append((start, limit))
        return self.items[start : start + limit]


def _long_session(count: int, first: str = "message number 0") -> list[Turn]:
    out = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        text = first if index == 0 else f"message number {index}"
        out.append(Turn("claude", "s1", index, index, role, "2026-11-16T00:00:00Z", text))
    return out


# -- share.handoff: tail, not head -------------------------------------------------


def test_handoff_recent_turns_come_from_the_tail_of_a_long_session() -> None:
    total = HANDOFF_MAX_TURNS + 100
    source = ListSource(_long_session(total))
    share = ConversationShare(source, SharePolicy(enabled=True), "operator")
    pack = share.handoff("s1", max_bytes=65536, recent=3)
    recent = [turn["text"] for turn in pack["system"]["recent_turns"]]
    assert recent == [f"message number {total - 3 + i}" for i in range(3)]
    assert pack["source"]["turns_read"] == HANDOFF_MAX_TURNS
    assert pack["source"]["turns_start"] == 100
    # The goal still comes from the head, via a small bounded second read.
    assert pack["system"]["goal"] == "message number 0"
    assert (0, 200) in source.calls


def test_handoff_short_session_reads_once() -> None:
    source = ListSource(_long_session(10))
    pack = ConversationShare(source, SharePolicy(enabled=True), "operator").handoff("s1")
    assert source.calls == [(0, HANDOFF_MAX_TURNS)]
    assert pack["source"]["turns_start"] == 0
    assert pack["system"]["goal"] == "message number 0"


def test_handoff_goal_skips_injected_head_in_long_session() -> None:
    turns = _long_session(HANDOFF_MAX_TURNS + 10, first=INJECTED)
    share = ConversationShare(ListSource(turns), SharePolicy(enabled=True), "operator")
    assert share.handoff("s1")["system"]["goal"] == "message number 2"


def test_share_handoff_goal_skips_harness_injected_first_turn(tmp_path: Path) -> None:
    db = write_store(
        tmp_path / "store.sqlite",
        [
            row("claude", "s1", 0, "user", INJECTED, "2026-11-16T01:00:00Z"),
            row("claude", "s1", 1, "user", "Fix the login redirect bug", "2026-11-16T01:00:01Z"),
            row("claude", "s1", 2, "assistant", "Looking at auth.py", "2026-11-16T01:00:02Z"),
        ],
    )
    share = ConversationShare(SqliteStoreSource(db), SharePolicy(enabled=True), "kimi")
    goal = share.handoff("s1")["system"]["goal"]
    assert goal == "Fix the login redirect bug"
    assert "pnpm" not in goal


def test_goal_text_rules() -> None:
    assert goal_text([INJECTED, "", "Fix the bug please"]) == "Fix the bug please"
    assert goal_text([INJECTED, "fix"]) == "fix"  # fallback: residual text without 2 words
    assert goal_text([INJECTED]) == ""
    assert goal_text([f"{INJECTED}\n\nRefactor the parser module"]) == (
        "Refactor the parser module"
    )


# -- timestamps: offsets, mixed forms ---------------------------------------------


def test_since_with_offset_is_normalised(tmp_path: Path) -> None:
    db = write_store(
        tmp_path / "store.sqlite",
        [row("claude", "s1", 0, "user", "deploy the fan", "2026-11-16T03:00:00Z")],
    )
    source = SqliteStoreSource(db)
    # 10:00+08:00 == 02:00Z, so the 03:00Z turn is after it.
    assert [s.session_id for s in source.sessions(since="2026-11-16T02:00:00Z")] == ["s1"]
    assert [s.session_id for s in source.sessions(since="2026-11-16T10:00:00+08:00")] == ["s1"]
    assert [t.session_id for t in source.search("fan", since="2026-11-16T10:00:00+08:00")] == ["s1"]
    # 12:00+08:00 == 04:00Z: after the turn.
    assert source.sessions(since="2026-11-16T12:00:00+08:00") == []
    assert source.search("fan", since="2026-11-16T12:00:00+08:00") == []


def _mixed_store(tmp_path: Path) -> Path:
    return write_store(
        tmp_path / "store.sqlite",
        [
            # Rows an older ingest stored with offsets / fractions.
            row("claude", "a", 0, "user", "alpha topic", "2025-03-01T08:00:00+08:00"),  # 00:00Z
            row("claude", "b", 0, "user", "bravo topic", "2025-03-01T03:00:00-05:00"),  # 08:00Z
            row("claude", "c", 0, "user", "charlie topic", "2025-03-01T08:00:00.123Z"),
        ],
    )


def test_since_and_ordering_with_stored_offset_rows(tmp_path: Path) -> None:
    source = SqliteStoreSource(_mixed_store(tmp_path))
    hits = source.search("topic", since="2025-03-01T05:00:00Z")
    assert [t.session_id for t in hits] == ["c", "b"]
    assert [t.session_id for t in source.search("topic")] == ["c", "b", "a"]
    sessions = source.sessions()
    assert [s.session_id for s in sessions] == ["c", "b", "a"]
    assert {s.session_id: s.updated_at for s in sessions} == {
        "a": "2025-03-01T00:00:00Z",
        "b": "2025-03-01T08:00:00Z",
        "c": "2025-03-01T08:00:00.123Z",
    }
    assert [s.session_id for s in source.sessions(since="2025-03-01T13:00:00+05:00")] == [
        "c",
        "b",
    ]
    assert source.turns("claude", "a")[0].ts == "2025-03-01T00:00:00Z"


def test_session_bounds_use_utc_not_text(tmp_path: Path) -> None:
    db = write_store(
        tmp_path / "store.sqlite",
        [
            row("claude", "s1", 0, "user", "first words here", "2025-03-01T09:00:00+08:00"),
            row("claude", "s1", 1, "assistant", "second words", "2025-03-01T02:00:00Z"),
        ],
    )
    info = SqliteStoreSource(db).session("claude", "s1")
    assert info is not None
    assert info.started_at == "2025-03-01T01:00:00Z"
    assert info.updated_at == "2025-03-01T02:00:00Z"


def test_ts_key_orders_fractions_and_offsets() -> None:
    values = [
        "2025-03-01T08:00:00Z",
        "2025-03-01T16:00:00.250+08:00",
        "2025-03-01T08:00:00.5Z",
        "2025-03-01T08:00:01Z",
    ]
    keys = [ts_key(value) for value in values]
    assert keys == sorted(keys, key=str)
    assert ts_display(ts_key("2025-03-01T16:00:00.250+08:00")) == "2025-03-01T08:00:00.250Z"
    assert ts_key(None) is None
    assert ts_key("2025-03-01") == "2025-03-01"  # naive dates pass through


def test_worker_cursor_comparison_uses_utc_key() -> None:
    info = SessionInfo("claude", "s1", 1, 0, 0, updated_at="2025-03-01T00:30:00Z")
    # The key's text says 08:00 but it is 00:00Z: info (00:30Z) is newer, so not after it.
    assert not ConversationWorker._comes_after(info, ("2025-03-01T08:00:00+08:00", "x", "y"))
    assert ConversationWorker._comes_after(info, ("2025-03-01T09:00:00+08:00", "x", "y"))


# -- worker: sessions without timestamps ------------------------------------------

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _no_ts_worker(tmp_path: Path, db: Path, now: list[datetime], idle: float) -> ConversationWorker:
    return ConversationWorker(
        SqliteStoreSource(db),
        WorkState(tmp_path / "state" / "w.sqlite"),
        backend=ExtractiveBackend(),
        policy=SharePolicy(enabled=True),
        budgets=Budgets(idle_minutes=idle),
        now=lambda: now[0],
    )


def _no_ts_rows(extra: int = 0) -> list[dict[str, Any]]:
    rows = [
        row("claude", "active-with-ts", 0, "user", "still typing here", "2026-11-16T11:59:00Z"),
        row("grok", "active-no-ts", 0, "user", "still typing here", ""),
        row("grok", "active-no-ts", 1, "assistant", "working on it", ""),
    ]
    for index in range(extra):
        rows.append(row("grok", "active-no-ts", 2 + index, "user", "more words here", ""))
    return rows


def test_session_without_timestamps_is_not_idle_on_first_sight(tmp_path: Path) -> None:
    db = write_store(tmp_path / "store.sqlite", _no_ts_rows())
    now = [NOW]
    report = _no_ts_worker(tmp_path, db, now, 30).run()
    assert report.sessions_queued == 0
    assert report.sessions_waiting_idle == 2


def test_session_without_timestamps_is_idle_after_unchanged_window(tmp_path: Path) -> None:
    db = write_store(tmp_path / "store.sqlite", _no_ts_rows())
    now = [NOW]
    _no_ts_worker(tmp_path, db, now, 30).run()
    now[0] = NOW + timedelta(minutes=10)
    assert _no_ts_worker(tmp_path, db, now, 30).run().sessions_queued == 0
    # It grew: the unchanged-window restarts from this sighting.
    write_store(db, _no_ts_rows(extra=1))
    now[0] = NOW + timedelta(minutes=35)
    report = _no_ts_worker(tmp_path, db, now, 30).run()
    assert report.sessions_queued == 1  # only the stamped session (idle since 11:59Z)
    assert report.sessions_waiting_idle == 1
    now[0] = NOW + timedelta(minutes=66)
    report = _no_ts_worker(tmp_path, db, now, 30).run()
    assert report.sessions_queued == 1  # the unstamped one, unchanged for 31 minutes
    assert report.sessions_waiting_idle == 0


def test_session_without_timestamps_with_zero_idle_runs_now(tmp_path: Path) -> None:
    db = write_store(tmp_path / "store.sqlite", _no_ts_rows())
    now = [NOW]
    report = _no_ts_worker(tmp_path, db, now, 0).run()
    assert report.sessions_queued == 2
    assert report.sessions_waiting_idle == 0


def test_ts_key_normalises_nanoseconds_and_missing_seconds() -> None:
    assert ts_key("2026-01-01T00:00:00.123456789Z") == "2026-01-01T00:00:00.123456Z"
    assert ts_key("2026-01-01T00:00Z") == "2026-01-01T00:00:00.000000Z"
    assert ts_key("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00.000000Z"


def test_goal_text_strips_ide_and_hook_blocks() -> None:
    ide = "<ide_opened_file>The user opened a.py in the IDE.</ide_opened_file>"
    hook = "<user-prompt-submit-hook>extra context here</user-prompt-submit-hook>"
    assert goal_text([f"{ide}\n{hook}", f"{ide}\nFix the login bug"]) == "Fix the login bug"


def test_unclosed_ide_tags_in_prose_stay() -> None:
    from memfw.history.injected import strip_structural

    prose = "what do <ide_selection> tags do, and <user-prompt-submit-hook> too?"
    assert strip_structural(prose) == prose
    closed = "<ide_selection>x</ide_selection>\nkeep this"
    assert strip_structural(closed).strip() == "keep this"
    # old tags keep their unclosed behaviour: stripped to the end
    assert strip_structural("hi\n\n<system-reminder>never closed").strip() == "hi"
