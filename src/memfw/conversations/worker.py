"""``memfw conversations worker``: asynchronous, idempotent processing of the store.

One run:

1. takes an exclusive lock (a second concurrent run exits ``locked``);
2. puts jobs a crashed run left ``running`` back to ``pending`` (resumable);
3. ingests new records incrementally (the store's own ingest, via the adapter);
4. scans for sessions that changed and have been idle for ``idle_minutes``,
   and queues per-session jobs keyed by the session's fingerprint:
   ``chunk_summary`` (one per ~8k-token chunk), ``session_summary``,
   ``memory_claims`` (verify-first, staged as dream candidates) and
   ``episode`` (handed to the skill-candidate step through the ``episodes``
   table; skill extraction itself lives elsewhere);
5. runs pending jobs within the budgets (sessions, tokens, seconds); whatever
   is left stays queued for the next run.

Job ids are content keys, so re-running never repeats finished work. All
state is local (``~/.local/share/memfw/conversations-work.sqlite``, 0600) and
holds only redacted text: summaries are validated and redacted again before
they are written. The run report carries counts and codes, never text.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .adapter import Ingestor, data_dir, synced_dirs
from .backends import PROMPT_VERSION, ModelBackend, estimate_tokens, parse_reply
from .policy import SharePolicy
from .store import (
    ConversationError,
    ConversationSource,
    SessionInfo,
    Turn,
    check_private_location,
    ensure_private_dir,
    ensure_private_file,
    safe_text,
)

STATE_NAME = "conversations-work.sqlite"
LOCK_NAME = "conversations-work.lock"
SCHEMA_VERSION = "1"
CHUNK_CHARS = 32_000
"""About 8k tokens of redacted text per chunk summary."""
TURN_INPUT_CHARS = 4_000
MAX_ATTEMPTS = 3
SCAN_PAGE = 200
VERBATIM_CHARS = 40
KIND_ORDER = {"chunk_summary": 0, "session_summary": 1, "memory_claims": 2, "episode": 3}

Judge = Callable[[str, str], str]


def state_path(config: Mapping[str, Any] | None, *, vault: Path | None = None) -> Path:
    section = (config or {}).get("conversation_worker") or {}
    if not isinstance(section, Mapping):
        raise ConversationError("conversation_worker must be a mapping")
    raw = section.get("state_path")
    path = Path(str(raw)).expanduser() if raw else data_dir() / STATE_NAME
    return check_private_location(path, vault=vault, extra_synced=synced_dirs(config))


@dataclass
class Budgets:
    max_sessions: int = 20
    max_tokens: int = 200_000
    max_seconds: float = 120.0
    max_scan: int = 2_000
    idle_minutes: float = 30.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None, **overrides: Any) -> Budgets:
        section = (config or {}).get("conversation_worker") or {}
        values: dict[str, Any] = {}
        for name in ("max_sessions", "max_tokens", "max_seconds", "max_scan", "idle_minutes"):
            raw = overrides.get(name)
            if raw is None and isinstance(section, Mapping):
                raw = section.get(name)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
                raise ConversationError(f"conversation_worker.{name} must be a number >= 0")
            values[name] = raw
        return cls(**values)


@dataclass
class RunReport:
    status: str = "ok"
    run_id: str = ""
    backend: str = ""
    ingest: dict[str, Any] = field(default_factory=dict)
    sessions_scanned: int = 0
    sessions_queued: int = 0
    sessions_waiting_idle: int = 0
    jobs: dict[str, int] = field(default_factory=dict)
    tokens: int = 0
    candidates_staged: int = 0
    episodes_queued: int = 0
    purged_apps: list[str] = field(default_factory=list)
    stopped: str = ""
    errors: dict[str, int] = field(default_factory=dict)

    def bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def fingerprint(info: SessionInfo) -> str:
    return f"{info.turns}:{info.last_offset}:{info.updated_at}"


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WorkState:
    """The worker's local SQLite state: jobs, summaries, episodes, runs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def open(self) -> Iterator[sqlite3.Connection]:
        ensure_private_dir(self.path.parent)
        ensure_private_file(self.path)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.row_factory = sqlite3.Row
            self._schema(connection)
            yield connection

    @contextmanager
    def open_readonly(self) -> Iterator[sqlite3.Connection | None]:
        if not self.path.is_file() or self.path.is_symlink():
            yield None
            return
        uri = f"file:{quote(str(self.path))}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS session_state (
                app TEXT NOT NULL, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                queued_at TEXT NOT NULL, PRIMARY KEY (app, session_id));
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, app TEXT NOT NULL,
                session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                chunk_start INTEGER NOT NULL, chunk_end INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created_at);
            CREATE INDEX IF NOT EXISTS jobs_session ON jobs (app, session_id, fingerprint);
            CREATE TABLE IF NOT EXISTS summaries (
                app TEXT NOT NULL, session_id TEXT NOT NULL, chunk_start INTEGER NOT NULL,
                chunk_end INTEGER NOT NULL, prompt_version TEXT NOT NULL, model TEXT NOT NULL,
                summary TEXT NOT NULL, intent TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (app, session_id, chunk_start, chunk_end, prompt_version));
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY, app TEXT NOT NULL, session_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, intent TEXT NOT NULL, outcome TEXT NOT NULL,
                first_offset INTEGER NOT NULL, last_offset INTEGER NOT NULL,
                created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                claimed_by TEXT NOT NULL DEFAULT '', claimed_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS staged_claims (
                claim_digest TEXT PRIMARY KEY, app TEXT NOT NULL, session_id TEXT NOT NULL,
                dream_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}');
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    # -- read helpers used by the share layer and the CLI --------------------------

    def session_summary(self, app: str, session_id: str) -> str | None:
        with self.open_readonly() as connection:
            if connection is None:
                return None
            try:
                row = connection.execute(
                    "SELECT summary FROM summaries WHERE app = ? AND session_id = ?"
                    " AND chunk_start = -1 ORDER BY chunk_end DESC, created_at DESC LIMIT 1",
                    (app, session_id),
                ).fetchone()
            except sqlite3.Error:
                return None
        return str(row["summary"]) if row else None

    def status(self) -> dict[str, Any]:
        with self.open_readonly() as connection:
            if connection is None:
                return {"state": "absent"}
            jobs = {
                f"{row['kind']}:{row['status']}": int(row["n"])
                for row in connection.execute(
                    "SELECT kind, status, COUNT(*) AS n FROM jobs GROUP BY kind, status"
                )
            }
            episodes = {
                str(row["status"]): int(row["n"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status"
                )
            }
            last = connection.execute(
                "SELECT run_id, started_at, finished_at, status FROM runs"
                " ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return {
            "state": "present",
            "jobs": jobs,
            "episodes": episodes,
            "last_run": dict(last) if last else None,
        }


class EpisodeQueue:
    """Hand-off of processed sessions to the skill-candidate step.

    The producer is the worker (``episode`` jobs). A consumer claims pending
    episodes, reads the referenced session through the store, and acks them.
    Episodes carry ids, offsets, intent and outcome only, never turn text.
    """

    def __init__(self, state: WorkState) -> None:
        self.state = state

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.state.open() as connection:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE status = 'pending' ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim(self, consumer: str, limit: int = 10, *, now: str | None = None) -> list[str]:
        stamp = now or datetime.now(UTC).isoformat()
        with self.state.open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ids = [
                str(row["episode_id"])
                for row in connection.execute(
                    "SELECT episode_id FROM episodes WHERE status = 'pending'"
                    " ORDER BY created_at LIMIT ?",
                    (max(1, int(limit)),),
                )
            ]
            connection.executemany(
                "UPDATE episodes SET status = 'claimed', claimed_by = ?, claimed_at = ?"
                " WHERE episode_id = ?",
                [(consumer[:64], stamp, episode_id) for episode_id in ids],
            )
        return ids

    def ack(self, episode_id: str) -> bool:
        with self.state.open() as connection:
            cursor = connection.execute(
                "UPDATE episodes SET status = 'done' WHERE episode_id = ? AND status = 'claimed'",
                (episode_id,),
            )
        return cursor.rowcount == 1


@contextmanager
def run_lock(path: Path) -> Iterator[bool]:
    """``True`` while this process holds the worker lock, ``False`` if another does."""

    ensure_private_dir(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _chunks(turns: Sequence[Turn]) -> list[list[Turn]]:
    chunks: list[list[Turn]] = []
    current: list[Turn] = []
    size = 0
    for turn in turns:
        current.append(turn)
        size += min(len(turn.text), TURN_INPUT_CHARS)
        if size >= CHUNK_CHARS:
            chunks.append(current)
            current, size = [], 0
    if current:
        chunks.append(current)
    return chunks


def _shares_verbatim(claim: str, text: str, length: int = VERBATIM_CHARS) -> bool:
    if len(claim) < length:
        return False
    lowered = text.lower()
    claim_lowered = claim.lower()
    return any(
        claim_lowered[start : start + length] in lowered
        for start in range(0, len(claim_lowered) - length + 1)
    )


class ConversationWorker:
    def __init__(
        self,
        source: ConversationSource,
        state: WorkState,
        *,
        backend: ModelBackend,
        policy: SharePolicy,
        budgets: Budgets | None = None,
        ingestor: Ingestor | None = None,
        vault: Path | None = None,
        judge: Judge | None = None,
        lock_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.state = state
        self.backend = backend
        self.policy = policy
        self.budgets = budgets or Budgets()
        self.ingestor = ingestor
        self.vault = vault
        self.judge = judge
        self.lock_path = lock_path or state.path.with_name(LOCK_NAME)
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic

    # -- entry point --------------------------------------------------------------

    def run(self) -> RunReport:
        report = RunReport(backend=self.backend.name)
        with run_lock(self.lock_path) as held:
            if not held:
                report.status = "locked"
                return report
            started = self.monotonic()
            stamp = self.now().isoformat()
            report.run_id = f"run-{_digest(stamp, os.getpid(), os.urandom(8).hex())[:12]}"
            with self.state.open() as connection:
                connection.execute(
                    "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
                    (report.run_id, stamp),
                )
                connection.execute(
                    "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
                    (stamp,),
                )
                report.purged_apps = self._purge(connection)
            if self.ingestor is not None:
                try:
                    report.ingest = self.ingestor()
                except Exception as exc:  # codes only; never the message
                    report.ingest = {"status": "error", "error": type(exc).__name__}
            if self.source.available():
                self._scan(report)
                self._execute(report, started)
            else:
                report.stopped = "store_unavailable"
            with self.state.open() as connection:
                connection.execute(
                    "UPDATE runs SET finished_at = ?, status = ?, report = ? WHERE run_id = ?",
                    (
                        self.now().isoformat(),
                        report.status,
                        json.dumps(report.as_dict(), sort_keys=True),
                        report.run_id,
                    ),
                )
        return report

    # -- opt-out ------------------------------------------------------------------

    def _purge(self, connection: sqlite3.Connection) -> list[str]:
        """Drop derived data of apps that may no longer be processed."""

        apps = {
            str(row["app"])
            for table in ("jobs", "summaries", "episodes", "session_state")
            for row in connection.execute(f"SELECT DISTINCT app FROM {table}")
        }
        purged = sorted(app for app in apps if not self.policy.processable(app))
        for app in purged:
            for table in ("jobs", "summaries", "episodes", "session_state", "staged_claims"):
                connection.execute(f"DELETE FROM {table} WHERE app = ?", (app,))
        return purged

    # -- scan + plan ----------------------------------------------------------------

    def _scan(self, report: RunReport) -> None:
        cutoff = self.now() - timedelta(minutes=self.budgets.idle_minutes)
        exclude = tuple(
            sorted(
                self.policy.optout
                | {app for app, rule in self.policy.apps.items() if not rule.process}
            )
        )
        offset = 0
        queued = 0
        while offset < self.budgets.max_scan and queued < self.budgets.max_sessions:
            page = self.source.sessions(
                exclude=exclude,
                limit=min(SCAN_PAGE, self.budgets.max_scan - offset),
                offset=offset,
            )
            if not page:
                break
            offset += len(page)
            for info in page:
                report.sessions_scanned += 1
                if not self.policy.processable(info.app):
                    continue
                updated = _parse_ts(info.updated_at)
                if updated is not None and updated > cutoff:
                    report.sessions_waiting_idle += 1
                    continue
                if self._plan(info):
                    queued += 1
                    report.sessions_queued += 1
                    if queued >= self.budgets.max_sessions:
                        break

    def _plan(self, info: SessionInfo) -> bool:
        """Queue jobs for ``info`` if its fingerprint is new. Returns whether it queued."""

        mark = fingerprint(info)
        stamp = self.now().isoformat()
        with self.state.open() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM session_state WHERE app = ? AND session_id = ?",
                (info.app, info.session_id),
            ).fetchone()
            if row is not None and row["fingerprint"] == mark:
                return False
            turns = self.source.turns(info.app, info.session_id, start=0, limit=info.turns)
            jobs: list[tuple[str, int, int]] = [
                ("chunk_summary", chunk[0].offset, chunk[-1].offset) for chunk in _chunks(turns)
            ]
            jobs += [
                ("session_summary", -1, info.last_offset),
                ("memory_claims", -1, info.last_offset),
                ("episode", -1, info.last_offset),
            ]
            for kind, chunk_start, chunk_end in jobs:
                # Chunk ids ignore the fingerprint: an unchanged chunk is never redone.
                key = (
                    _digest(kind, info.app, info.session_id, chunk_start, chunk_end, PROMPT_VERSION)
                    if kind == "chunk_summary"
                    else _digest(kind, info.app, info.session_id, mark, PROMPT_VERSION)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO jobs (job_id, kind, app, session_id, fingerprint,"
                    " chunk_start, chunk_end, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        key,
                        kind,
                        info.app,
                        info.session_id,
                        mark,
                        chunk_start,
                        chunk_end,
                        stamp,
                        stamp,
                    ),
                )
                # A chunk reused from an older fingerprint belongs to the newest one too.
                connection.execute("UPDATE jobs SET fingerprint = ? WHERE job_id = ?", (mark, key))
            # Session-level jobs of an older fingerprint are no longer wanted.
            connection.execute(
                "UPDATE jobs SET status = 'superseded', updated_at = ? WHERE app = ?"
                " AND session_id = ? AND fingerprint != ? AND kind != 'chunk_summary'"
                " AND status IN ('pending', 'running')",
                (stamp, info.app, info.session_id, mark),
            )
            connection.execute(
                "INSERT OR REPLACE INTO session_state (app, session_id, fingerprint, queued_at)"
                " VALUES (?, ?, ?, ?)",
                (info.app, info.session_id, mark, stamp),
            )
        return True

    # -- execute ------------------------------------------------------------------------

    def _execute(self, report: RunReport, started: float) -> None:
        sessions_done: set[tuple[str, str]] = set()
        while True:
            if self.monotonic() - started > self.budgets.max_seconds:
                report.stopped = "max_seconds"
                return
            with self.state.open() as connection:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at, app,"
                    " session_id, CASE kind WHEN 'chunk_summary' THEN 0"
                    " WHEN 'session_summary' THEN 1 WHEN 'memory_claims' THEN 2 ELSE 3 END,"
                    " chunk_start LIMIT 50"
                ).fetchall()
            states = [(row, self._ready(row)) for row in rows]
            runnable = [row for row, state in states if state == "run"]
            skipped = any(state == "skipped" for _row, state in states)
            if not runnable:
                if skipped:
                    continue
                return
            progressed = False
            for job in runnable:
                key = (str(job["app"]), str(job["session_id"]))
                if key not in sessions_done and len(sessions_done) >= self.budgets.max_sessions:
                    report.stopped = "max_sessions"
                    return
                if self.monotonic() - started > self.budgets.max_seconds:
                    report.stopped = "max_seconds"
                    return
                outcome = self._run_job(job, report)
                if outcome == "budget":
                    report.stopped = "max_tokens"
                    return
                sessions_done.add(key)
                progressed = True
            if not progressed:
                return

    def _ready(self, job: sqlite3.Row) -> str:
        """``run``, ``wait`` or ``skipped``: the session summary needs every chunk, the
        rest need the session summary."""

        kind = str(job["kind"])
        if kind == "chunk_summary":
            return "run"
        with self.state.open() as connection:
            needed = "chunk_summary" if kind == "session_summary" else "session_summary"
            blocking = connection.execute(
                "SELECT status FROM jobs WHERE app = ? AND session_id = ? AND fingerprint = ?"
                " AND kind = ?",
                (job["app"], job["session_id"], job["fingerprint"], needed),
            ).fetchall()
            statuses = {str(row["status"]) for row in blocking}
            if not statuses:
                return "run" if kind == "session_summary" else "wait"
            if statuses & {"failed", "skipped"}:
                self._finish(connection, job, "skipped", f"needs_{needed}")
                return "skipped"
            return "run" if statuses == {"done"} else "wait"

    def _finish(
        self, connection: sqlite3.Connection, job: sqlite3.Row, status: str, error: str = ""
    ) -> None:
        connection.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
            (status, error, self.now().isoformat(), job["job_id"]),
        )

    def _run_job(self, job: sqlite3.Row, report: RunReport) -> str:
        kind = str(job["kind"])
        with self.state.open() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?"
                " WHERE job_id = ?",
                (self.now().isoformat(), job["job_id"]),
            )
        try:
            if kind == "chunk_summary":
                result = self._chunk_summary(job, report)
            elif kind == "session_summary":
                result = self._session_summary(job, report)
            elif kind == "memory_claims":
                result = self._memory_claims(job, report)
            else:
                result = self._episode(job, report)
        except ConversationError as exc:
            result = ("error", str(exc)[:80])
        except Exception as exc:  # backend failure: record the kind only
            result = ("error", type(exc).__name__)
        status, code = result
        with self.state.open() as connection:
            if status == "budget":
                connection.execute(
                    "UPDATE jobs SET status = 'pending', attempts = attempts - 1 WHERE job_id = ?",
                    (job["job_id"],),
                )
                return "budget"
            if status == "error":
                attempts = int(job["attempts"]) + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                report.bump(report.errors, code)
            self._finish(connection, job, status, code)
        report.bump(report.jobs, f"{kind}:{status}")
        return status

    # -- model calls ----------------------------------------------------------------------

    def _call(
        self, task: str, data: dict[str, Any], report: RunReport
    ) -> tuple[dict[str, Any] | None, int, int, str]:
        """``(parsed, tokens_in, tokens_out, code)``; code is ``budget`` when over."""

        estimate = estimate_tokens(json.dumps(data, ensure_ascii=False))
        if report.tokens + estimate > self.budgets.max_tokens:
            return None, 0, 0, "budget"
        reply = self.backend.run(task, data, max_tokens=1024)
        tokens_in = reply.tokens_in or estimate
        tokens_out = reply.tokens_out or estimate_tokens(reply.text)
        report.tokens += tokens_in + tokens_out
        parsed = parse_reply(task, reply.text)
        return parsed, tokens_in, tokens_out, "" if parsed is not None else "bad_output"

    def _store_summary(
        self, job: sqlite3.Row, parsed: dict[str, Any], tokens_in: int, tokens_out: int
    ) -> None:
        with self.state.open() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO summaries (app, session_id, chunk_start, chunk_end,"
                " prompt_version, model, summary, intent, outcome, created_at, tokens_in,"
                " tokens_out) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["app"],
                    job["session_id"],
                    job["chunk_start"],
                    job["chunk_end"],
                    PROMPT_VERSION,
                    self.backend.name,
                    parsed["summary"],
                    parsed.get("intent", ""),
                    parsed.get("outcome", ""),
                    self.now().isoformat(),
                    tokens_in,
                    tokens_out,
                ),
            )

    def _session_turns(self, job: sqlite3.Row) -> list[Turn]:
        info = self.source.session(str(job["app"]), str(job["session_id"]))
        if info is None:
            raise ConversationError("session_gone")
        return self.source.turns(info.app, info.session_id, start=0, limit=info.turns)

    def _chunk_summary(self, job: sqlite3.Row, report: RunReport) -> tuple[str, str]:
        turns = [
            turn
            for turn in self._session_turns(job)
            if int(job["chunk_start"]) <= turn.offset <= int(job["chunk_end"])
        ]
        data_turns = []
        for turn in turns:
            text, withheld = safe_text(turn.text, TURN_INPUT_CHARS)
            if not withheld and text and turn.role in {"user", "assistant", "summary"}:
                data_turns.append({"role": turn.role, "text": text})
        parsed, tokens_in, tokens_out, code = self._call(
            "chunk_summary", {"turns": data_turns}, report
        )
        if code == "budget":
            return "budget", ""
        if parsed is None:
            return "error", code
        self._store_summary(job, parsed, tokens_in, tokens_out)
        return "done", ""

    def _chunk_summaries(self, job: sqlite3.Row) -> list[str]:
        with self.state.open() as connection:
            rows = connection.execute(
                "SELECT s.summary FROM jobs j JOIN summaries s ON s.app = j.app"
                " AND s.session_id = j.session_id AND s.chunk_start = j.chunk_start"
                " AND s.chunk_end = j.chunk_end AND s.prompt_version = ?"
                " WHERE j.app = ? AND j.session_id = ? AND j.fingerprint = ?"
                " AND j.kind = 'chunk_summary' ORDER BY j.chunk_start",
                (PROMPT_VERSION, job["app"], job["session_id"], job["fingerprint"]),
            ).fetchall()
        return [str(row["summary"]) for row in rows]

    def _session_row(self, job: sqlite3.Row) -> sqlite3.Row | None:
        with self.state.open() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM summaries WHERE app = ? AND session_id = ? AND chunk_start = -1"
                " AND chunk_end = ? AND prompt_version = ?",
                (job["app"], job["session_id"], job["chunk_end"], PROMPT_VERSION),
            ).fetchone()
        return row

    def _session_summary(self, job: sqlite3.Row, report: RunReport) -> tuple[str, str]:
        first_user = next(
            (turn for turn in self._session_turns(job) if turn.role == "user" and turn.text),
            None,
        )
        first_text = safe_text(first_user.text, 1000)[0] if first_user else ""
        if first_text.startswith("[withheld"):
            first_text = ""
        data = {"first_user": first_text, "chunks": self._chunk_summaries(job)}
        parsed, tokens_in, tokens_out, code = self._call("session_summary", data, report)
        if code == "budget":
            return "budget", ""
        if parsed is None:
            return "error", code
        self._store_summary(job, parsed, tokens_in, tokens_out)
        return "done", ""

    def _memory_claims(self, job: sqlite3.Row, report: RunReport) -> tuple[str, str]:
        if self.vault is None:
            return "skipped", "no_vault"
        summary = self._session_row(job)
        if summary is None:
            return "skipped", "no_summary"
        parsed, _tokens_in, _tokens_out, code = self._call(
            "memory_claims", {"summary": str(summary["summary"])}, report
        )
        if code == "budget":
            return "budget", ""
        if parsed is None:
            return "error", code
        turns = self._session_turns(job)
        evidence = [(turn, safe_text(turn.text)) for turn in turns]
        verified = []
        for claim in parsed["claims"]:
            supporting = self._verify(claim, evidence)
            if supporting is not None:
                verified.append((claim, supporting))
        if not verified:
            return "done", "no_verified_claims"
        staged = self._stage(job, verified)
        report.candidates_staged += staged
        return "done", ""

    def _verify(self, claim: str, evidence: Sequence[tuple[Turn, tuple[str, bool]]]) -> Turn | None:
        """Verify-first: a supporting turn, no contradicting one, and no copied text."""

        from memfw.dream.verify import classify

        support: Turn | None = None
        for turn, (text, withheld) in evidence:
            if withheld or not text:
                continue
            if _shares_verbatim(claim, text):
                return None
            if self.judge is not None:
                try:
                    polarity = self.judge(claim, text)
                except Exception:
                    continue
            else:
                polarity, _ = classify(claim, text)
            if polarity == "contradict":
                return None
            if polarity == "support" and support is None:
                support = turn
        return support

    def _stage(self, job: sqlite3.Row, verified: Sequence[tuple[str, Turn]]) -> int:
        from memfw.dream.model import Candidate, EvidenceRef
        from memfw.dream.stage import DreamExistsError, stage

        assert self.vault is not None
        app, session_id = str(job["app"]), str(job["session_id"])
        fresh: list[tuple[str, str, Turn]] = []
        with self.state.open() as connection:
            for claim, turn in verified:
                claim_digest = _digest(app, claim.lower())
                seen = connection.execute(
                    "SELECT 1 FROM staged_claims WHERE claim_digest = ?", (claim_digest,)
                ).fetchone()
                if seen is None:
                    fresh.append((claim_digest, claim, turn))
        if not fresh:
            return 0
        dream_id = f"conv-{_digest(app, session_id, job['fingerprint'])[:16]}"
        candidates = [
            Candidate(
                operation="distill",
                action="distill",
                memory_ids=[],
                reason=claim,
                oid=f"conversation-{app}",
                evidence=[
                    EvidenceRef(
                        ref=f"conv:{turn.ref}",
                        polarity="support",
                        session_id=session_id,
                        timestamp=turn.ts or None,
                    )
                ],
            )
            for _digest_value, claim, turn in fresh
        ]
        report = {
            "kind": "conversation-memory-candidates",
            "backend": self.backend.name,
            "prompt_version": PROMPT_VERSION,
            "evidence_refs": "conv:APP:SESSION@OFFSET, read with memfw conversations get",
            "candidates": len(candidates),
        }
        try:
            stage(self.vault, dream_id, candidates, report, created_at=self.now().isoformat())
        except DreamExistsError:
            pass  # staged by an earlier, interrupted run: idempotent
        with self.state.open() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO staged_claims (claim_digest, app, session_id, dream_id,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (claim_digest, app, session_id, dream_id, self.now().isoformat())
                    for claim_digest, _claim, _turn in fresh
                ],
            )
        return len(fresh)

    def _episode(self, job: sqlite3.Row, report: RunReport) -> tuple[str, str]:
        summary = self._session_row(job)
        if summary is None:
            return "skipped", "no_summary"
        info = self.source.session(str(job["app"]), str(job["session_id"]))
        if info is None:
            return "skipped", "session_gone"
        episode_id = _digest("episode", info.app, info.session_id, job["fingerprint"])
        with self.state.open() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO episodes (episode_id, app, session_id, fingerprint,"
                " intent, outcome, first_offset, last_offset, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    info.app,
                    info.session_id,
                    job["fingerprint"],
                    str(summary["intent"]),
                    str(summary["outcome"]),
                    info.first_offset,
                    info.last_offset,
                    self.now().isoformat(),
                ),
            )
        if cursor.rowcount:
            report.episodes_queued += 1
        return "done", ""
