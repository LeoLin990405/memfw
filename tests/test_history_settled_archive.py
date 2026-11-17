"""M3e-a: settled ingest (``history ingest --settled-minutes``) and archive state.

Synthetic transcripts in temp dirs only; ``HOME`` is a temp dir for every test.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import SharePolicy, open_share
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import ExtractiveBackend
from memfw.conversations.share import ConversationShare
from memfw.conversations.store import ConversationError
from memfw.conversations.worker import Budgets, ConversationWorker, WorkState, fingerprint
from memfw.history.conversations import ConversationStore, HistoryError, OptOut
from memfw.history.sources.base import ConversationRecord, SessionLocator, StoreRoot
from memfw.history.sources.claude import READER as CLAUDE
from tests.conversation_fixtures import (
    isolated_home,  # noqa: F401  (fixture)
    make_vault,
    standard_rows,
    write_store,
)

NOW = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
REAL_DATA = Path(os.path.expanduser("~")) / ".local" / "share" / "memfw"


@pytest.fixture(autouse=True)
def real_home_untouched(isolated_home: Path) -> Any:  # noqa: F811
    def state() -> Any:
        try:
            return sorted((entry.name, entry.stat().st_mtime_ns) for entry in REAL_DATA.iterdir())
        except OSError:
            return None

    before = state()
    yield
    assert Path.home() == isolated_home
    assert state() == before


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(session: str, uuid: str, role: str, text: str, ts: datetime | None, cwd: str) -> str:
    raw: dict[str, Any] = {
        "sessionId": session,
        "uuid": uuid,
        "cwd": cwd,
        "message": {"role": role, "content": text},
    }
    if ts is not None:
        raw["timestamp"] = _stamp(ts)
    return json.dumps(raw) + "\n"


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "projects"
        (self.root / "demo").mkdir(parents=True)
        self.db = tmp_path / "data" / "conversations.sqlite"
        self.cwd = str(tmp_path / "work")
        self.store = ConversationStore(self.db)

    def path(self, session: str) -> Path:
        return self.root / "demo" / f"{session}.jsonl"

    def append(self, session: str, role: str, text: str, ts: datetime | None) -> None:
        path = self.path(session)
        count = len(path.read_text().splitlines()) if path.exists() else 0
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_line(session, f"u{count}", role, text, ts, self.cwd))
        if ts is not None:
            # The writer's mtime matches the line it just wrote.
            os.utime(path, (ts.timestamp(), ts.timestamp()))

    def touch(self, session: str, when: datetime) -> None:
        os.utime(self.path(session), (when.timestamp(), when.timestamp()))

    def ingest(self, settled: float | None = 30, now: datetime = NOW) -> dict[str, Any]:
        if settled is None:
            return self.store.ingest(CLAUDE, [StoreRoot(self.root)]).as_dict()
        return self.store.ingest(
            CLAUDE, [StoreRoot(self.root)], settled_minutes=settled, now=now
        ).as_dict()

    def archive(self, session: str) -> dict[str, Any]:
        return self.store.archive_state("claude", session)

    def archive_row(self, session: str) -> tuple[str, str] | None:
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT archived_at, archived_fingerprint FROM conversation_archive"
                " WHERE agent = 'claude' AND session_id = ?",
                (session,),
            ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


# -- settled ingest ---------------------------------------------------------------


def test_active_session_stays_out_until_idle_then_is_archived(fx: Fixture) -> None:
    fx.append("s1", "user", "start the task", NOW - timedelta(minutes=10))
    fx.append("s1", "assistant", "working on it", NOW - timedelta(minutes=5))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 1 and counts["sessions_read"] == 0
    assert counts["sessions_archived"] == 0 and counts["records_new"] == 0
    assert fx.store.turns("claude", "s1") == []
    # The same file, 40 minutes later: settled, ingested in its final form, archived.
    later = NOW + timedelta(minutes=40)
    counts = fx.ingest(30, now=later)
    assert counts["sessions_unsettled"] == 0 and counts["records_new"] == 2
    assert counts["sessions_archived"] == 1
    state = fx.archive("s1")
    assert state == {"archived": True, "archived_at": _stamp(later)}
    # Nothing changed: unchanged, not archived again, archived_at kept.
    counts = fx.ingest(30, now=later + timedelta(minutes=15))
    assert counts["sessions_unchanged"] == 1 and counts["sessions_archived"] == 0
    assert fx.archive("s1")["archived_at"] == _stamp(later)


def test_resumed_session_is_unarchived_then_archived_again(fx: Fixture) -> None:
    fx.append("s1", "user", "first question", NOW - timedelta(hours=2))
    fx.ingest(30)
    first = fx.archive_row("s1")
    assert first is not None and fx.archive("s1")["archived"]
    # The user resumes the conversation: active again, its new turns are held back
    # and the session is no longer archived.
    fx.append("s1", "user", "one more thing", NOW - timedelta(minutes=2))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 1
    assert len(fx.store.turns("claude", "s1")) == 1
    assert fx.archive("s1") == {"archived": False, "archived_at": ""}
    # Idle again: the new turn is stored and the session re-archived in its new shape.
    later = NOW + timedelta(minutes=45)
    counts = fx.ingest(30, now=later)
    assert counts["records_new"] == 1 and counts["sessions_archived"] == 1
    second = fx.archive_row("s1")
    assert second is not None and second[1] != first[1]
    assert fx.archive("s1") == {"archived": True, "archived_at": _stamp(later)}


def test_plain_ingest_of_new_turns_makes_the_session_unarchived(fx: Fixture) -> None:
    fx.append("s1", "user", "first question", NOW - timedelta(hours=2))
    fx.ingest(30)
    assert fx.archive("s1")["archived"]
    fx.append("s1", "assistant", "an answer", NOW - timedelta(minutes=1))
    counts = fx.ingest(None)
    assert "sessions_archived" not in counts and counts["records_new"] == 1
    # The stored fingerprint no longer matches: not archived until the next settled run.
    assert fx.archive("s1")["archived"] is False
    counts = fx.ingest(30, now=NOW + timedelta(minutes=31))
    assert counts["sessions_unchanged"] == 1 and counts["sessions_archived"] == 1
    assert fx.archive("s1")["archived"] is True


def test_session_without_timestamps_falls_back_to_file_mtime(fx: Fixture) -> None:
    fx.append("s1", "user", "no stamps at all", None)
    recent = (NOW - timedelta(minutes=3)).timestamp()
    os.utime(fx.path("s1"), (recent, recent))
    assert fx.ingest(30)["sessions_unsettled"] == 1
    assert fx.store.turns("claude", "s1") == []
    old = (NOW - timedelta(minutes=50)).timestamp()
    os.utime(fx.path("s1"), (old, old))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 0 and counts["records_new"] == 1
    assert fx.archive("s1")["archived"] is True


def test_fresh_mtime_with_old_timestamps_is_still_active(fx: Fixture) -> None:
    # A reader that stamps a reply with the prompt time (or skips lines without a
    # timestamp): the file is still being written although its stamps are old.
    fx.append("s1", "user", "a long single prompt", NOW - timedelta(hours=1))
    fx.touch("s1", NOW - timedelta(minutes=2))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 1 and fx.store.turns("claude", "s1") == []
    # Already stored and archived, then the file keeps moving: archive row goes.
    fx.touch("s1", NOW - timedelta(hours=1))
    assert fx.ingest(30)["sessions_archived"] == 1
    with fx.path("s1").open("a", encoding="utf-8") as handle:
        handle.write("{}\n")  # a line without a timestamp (not a turn)
    fx.touch("s1", NOW - timedelta(minutes=1))
    assert fx.ingest(30)["sessions_unsettled"] == 1
    assert fx.archive("s1")["archived"] is False


class _SharedDbReader:
    """Sessions inside one database file (``db#id`` keys), like opencode or cursor_ide."""

    agent = "fake"
    default_roots: tuple[str, ...] = ()
    parser_version = "1"
    append_only = False

    def __init__(self, stamp: datetime | None) -> None:
        self.stamp = stamp

    def sessions(self, root: Path) -> Any:
        yield SessionLocator(self.agent, root / "store.db", "store.db#s1", "s1")

    def read(self, locator: SessionLocator, *, after: int = -1) -> Any:
        ts = _stamp(self.stamp) if self.stamp else ""
        yield ConversationRecord("fake", "s1", 0, 0, "user", "hello", timestamp=ts)


def test_shared_database_session_uses_its_timestamps_not_the_mtime(tmp_path: Path) -> None:
    (tmp_path / "store.db").write_text("db")
    fresh = NOW - timedelta(minutes=1)
    os.utime(tmp_path / "store.db", (fresh.timestamp(), fresh.timestamp()))
    store = ConversationStore(tmp_path / "conv.sqlite")
    # Another session keeps the shared file's mtime fresh; this one is quiet.
    counts = store.ingest(
        _SharedDbReader(NOW - timedelta(hours=2)), [tmp_path], settled_minutes=30, now=NOW
    )
    assert counts.extra["sessions_unsettled"] == 0 and counts.extra["sessions_archived"] == 1
    # Without any timestamp the mtime is the fallback.
    other = ConversationStore(tmp_path / "conv2.sqlite")
    counts = other.ingest(_SharedDbReader(None), [tmp_path], settled_minutes=30, now=NOW)
    assert counts.extra["sessions_unsettled"] == 1


def test_future_timestamps_fall_back_to_mtime(fx: Fixture) -> None:
    fx.append("s1", "user", "clock was wrong", NOW + timedelta(days=30))
    fx.touch("s1", NOW - timedelta(hours=1))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 0 and counts["sessions_archived"] == 1
    # Stored, unchanged and plainly ingested: the stored future stamp is ignored too.
    fx2 = Fixture(fx.root.parent / "second")
    fx2.append("s2", "user", "skewed", NOW + timedelta(days=30))
    fx2.touch("s2", NOW - timedelta(hours=1))
    fx2.ingest(None)
    assert fx2.ingest(30)["sessions_archived"] == 1
    # A small skew (within five minutes) still counts as fresh activity.
    fx3 = Fixture(fx.root.parent / "third")
    fx3.append("s3", "user", "slightly ahead", NOW + timedelta(minutes=2))
    fx3.touch("s3", NOW - timedelta(hours=1))
    assert fx3.ingest(30)["sessions_unsettled"] == 1


def test_one_active_session_does_not_hold_back_another(fx: Fixture) -> None:
    fx.append("quiet", "user", "done long ago", NOW - timedelta(hours=5))
    fx.append("busy", "user", "still typing", NOW - timedelta(minutes=1))
    counts = fx.ingest(30)
    assert counts["sessions_unsettled"] == 1 and counts["sessions_archived"] == 1
    assert fx.archive("quiet")["archived"] and fx.store.turns("claude", "busy") == []


def test_cwd_optout_still_drops_an_unsettled_session(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.append("s1", "user", "private work", NOW - timedelta(hours=2))
    fx.ingest(30)
    assert fx.store.turns("claude", "s1")
    # The cwd is opted out while the session is active again: its rows still go.
    Path(fx.cwd).mkdir(parents=True, exist_ok=True)
    (Path(fx.cwd) / ".memfw-no-history").write_text("")
    fx.append("s1", "user", "more private work", NOW - timedelta(minutes=1))
    counts = fx.ingest(30)
    assert counts["sessions_opted_out"] == 1 and counts["records_dropped"] == 1
    assert fx.store.turns("claude", "s1") == [] and fx.archive_row("s1") is None


def test_agent_optout_forgets_archive_rows(fx: Fixture) -> None:
    fx.append("s1", "user", "hello", NOW - timedelta(hours=2))
    fx.ingest(30)
    assert fx.archive_row("s1") is not None
    store = ConversationStore(fx.db, optout=OptOut(agents=frozenset({"claude"})))
    store.ingest(CLAUDE, [StoreRoot(fx.root)], settled_minutes=30, now=NOW)
    assert fx.archive_row("s1") is None


def _meta(db: Path, prefix: str) -> list[str]:
    with sqlite3.connect(db) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT key FROM conversation_meta WHERE key LIKE ?", (prefix + "%",)
            )
        ]


