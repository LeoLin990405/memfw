"""The worker's session scan is fair and resumable (no newest-first starvation)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.conversations import SharePolicy
from memfw.conversations.backends import ExtractiveBackend
from memfw.conversations.store import SessionInfo, Turn
from memfw.conversations.worker import Budgets, ConversationWorker, RunReport, WorkState

NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)


class FakeSource:
    """Synthetic sessions, newest first, like the real store."""

    def __init__(self, count: int) -> None:
        self.infos: list[SessionInfo] = []
        for index in range(count):
            self.add(f"s-{index:04d}", NOW - timedelta(days=2, minutes=count - index))

    def add(self, session_id: str, updated: datetime, app: str = "codex") -> None:
        self.infos.append(
            SessionInfo(
                app=app,
                session_id=session_id,
                turns=1,
                first_offset=0,
                last_offset=0,
                updated_at=updated.isoformat(),
            )
        )
        self.sort()

    def sort(self) -> None:
        # The store's order: updated_at DESC, then app, then session id.
        self.infos.sort(key=lambda info: (info.app, info.session_id))
        self.infos.sort(key=lambda info: info.updated_at, reverse=True)

    def available(self) -> bool:
        return True

    def sessions(
        self,
        *,
        apps: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        visible = [
            info
            for info in self.infos
            if info.app not in exclude and (since is None or info.updated_at >= since)
        ]
        return visible[offset : offset + limit]

    def turns(self, app: str, session_id: str, *, start: int = 0, limit: int = 200) -> list[Turn]:
        return []


def _worker(
    tmp_path: Path, source: Any, *, now: datetime = NOW, **budgets: Any
) -> ConversationWorker:
    return ConversationWorker(
        source,
        WorkState(tmp_path / "state" / "work.sqlite"),
        backend=ExtractiveBackend(),
        policy=SharePolicy.from_config({}),
        budgets=Budgets(**budgets),
        now=lambda: now,
    )


def _planned(tmp_path: Path) -> set[str]:
    with sqlite3.connect(tmp_path / "state" / "work.sqlite") as connection:
        return {row[0] for row in connection.execute("SELECT session_id FROM session_state")}


def _scan(worker: ConversationWorker) -> RunReport:
    report = RunReport()
    worker._scan(report)
    return report


def test_backlog_larger_than_max_scan_is_eventually_fully_planned(tmp_path: Path) -> None:
    source = FakeSource(50)
    for _ in range(8):
        # A fresh worker each time: the cursor lives in the state DB.
        _scan(_worker(tmp_path, source, max_scan=12, max_sessions=100))
    assert _planned(tmp_path) == {info.session_id for info in source.infos}


def test_session_budget_stop_resumes_where_it_stopped(tmp_path: Path) -> None:
    source = FakeSource(40)
    first = _scan(_worker(tmp_path, source, max_scan=40, max_sessions=15))
    assert first.sessions_queued == 15
    planned = _planned(tmp_path)
    for _ in range(3):
        _scan(_worker(tmp_path, source, max_scan=40, max_sessions=15))
    assert _planned(tmp_path) == {info.session_id for info in source.infos}
    assert len(planned) == 15


def test_cursor_wraps_to_the_top(tmp_path: Path) -> None:
    source = FakeSource(30)
    reports = [_scan(_worker(tmp_path, source, max_scan=12, max_sessions=100)) for _ in range(4)]
    assert any(report.scan_wrapped for report in reports)
    assert reports[0].scan_cursor_start == 3  # head window is max_scan // 4
    assert reports[0].scan_cursor_end == 12
    assert reports[1].scan_cursor_start == 12


def test_fresh_sessions_are_planned_in_the_head_window(tmp_path: Path) -> None:
    source = FakeSource(60)
    _scan(_worker(tmp_path, source, max_scan=20, max_sessions=100))
    _scan(_worker(tmp_path, source, max_scan=20, max_sessions=100))
    source.add("s-fresh", NOW - timedelta(hours=3))
    _scan(_worker(tmp_path, source, max_scan=20, max_sessions=100))
    assert "s-fresh" in _planned(tmp_path)


def test_no_session_is_counted_twice_in_one_run(tmp_path: Path) -> None:
    source = FakeSource(5)
    report = _scan(_worker(tmp_path, source, max_scan=40, max_sessions=100))
    assert report.sessions_scanned == 5
    assert report.sessions_queued == 5


def test_older_state_db_without_cursor_table_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "state" / "work.sqlite"
    path.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    path.chmod(0o600)
    report = _scan(_worker(tmp_path, FakeSource(4), max_scan=8, max_sessions=10))
    assert report.sessions_queued == 4
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        version = connection.execute("SELECT value FROM meta").fetchone()[0]
    assert "scan_cursor" in tables
    assert version == "1"  # additive: older readers see the same schema version


def test_report_keeps_existing_keys(tmp_path: Path) -> None:
    report = _worker(tmp_path, FakeSource(3), max_scan=8).run()
    data = report.as_dict()
    for key in ("sessions_scanned", "sessions_queued", "sessions_waiting_idle", "jobs"):
        assert key in data
    assert {"scan_cursor_start", "scan_cursor_end", "scan_wrapped"} <= data.keys()


def test_busy_head_window_does_not_stall_the_cursor(tmp_path: Path) -> None:
    # Hourly runs. Before each one, as many fresh (idle) sessions arrive at the
    # top as max_sessions: the old scan spent every run on them and never moved
    # its cursor, and offsets shift by the arrivals, so an offset-only cursor
    # never reaches the old backlog either.
    source = FakeSource(40)
    old = {info.session_id for info in source.infos}
    for run in range(40):
        clock = NOW + timedelta(hours=run + 1)
        for extra in range(4):
            source.add(f"n-{run:02d}-{extra}", clock - timedelta(minutes=45, seconds=extra))
        _scan(_worker(tmp_path, source, now=clock, max_scan=40, max_sessions=4))
    assert old <= _planned(tmp_path)


def test_max_sessions_one_still_scans_from_the_top(tmp_path: Path) -> None:
    source = FakeSource(3)
    for _ in range(3):
        assert _scan(_worker(tmp_path, source, max_scan=2000, max_sessions=1)).sessions_queued == 1
    assert _planned(tmp_path) == {info.session_id for info in source.infos}


def _touch_busy(source: FakeSource, now: datetime) -> None:
    source.infos = [
        replace(info, updated_at=(now - timedelta(minutes=1)).isoformat())
        if info.session_id.startswith("busy")
        else info
        for info in source.infos
    ]
    source.sort()


@pytest.mark.parametrize("busy", [0, 3, 10])
def test_busy_sessions_above_the_cursor_do_not_cause_skips(tmp_path: Path, busy: int) -> None:
    # Review scenario: 200 backlog sessions, active sessions updated every run.
    # An offset cursor shifted by "sessions updated since" overshot every run and
    # left the same 33 sessions unplanned for ever (busy=3).
    source = FakeSource(200)
    for index in range(busy):
        source.add(f"busy-{index}", NOW - timedelta(days=1))
    backlog = {info.session_id for info in source.infos if info.session_id.startswith("s-")}
    now = NOW
    # head window 5, so 15 cursor slots per run: 200 sessions need 14 runs.
    for _ in range(16):
        now += timedelta(minutes=10)
        _touch_busy(source, now)
        _scan(_worker(tmp_path, source, now=now, max_scan=20, max_sessions=100))
    assert backlog <= _planned(tmp_path)


def test_cursor_walk_visits_each_session_once_per_pass(tmp_path: Path) -> None:
    source = FakeSource(60)
    visited: list[str] = []
    original = source.sessions

    def recording(**kwargs: Any) -> list[SessionInfo]:
        page = original(**kwargs)
        if kwargs.get("limit", 0) > 1:  # not a keyset probe
            visited.extend(info.session_id for info in page)
        return page

    source.sessions = recording  # type: ignore[method-assign]
    now = NOW
    for _ in range(4):  # 4 runs x 16 slots past the head window cover one pass
        now += timedelta(minutes=10)
        _touch_busy(source, now)
        _scan(_worker(tmp_path, source, now=now, max_scan=20, max_sessions=100))
    head = {f"s-{index:04d}" for index in range(55, 60)}  # the newest five
    beyond = [item for item in visited if item not in head]
    assert len(beyond) == len(set(beyond)) == 55


def test_keyset_order_matches_the_real_store(tmp_path: Path) -> None:
    from conversation_fixtures import turn, write_store

    from memfw.conversations.adapter import SqliteStoreSource

    store = tmp_path / "data" / "conversations.sqlite"
    stamp = "2026-05-01T10:00:00Z"
    rows = [
        turn(app, f"s-{index}", 0, "user", "Task.", stamp)
        for app in ("codex", "claude", "cursor")
        for index in range(4)
    ]
    write_store(store, rows)
    source = SqliteStoreSource(store)
    infos = source.sessions(limit=50)
    worker = _worker(tmp_path, source, max_scan=40, max_sessions=4)
    for index, info in enumerate(infos):
        key = (info.updated_at, info.app, info.session_id)
        assert worker._keyset_offset((), key) == index + 1


def test_cursor_table_from_the_offset_cursor_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "state" / "work.sqlite"
    path.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE scan_cursor (name TEXT PRIMARY KEY, position INTEGER NOT NULL"
            " DEFAULT 0, passes INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL"
            " DEFAULT '')"
        )
        connection.execute("INSERT INTO scan_cursor VALUES ('sessions', 7, 1, '')")
    path.chmod(0o600)
    report = _scan(_worker(tmp_path, FakeSource(4), max_scan=8, max_sessions=10))
    assert report.sessions_queued == 4
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_cursor)")}
    assert {"last_updated_at", "last_app", "last_session_id"} <= columns
