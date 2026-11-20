"""Re-redact text already at rest in the conversation store (``memfw conversations reredact``).

Why: :func:`memfw.history.redact.redact` gets stricter over time. Reads redact
again on the way out, but rows written under older rules keep their unmasked
values on disk. Deleting the store and ingesting again is not an option (many
raw transcripts no longer exist), so this module rewrites the stored values in
place.

What is rewritten in ``conversation_turns`` (the columns ingest passes through
``redact``, see :meth:`memfw.history.conversations.ConversationStore._row`):
``role``, ``text``, ``tool_calls`` (each call's name and summary, re-encoded as
ingest encodes them), ``cwd``, ``project`` and ``model``. A row whose stored
text now trips :func:`~memfw.history.redact.has_secret` is withheld exactly as
ingest would withhold it (``withheld = 1``, every text field empty). ``digest``
is recomputed from the new values with the ingest formula
(:func:`~memfw.history.conversations.turn_digest`), so it keeps describing what
is stored.

What is never rewritten, and why:

* ``agent``, ``session_id``, ``offset``: the primary key, also used by
  ``conversation_turn_locators``, ``conversation_archive``, the worker state and
  every handoff / ``get`` reference. ``session_id`` (and ``parent_session``,
  which points at a ``session_id``) is only *checked*: rows whose value would
  change are counted in ``key_columns_flagged`` and left alone. Rewriting them
  would orphan those references; the next full re-read of such a session by
  ingest already re-keys it under the new rules (it drops the old offsets), so
  leaving them does not create anything ingest would not create anyway.
* ``ts`` (checked only): it feeds the session fingerprint that the archive and
  the worker key jobs by; a timestamp has nothing to mask.
* ``locator``: an opaque hash (``origin:<root hash>/<key hash>``), never
  redacted at ingest; changing it would break cursor and ownership matching.
* ``origin``, ``withheld`` (except a new withhold), ``parser_version``, ``turn``.

Dedupe after a re-redaction: rows keep their key, so a later ingest of the same
turn finds the row (``INSERT OR REPLACE`` on the same key, never a second row).
Cursors are untouched, so an unchanged session file is still skipped; a changed
one is compared by digest: equal (``redact`` is idempotent, so usually) is a
duplicate and nothing is written; different (the raw text gave another mask) is
an in-place update from the raw text. Session fingerprints (turn count, last
offset, last timestamp) do not change, so archive state and worker jobs stay.

Safety (``--apply``): the store's writer lock
(:func:`~memfw.history.conversations.store_writer_lock`) and the conversation
worker's run lock are taken without waiting (refused when either is held); a
full backup is written first with the SQLite online backup API (``0600``,
location rules of the store, free space checked) and verified
(``integrity_check`` plus row counts); updates run with ``secure_delete`` on, in
batches, one transaction each, only for rows that change. A batch is idempotent,
so an interrupted run is simply run again. Afterwards the rewritten rows are
scanned again (no change allowed), the file is checked (``integrity_check``) and
checkpointed (``wal_checkpoint(TRUNCATE)``); ``--vacuum`` rewrites the file so free pages no
longer hold old values.

Nothing here prints or returns text, values or paths taken from rows: only
counts, column names and mask kinds.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sqlite3
import stat
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .conversations import (
    FIELD_CHARS,
    SUMMARY_CHARS,
    TEXT_CHARS,
    store_writer_lock,
    turn_digest,
)
from .redact import has_secret, redact

TURNS_TABLE = "conversation_turns"
REWRITTEN = ("role", "text", "tool_calls", "cwd", "project", "model")
"""Columns re-redacted in place (ingest redacts them; none is a key)."""
CHECKED = ("session_id", "parent_session", "ts")
"""Columns only checked: a would-be change is counted, never written (see the module doc)."""
SCANNED_FOR_SECRETS = ("text", "cwd", "project", "model")
"""Ingest withholds a turn when one of these (or a tool call) has a secret."""
WORKER_COLUMNS: dict[str, tuple[str, ...]] = {
    "summaries": ("summary", "intent", "outcome", "tags"),
    "episodes": ("intent", "outcome"),
}
"""Worker state columns holding text derived from conversations (``--worker-state``)."""
DEFAULT_BATCH = 2000
BACKUP_MARK = ".bak-reredact-"
SPACE_MARGIN = 64 * 1024 * 1024
"""Free space a backup needs beyond the store's own size (bytes)."""