def test_active_file_does_not_keep_the_relabel_pass_open(fx: Fixture) -> None:
    for index in range(3):
        fx.append(f"quiet{index}", "user", "done", NOW - timedelta(hours=3))
    fx.append("live", "user", "always typing", NOW - timedelta(minutes=1))
    first = fx.ingest(30)
    assert first["relabelled"] is True and first["sessions_unsettled"] == 1
    assert _meta(fx.db, "labels_version:claude")  # the pass is recorded
    # Later runs: the settled files take the unchanged fast path, only "live" is read.
    for minutes in (15, 30, 45):
        now = NOW + timedelta(minutes=minutes)
        fx.append("live", "user", f"still typing {minutes}", now - timedelta(minutes=1))
        counts = fx.ingest(30, now=now)
        assert counts["relabelled"] is False
        assert counts["sessions_unchanged"] == 3 and counts["sessions_unsettled"] == 1


def test_locator_held_back_in_a_forced_pass_is_reread_once_settled(fx: Fixture) -> None:
    fx.append("live", "user", "typing", NOW - timedelta(minutes=1))
    fx.ingest(None)  # stored with a cursor by a plain ingest
    with sqlite3.connect(fx.db) as connection:
        connection.execute("DELETE FROM conversation_meta WHERE key = 'labels_version:claude'")
    counts = fx.ingest(30)  # a re-label pass while "live" is active
    assert counts["relabelled"] is True and counts["sessions_unsettled"] == 1
    assert _meta(fx.db, "labels_version:claude") and len(_meta(fx.db, "force:claude:")) == 1
    # Quiet now and the file unchanged: still re-read in full once, then the marker goes.
    later = fx.ingest(30, now=NOW + timedelta(hours=1))
    assert later["sessions_read"] == 1 and later["sessions_unchanged"] == 0
    assert _meta(fx.db, "force:claude:") == []
    assert fx.ingest(30, now=NOW + timedelta(hours=2))["sessions_unchanged"] == 1


