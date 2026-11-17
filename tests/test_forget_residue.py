"""``memfw forget residue`` and ``memfw forget requests`` (roadmap R5).

The residue check is read-only and reports, per derived store, whether a
forgotten memory is still carried: after a hard delete nothing may name the
id; after a soft close (``valid_to``/``superseded_by``) the FTS index may keep
history rows but must not list it as current, and the current-only related
projection must not carry it at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import scripts.memfw_readonly_service as service
from memfw.cli import app
from memfw.core.repository import Repository
from memfw.curation.residue import list_forget_requests, projection_residue
from memfw.index import MemoryIndex
from memfw.ports.usage import UsageSignal
from memfw.related import RelatedIndex
from memfw.usage import SqliteUsageSink
from memfw.usage import projection_path as usage_path

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Alpha", "alpha corpus keyword shared", memory_id="mem-a", tags=["shared"])
    repository.add("Beta", "beta corpus keyword shared", memory_id="mem-b", tags=["shared"])
    repository.add("Gamma", "gamma corpus keyword", memory_id="mem-c", tags=["shared"])
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _related(repository: Repository) -> Path:
    path = repository.root / ".memfw-related.sqlite"
    RelatedIndex(path).rebuild_memory(repository)
    return path


def _corroboration(repository: Repository, memory_id: str) -> Path:
    path = repository.root / "corroboration.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS mentions (memory_id TEXT NOT NULL, app TEXT NOT NULL,"
            " session_id TEXT NOT NULL, first_seen TEXT, last_seen TEXT,"
            " PRIMARY KEY (memory_id, app, session_id)) WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO mentions VALUES (?, 'app', 'session-1', NULL, NULL)", (memory_id,)
        )
    return path


def _usage(repository: Repository, home: Path, memory_id: str) -> Path:
    path = usage_path(repository.root, home=home)
    SqliteUsageSink(path).record(
        UsageSignal(
            memory_ids=(memory_id,),
            session_ref="session-1",
            app="",
            at=datetime(2026, 11, 17, tzinfo=UTC),
        )
    )
    return path


def _checks(report: dict) -> dict[str, dict]:
    return {check["store"]: check for check in report["checks"]}


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_live_memory_is_not_forgotten(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    report = projection_residue(repository.root, "mem-a", home=home)
    assert report["state"] == "live"
    assert report["clean"] is None
    result = runner.invoke(app, ["forget", "residue", "mem-a", "--root", str(repository.root)])
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["state"] == "live"


def test_hard_delete_leaves_projections_until_they_are_rebuilt(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    related = _related(repository)
    _corroboration(repository, "mem-b")
    usage = _usage(repository, home, "mem-b")
    repository.delete("mem-b")
    MemoryIndex(repository.index_path).rebuild(repository)

    report = projection_residue(repository.root, "mem-b", home=home)
    assert report["state"] == "deleted"
    assert report["clean"] is False
    # ``delete`` drops the FTS index; the other projections still name the id.
    assert report["residue"] == ["related", "corroboration", "usage"]
    checks = _checks(report)
    assert checks["index"]["residue"] is False
    assert checks["related"]["rows"]["related_items"] == 1
    assert checks["related"]["remedy"] == "memfw memory-related-build"
    assert checks["vault"]["residue"] is False
    # Ids, counts and remedies only: no body text, no host path.
    raw = json.dumps(report)
    assert "beta corpus" not in raw and str(tmp_path) not in raw

    result = runner.invoke(app, ["forget", "residue", "mem-b", "--root", str(repository.root)])
    assert result.exit_code == 1, result.output

    RelatedIndex(related).rebuild_memory(repository)
    (repository.root / "corroboration.sqlite3").unlink()
    usage.unlink()
    report = projection_residue(repository.root, "mem-b", home=home)
    assert report == {**report, "clean": True, "residue": []}
    result = runner.invoke(app, ["forget", "residue", "mem-b", "--root", str(repository.root)])
    assert result.exit_code == 0, result.output


def test_soft_close_keeps_history_rows_but_not_current_ones(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    related = _related(repository)
    _corroboration(repository, "mem-a")
    repository.supersede(
        "mem-a", "Alpha v2", "alpha corpus keyword replaced", memory_id_new="mem-a2"
    )
    # An index built before the close still lists mem-a as current.
    stale = projection_residue(repository.root, "mem-a", home=home, related_db=related)
    assert stale["state"] == "closed"
    MemoryIndex(repository.index_path).rebuild(repository)
    report = projection_residue(repository.root, "mem-a", home=home, related_db=related)
    checks = _checks(report)
    assert checks["index"]["residue"] is False
    assert checks["index"]["rows"]["memory_rows"] == 1  # history stays readable
    assert checks["related"]["residue"] is True  # built while mem-a was current
    assert checks["corroboration"]["retained"] is True
    assert checks["corroboration"]["residue"] is False
    assert report["residue"] == ["related"]
    RelatedIndex(related).rebuild_memory(repository)
    report = projection_residue(repository.root, "mem-a", home=home, related_db=related)
    assert report["clean"] is True


def test_stale_index_listing_a_closed_memory_as_current(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    path = repository.memories_dir / "mem-c.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("---\n", "---\nvalid_to: '2026-11-07T00:00:00+00:00'\n", 1), encoding="utf-8"
    )
    report = projection_residue(repository.root, "mem-c", home=home)
    assert report["state"] == "closed"
    index = _checks(report)["index"]
    assert index["residue"] is True
    assert index["detail"] == "index lists the memory as current"
    assert index["remedy"] == "memfw index rebuild"


def test_closed_memory_with_unknown_index_state_is_residue(tmp_path: Path, home: Path) -> None:
    """Rows name a closed memory but no ``memory_temporal`` row says how: fail closed."""

    repository = _vault(tmp_path)
    path = repository.memories_dir / "mem-c.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("---\n", "---\nvalid_to: '2026-11-07T00:00:00+00:00'\n", 1), encoding="utf-8"
    )
    with sqlite3.connect(repository.index_path) as connection:
        connection.execute("DELETE FROM memory_temporal WHERE id = 'mem-c'")
    connection.close()
    report = projection_residue(repository.root, "mem-c", home=home)
    assert report["state"] == "closed"
    index = _checks(report)["index"]
    assert index["rows"]["memory_rows"] == 1 and index["rows"]["memory_temporal"] == 0
    assert index["residue"] is True
    assert index["detail"] == "index state of the memory is unknown"
    assert report["clean"] is False


def test_store_changing_during_the_read_is_retried_then_unreadable(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.curation.residue as residue

    repository = _vault(tmp_path)
    path = repository.memories_dir / "mem-c.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("---\n", "---\nvalid_to: '2026-11-07T00:00:00+00:00'\n", 1), encoding="utf-8"
    )
    calls = {"count": 0}
    real = residue._signature

    def changing_once(path: Path) -> tuple:
        calls["count"] += 1
        # The first read of the index sees a change after it; the retry is stable.
        if path == repository.index_path and calls["count"] == 2:
            return ("changed",)
        return real(path)

    monkeypatch.setattr(residue, "_signature", changing_once)
    index = _checks(projection_residue(repository.root, "mem-c", home=home))["index"]
    # Retried, then read: the stale index still lists the closed memory as current.
    assert index.get("error") is None and index["residue"] is True
    assert index["detail"] == "index lists the memory as current"

    def always_changing(path: Path) -> tuple:
        calls["count"] += 1
        return (calls["count"],) if path == repository.index_path else real(path)

    monkeypatch.setattr(residue, "_signature", always_changing)
    index = _checks(projection_residue(repository.root, "mem-c", home=home))["index"]
    assert index["error"] == "unreadable" and index["residue"] is True


def test_absent_id_and_unreadable_store(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    report = projection_residue(repository.root, "mem-never", home=home)
    assert report["state"] == "absent"
    assert report["clean"] is True
    garbage = repository.root / ".memfw-related.sqlite"
    garbage.write_bytes(b"not a database" * 100)
    report = projection_residue(repository.root, "mem-never", home=home)
    related = _checks(report)["related"]
    assert related["error"] == "unreadable" and related["residue"] is True
    assert report["clean"] is False


def test_residue_check_is_read_only(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    _related(repository)
    _corroboration(repository, "mem-b")
    _usage(repository, home, "mem-b")
    repository.delete("mem-b")
    before = _tree(repository.root), _tree(home)
    projection_residue(repository.root, "mem-b", home=home)
    runner.invoke(app, ["forget", "residue", "mem-b", "--root", str(repository.root)])
    assert (_tree(repository.root), _tree(home)) == before


def test_residue_rejects_bad_ids(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    for bad in ("../mem-a", "", "mem.a", "a" * 200):
        with pytest.raises(ValueError):
            projection_residue(repository.root, bad)
    result = runner.invoke(app, ["forget", "residue", "../x", "--root", str(repository.root)])
    assert result.exit_code != 0


def test_residue_exit_codes_do_not_collide(tmp_path: Path, home: Path) -> None:
    """0 clean, 1 residue, 2 usage error (typer), 3 live: a script can tell them apart."""

    repository = _vault(tmp_path)
    root = str(repository.root)
    bad_id = runner.invoke(app, ["forget", "residue", "../x", "--root", root])
    assert bad_id.exit_code == 2, bad_id.output
    bad_root = runner.invoke(app, ["forget", "residue", "mem-a", "--root", str(tmp_path / "no")])
    assert bad_root.exit_code == 2, bad_root.output
    live = runner.invoke(app, ["forget", "residue", "mem-a", "--root", root])
    assert live.exit_code == 3, live.output
    assert json.loads(live.output)["clean"] is None
    absent = runner.invoke(app, ["forget", "residue", "mem-never", "--root", root])
    assert absent.exit_code in (0, 1), absent.output
    assert json.loads(absent.output)["clean"] is (absent.exit_code == 0)


def test_entities_projection_is_checked_when_given(tmp_path: Path, home: Path) -> None:
    repository = _vault(tmp_path)
    entities = tmp_path / "entities.sqlite3"
    with sqlite3.connect(entities) as connection:
        connection.execute(
            "CREATE TABLE entities (entity TEXT NOT NULL, memory_id TEXT NOT NULL, "
            "PRIMARY KEY(entity, memory_id))"
        )
        connection.execute("INSERT INTO entities VALUES ('beta', 'mem-b')")
    repository.delete("mem-b")
    report = projection_residue(repository.root, "mem-b", home=home, entities_db=entities)
    assert "entities" in report["residue"]


def test_forget_requests_lists_http_requests(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    service.forget_request(
        repository.root,
        {"request_id": "req-1", "memory_id": "mem-a", "reason": "asked to"},
        tenant="",
    )
    rows = list_forget_requests(repository.root)
    assert rows == [
        {
            "request_id": "req-1",
            "memory_id": "mem-a",
            "tenant": "",
            "source": "conductor",
            "reason": "asked to",
            "status": "pending",
            "recorded_at": rows[0]["recorded_at"],
            "path": "_candidates/forget/forget-e3b0c44298fc1c14-req-1.md",  # sha256("")[:16]
            "target_present": True,
            "target_changed": False,
        }
    ]
    repository.update("mem-a", body="alpha changed")
    assert list_forget_requests(repository.root)[0]["target_changed"] is True
    result = runner.invoke(app, ["forget", "requests", "--root", str(repository.root)])
    assert result.exit_code == 0, result.output
    assert [row["request_id"] for row in json.loads(result.output)["requests"]] == ["req-1"]
    # A symlinked request directory is not followed.
    other = tmp_path / "other"
    other.mkdir()
    forget_dir = repository.root / "_candidates" / "forget"
    for path in forget_dir.iterdir():
        path.rename(other / path.name)
    forget_dir.rmdir()
    forget_dir.symlink_to(other, target_is_directory=True)
    assert list_forget_requests(repository.root) == []


def _wal_store(directory: Path, memory_id: str, *, keep_open: bool) -> tuple[Path, object]:
    """A WAL-mode related projection naming ``memory_id``; optionally with a pending WAL."""

    directory.mkdir()
    path = directory / "related.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE related_items (source TEXT, id TEXT)")
    connection.execute(
        "CREATE TABLE related_edges (src_source TEXT, src_id TEXT, dst_source TEXT, dst_id TEXT)"
    )
    connection.commit()
    if keep_open:
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("INSERT INTO related_items VALUES ('memory', ?)", (memory_id,))
    connection.commit()
    if not keep_open:
        connection.close()
        return path, None
    return path, connection


def _listing(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)
def test_residue_reads_a_store_in_a_read_only_directory(tmp_path: Path, home: Path) -> None:
    """No sidecar is created, so a read-only directory is read, not reported unreadable."""

    repository = _vault(tmp_path)
    clean_dir = tmp_path / "clean"
    clean_path, _ = _wal_store(clean_dir, "mem-other", keep_open=False)
    dirty_dir = tmp_path / "dirty"
    dirty_path, _ = _wal_store(dirty_dir, "mem-gone", keep_open=False)
    before = {name: _listing(path) for name, path in (("c", clean_dir), ("d", dirty_dir))}
    for directory in (clean_dir, dirty_dir):
        directory.chmod(0o555)
    try:
        clean = projection_residue(repository.root, "mem-gone", related_db=clean_path, home=home)
        dirty = projection_residue(repository.root, "mem-gone", related_db=dirty_path, home=home)
    finally:
        for directory in (clean_dir, dirty_dir):
            directory.chmod(0o755)
    assert _checks(clean)["related"].get("error") is None
    assert _checks(clean)["related"]["residue"] is False
    assert clean["clean"] is True
    assert _checks(dirty)["related"].get("error") is None
    assert _checks(dirty)["related"]["rows"]["related_items"] == 1
    assert dirty["residue"] == ["related"]
    after = {name: _listing(path) for name, path in (("c", clean_dir), ("d", dirty_dir))}
    assert after == before  # no -wal/-shm/-journal appeared, nothing changed


def test_residue_counts_rows_still_in_the_wal(tmp_path: Path, home: Path) -> None:
    """A committed row not yet checkpointed is residue; the store's files are left alone."""

    repository = _vault(tmp_path)
    directory = tmp_path / "live-wal"
    path, writer = _wal_store(directory, "mem-gone", keep_open=True)
    try:
        assert (directory / "related.sqlite3-wal").stat().st_size > 0
        before = _listing(directory)
        report = projection_residue(repository.root, "mem-gone", related_db=path, home=home)
        assert _listing(directory) == before
    finally:
        writer.close()  # type: ignore[attr-defined]
    related = _checks(report)["related"]
    assert related.get("error") is None
    assert related["rows"]["related_items"] == 1
    assert report["residue"] == ["related"]