EXIT_OK = 0
EXIT_ERROR = 2
"""Refused or failed before anything was written (location, missing store, space ...)."""
EXIT_LOCKED = 3
"""Another writer (ingest, the worker, another re-redaction) holds a lock."""
EXIT_VERIFY = 4
"""A verification failed: backup, re-scan (not idempotent) or integrity check."""

_MARK = re.compile(r"\[REDACTED(?::([^\]\s]{1,40}))?\]")


class ReredactError(Exception):
    """A refusal or failure with its exit code; the message never holds row content."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class TableCounts:
    """Counts for one table (no text)."""

    rows_scanned: int = 0
    rows_changed: int = 0
    rows_withheld: int = 0
    columns: Counter[str] = field(default_factory=Counter)
    markers: Counter[str] = field(default_factory=Counter)
    key_columns_flagged: Counter[str] = field(default_factory=Counter)
    changed_rowids: list[int] = field(default_factory=list)
    """Rows this pass changed (in memory only; never reported)."""

    def add(self, other: TableCounts) -> None:
        self.rows_scanned += other.rows_scanned
        self.rows_changed += other.rows_changed
        self.rows_withheld += other.rows_withheld
        self.columns.update(other.columns)
        self.markers.update(other.markers)
        self.key_columns_flagged.update(other.key_columns_flagged)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_scanned": self.rows_scanned,
            "rows_changed": self.rows_changed,
            "rows_withheld": self.rows_withheld,
            "columns": dict(sorted(self.columns.items())),
            "markers": dict(sorted(self.markers.items())),
            "key_columns_flagged": dict(sorted(self.key_columns_flagged.items())),
        }


@dataclass
class RowPlan:
    """The new values of one row (only changed columns) and what changed."""

    updates: dict[str, Any]
    markers: Counter[str]
    withheld: bool = False
    flagged: tuple[str, ...] = ()


def _kinds(value: str) -> Counter[str]:
    return Counter(match.group(1) or "unknown" for match in _MARK.finditer(value or ""))


def _new_markers(old: str, new: str) -> Counter[str]:
    before, after = _kinds(old), _kinds(new)
    return Counter(
        {kind: count - before[kind] for kind, count in after.items() if count > before[kind]}
    )


def _clean(value: str, limit: int) -> str:
    """Ingest's field rule: ``redact(value)[:limit]``."""

    return redact(value)[:limit] if value else ""