class _ToggleReader(_SharedDbReader):
    """A shared-database reader that can read nothing (locked, mid-rewrite)."""

    def __init__(self, stamp: datetime | None) -> None:
        super().__init__(stamp)
        self.empty = False

    def read(self, locator: SessionLocator, *, after: int = -1) -> Any:
        if self.empty:
            return iter(())
        return super().read(locator, after=after)


def test_force_marker_survives_a_busy_read_with_zero_rows(tmp_path: Path) -> None:
    (tmp_path / "store.db").write_text("db")
    quiet = (NOW - timedelta(hours=2)).timestamp()
    os.utime(tmp_path / "store.db", (quiet, quiet))
    db = tmp_path / "conv.sqlite"
    store = ConversationStore(db)
    reader = _ToggleReader(NOW - timedelta(minutes=1))
    store.ingest(reader, [tmp_path])  # stored with a cursor by a plain ingest
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM conversation_meta WHERE key = 'labels_version:fake'")
    counts = store.ingest(reader, [tmp_path], settled_minutes=30, now=NOW)
    assert counts.extra["relabelled"] is True and counts.extra["sessions_unsettled"] == 1
    assert len(_meta(db, "force:fake:")) == 1
    # Settled now, but the forced full re-read finds nothing readable: busy, and
    # the owed re-read is kept rather than dropped.
    reader.empty = True
    later = NOW + timedelta(hours=1)
    counts = store.ingest(reader, [tmp_path], settled_minutes=30, now=later)
    assert counts.sessions_busy == 1
    assert len(_meta(db, "force:fake:")) == 1
    # Readable again, file unchanged: still re-read in full once, then the marker goes.
    reader.empty = False
    counts = store.ingest(reader, [tmp_path], settled_minutes=30, now=later)
    assert counts.sessions_read == 1 and counts.sessions_unchanged == 0
    assert _meta(db, "force:fake:") == []


