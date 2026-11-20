"""``memfw conversations reredact``: re-redact text already at rest in the store.

Synthetic data only (RFC 5737 addresses, 555-01xx and 13800138000 numbers,
``example.test`` mail, a ``tail0000`` tailnet). ``HOME`` is a temp dir: the
real store is never opened. "Old rules" are simulated by ingesting with the
store module's ``redact`` / ``has_secret`` replaced by no-ops.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import memfw.history.conversations as conversations_module
import memfw.history.reredact as reredact
from memfw.cli import app as cli_app
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.worker import WorkState, run_lock
from memfw.history.conversations import (
    ConversationStore,
    HistoryError,
    store_writer_lock,
)
from memfw.history.sources.base import StoreRoot
from memfw.history.sources.claude import READER as CLAUDE
from tests.conversation_fixtures import isolated_home  # noqa: F401  (fixture)

# Built at runtime so the repository holds no credential-shaped literal.
TOKEN = "gh" + "p_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"
SEEDS = {
    "ip": "192.0.2.7",
    "ip2": "198.51.100.4",
    "ip3": "203.0.113.9",
    "email": "someone@example.test",
    "phone": "13800138000",
    "tailnet": "tail0000",
}
RUNNER = CliRunner()


def _line(session: str, uuid: str, role: str, content: Any, ts: str, cwd: str) -> str:
    return (
        json.dumps(
            {
                "sessionId": session,
                "uuid": uuid,
                "cwd": cwd,
                "timestamp": ts,
                "message": {"role": role, "content": content},
            }
        )
        + "\n"
    )


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "projects"
        (self.root / "demo").mkdir(parents=True)
        self.data = tmp_path / "data"
        self.db = self.data / "conversations.sqlite"
        self.state = self.data / "conversations-work.sqlite"
        self.cwd = f"/work/{SEEDS['ip3']}/demo"
        self.store = ConversationStore(self.db)
        self.count = 0

    @property
    def path(self) -> Path:
        return self.root / "demo" / "s1.jsonl"

    def append(self, role: str, content: Any) -> None:
        self.count += 1
        ts = f"2026-01-05T10:{self.count:02d}:00Z"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_line("s1", f"u{self.count}", role, content, ts, self.cwd))

    def seed(self) -> None:
        self.append("user", f"ssh {SEEDS['ip']} and mail {SEEDS['email']}")
        self.append(
            "assistant",
            [
                {"type": "text", "text": f"call {SEEDS['phone']} or +1 202-555-0142"},
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": f"curl git.{SEEDS['ip2']}.sslip.io"},
                },
            ],
        )
        self.append("user", f"host build-01.{SEEDS['tailnet']}.ts.net is up")
        self.append("assistant", f"the key is {TOKEN}")

    def ingest(self) -> dict[str, Any]:
        return self.store.ingest(CLAUDE, [StoreRoot(self.root)]).as_dict()

    def ingest_old(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        with monkeypatch.context() as patch:
            patch.setattr(conversations_module, "redact", lambda value, **_: value)
            patch.setattr(conversations_module, "has_secret", lambda value: False)
            return self.ingest()

    def rows(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM conversation_turns ORDER BY agent, session_id, offset"
                )
            ]

    def dump(self) -> str:
        return json.dumps(self.rows())

    def cli(self, *args: str) -> Any:
        return RUNNER.invoke(
            cli_app,
            [
                "conversations",
                "reredact",
                "--store",
                str(self.db),
                "--state",
                str(self.state),
                *args,
            ],
        )

    def snapshot(self) -> list[tuple[str, str, int]]:
        return sorted(
            (
                entry.name,
                hashlib.sha256(entry.read_bytes()).hexdigest(),
                entry.stat().st_mtime_ns,
            )
            for entry in self.data.iterdir()
            if not entry.name.endswith(".lock")  # empty lock files a refused run may create
        )


def _plaintext(text: str) -> list[str]:
    return [name for name, value in SEEDS.items() if value in text] + (
        ["token"] if TOKEN in text else []
    )


@pytest.fixture
def fx(tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:  # noqa: F811
    fixture = Fixture(tmp_path)
    fixture.seed()
    counts = fixture.ingest_old(monkeypatch)
    assert counts["records_new"] == 4 and counts["records_withheld"] == 0
    assert set(_plaintext(fixture.dump())) == set(SEEDS) | {"token"}
    return fixture


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


def test_dry_run_writes_nothing_and_prints_no_text(fx: Fixture) -> None:
    before = fx.snapshot()
    result = fx.cli("--json")
    assert result.exit_code == 0, result.output
    assert fx.snapshot() == before
    assert _plaintext(result.output) == []
    report = _json(result)
    assert report["mode"] == "dry_run" and report["status"] == "ok"
    store = report["store"]
    assert store["rows_scanned"] == 4 and store["rows_changed"] == 4
    assert store["rows_withheld"] == 1
    assert {"ip", "email", "phone", "tailnet"} <= set(store["markers"])
    assert {"text", "tool_calls", "cwd"} <= set(store["columns"])
    assert report["worker_state"] == {"present": False, "included": False}
    # Human output: counts only, too.
    plain = fx.cli()
    assert plain.exit_code == 0 and _plaintext(plain.output) == []
    assert fx.snapshot() == before


def test_apply_masks_backs_up_verifies_and_is_idempotent(fx: Fixture) -> None:
    rows_before = fx.rows()
    result = fx.cli("--apply", "--json", "--batch", "2")
    assert result.exit_code == 0, result.output
    assert _plaintext(result.output) == []
    report = _json(result)
    assert report["status"] == "ok"
    assert report["store"]["rows_changed"] == 4
    assert report["store"]["verify"] == {
        "rescan_rows_changed": 0,
        "integrity": "ok",
        "journal_mode": "delete",
        "checkpoint_busy": 0,
    }
    # The values are masked at rest; the withheld turn keeps nothing.
    dump = fx.dump()
    assert _plaintext(dump) == []
    for marker in ("ip", "email", "phone", "tailnet"):
        assert f"[REDACTED:{marker}]" in dump
    rows = fx.rows()
    assert [(r["agent"], r["session_id"], r["offset"]) for r in rows] == [
        (r["agent"], r["session_id"], r["offset"]) for r in rows_before
    ]
    withheld = [r for r in rows if r["withheld"]]
    assert len(withheld) == 1 and withheld[0]["text"] == "" and withheld[0]["tool_calls"] == "[]"
    assert all(r["locator"] == b["locator"] for r, b in zip(rows, rows_before, strict=True))
    assert all(r["digest"] != b["digest"] for r, b in zip(rows, rows_before, strict=True))
    # The backup: 0600, intact, holding the rows as they were.
    (item,) = report["backups"]
    backup = Path(item["path"])
    assert backup.name.startswith("conversations.sqlite.bak-reredact-")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert item["integrity"] == "ok" and item["rows"] == {"conversation_turns": 4}
    assert not Path(str(backup) + "-wal").exists()
    with sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0] == 4
    assert stat.S_IMODE(fx.db.stat().st_mode) == 0o600
    # Second run: nothing left to change, in a dry run and in an apply.
    again = _json(fx.cli("--json"))
    assert again["store"]["rows_changed"] == 0 and again["store"]["markers"] == {}
    second = fx.cli("--apply", "--json")
    assert second.exit_code == 0 and _json(second)["store"]["rows_changed"] == 0


def test_apply_with_vacuum(fx: Fixture) -> None:
    result = fx.cli("--apply", "--vacuum", "--json")
    assert result.exit_code == 0, result.output
    assert _json(result)["store"]["vacuumed"] is True
    assert _plaintext(fx.db.read_bytes().decode("latin-1")) == []


def test_refused_while_another_writer_holds_the_lock(fx: Fixture) -> None:
    before = fx.snapshot()
    with store_writer_lock(fx.db, wait=0) as held:
        assert held
        result = fx.cli("--apply", "--json")
    assert result.exit_code == reredact.EXIT_LOCKED
    assert _json(result)["status"] == "refused"
    assert fx.snapshot() == before
    with run_lock(fx.state.with_name("conversations-work.lock")) as held:
        assert held
        result = fx.cli("--apply", "--json")
    assert result.exit_code == reredact.EXIT_LOCKED
    assert fx.snapshot() == before
    assert list(fx.data.glob("*.bak-reredact-*")) == []


def test_ingest_waits_for_the_writer_lock_then_fails(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversations_module, "WRITER_LOCK_WAIT", 0.2)
    with store_writer_lock(fx.db, wait=0) as held:
        assert held
        with pytest.raises(HistoryError, match="locked"):
            fx.ingest()
    assert fx.ingest()["sessions_unchanged"] == 1


def test_interrupted_run_resumes(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    real = reredact._apply_batch
    calls = {"n": 0}

    def flaky(connection: sqlite3.Connection, table: str, plans: Any) -> set[int]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(connection, table, plans)

    monkeypatch.setattr(reredact, "_apply_batch", flaky)
    options = reredact.Options(
        store=fx.db, apply=True, batch=1, worker_lock=lambda: run_lock(fx.state.with_name("x.lock"))
    )
    with pytest.raises(KeyboardInterrupt):
        reredact.run(options)
    # The first batch committed, the second rolled back; the locks were released.
    partial = reredact.run(reredact.Options(store=fx.db))
    assert partial["store"]["rows_changed"] == 3
    monkeypatch.setattr(reredact, "_apply_batch", real)
    stamp = datetime(2026, 1, 6, tzinfo=UTC)
    done = reredact.run(
        reredact.Options(
            store=fx.db,
            apply=True,
            batch=1,
            worker_lock=lambda: run_lock(fx.state.with_name("x.lock")),
            now=stamp,
        )
    )
    assert done["store"]["rows_changed"] == 3
    assert done["store"]["verify"]["rescan_rows_changed"] == 0
    assert _plaintext(fx.dump()) == []
    assert len(list(fx.data.glob("*.bak-reredact-*"))) == 2


def test_ingest_after_reredact_does_not_duplicate(fx: Fixture) -> None:
    assert fx.cli("--apply", "--json").exit_code == 0
    keys = [(r["session_id"], r["offset"]) for r in fx.rows()]
    # Unchanged file: skipped (cursor untouched).
    assert fx.ingest()["sessions_unchanged"] == 1
    # The file grows: only the new turn is new.
    fx.append("user", "one more question")
    counts = fx.ingest()
    assert counts["records_new"] == 1 and counts["records_dropped"] == 0
    assert len(fx.rows()) == 5
    # A full re-read under the current rules: every stored turn is a duplicate.
    with sqlite3.connect(fx.db) as connection:
        connection.execute("DELETE FROM conversation_cursors")
    counts = fx.ingest()
    assert counts["records_new"] == 0 and counts["records_updated"] == 0
    assert counts["records_duplicate"] == 5 and counts["records_dropped"] == 0
    rows = fx.rows()
    assert [(r["session_id"], r["offset"]) for r in rows][:4] == keys
    # Reads (get / handoff) still find the session and its turns.
    turns = SqliteStoreSource(fx.db).turns("claude", "s1")
    assert len(turns) == 5 and sum(t.withheld for t in turns) == 1
    assert _plaintext(fx.dump()) == []


def test_worker_state_is_scanned_and_rewritten_on_request(fx: Fixture) -> None:
    state = WorkState(fx.state)
    with state.open() as connection:
        connection.execute(
            "INSERT INTO summaries (app, session_id, chunk_start, chunk_end, prompt_version,"
            " model, summary, intent, outcome, created_at, tags) VALUES"
            " ('claude', 's1', 0, 3, 'conv-v2', 'm', ?, ?, '', '2026-01-05T11:00:00Z', ?)",
            (
                f"Fixed the proxy on {SEEDS['ip']}",
                f"reach {SEEDS['email']}",
                json.dumps({"intent": f"ssh {SEEDS['ip2']}", "topics": ["net"]}),
            ),
        )
    report = _json(fx.cli("--json"))
    assert report["worker_state"]["present"] is True
    assert report["worker_state"]["included"] is False
    assert report["worker_state"]["rows_changed"] == 1
    # --apply without --worker-state leaves it as it was (and says so).
    result = _json(fx.cli("--apply", "--json"))
    assert result["worker_state"]["included"] is False
    assert result["worker_state"]["rows_changed"] == 1
    result = fx.cli("--apply", "--worker-state", "--json")
    assert result.exit_code == 0, result.output
    report = _json(result)
    assert report["worker_state"]["rows_changed"] == 1
    assert set(report["worker_state"]["columns"]) == {"summary", "intent", "tags"}
    assert len(report["backups"]) == 2
    with sqlite3.connect(fx.state) as connection:
        row = connection.execute("SELECT summary, intent, tags FROM summaries").fetchone()
    assert _plaintext(json.dumps(row)) == []
    assert json.loads(row[2])["topics"] == ["net"]


def test_backup_location_rules_and_space(fx: Fixture, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    before = fx.snapshot()
    result = fx.cli("--apply", "--json", "--backup", str(repo / "copy.sqlite"))
    assert result.exit_code == reredact.EXIT_ERROR
    assert "git" in _json(result)["error"]
    assert fx.snapshot() == before
    assert fx.cli("--backup", str(tmp_path / "b.sqlite")).exit_code == reredact.EXIT_ERROR


def test_not_enough_space_refuses(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    class Usage:
        free = 1

    monkeypatch.setattr(reredact.shutil, "disk_usage", lambda _path: Usage())
    before = fx.snapshot()
    result = fx.cli("--apply", "--json")
    assert result.exit_code == reredact.EXIT_ERROR
    assert "space" in _json(result)["error"]
    assert [name for name, _, _ in fx.snapshot()] == [name for name, _, _ in before]
    assert _plaintext(fx.dump()) != []


def test_missing_store_is_refused_and_not_created(tmp_path: Path, isolated_home: Path) -> None:  # noqa: F811
    missing = tmp_path / "data" / "conversations.sqlite"
    result = RUNNER.invoke(
        cli_app, ["conversations", "reredact", "--store", str(missing), "--json"]
    )
    assert result.exit_code == reredact.EXIT_ERROR
    assert not missing.exists()


def test_key_columns_are_checked_not_rewritten() -> None:
    row = {
        "agent": "claude",
        "session_id": f"s-{SEEDS['ip']}",
        "offset": 0,
        "turn": 0,
        "role": "user",
        "ts": "2026-01-05T10:00:00Z",
        "text": "plain text",
        "tool_calls": "[]",
        "cwd": "",
        "project": "",
        "model": "",
        "origin": "interactive",
        "withheld": 0,
        "parser_version": "1",
        "locator": "interactive:abc/def",
        "digest": "x",
        "parent_session": f"p-{SEEDS['ip']}",
    }
    plan = reredact.plan_turn(row)
    assert set(plan.flagged) == {"session_id", "parent_session"}
    assert "session_id" not in plan.updates and "parent_session" not in plan.updates
    # Only the digest differs here (the fixture's is fake): no content change.
    assert set(plan.updates) <= {"digest"}


def test_digest_matches_ingest_formula(fx: Fixture) -> None:
    fx.cli("--apply")
    for row in fx.rows():
        values = tuple(
            row[name]
            for name in (
                "role",
                "ts",
                "text",
                "tool_calls",
                "cwd",
                "project",
                "model",
                "origin",
                "withheld",
                "parser_version",
            )
        )
        assert row["digest"] == conversations_module.turn_digest(
            row["turn"], values, row["parent_session"]
        )


def test_lock_file_is_private(fx: Fixture) -> None:
    lock = Path(str(fx.db) + ".lock")
    assert lock.is_file() and stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_a_pass_that_is_not_idempotent_fails_verification(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reredact, "redact", lambda value, **_: value + "!")
    result = fx.cli("--apply", "--json")
    assert result.exit_code == reredact.EXIT_VERIFY
    report = _json(result)
    assert report["status"] == "verify_failed"
    assert report["store"]["verify"]["rescan_rows_changed"] > 0


def _options(fx: Fixture, **extra: Any) -> reredact.Options:
    return reredact.Options(
        store=fx.db,
        apply=True,
        worker_lock=lambda: run_lock(fx.state.with_name("x.lock")),
        **extra,
    )


def test_each_batch_holds_the_write_lock_between_read_and_write(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A writer that ignores the store lock (an older ingest) tries to write while
    # a batch is being planned: SQLite's write lock (BEGIN IMMEDIATE) refuses it.
    real = reredact.plan_turn
    outcomes: list[str] = []

    def planner(row: Any) -> Any:
        with closing(sqlite3.connect(fx.db, timeout=0)) as other:
            try:
                other.execute("UPDATE conversation_turns SET text = 'x' WHERE offset = -1")
                other.commit()
                outcomes.append("written")
            except sqlite3.OperationalError:
                outcomes.append("locked")
        return real(row)

    monkeypatch.setattr(reredact, "plan_turn", planner)
    report = reredact.run(_options(fx))
    # The four rows of the writing pass (the re-scan after it only reads).
    assert outcomes[:4] == ["locked"] * 4
    assert report["store"]["rows_changed"] == 4


def test_guarded_update_skips_a_row_replaced_concurrently(fx: Fixture) -> None:
    # Read and plan a batch, then another writer replaces one row (same key,
    # new content, as ingest's INSERT OR REPLACE does) before the write.
    with closing(sqlite3.connect(fx.db)) as connection:
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        rows = connection.execute(
            "SELECT rowid AS memfw_rowid, * FROM conversation_turns ORDER BY rowid"
        ).fetchall()
        plans = []
        for row in rows:
            item = dict(row)
            rowid = int(item.pop("memfw_rowid"))
            plans.append((rowid, item, reredact.plan_turn(item)))
        victim = plans[0][1]
        with closing(sqlite3.connect(fx.db)) as other:
            other.execute(
                "UPDATE conversation_turns SET text = 'replaced', digest = 'new'"
                " WHERE agent = ? AND session_id = ? AND offset = ?",
                (victim["agent"], victim["session_id"], victim["offset"]),
            )
            other.commit()
        connection.execute("BEGIN IMMEDIATE")
        skipped = reredact._apply_batch(connection, "conversation_turns", plans)
        connection.execute("COMMIT")
    assert skipped == {plans[0][0]}
    texts = {(r["session_id"], r["offset"]): r["text"] for r in fx.rows()}
    assert texts[(victim["session_id"], victim["offset"])] == "replaced"


def test_skipped_rows_are_counted(fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    real = reredact._apply_batch

    def racing(connection: sqlite3.Connection, table: str, plans: Any) -> set[int]:
        if not plans:
            return real(connection, table, plans)
        written = real(connection, table, plans[1:])
        return written | {plans[0][0]}

    monkeypatch.setattr(reredact, "_apply_batch", racing)
    report = reredact.run(_options(fx))
    assert report["store"]["skipped_concurrent"] == 1
    assert report["store"]["rows_changed"] == 3


def test_failure_after_writes_is_exit_5_with_counts_and_backup(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real_usage = reredact.shutil.disk_usage

    def usage(path: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_usage(path)  # the backup's check passes

        class Full:
            free = 1

        return Full()

    monkeypatch.setattr(reredact.shutil, "disk_usage", usage)
    result = fx.cli("--apply", "--vacuum", "--json")
    assert result.exit_code == reredact.EXIT_FAILED, result.output
    report = _json(result)
    assert report["status"] == "failed_after_writes"
    assert "space" in report["error"]
    assert report["store"]["rows_changed"] == 4
    assert Path(report["backups"][0]["path"]).is_file()
    assert _plaintext(result.output) == []
    assert _plaintext(fx.dump()) == []


def test_sqlite_error_while_writing_is_reported_cleanly(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(connection: sqlite3.Connection, table: str, plans: Any) -> set[int]:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(reredact, "_apply_batch", broken)
    result = fx.cli("--apply", "--json")
    assert result.exit_code == reredact.EXIT_FAILED
    report = _json(result)
    assert report["status"] == "failed_after_writes"
    assert "OperationalError" in report["error"] and "Traceback" not in result.output
    assert report["store"]["rows_changed"] == 0
    assert _plaintext(result.output) == []


def test_withheld_branch_cleans_role_like_ingest() -> None:
    row = {
        "agent": "claude",
        "session_id": "s",
        "offset": 0,
        "turn": 0,
        "role": f"user {SEEDS['ip']}",
        "ts": "2026-01-05T10:00:00Z",
        "text": f"key {TOKEN}",
        "tool_calls": "[]",
        "cwd": "",
        "project": "",
        "model": "",
        "origin": "interactive",
        "withheld": 0,
        "parser_version": "1",
        "locator": "l",
        "digest": "d",
        "parent_session": "",
    }
    plan = reredact.plan_turn(row)
    assert plan.withheld and plan.updates["role"] == "user [REDACTED:ip]"


def test_journal_mode_is_reported(fx: Fixture) -> None:
    report = _json(fx.cli("--apply", "--json"))
    assert report["store"]["verify"]["journal_mode"] == "delete"