def _calls(raw: str) -> list[tuple[str, str]] | None:
    """Stored tool calls as ``(name, summary)`` pairs, or None when not ingest's shape."""

    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    out: list[tuple[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            return None
        name, summary = item.get("name", ""), item.get("summary", "")
        if not isinstance(name, str) or not isinstance(summary, str):
            return None
        out.append((name, summary))
    return out


def plan_turn(row: Mapping[str, Any]) -> RowPlan:
    """What re-redacting one ``conversation_turns`` row changes (pure, no I/O)."""

    flagged = tuple(
        name
        for name in CHECKED
        if isinstance(row.get(name), str)
        and row[name]
        and _clean(row[name], FIELD_CHARS) != row[name]
    )
    if int(row.get("withheld") or 0):
        return RowPlan({}, Counter(), flagged=flagged)  # nothing kept to redact
    raw_calls = str(row.get("tool_calls") or "[]")
    calls = _calls(raw_calls)
    scanned = [str(row.get(name) or "") for name in SCANNED_FOR_SECRETS]
    if calls is None:
        scanned.append(raw_calls)
    else:
        for name, summary in calls:
            scanned.extend((name, summary))
    new: dict[str, Any]
    if any(value and has_secret(value) for value in scanned):
        # Ingest's rule: a secret anywhere withholds the whole turn.
        new = {
            "text": "",
            "tool_calls": "[]",
            "cwd": "",
            "project": "",
            "model": "",
            "withheld": 1,
            "parent_session": "",
        }
        withheld = True
    else:
        new = {
            "role": _clean(str(row.get("role") or ""), FIELD_CHARS),
            "text": _clean(str(row.get("text") or ""), TEXT_CHARS),
            "cwd": _clean(str(row.get("cwd") or ""), FIELD_CHARS),
            "project": _clean(str(row.get("project") or ""), FIELD_CHARS),
            "model": _clean(str(row.get("model") or ""), FIELD_CHARS),
        }
        if calls is None:
            new["tool_calls"] = _clean(raw_calls, TEXT_CHARS)
        else:
            cleaned = [(_clean(n, FIELD_CHARS), _clean(s, SUMMARY_CHARS)) for n, s in calls]
            if cleaned != calls:
                # Encoded exactly as ingest encodes it; unchanged calls keep their bytes.
                new["tool_calls"] = json.dumps(
                    [{"name": n, "summary": s} for n, s in cleaned], ensure_ascii=False
                )
        withheld = False
    updates = {name: value for name, value in new.items() if value != row.get(name)}
    markers: Counter[str] = Counter()
    if not updates:
        return RowPlan({}, markers, flagged=flagged)
    for name, value in updates.items():
        if isinstance(value, str):
            markers.update(_new_markers(str(row.get(name) or ""), value))
    merged = {**dict(row), **updates}
    values = tuple(
        merged[name]
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
    digest = turn_digest(int(merged["turn"]), values, str(merged.get("parent_session") or ""))
    if digest != row.get("digest"):
        updates["digest"] = digest
    return RowPlan(updates, markers, withheld=withheld, flagged=flagged)


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json(item) for key, item in value.items()}
    return value


def _plan_worker_row(row: Mapping[str, Any], *, columns: tuple[str, ...]) -> RowPlan:
    return plan_worker(row, columns)


def plan_worker(row: Mapping[str, Any], columns: tuple[str, ...]) -> RowPlan:
    """What re-redacting one worker-state row changes (``tags`` is JSON-aware)."""

    updates: dict[str, Any] = {}
    markers: Counter[str] = Counter()
    for name in columns:
        old = row.get(name)
        if not isinstance(old, str) or not old:
            continue
        new = redact(old)
        if name == "tags":
            try:
                parsed = json.loads(old)
            except ValueError:
                parsed = None
            if parsed is not None:
                cleaned = _redact_json(parsed)
                new = (
                    old
                    if cleaned == parsed
                    else json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
                )
        if new != old:
            updates[name] = new
            markers.update(_new_markers(old, new))
    return RowPlan(updates, markers)


# -- the pass ------------------------------------------------------------------------

Planner = Callable[[Mapping[str, Any]], RowPlan]
WorkerLock = Callable[[], AbstractContextManager[bool]]
"""A non-blocking lock: entering yields True when held, False when another holds it."""


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _apply_batch(
    connection: sqlite3.Connection, table: str, plans: list[tuple[int, RowPlan]]
) -> None:
    """One transaction: write every planned row of a batch (idempotent)."""

    with connection:
        for rowid, plan in plans:
            names = sorted(plan.updates)
            connection.execute(
                f"UPDATE {table} SET {', '.join(f'{name} = ?' for name in names)} WHERE rowid = ?",
                [*(plan.updates[name] for name in names), rowid],
            )


def _pass(
    connection: sqlite3.Connection,
    table: str,
    planner: Planner,
    *,
    batch: int,
    write: bool,
    only: list[int] | None = None,
) -> TableCounts:
    """Scan ``table`` in rowid order, ``batch`` rows at a time; write changes when asked.

    ``only``: scan just these rowids (the post-pass re-scan reads the rows the pass
    rewrote; a row the pass left alone is a fixed point, the planner being pure).
    """

    counts = TableCounts()
    last = 0
    pending = sorted(only) if only is not None else None
    while True:
        if pending is None:
            rows = connection.execute(
                f"SELECT rowid AS memfw_rowid, * FROM {table} WHERE rowid > ?"
                " ORDER BY rowid LIMIT ?",
                (last, batch),
            ).fetchall()
        else:
            chunk, pending = pending[: min(batch, 500)], pending[min(batch, 500) :]
            rows = (
                connection.execute(
                    f"SELECT rowid AS memfw_rowid, * FROM {table}"
                    f" WHERE rowid IN ({', '.join('?' for _ in chunk)}) ORDER BY rowid",
                    chunk,
                ).fetchall()
                if chunk
                else []
            )
        if not rows:
            return counts
        plans: list[tuple[int, RowPlan]] = []
        for row in rows:
            item = dict(row)
            rowid = int(item.pop("memfw_rowid"))
            last = rowid
            counts.rows_scanned += 1
            plan = planner(item)
            counts.key_columns_flagged.update(plan.flagged)
            if not plan.updates:
                continue
            counts.rows_changed += 1
            counts.rows_withheld += int(plan.withheld)
            counts.columns.update(name for name in plan.updates if name != "digest")
            counts.markers.update(plan.markers)
            counts.changed_rowids.append(rowid)
            plans.append((rowid, plan))
        if write and plans:
            _apply_batch(connection, table, plans)


def _turn_planner(connection: sqlite3.Connection) -> Planner:
    missing = {"turn", "digest", "withheld", "parser_version", *REWRITTEN} - _columns(
        connection, TURNS_TABLE
    )
    if missing:
        raise ReredactError("conversation store has an unknown schema (columns missing)")
    return plan_turn


def _worker_tables(connection: sqlite3.Connection) -> list[tuple[str, tuple[str, ...]]]:
    present = []
    for table, wanted in WORKER_COLUMNS.items():
        columns = _columns(connection, table)
        chosen = tuple(name for name in wanted if name in columns)
        if chosen:
            present.append((table, chosen))
    return present


def _scan_worker(connection: sqlite3.Connection, *, batch: int, write: bool) -> TableCounts:
    total = TableCounts()
    for table, columns in _worker_tables(connection):
        total.add(
            _pass(
                connection,
                table,
                functools.partial(_plan_worker_row, columns=columns),
                batch=batch,
                write=write,
            )
        )
    return total


# -- files, locks, backup --------------------------------------------------------------


def _existing_db(path: Path, what: str) -> Path:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ReredactError(f"{what} does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReredactError(f"{what} is not a regular file")
    return path


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _size(path: Path) -> int:
    total = 0
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            pass
    return total


def default_backup(path: Path, stamp: str) -> Path:
    """``<store>.bak-reredact-<stamp>``, with ``-N`` added when that name is taken."""

    base = path.with_name(f"{path.name}{BACKUP_MARK}{stamp}")
    candidate, number = base, 1
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    return candidate


def _integrity(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row is not None else ""


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def backup(
    source: sqlite3.Connection, source_path: Path, target: Path, tables: tuple[str, ...]
) -> dict[str, Any]:
    """Online backup of ``source`` to a new ``0600`` file, verified; removed if it fails."""

    if target.exists() or target.is_symlink():
        raise ReredactError("backup path already exists")
    needed = _size(source_path) + SPACE_MARGIN
    free = shutil.disk_usage(target.parent).free
    if free < needed:
        raise ReredactError("not enough free disk space for the backup")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(target, flags, 0o600))
    except OSError as exc:
        raise ReredactError(f"cannot create the backup file: {exc.strerror}") from exc
    try:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            # One self-contained file: no -wal / -shm sidecars next to the backup.
            destination.execute("PRAGMA journal_mode=DELETE")
        os.chmod(target, 0o600)
        with closing(_readonly(target)) as check:
            integrity = _integrity(check)
            counts = {table: _count(check, table) for table in tables}
        expected = {table: _count(source, table) for table in tables}
        if integrity != "ok" or counts != expected:
            raise ReredactError("backup verification failed", EXIT_VERIFY)
    except BaseException:
        for leftover in (target, Path(str(target) + "-journal")):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return {"path": str(target), "integrity": integrity, "rows": counts}


@contextmanager
def _locks(store: Path, worker_lock: WorkerLock | None) -> Iterator[None]:
    """The store's writer lock and the worker's run lock, both without waiting."""

    with ExitStack() as stack:
        if not stack.enter_context(store_writer_lock(store, wait=0)):
            raise ReredactError("conversation store is locked by another writer", EXIT_LOCKED)
        if worker_lock is not None and not stack.enter_context(worker_lock()):
            raise ReredactError("the conversation worker is running", EXIT_LOCKED)
        yield


# -- entry point ---------------------------------------------------------------------


@dataclass
class Options:
    store: Path
    apply: bool = False
    batch: int = DEFAULT_BATCH
    backup: Path | None = None
    vacuum: bool = False
    worker_state: Path | None = None
    """The worker state file (scanned when it exists)."""
    include_worker: bool = False
    """``--worker-state``: also rewrite the worker state (else it is only scanned)."""
    worker_lock: WorkerLock | None = None
    """The worker's run lock (``memfw.conversations.worker.run_lock`` on its lock file),
    injected by the CLI: this package does not import the conversations layer."""
    check_backup: Callable[[Path], Path] | None = None
    """Location rules for a backup path (git worktree, vault, synced folder)."""
    now: datetime | None = None


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def run(options: Options) -> dict[str, Any]:
    """Dry run (default) or ``apply``; returns a JSON-ready summary of counts only."""

    if options.batch < 1:
        raise ReredactError("--batch must be at least 1")
    if options.backup is not None and not options.apply:
        raise ReredactError("--backup needs --apply")
    if options.vacuum and not options.apply:
        raise ReredactError("--vacuum needs --apply")
    if options.include_worker and options.worker_state is None:
        raise ReredactError("--worker-state needs a worker state path")
    store = _existing_db(options.store, "conversation store")
    worker = options.worker_state
    worker_present = worker is not None and worker.is_file() and not worker.is_symlink()
    if options.include_worker and not worker_present:
        raise ReredactError("worker state does not exist")
    report: dict[str, Any] = {"mode": "apply" if options.apply else "dry_run"}
    if not options.apply:
        with closing(_readonly(store)) as connection:
            report["store"] = _pass(
                connection, TURNS_TABLE, _turn_planner(connection), batch=options.batch, write=False
            ).as_dict()
        report["worker_state"] = _worker_report(worker if worker_present else None, options, False)
        report["status"] = "ok"
        return report
    stamp = _stamp(options.now)
    sources = [store] + ([worker] if options.include_worker and worker is not None else [])
    targets: list[tuple[Path, Path]] = []
    for path in sources:
        # Location rules first, before any lock or file is taken.
        chosen = options.backup if path == store and options.backup is not None else None
        target = chosen if chosen is not None else default_backup(path, stamp)
        if options.check_backup is not None:
            target = options.check_backup(target)
        targets.append((path, target))
    with _locks(store, options.worker_lock):
        backups = []
        for path, target in targets:
            with closing(_writable(path)) as connection:
                if path == store:
                    tables: tuple[str, ...] = (TURNS_TABLE,)
                else:
                    tables = tuple(table for table, _ in _worker_tables(connection))
                backups.append(backup(connection, path, target, tables))
        report["backups"] = backups
        report["store"] = _apply_one(store, options, worker=False)
        if options.include_worker and worker is not None:
            report["worker_state"] = {
                "present": True,
                "included": True,
                **_apply_one(worker, options, worker=True),
            }
        else:
            report["worker_state"] = _worker_report(
                worker if worker_present else None, options, False
            )
    report["status"] = "ok"
    return report


def _worker_report(path: Path | None, options: Options, included: bool) -> dict[str, Any]:
    if path is None:
        return {"present": False, "included": False}
    with closing(_readonly(path)) as connection:
        counts = _scan_worker(connection, batch=options.batch, write=False).as_dict()
    counts.pop("key_columns_flagged", None)
    counts.pop("rows_withheld", None)
    return {"present": True, "included": included, **counts}


def _apply_one(path: Path, options: Options, *, worker: bool) -> dict[str, Any]:
    """Re-redact one database in place, then re-scan, check and checkpoint it."""

    with closing(_writable(path)) as connection:
        # Rows rewritten from here on leave no old bytes in the pages they free.
        connection.execute("PRAGMA secure_delete=ON")
        if worker:
            counts = _scan_worker(connection, batch=options.batch, write=True)
            rescan = _scan_worker(connection, batch=options.batch, write=False)
        else:
            planner = _turn_planner(connection)
            counts = _pass(connection, TURNS_TABLE, planner, batch=options.batch, write=True)
            rescan = _pass(
                connection,
                TURNS_TABLE,
                planner,
                batch=options.batch,
                write=False,
                only=counts.changed_rowids,
            )
        integrity = _integrity(connection)
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        vacuumed = False
        if options.vacuum and rescan.rows_changed == 0 and integrity == "ok":
            if shutil.disk_usage(path.parent).free < _size(path) + SPACE_MARGIN:
                raise ReredactError("not enough free disk space for --vacuum")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vacuumed = True
    os.chmod(path, 0o600)
    out = counts.as_dict()
    if worker:
        out.pop("key_columns_flagged", None)
        out.pop("rows_withheld", None)
    out["verify"] = {
        "rescan_rows_changed": rescan.rows_changed,
        "integrity": "ok" if integrity == "ok" else "failed",
        "checkpoint_busy": int(checkpoint[0]) if checkpoint is not None else 0,
    }
    out["vacuumed"] = vacuumed
    if rescan.rows_changed or integrity != "ok":
        raise ReredactVerifyError("verification after the pass failed", out)
    return out


class ReredactVerifyError(ReredactError):
    """The pass finished but its verification did not; carries the counts."""

    def __init__(self, message: str, counts: dict[str, Any]) -> None:
        super().__init__(message, EXIT_VERIFY)
        self.counts = counts