def test_optout_forced_pass_on_a_locked_locator_keeps_the_reread(tmp_path: Path) -> None:
    (tmp_path / "store.db").write_text("db")
    db = tmp_path / "conv.sqlite"
    reader = _ToggleReader(NOW - timedelta(hours=2))
    ConversationStore(db).ingest(reader, [tmp_path])  # stored, opt-out rules recorded
    # The cwd opt-out rules change (a forced full pass), but the store is locked.
    changed = ConversationStore(db, optout=OptOut(cwd_globs=("/elsewhere/*",)))
    reader.empty = True
    assert changed.ingest(reader, [tmp_path]).sessions_busy == 1
    assert len(_meta(db, "force:fake:")) == 1
    # Readable again, unchanged file, no longer a forced pass: still re-read once.
    reader.empty = False
    counts = changed.ingest(reader, [tmp_path])
    assert counts.sessions_read == 1 and counts.sessions_unchanged == 0
    assert _meta(db, "force:fake:") == []
    assert changed.ingest(reader, [tmp_path]).sessions_unchanged == 1


def test_future_mtime_without_timestamps_falls_back_to_ctime(fx: Fixture) -> None:
    fx.append("s1", "user", "no stamps, clock ahead", None)
    real_now = datetime.now(UTC)
    ahead = (real_now + timedelta(days=30)).timestamp()
    os.utime(fx.path("s1"), (ahead, ahead))  # the ctime moves to the present
    # Within the settled window of the ctime: still active.
    assert fx.ingest(30, now=real_now)["sessions_unsettled"] == 1
    # Once the ctime is quiet long enough the session settles (the future mtime
    # alone kept it unsettled until the clock caught up).
    counts = fx.ingest(30, now=real_now + timedelta(hours=1))
    assert counts["sessions_unsettled"] == 0 and counts["records_new"] == 1
    assert fx.archive("s1")["archived"] is True


def test_no_ctime_fallback_on_windows(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows st_ctime is the creation time: a future mtime stays untrusted.
    fx.append("s1", "user", "no stamps, clock ahead", None)
    real_now = datetime.now(UTC)
    ahead = (real_now + timedelta(days=30)).timestamp()
    os.utime(fx.path("s1"), (ahead, ahead))
    locator = SessionLocator("claude", fx.path("s1"), "demo/s1.jsonl", "s1")
    store = fx.store
    store._now = real_now + timedelta(hours=1)
    store._settled = store._now - timedelta(minutes=30)
    assert store._is_settled(None, locator) is True
    with monkeypatch.context() as patch:  # undone before the fixtures' teardown
        patch.setattr(os, "name", "nt")
        assert store._is_settled(None, locator) is False


def test_unstatable_path_is_unknown_not_an_error(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conv.sqlite")
    store._now = NOW
    store._settled = NOW - timedelta(minutes=30)
    locator = SessionLocator("fake", tmp_path / "bad\0name", "bad#s1", "s1", fingerprint="m1")
    assert store._is_settled(None, locator) is False
    assert store._is_settled(NOW - timedelta(hours=1), locator) is True


def test_resume_resets_the_settled_marker(fx: Fixture) -> None:
    fx.append("s1", "user", "first", NOW - timedelta(hours=2))
    fx.ingest(30)

    def marker() -> str:
        with sqlite3.connect(fx.db) as connection:
            return str(
                connection.execute(
                    "SELECT settled_fingerprint FROM conversation_cursors"
                ).fetchone()[0]
            )

    assert marker() != ""
    fx.append("s1", "user", "again", NOW - timedelta(minutes=1))
    assert fx.ingest(30)["sessions_unsettled"] == 1
    assert marker() == ""


def test_resumed_turns_in_another_locator_unarchive_the_session(tmp_path: Path) -> None:
    home = Fixture(tmp_path / "a")
    mirror = tmp_path / "b" / "projects"
    home.append("s1", "user", "first", NOW - timedelta(hours=2))
    store = home.store
    store.ingest(CLAUDE, [StoreRoot(home.root)], settled_minutes=30, now=NOW)
    assert home.archive("s1")["archived"]
    # The same session continues in a copy under another root (a Worker home).
    (mirror / "demo").mkdir(parents=True)
    copy = mirror / "demo" / "s1.jsonl"
    copy.write_text(home.path("s1").read_text())
    with copy.open("a", encoding="utf-8") as handle:
        handle.write(_line("s1", "u9", "user", "resumed here", NOW - timedelta(minutes=1), "/w"))
    counts = store.ingest(
        CLAUDE, [StoreRoot(home.root), StoreRoot(mirror)], settled_minutes=30, now=NOW
    )
    assert counts.extra["sessions_unsettled"] == 1
    assert home.archive("s1")["archived"] is False


def test_settled_minutes_is_validated(fx: Fixture) -> None:
    with pytest.raises(HistoryError):
        fx.ingest(-1)


# -- default behaviour and migration ----------------------------------------------


def test_default_ingest_reports_no_new_counts(fx: Fixture) -> None:
    fx.append("s1", "user", "hello", NOW - timedelta(minutes=1))
    counts = fx.ingest(None)
    assert not {"sessions_unsettled", "sessions_archived"} & counts.keys()
    assert counts["records_new"] == 1
    # A plain ingest never archives.
    assert fx.archive_row("s1") is None and fx.archive("s1")["archived"] is False


def test_store_written_before_archive_state_is_migrated(fx: Fixture) -> None:
    fx.append("s1", "user", "old store", NOW - timedelta(hours=3))
    fx.ingest(None)
    with sqlite3.connect(fx.db) as connection:
        # Shape of a store written before M3e-a.
        connection.execute("DROP TABLE conversation_archive")
        connection.execute("ALTER TABLE conversation_cursors DROP COLUMN settled_fingerprint")
    source = SqliteStoreSource(fx.db)
    info = source.session("claude", "s1")
    assert info is not None and source.archive_state([info]) == {("claude", "s1"): False}
    # Reopening adds the table and column without resetting cursors.
    counts = fx.ingest(None)
    assert counts["sessions_unchanged"] == 1 and counts["sessions_read"] == 0
    assert fx.archive("s1")["archived"] is False
    counts = fx.ingest(30)
    assert counts["sessions_unchanged"] == 1 and counts["sessions_archived"] == 1
    assert source.archive_state([info]) == {("claude", "s1"): True}


# -- CLI ----------------------------------------------------------------------------


def _cli(*args: str) -> Any:
    result = CliRunner().invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_history_ingest_cli_flag(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    real_now = datetime.now(UTC)
    fx.append("old", "user", "finished", real_now - timedelta(hours=2))
    fx.append("new", "user", "in progress", real_now - timedelta(minutes=1))
    vault = make_vault(tmp_path / "vault")
    base = ("history", "ingest", "claude", "--root", str(vault), "--db", str(fx.db))
    base += ("--path", str(fx.root))
    out = _cli(*base, "--settled-minutes", "30")["ingested"]["claude"]
    assert out["sessions_unsettled"] == 1 and out["sessions_archived"] == 1
    assert fx.store.turns("claude", "new") == []
    plain = _cli(*base)["ingested"]["claude"]
    assert "sessions_unsettled" not in plain and plain["records_new"] == 1
    bad = CliRunner().invoke(cli_app, [*base, "--settled-minutes", "-5"])
    assert bad.exit_code != 0


# -- query visibility -----------------------------------------------------------------


def _share_setup(tmp_path: Path) -> tuple[Fixture, Path]:
    fx = Fixture(tmp_path)
    fx.append("done", "user", "retry wrapper finished", NOW - timedelta(hours=2))
    fx.append("open", "user", "retry wrapper draft", NOW - timedelta(hours=1))
    fx.ingest(None)
    # Archive only "done": "open" gets a new turn written by a plain ingest afterwards.
    fx.ingest(30, now=NOW)
    fx.append("open", "assistant", "retry wrapper, still going", NOW)
    fx.ingest(None)
    vault = make_vault(tmp_path / "vault", {"conversation_share": {"enabled": True}})
    return fx, vault


def test_share_archive_state_and_filter(tmp_path: Path) -> None:
    fx, vault = _share_setup(tmp_path)
    share = open_share(vault, "operator", store=fx.db)
    plain = share.list_sessions()
    assert all("archived" not in item for item in plain["sessions"])
    listed = share.list_sessions(with_archive_state=True)["sessions"]
    assert {item["session_id"]: item["archived"] for item in listed} == {
        "done": True,
        "open": False,
    }
    only = share.list_sessions(archived_only=True)["sessions"]
    assert [item["session_id"] for item in only] == ["done"] and "archived" not in only[0]

    hits = share.search("retry", with_archive_state=True)["results"]
    assert {(hit["session_id"], hit["archived"]) for hit in hits} == {
        ("done", True),
        ("open", False),
    }
    hits = share.search("retry", archived_only=True)["results"]
    assert {hit["session_id"] for hit in hits} == {"done"}
    assert all("archived" not in hit for hit in share.search("retry")["results"])

    got = share.get("done", app="claude", with_archive_state=True)
    assert got["session"]["archived"] is True
    assert "archived" not in share.get("done", app="claude")["session"]
    with pytest.raises(ConversationError, match="not archived"):
        share.get("open", app="claude", archived_only=True)


def test_store_without_archive_table_reports_not_archived(tmp_path: Path) -> None:
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    vault = make_vault(tmp_path / "vault", {"conversation_share": {"enabled": True}})
    share = open_share(vault, "operator", store=store)
    listed = share.list_sessions(with_archive_state=True)["sessions"]
    assert listed and all(item["archived"] is False for item in listed)
    assert share.list_sessions(archived_only=True)["sessions"] == []


def test_source_without_archive_support_is_refused(tmp_path: Path) -> None:
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())

    class Plain:
        def __init__(self) -> None:
            self.inner = SqliteStoreSource(store)

        def __getattr__(self, name: str) -> Any:
            if name == "archive_state":
                raise AttributeError(name)
            return getattr(self.inner, name)

    share = ConversationShare(Plain(), SharePolicy.from_config(_on()), "operator")
    assert share.list_sessions()["sessions"]  # defaults still work
    with pytest.raises(ConversationError, match="archive state"):
        share.list_sessions(with_archive_state=True)


def _on() -> dict[str, Any]:
    return {"conversation_share": {"enabled": True}}


def test_hidden_app_is_not_revealed_by_archive_filter(tmp_path: Path) -> None:
    fx, _vault = _share_setup(tmp_path)
    vault = make_vault(
        tmp_path / "vault2",
        {"conversation_share": {"enabled": True, "apps": {"claude": {"readable": False}}}},
    )
    share = open_share(vault, "codex", store=fx.db)
    assert share.list_sessions(archived_only=True, with_archive_state=True)["sessions"] == []
    assert share.search("retry", archived_only=True)["results"] == []


def test_conversations_cli_flags(tmp_path: Path) -> None:
    fx, vault = _share_setup(tmp_path)
    common = ("--root", str(vault), "--store", str(fx.db))
    listed = _cli("conversations", "list", *common, "--with-archive-state")["sessions"]
    assert {item["session_id"]: item["archived"] for item in listed} == {
        "done": True,
        "open": False,
    }
    only = _cli("conversations", "list", *common, "--archived-only")["sessions"]
    assert [item["session_id"] for item in only] == ["done"]
    hits = _cli("conversations", "search", "retry", *common, "--archived-only")["results"]
    assert {hit["session_id"] for hit in hits} == {"done"}
    got = _cli("conversations", "get", "done", *common, "--with-archive-state")
    assert got["session"]["archived"] is True
    default = _cli("conversations", "list", *common)["sessions"]
    assert all("archived" not in item for item in default)


# -- the worker agrees with settled ingest --------------------------------------------


def test_worker_processes_an_archived_session_once(tmp_path: Path) -> None:
    fx = Fixture(tmp_path)
    fx.append("s1", "user", "Add a retry wrapper to the uploader.", NOW - timedelta(hours=2))
    fx.append("s1", "assistant", "Done; tests pass.", NOW - timedelta(hours=2, minutes=-1))
    fx.ingest(30)
    clock = {"now": NOW}
    worker = ConversationWorker(
        SqliteStoreSource(fx.db),
        WorkState(tmp_path / "data" / "work.sqlite"),
        backend=ExtractiveBackend(),
        policy=SharePolicy.from_config({}),
        budgets=Budgets(),
        now=lambda: clock["now"],
    )
    first = worker.run()
    assert first.sessions_queued == 1 and first.sessions_waiting_idle == 0
    info = SqliteStoreSource(fx.db).session("claude", "s1")
    assert info is not None
    row = fx.archive_row("s1")
    # The worker's job key and the archive fingerprint are the same value.
    assert row is not None and row[1] == fingerprint(info)
    with sqlite3.connect(tmp_path / "data" / "work.sqlite") as connection:
        queued = connection.execute(
            "SELECT fingerprint FROM session_state WHERE app = 'claude' AND session_id = 's1'"
        ).fetchone()
    assert queued == (row[1],)
    assert worker.run().sessions_queued == 0  # nothing new: never processed twice
    # Resumed and settled again: re-archived and queued exactly once more.
    fx.append("s1", "user", "One more change.", NOW + timedelta(minutes=5))
    clock["now"] = NOW + timedelta(minutes=50)
    fx.ingest(30, now=clock["now"])
    assert worker.run().sessions_queued == 1
    assert worker.run().sessions_queued == 0


def test_worker_cli_settled_ingest_keeps_active_sessions_out(
    tmp_path: Path,
    isolated_home: Path,  # noqa: F811
) -> None:
    projects = isolated_home / ".claude" / "projects" / "demo"
    projects.mkdir(parents=True)
    real_now = datetime.now(UTC)
    (projects / "live.jsonl").write_text(
        _line("live", "a", "user", "still going", real_now - timedelta(minutes=2), "/w")
    )
    (projects / "done.jsonl").write_text(
        _line("done", "a", "user", "all finished", real_now - timedelta(hours=3), "/w")
    )
    done_at = (real_now - timedelta(hours=3)).timestamp()
    os.utime(projects / "done.jsonl", (done_at, done_at))
    vault = make_vault(tmp_path / "vault")
    db = tmp_path / "data" / "conversations.sqlite"
    common = ("conversations", "worker", "--root", str(vault), "--store", str(db))
    common += ("--state", str(tmp_path / "data" / "work.sqlite"))
    report = _cli(*common, "--settled-minutes", "30")
    claude = report["ingest"]["agents"]["claude"]
    assert claude["sessions_unsettled"] == 1 and claude["sessions_archived"] == 1
    assert report["sessions_queued"] == 1  # only the settled session
    store = ConversationStore(db)
    assert store.turns("claude", "live") == [] and store.archive_state("claude", "done")["archived"]
    plain = _cli(*common)["ingest"]["agents"]["claude"]
    assert "sessions_unsettled" not in plain and plain["records_new"] == 1


# -- opt-in: byte-identical against a base ref ----------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_byte_identical_against_base_ref() -> None:
    from tests.harness import byte_identical

    results = byte_identical.compare(
        "tests.harness.settled_probe:settled_probe",
        [{"unstamped": False}, {"unstamped": True}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, result.diff
