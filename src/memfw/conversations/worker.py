"""``memfw conversations worker``: asynchronous, idempotent processing of the store.

One run:

1. takes an exclusive lock (a second concurrent run exits ``locked``);
2. puts jobs a crashed run left ``running`` back to ``pending`` (resumable);
3. ingests new records incrementally (the store's own ingest, via the adapter);
4. scans for sessions that changed and have been idle for ``idle_minutes``
   (a session whose turns carry no timestamp is idle only once the worker has
   seen it unchanged for ``idle_minutes``: unknown activity is never taken as
   idle, since the store records no file mtime to fall back on), and queues
   per-session jobs keyed by the session's fingerprint:
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

Jobs and summaries carry the backend's prompt version. A ``conv-v2`` backend
(summary plus tags, :mod:`memfw.ports.models`) writes the ``tags`` column;
``conv-v1`` rows stay. Under a version other than ``conv-v1`` it plans
sessions it never planned, sessions that changed, and unchanged sessions
updated within the lookback window (``DEFAULT_LOOKBACK_DAYS``, or
``backfill_days``); older history is not re-processed unless asked for, and
its jobs run newest session first.
``memory_claims`` always runs on a ``conv-v1`` backend: the configured one
when it accepts the task, else the extractive one. A configured backend that
was refused (see :func:`memfw.conversations.backends.configured_backend`)
gives a ``degraded`` run that processes nothing.
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

from ..history.injected import has_content, strip_structural
from ..ports.claims import ClaimEvidence, ClaimStager, ClaimVerifier, StagedClaim
from ..ports.models import (
    TAG_PROMPT_VERSION,
    TAG_TASKS,
    BackendBudgetExhausted,
    BackendUnavailable,
    map_project_names,
)
from .adapter import Ingestor, checked_location, data_dir
from .backends import (
    OUTPUT_ISSUE_CODES,
    PROMPT_VERSION,
    ExtractiveBackend,
    ModelBackend,
    accepts,
    defaulted_tag_keys,
    estimate_tokens,
    parse_reply,
    prompt_version_of,
    reply_issue,
)
from .policy import SharePolicy
from .store import (
    ConversationError,
    ConversationSource,
    SessionInfo,
    Turn,
    ensure_private_dir,
    ensure_private_file,
    safe_text,
    session_fingerprint,
    ts_key,
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
#: Backend codes that stop the run and leave the job queued (``max_tokens`` is the
#: worker's own budget): the backend's budget is spent, or it became unusable.
BACKEND_STOPS = ("backend_budget", "backend_unavailable", "backend_deadline")
DEFAULT_LOOKBACK_DAYS = 30.0
"""Under a new prompt version, unchanged sessions older than this are not re-planned
(a backfill needs ``conversations.backfill_days`` or ``--backfill-days``)."""
DEFAULT_REPLAN_LIMIT = 1_000
#: Share of the token budget kept for sessions that have not started, when both
#: already-started work and new sessions are waiting. 0.25 means continuation
#: may spend the first 75 percent; the rest starts a new session. With no new
#: session waiting, continuation may use the whole budget.
NEW_SESSION_FLOOR = 0.25
"""Under a new prompt version, only the newest this many sessions may be re-planned
(the M3d pilot size); ``backfill_days`` lifts the cap, ``replan_limit`` sets it."""

Judge = Callable[[str, str], str]


def state_path(config: Mapping[str, Any] | None, *, vault: Path | None = None) -> Path:
    section = (config or {}).get("conversation_worker") or {}
    if not isinstance(section, Mapping):
        raise ConversationError("conversation_worker must be a mapping")
    raw = section.get("state_path")
    path = Path(str(raw)).expanduser() if raw else data_dir() / STATE_NAME
    return checked_location(path, config, vault=vault)


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
    scan_cursor_start: int = 0
    """Where this run resumed scanning (after the newest-first head window)."""
    scan_cursor_end: int = 0
    """Where the next run resumes."""
    scan_wrapped: bool = False
    """This run reached the oldest session and wrapped to the top."""
    jobs: dict[str, int] = field(default_factory=dict)
    tokens: int = 0
    candidates_staged: int = 0
    episodes_queued: int = 0
    purged_apps: int = 0
    candidates_to_review: list[str] = field(default_factory=list)
    """Dream ids staged earlier for apps that are now excluded: review or reject them."""
    stopped: str = ""
    errors: dict[str, int] = field(default_factory=dict)
    sessions_summarised: int = 0
    """Sessions whose whole-session summary was stored during this run."""
    project_unmapped: int = 0
    """Project tag values that matched no canonical name or alias."""
    tag_key_defaulted: dict[str, int] = field(default_factory=dict)
    """How many kept conv-v2 replies omitted each tag key. Key names only."""
    replan_held_back: int = 0
    """Already-processed sessions a new prompt version did not re-plan this run (older
    than the lookback, or past the re-plan cap). Reported only when not 0."""

    def bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if not out["replan_held_back"]:
            del out["replan_held_back"]  # conv-v1 runs keep their exact report shape
        if not out["tag_key_defaulted"]:
            del out["tag_key_defaulted"]  # absent when no kept reply omitted a tag key
        return out


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def fingerprint(info: SessionInfo) -> str:
    """The session's job key; the store's ``archived_fingerprint`` uses the same value."""

    return session_fingerprint(info.turns, info.last_offset, info.updated_at)


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
                updated_at TEXT NOT NULL, tried_run TEXT NOT NULL DEFAULT '');
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
            CREATE TABLE IF NOT EXISTS scan_cursor (
                name TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0,
                passes INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '',
                last_updated_at TEXT NOT NULL DEFAULT '', last_app TEXT NOT NULL DEFAULT '',
                last_session_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS unstamped_seen (
                app TEXT NOT NULL, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                first_seen TEXT NOT NULL, PRIMARY KEY (app, session_id));
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}');
            """
        )
        # Additive migration only: a state file from an earlier build gains new columns.
        job_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")}
        if "tried_run" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN tried_run TEXT NOT NULL DEFAULT ''")
        if "prompt_version" not in job_columns:
            # Every job queued before this column existed was a conv-v1 job.
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN prompt_version TEXT NOT NULL DEFAULT 'conv-v1'"
            )
        if "session_ts" not in job_columns:
            # The session's last activity (UTC key) when queued: newest-first order.
            connection.execute("ALTER TABLE jobs ADD COLUMN session_ts TEXT NOT NULL DEFAULT ''")
        summary_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(summaries)")
        }
        if "tags" not in summary_columns:
            # conv-v2 tags as JSON; NULL for conv-v1 rows (existing rows are not touched).
            connection.execute("ALTER TABLE summaries ADD COLUMN tags TEXT")
        cursor_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(scan_cursor)")
        }
        for column in ("last_updated_at", "last_app", "last_session_id"):
            if column not in cursor_columns:
                connection.execute(
                    f"ALTER TABLE scan_cursor ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
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

    def session_labels(
        self,
        *,
        keys: Sequence[tuple[str, str]] | None = None,
        apps: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        filters: Mapping[str, str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None:
        """Each session's label: its newest session summary (the one ``session_summary`` reads).

        Rows ``{"app", "session_id", "summary", "tags", "prompt_version"}``,
        ``tags`` the raw JSON text or ``None`` (a ``conv-v1`` row), newest first,
        at most ``limit``. ``keys`` restricts to those sessions; ``apps`` /
        ``exclude`` to the app scope. ``filters`` (``task_type``, ``outcome``,
        ``project``, ``component``, ``intent_like``; values already normalised)
        keep only sessions whose newest summary is tagged and matches all of
        them (an older tagged row behind a newer untagged one does not count).
        ``None`` when there is no state file (nothing was ever processed); an
        unreadable one gives ``[]``.
        """

        clauses = ["chunk_start = -1"]
        params: list[Any] = []
        if apps is not None:
            if not apps:
                return []
            clauses.append(f"app IN ({','.join('?' * len(apps))})")
            params.extend(apps)
        if exclude:
            clauses.append(f"app NOT IN ({','.join('?' * len(exclude))})")
            params.extend(exclude)
        if keys is not None:
            if not keys:
                return []
            clauses.append("(" + " OR ".join("(app = ? AND session_id = ?)" for _ in keys) + ")")
            params.extend(value for key in keys for value in key)
        outer = ["n = 1"]
        outer_params: list[Any] = []
        wanted = dict(filters or {})
        if wanted:
            outer.append("tags IS NOT NULL AND json_valid(tags)")
        for name, path in (("task_type", "$.task_type"), ("outcome", "$.outcome")):
            if name in wanted:
                outer.append(f"json_extract(tags, '{path}') = ?")
                outer_params.append(wanted[name])
        for name, path in (("project", "$.project"), ("component", "$.components")):
            if name in wanted:
                outer.append(f"EXISTS (SELECT 1 FROM json_each(tags, '{path}') WHERE value = ?)")
                outer_params.append(wanted[name])
        if "intent_like" in wanted:
            outer.append("instr(lower(COALESCE(json_extract(tags, '$.intent'), '')), ?) > 0")
            outer_params.append(wanted["intent_like"])
        sql = (
            "WITH best AS (SELECT app, session_id, summary, tags, prompt_version, created_at,"
            " ROW_NUMBER() OVER (PARTITION BY app, session_id ORDER BY"
            " chunk_end DESC, created_at DESC) AS n FROM summaries"
            f" WHERE {' AND '.join(clauses)})"
            " SELECT app, session_id, summary, tags, prompt_version FROM best"
            f" WHERE {' AND '.join(outer)}"
            " ORDER BY created_at DESC, app, session_id LIMIT ?"
        )
        with self.open_readonly() as connection:
            if connection is None:
                return None
            try:
                rows = connection.execute(
                    sql, [*params, *outer_params, max(0, int(limit))]
                ).fetchall()
            except sqlite3.Error:
                return []  # an old or damaged state file matches nothing
        return [dict(row) for row in rows]

    def status(self, *, now: datetime | None = None, runs: int = 20) -> dict[str, Any]:
        """Job counts plus recent run codes. No summary text.

        ``summaries_24h`` counts rows written in the 24 hours before ``now``,
        by ``prompt_version``. ``recent_runs`` covers the last ``runs`` runs:
        how many were degraded or refused, and which reason codes they stored.
        ``bad_output_share`` is output-issue codes divided by those codes plus
        chunk and session summaries stored in that same window of runs.
        """

        moment = now or datetime.now(UTC)
        limit = max(1, min(100, int(runs)))
        cutoff = (moment - timedelta(hours=24)).isoformat()
        activity: dict[str, Any] = {
            "summaries_24h": {},
            "recent_runs": {"n": limit, "degraded_or_refused": 0, "reasons": {}},
            "bad_output_share": 0.0,
        }
        with self.open_readonly() as connection:
            if connection is None:
                return {"state": "absent", **activity}
            try:
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
                written = connection.execute(
                    "SELECT prompt_version, COUNT(*) AS n FROM summaries"
                    " WHERE created_at >= ? GROUP BY prompt_version",
                    (cutoff,),
                ).fetchall()
                recent = connection.execute(
                    "SELECT status, report FROM runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.Error:
                return {
                    "state": "present",
                    "jobs": {},
                    "episodes": {},
                    "last_run": None,
                    **activity,
                }
        summaries_24h = {
            str(row["prompt_version"]): int(row["n"])
            for row in written
            if row["prompt_version"]
        }
        degraded = 0
        reasons: dict[str, int] = {}
        bad = 0
        good = 0
        for row in recent:
            try:
                payload = json.loads(row["report"] or "{}")
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            raw_errors = payload.get("errors")
            errors: dict[Any, Any] = raw_errors if isinstance(raw_errors, dict) else {}
            stopped = str(payload.get("stopped") or "")
            if row["status"] == "degraded" or stopped in {"backend_refused", "backend_unavailable"}:
                degraded += 1
            for key, count in errors.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                ):
                    continue
                code = key[:64]
                if code in OUTPUT_ISSUE_CODES or code.startswith("unavailable:") or code.startswith(
                    "backend_"
                ):
                    reasons[code] = reasons.get(code, 0) + count
                if code in OUTPUT_ISSUE_CODES:
                    bad += count
            if stopped in {"backend_refused", "backend_unavailable"} and stopped not in reasons:
                reasons[stopped] = reasons.get(stopped, 0) + 1
            raw_jobs = payload.get("jobs")
            job_counts: dict[Any, Any] = raw_jobs if isinstance(raw_jobs, dict) else {}
            for name in ("chunk_summary:done", "session_summary:done"):
                value = job_counts.get(name, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    good += value
        share = round(bad / (bad + good), 4) if bad + good else 0.0
        return {
            "state": "present",
            "jobs": jobs,
            "episodes": episodes,
            "last_run": dict(last) if last else None,
            "summaries_24h": summaries_24h,
            "recent_runs": {"n": limit, "degraded_or_refused": degraded, "reasons": reasons},
            "bad_output_share": share,
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
        claimed: list[str] = []
        with self.state.open() as connection:
            candidates = [
                str(row["episode_id"])
                for row in connection.execute(
                    "SELECT episode_id FROM episodes WHERE status = 'pending'"
                    " ORDER BY created_at LIMIT ?",
                    (max(1, int(limit)),),
                )
            ]
            for episode_id in candidates:
                # Conditional update: two consumers never claim the same episode.
                cursor = connection.execute(
                    "UPDATE episodes SET status = 'claimed', claimed_by = ?, claimed_at = ?"
                    " WHERE episode_id = ? AND status = 'pending'",
                    (consumer[:64], stamp, episode_id),
                )
                if cursor.rowcount == 1:
                    claimed.append(episode_id)
        return claimed

    def ack(self, episode_id: str) -> bool:
        with self.state.open() as connection:
            cursor = connection.execute(
                "UPDATE episodes SET status = 'done' WHERE episode_id = ? AND status = 'claimed'",
                (episode_id,),
            )
        return cursor.rowcount == 1

    def release(self, episode_id: str) -> bool:
        """Put a claimed episode back to pending (its consumer failed)."""

        with self.state.open() as connection:
            cursor = connection.execute(
                "UPDATE episodes SET status = 'pending', claimed_by = '', claimed_at = ''"
                " WHERE episode_id = ? AND status = 'claimed'",
                (episode_id,),
            )
        return cursor.rowcount == 1

    def rows(self, episode_ids: Sequence[str]) -> list[dict[str, Any]]:
        """The episodes with these ids, in the given order (unknown ids are skipped)."""

        if not episode_ids:
            return []
        marks = ",".join("?" * len(episode_ids))
        with self.state.open() as connection:
            found = {
                str(row["episode_id"]): dict(row)
                for row in connection.execute(
                    f"SELECT * FROM episodes WHERE episode_id IN ({marks})", list(episode_ids)
                )
            }
        return [found[item] for item in episode_ids if item in found]

    def reclaim(self, before: str) -> int:
        """Return claims taken before ``before`` (a crashed consumer's) to pending."""

        with self.state.open() as connection:
            cursor = connection.execute(
                "UPDATE episodes SET status = 'pending', claimed_by = '', claimed_at = ''"
                " WHERE status = 'claimed' AND claimed_at < ?",
                (before,),
            )
        return int(cursor.rowcount)

    def consumed(self, limit: int = 50) -> list[dict[str, Any]]:
        """Episodes already acked, newest first (consumers that look across runs)."""

        with self.state.open() as connection:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE status = 'done' ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]


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


SESSION_INPUT_CHARS = 24_000


def _bounded(chunks: list[str], limit: int = SESSION_INPUT_CHARS) -> list[str]:
    """Chunk summaries for the session summary: the first and the latest ones that fit."""

    if sum(len(item) for item in chunks) <= limit:
        return chunks
    head = [chunks[0]]
    tail: list[str] = []
    used = len(chunks[0])
    for item in reversed(chunks[1:]):
        if used + len(item) > limit:
            break
        tail.insert(0, item)
        used += len(item)
    return [*head, "(earlier parts omitted)", *tail]


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
        verifier: ClaimVerifier | None = None,
        stager: ClaimStager | None = None,
        lock_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        claims_backend: ModelBackend | None = None,
        backfill_days: float | None = None,
        replan_limit: int | None = None,
        summary_language: str = "zh",
        project_vocab: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        # Claims are verified and staged through ports (memfw.ports.claims); the
        # caller injects them (memfw conversations work: memfw.dream.claims).
        if vault is not None and (stager is None or (judge is None and verifier is None)):
            raise ConversationError(
                "a worker with a vault needs a claim stager and a claim verifier (or judge)"
            )
        self.source = source
        self.state = state
        self.backend = backend
        self.prompt_version = prompt_version_of(backend)
        # memory_claims stays conv-v1 (U4 corroboration reads its claims): a backend
        # that does not take the task, or speaks another version, hands it to the
        # extractive backend.
        if claims_backend is None:
            usable = accepts(backend, "memory_claims") and self.prompt_version == PROMPT_VERSION
            claims_backend = backend if usable else ExtractiveBackend()
        if prompt_version_of(claims_backend) != PROMPT_VERSION:
            raise ConversationError("the claims backend must speak conv-v1")
        self.claims_backend = claims_backend
        if backfill_days is not None and (
            isinstance(backfill_days, bool)
            or not isinstance(backfill_days, (int, float))
            or backfill_days < 0
        ):
            raise ConversationError("backfill_days must be a number >= 0")
        self.lookback_days = (
            DEFAULT_LOOKBACK_DAYS if backfill_days is None else float(backfill_days)
        )
        if replan_limit is not None and (
            isinstance(replan_limit, bool) or not isinstance(replan_limit, int) or replan_limit < 0
        ):
            raise ConversationError("replan_limit must be an integer >= 0")
        # An explicit backfill lifts the default cap; an explicit limit always applies.
        self.replan_limit: int | None = (
            replan_limit
            if replan_limit is not None
            else None
            if backfill_days is not None
            else DEFAULT_REPLAN_LIMIT
        )
        self._replan_floor: str | None = None
        self._held_back = 0
        self._started = 0.0
        self.policy = policy
        self.budgets = budgets or Budgets()
        self.ingestor = ingestor
        self.vault = vault
        self.judge = judge
        self.verifier = verifier
        self.stager = stager
        self.lock_path = lock_path or state.path.with_name(LOCK_NAME)
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        if (
            not isinstance(summary_language, str)
            or not summary_language.strip()
            or len(summary_language.strip()) > 16
        ):
            raise ConversationError("summary_language must be a short string")
        self.summary_language = summary_language.strip().lower()
        self.project_vocab: dict[str, tuple[str, ...]] = {}
        for name, aliases in (project_vocab or {}).items():
            if not isinstance(name, str) or not name.strip():
                raise ConversationError("project vocab names must be strings")
            if isinstance(aliases, str):
                aliases = (aliases,)
            if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
                raise ConversationError("project vocab aliases must be a list of strings")
            self.project_vocab[name.strip()] = tuple(
                item.strip() for item in aliases if isinstance(item, str) and item.strip()
            )
        bind = getattr(self.backend, "bind_labels", None)
        if callable(bind):
            bind(language=self.summary_language, projects=tuple(self.project_vocab))

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
            refused = str(getattr(self.backend, "refused", "") or "")
            if refused:
                return self._degraded(report, stamp, refused)
            with self.state.open() as connection:
                connection.execute(
                    "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
                    (report.run_id, stamp),
                )
                # A run that died mid-job left it running: retry it, unless it keeps dying.
                connection.execute(
                    "UPDATE jobs SET status = CASE WHEN attempts >= ? THEN 'failed'"
                    " ELSE 'pending' END, updated_at = ? WHERE status = 'running'",
                    (MAX_ATTEMPTS, stamp),
                )
                self._purge(connection, report)
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

    def _degraded(self, report: RunReport, stamp: str, code: str) -> RunReport:
        """Record a run that did nothing because the configured backend was refused."""

        report.status = "degraded"
        report.stopped = "backend_refused"
        report.bump(report.errors, code)
        with self.state.open() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, started_at, finished_at, status, report)"
                " VALUES (?, ?, ?, 'degraded', ?)",
                (
                    report.run_id,
                    stamp,
                    self.now().isoformat(),
                    json.dumps(report.as_dict(), sort_keys=True),
                ),
            )
        return report

    # -- opt-out ------------------------------------------------------------------

    def _purge(self, connection: sqlite3.Connection, report: RunReport) -> None:
        """Drop derived data of apps that may no longer be processed.

        Candidates already staged in the vault are not deleted (they are the
        human's review queue); their dream ids are reported for review.
        """

        apps = {
            str(row["app"])
            for table in ("jobs", "summaries", "episodes", "session_state")
            for row in connection.execute(f"SELECT DISTINCT app FROM {table}")
        }
        apps |= {
            str(row["app"]) for row in connection.execute("SELECT DISTINCT app FROM staged_claims")
        }
        purged = sorted(app for app in apps if not self.policy.processable(app))
        review: set[str] = set()
        for app in purged:
            review |= {
                str(row["dream_id"])
                for row in connection.execute(
                    "SELECT DISTINCT dream_id FROM staged_claims WHERE app = ?", (app,)
                )
            }
            for table in (
                "jobs",
                "summaries",
                "episodes",
                "session_state",
                "staged_claims",
                "unstamped_seen",
            ):
                connection.execute(f"DELETE FROM {table} WHERE app = ?", (app,))
        report.purged_apps = len(purged)
        report.candidates_to_review = sorted(review)

    # -- scan + plan ----------------------------------------------------------------

    def _scan(self, report: RunReport) -> None:
        """Queue changed, idle sessions, fairly across runs.

        ``source.sessions`` lists newest first, ordered by ``(updated_at DESC,
        app, session_id)``. Scanning only from the top would starve older
        sessions whenever the backlog exceeds ``max_scan`` (or ``max_sessions``
        is reached first). So each run scans a short head window (fresh sessions
        are picked up promptly) and then resumes from a **keyset cursor**: the
        last session the previous run visited. The walk continues at the first
        session strictly after it in that order. A session that is updated
        rises to the top (into the head window), and nothing after the cursor
        moves, so the cursor walk neither skips nor repeats sessions. Past the
        oldest session it wraps to the newest.
        """

        cutoff = self.now() - timedelta(minutes=self.budgets.idle_minutes)
        exclude = tuple(
            sorted(
                set(self.policy.hidden())
                | {app for app, rule in self.policy.apps.items() if not rule.process}
            )
        )
        budget = int(self.budgets.max_scan)
        # The head window may queue at most half the sessions, and never all of
        # them: at least one is always left for the cursor walk, so a busy top of
        # the list cannot stall the cursor. With max_sessions 1 there is no head
        # window and the one session goes to the cursor walk.
        sessions = int(self.budgets.max_sessions)
        head_quota = min(sessions - 1, max(1, sessions // 2)) if sessions >= 2 else 0
        head = min(SCAN_PAGE, budget // 4) if head_quota else 0
        if sessions <= 0 or budget <= 0:
            return
        self._held_back = 0
        self._replan_floor = self._newest_floor(exclude)
        key, passes = self._scan_cursor()
        start = self._keyset_offset(exclude, key) if key else 0
        seen: set[tuple[str, str]] = set()
        state = {"queued": 0, "scanned": 0}
        last: list[SessionInfo] = []

        def visit(info: SessionInfo, quota: int) -> bool:
            """Handle one session; True when ``quota`` sessions have been queued."""

            key = (info.app, info.session_id)
            if key in seen:
                return state["queued"] >= quota
            seen.add(key)
            report.sessions_scanned += 1
            if not self.policy.processable(info.app):
                return False
            updated = _parse_ts(info.updated_at)
            if updated is None:
                # No usable timestamp: last activity is unknown, so not idle yet.
                updated = self._first_seen_unchanged(info)
            if updated > cutoff:
                report.sessions_waiting_idle += 1
                return False
            if self._plan(info):
                state["queued"] += 1
                report.sessions_queued += 1
            report.replan_held_back = self._held_back
            return state["queued"] >= quota

        def walk(
            offset: int, stop: int | None, quota: int, *, track: bool
        ) -> tuple[int, bool, bool]:
            """Scan from ``offset``; returns (next offset, reached end, quota used)."""

            while state["scanned"] < budget and (stop is None or offset < stop):
                want = min(SCAN_PAGE, budget - state["scanned"])
                if stop is not None:
                    want = min(want, stop - offset)
                page = self.source.sessions(exclude=exclude, limit=want, offset=offset)
                for index, info in enumerate(page):
                    state["scanned"] += 1
                    if track:
                        last[:] = [info]
                    if visit(info, quota):
                        return offset + index + 1, False, True
                offset += len(page)
                if len(page) < want:
                    return offset, True, False
            return offset, False, False

        if head_quota > 0:
            # The head window only: a full quota here just ends it. When the whole
            # window was seen, the cursor walk need not see it again.
            _offset, _end, head_full = walk(0, head, head_quota, track=False)
            if not head_full:
                start = max(start, head)
        report.scan_cursor_start = start
        offset, end, full = walk(start, None, sessions, track=True)
        if end and not full:
            # Past the oldest session: wrap to the newest and use what is left of
            # this run's budget, up to where this run's cursor walk began.
            passes += 1
            report.scan_wrapped = True
            last.clear()
            offset, _end, _full = walk(0, start, sessions, track=True)
            if not last or offset >= start:
                # A whole pass is done: the next run starts again at the top.
                report.scan_cursor_end = 0
                self._save_scan_cursor(None, 0, passes)
                return
        report.scan_cursor_end = offset
        self._save_scan_cursor(last[0] if last else None, offset, passes)

    def _first_seen_unchanged(self, info: SessionInfo) -> datetime:
        """When this run's worker first saw ``info`` in its current shape.

        For a session without timestamps (the store keeps no file mtime) this
        stands in for its last activity: a new or changed fingerprint resets it
        to now, so the session waits ``idle_minutes`` after its last observed
        change before it is processed (conservative: never idle on first sight).
        """

        mark = fingerprint(info)
        now = self.now()
        with self.state.open() as connection:
            row = connection.execute(
                "SELECT fingerprint, first_seen FROM unstamped_seen"
                " WHERE app = ? AND session_id = ?",
                (info.app, info.session_id),
            ).fetchone()
            if row is not None and row["fingerprint"] == mark:
                seen = _parse_ts(str(row["first_seen"]))
                if seen is not None:
                    return seen
            connection.execute(
                "INSERT INTO unstamped_seen (app, session_id, fingerprint, first_seen)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (app, session_id) DO UPDATE SET"
                " fingerprint = excluded.fingerprint, first_seen = excluded.first_seen",
                (info.app, info.session_id, mark, now.isoformat()),
            )
        return now

    @staticmethod
    def _comes_after(info: SessionInfo, key: tuple[str, str, str]) -> bool:
        """Whether ``info`` lies strictly after ``key`` in newest-first order."""

        updated_at, app, session_id = key
        # Compare as the store orders: by the UTC key, not the raw text.
        mine, theirs = ts_key(info.updated_at) or "", ts_key(updated_at) or ""
        if mine != theirs:
            return mine < theirs
        return (info.app, info.session_id) > (app, session_id)

    def _keyset_offset(self, exclude: tuple[str, ...], key: tuple[str, str, str]) -> int:
        """Offset of the first session strictly after ``key`` (binary search, O(log n))."""

        def after(offset: int) -> bool:
            page = self.source.sessions(exclude=exclude, limit=1, offset=offset)
            return not page or self._comes_after(page[0], key)

        if after(0):
            return 0
        low, high = 0, 1
        while not after(high):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if after(middle):
                high = middle
            else:
                low = middle
        return high

    def _scan_cursor(self) -> tuple[tuple[str, str, str] | None, int]:
        with self.state.open() as connection:
            row = connection.execute(
                "SELECT passes, last_updated_at, last_app, last_session_id FROM scan_cursor"
                " WHERE name = 'sessions'"
            ).fetchone()
        if row is None:
            return None, 0
        passes = max(0, int(row["passes"]))
        if not row["last_app"] and not row["last_session_id"]:
            return None, passes
        return (
            str(row["last_updated_at"]),
            str(row["last_app"]),
            str(row["last_session_id"]),
        ), passes

    def _save_scan_cursor(self, last: SessionInfo | None, position: int, passes: int) -> None:
        """Remember the last session visited (``None``: start at the top next run)."""

        values = (last.updated_at, last.app, last.session_id) if last else ("", "", "")
        with self.state.open() as connection:
            connection.execute(
                "INSERT INTO scan_cursor (name, position, passes, updated_at, last_updated_at,"
                " last_app, last_session_id) VALUES ('sessions', ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET position = excluded.position,"
                " passes = excluded.passes, updated_at = excluded.updated_at,"
                " last_updated_at = excluded.last_updated_at, last_app = excluded.last_app,"
                " last_session_id = excluded.last_session_id",
                (position, passes, self.now().isoformat(), *values),
            )

    def _plan(self, info: SessionInfo) -> bool:
        """Queue jobs for ``info`` if its fingerprint is new. Returns whether it queued."""

        mark = fingerprint(info)
        version = self.prompt_version
        # Another prompt version re-plans an unchanged session once (conv-v1 keeps
        # the plain fingerprint, as before).
        planned = mark if version == PROMPT_VERSION else f"{mark}#{version}"
        stamp = self.now().isoformat()
        with self.state.open() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM session_state WHERE app = ? AND session_id = ?",
                (info.app, info.session_id),
            ).fetchone()
            if row is not None and row["fingerprint"] == planned:
                return False
            if (
                row is not None
                and version != PROMPT_VERSION
                and str(row["fingerprint"]).split("#", 1)[0] == mark
                and not self._may_replan(info)
            ):
                # Planned before under another version and unchanged since: old
                # history is not re-processed by a new version (that is a backfill).
                self._held_back += 1
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
                    _digest(kind, info.app, info.session_id, chunk_start, chunk_end, version)
                    if kind == "chunk_summary"
                    else _digest(kind, info.app, info.session_id, mark, version)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO jobs (job_id, kind, app, session_id, fingerprint,"
                    " chunk_start, chunk_end, status, created_at, updated_at, prompt_version,"
                    " session_ts) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
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
                        version,
                        ts_key(info.updated_at) or "",
                    ),
                )
                # A chunk reused from an older fingerprint belongs to the newest one too,
                # and sorts with it (newest-first order must not strand its session jobs).
                connection.execute(
                    "UPDATE jobs SET fingerprint = ?, session_ts = ? WHERE job_id = ?",
                    (mark, ts_key(info.updated_at) or "", key),
                )
            # Jobs of an older fingerprint are no longer wanted (an unfinished chunk
            # whose end moved, session-level jobs); failed chunks get a fresh start.
            connection.execute(
                "UPDATE jobs SET status = 'superseded', updated_at = ? WHERE app = ?"
                " AND session_id = ? AND fingerprint != ? AND status IN ('pending', 'running')",
                (stamp, info.app, info.session_id, mark),
            )
            connection.execute(
                "UPDATE jobs SET status = 'pending', attempts = 0, error = '', updated_at = ?"
                " WHERE app = ? AND session_id = ? AND fingerprint = ? AND kind = 'chunk_summary'"
                " AND status IN ('failed', 'superseded') AND prompt_version = ?",
                (stamp, info.app, info.session_id, mark, version),
            )
            connection.execute(
                "INSERT OR REPLACE INTO session_state (app, session_id, fingerprint, queued_at)"
                " VALUES (?, ?, ?, ?)",
                (info.app, info.session_id, planned, stamp),
            )
        return True

    def _newest_floor(self, exclude: tuple[str, ...]) -> str | None:
        """UTC key of the ``replan_limit``-th newest session (``None``: no cap applies).

        Only sessions at least this new may be re-planned under a new prompt
        version. The floor only moves forward as sessions arrive, so at most
        ``replan_limit`` sessions of the history are ever re-planned.
        """

        if self.prompt_version == PROMPT_VERSION or self.replan_limit is None:
            return None
        if self.replan_limit == 0:
            return "\uffff"  # nothing is new enough
        page = self.source.sessions(exclude=exclude, limit=1, offset=self.replan_limit - 1)
        return (ts_key(page[0].updated_at) or "") if page else None

    def _may_replan(self, info: SessionInfo) -> bool:
        """An unchanged, already-processed session: within the lookback and the cap."""

        updated = _parse_ts(info.updated_at)
        if updated is None:
            return False  # unknown activity: not recent
        if updated < self.now() - timedelta(days=self.lookback_days):
            return False
        floor = self._replan_floor
        return floor is None or (ts_key(info.updated_at) or "") >= floor

    # -- execute ------------------------------------------------------------------------

    def _execute(self, report: RunReport, started: float) -> None:
        self._started = started
        if self.prompt_version == PROMPT_VERSION:
            self._execute_page(report, started)
            return
        self._execute_finish_started(report, started)

    def _execute_page(self, report: RunReport, started: float) -> None:
        """conv-v1 queue order: a page of pending jobs, chunks before summaries."""

        sessions_done: set[tuple[str, str]] = set()
        offset = 0
        while True:
            if self.monotonic() - started > self.budgets.max_seconds:
                report.stopped = "max_seconds"
                return
            with self.state.open() as connection:
                # A job that errored in this run waits for the next one.
                # Only this backend's prompt version: jobs of another wait for theirs.
                # conv-v1 keeps its queue order; another version runs the newest
                # sessions first, so new work never waits behind a backfill.
                first = "" if self.prompt_version == PROMPT_VERSION else "session_ts DESC, "
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = 'pending' AND tried_run != ?"
                    f" AND prompt_version = ? ORDER BY {first}created_at, app, session_id,"
                    " CASE kind WHEN 'chunk_summary' THEN 0 WHEN 'session_summary' THEN 1"
                    " WHEN 'memory_claims' THEN 2 ELSE 3 END, chunk_start LIMIT 50 OFFSET ?",
                    (report.run_id, self.prompt_version, offset),
                ).fetchall()
            states = [(row, self._ready(row)) for row in rows]
            runnable = [row for row, state in states if state == "run"]
            skipped = any(state == "skipped" for _row, state in states)
            if not runnable:
                if skipped:
                    offset = 0
                    continue
                if self.prompt_version != PROMPT_VERSION and len(rows) == 50:
                    # A full page of waiting jobs: look past it instead of stopping
                    # (newest-first order can put waiting jobs on top).
                    offset += len(rows)
                    continue
                return
            offset = 0
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
                if outcome == "budget" or outcome in BACKEND_STOPS:
                    report.stopped = {
                        "budget": "max_tokens",
                        "backend_deadline": "max_seconds",
                    }.get(outcome, outcome)
                    if outcome == "backend_unavailable":
                        report.status = "degraded"
                    return
                sessions_done.add(key)
                progressed = True
            if not progressed:
                return

    def _execute_finish_started(self, report: RunReport, started: float) -> None:
        """Spend the budget on a session already started, then on a new one.

        conv-v2 used to run every runnable chunk in a page of 50 before the next
        query, so a token cap left many sessions with a chunk and almost no
        whole-session summary. One job is picked at a time. A session touched
        in this run is finished (remaining chunks, then the session summary)
        before another session starts. When both leftover sessions and new
        sessions are waiting, continuation stops once
        ``NEW_SESSION_FLOOR`` of the token budget is all that remains, and that
        remainder starts a new session. With nothing new waiting, continuation
        may use the whole budget.
        """

        sessions_done: set[tuple[str, str]] = set()
        focus: tuple[str, str] | None = None
        opened: set[tuple[str, str]] = set()
        while True:
            if self.monotonic() - started > self.budgets.max_seconds:
                report.stopped = "max_seconds"
                return
            picked = self._pick_conv2(report, focus, opened)
            if picked is None:
                return
            if isinstance(picked, str):
                focus = None
                continue
            job, focus = picked
            key = (str(job["app"]), str(job["session_id"]))
            if key not in sessions_done and len(sessions_done) >= self.budgets.max_sessions:
                report.stopped = "max_sessions"
                return
            if self.monotonic() - started > self.budgets.max_seconds:
                report.stopped = "max_seconds"
                return
            outcome = self._run_job(job, report)
            if outcome == "budget" or outcome in BACKEND_STOPS:
                report.stopped = {
                    "budget": "max_tokens",
                    "backend_deadline": "max_seconds",
                }.get(outcome, outcome)
                if outcome == "backend_unavailable":
                    report.status = "degraded"
                return
            sessions_done.add(key)
            if outcome != "done":
                focus = None

    def _pick_conv2(
        self,
        report: RunReport,
        focus: tuple[str, str] | None,
        opened: set[tuple[str, str]],
    ) -> tuple[sqlite3.Row, tuple[str, str]] | str | None:
        """The next conv-v2 job, ``again`` after a skip, or ``None`` when idle."""

        with self.state.open() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'pending' AND tried_run != ?"
                " AND prompt_version = ?",
                (report.run_id, self.prompt_version),
            ).fetchall()
            started = {
                (str(row["app"]), str(row["session_id"]))
                for row in connection.execute(
                    "SELECT DISTINCT app, session_id FROM jobs WHERE kind = 'chunk_summary'"
                    " AND status = 'done' AND prompt_version = ?",
                    (self.prompt_version,),
                )
            }
        if not rows:
            return None
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        newest: dict[tuple[str, str], str] = {}
        for row in rows:
            key = (str(row["app"]), str(row["session_id"]))
            groups.setdefault(key, []).append(row)
            stamp = str(row["session_ts"] or "")
            if stamp > newest.get(key, ""):
                newest[key] = stamp

        def ordered(jobs: list[sqlite3.Row]) -> list[sqlite3.Row]:
            return sorted(
                jobs,
                key=lambda job: (KIND_ORDER.get(str(job["kind"]), 9), int(job["chunk_start"])),
            )

        def first_runnable(jobs: list[sqlite3.Row]) -> tuple[sqlite3.Row | None, bool]:
            skipped = False
            for job in ordered(jobs):
                state = "run" if str(job["kind"]) == "chunk_summary" else self._ready(job)
                if state == "run":
                    return job, skipped
                if state == "skipped":
                    skipped = True
                if state == "wait":
                    return None, skipped
            return None, skipped

        fresh_keys = [key for key in groups if key not in started and key not in opened]
        old = [key for key in groups if key in started or key in opened]
        floor = int(self.budgets.max_tokens * NEW_SESSION_FLOOR) if fresh_keys else 0
        cap = self.budgets.max_tokens - floor
        # A session opened in this run is finished even into the reserved
        # slice. A session left over from earlier stops at that slice so a
        # new session still gets a turn.
        stick = focus in groups and (focus in opened or report.tokens < cap or not fresh_keys)
        if stick and focus is not None:
            job, skipped = first_runnable(groups[focus])
            if job is not None:
                return job, focus
            if skipped and len(groups) == 1:
                return "again"

        fresh = fresh_keys

        def by_newest(keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
            return sorted(keys, key=lambda key: (newest.get(key, ""), key[1]), reverse=True)

        if old and report.tokens < cap:
            order = by_newest(old) + by_newest(fresh)
        else:
            order = by_newest(fresh) + by_newest(old)
        skipped_any = False
        for key in order:
            if key == focus:
                continue
            job, skipped = first_runnable(groups[key])
            skipped_any = skipped_any or skipped
            if job is not None:
                if key not in started:
                    opened.add(key)
                return job, key
        return "again" if skipped_any else None

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
                " AND kind = ? AND prompt_version = ?",
                (job["app"], job["session_id"], job["fingerprint"], needed, self.prompt_version),
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
                return code if code in BACKEND_STOPS else "budget"
            if status == "too_large":
                status = "failed"
                report.bump(report.errors, code)
            if status == "error":
                attempts = int(job["attempts"]) + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                report.bump(report.errors, code)
                connection.execute(
                    "UPDATE jobs SET tried_run = ? WHERE job_id = ?", (report.run_id, job["job_id"])
                )
            self._finish(connection, job, status, code)
        report.bump(report.jobs, f"{kind}:{status}")
        if kind == "session_summary" and status == "done":
            report.sessions_summarised += 1
        return status

    # -- model calls ----------------------------------------------------------------------

    def _call(
        self, task: str, data: dict[str, Any], report: RunReport
    ) -> tuple[dict[str, Any] | None, int, int, str]:
        """``(parsed, tokens_in, tokens_out, code)``.

        ``code`` is ``budget`` when this run's token budget is spent (the job
        waits for the next run) and ``too_large`` when the job alone could never
        fit (it fails instead of blocking the queue).
        """

        estimate = estimate_tokens(json.dumps(data, ensure_ascii=False))
        if estimate > self.budgets.max_tokens:
            return None, 0, 0, "too_large"
        if report.tokens + estimate > self.budgets.max_tokens:
            return None, 0, 0, "budget"
        claims = task == "memory_claims"
        backend = self.claims_backend if claims else self.backend
        version = PROMPT_VERSION if claims else self.prompt_version
        limit = getattr(backend, "limit_time", None)
        if callable(limit):
            # A backend that can bound its own wall time (retries, backoff) gets
            # what is left of this run's max_seconds.
            limit(max(0.0, self.budgets.max_seconds - (self.monotonic() - self._started)))
        try:
            reply = backend.run(task, data, max_tokens=1024)
        except BackendBudgetExhausted:
            # The backend's own per-run token/cost budget: stop; the job waits.
            return None, 0, 0, "backend_budget"
        except BackendUnavailable as exc:
            # The key was rejected, the provider stayed down after its retries, or
            # the run's time is up: stop; the job waits, its attempt not counted.
            if exc.code == "deadline":
                return None, 0, 0, "backend_deadline"
            report.bump(report.errors, f"backend_{exc.code}"[:64])
            return None, 0, 0, "backend_unavailable"
        tokens_in = reply.tokens_in or estimate
        tokens_out = reply.tokens_out or estimate_tokens(reply.text)
        report.tokens += tokens_in + tokens_out
        finish = str(getattr(reply, "finish_reason", "") or "")
        issue = reply_issue(task, reply.text, prompt_version=version, finish_reason=finish)
        if issue:
            return None, tokens_in, tokens_out, issue
        parsed = parse_reply(task, reply.text, prompt_version=version)
        if parsed is None:
            return None, tokens_in, tokens_out, "bad_output"
        if version == TAG_PROMPT_VERSION and task in TAG_TASKS:
            for key in defaulted_tag_keys(reply.text):
                report.bump(report.tag_key_defaulted, key)
        tags = parsed.get("tags")
        if isinstance(tags, dict) and self.project_vocab:
            names, missed = map_project_names(list(tags.get("project") or []), self.project_vocab)
            rewritten = dict(parsed)
            rewritten["tags"] = {**tags, "project": names}
            parsed = rewritten
            report.project_unmapped += missed
        return parsed, tokens_in, tokens_out, ""

    def _store_summary(
        self, job: sqlite3.Row, parsed: dict[str, Any], tokens_in: int, tokens_out: int
    ) -> None:
        if "tags" in parsed:
            self._store_tagged(job, parsed, tokens_in, tokens_out)
            return
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

    def _store_tagged(
        self, job: sqlite3.Row, parsed: dict[str, Any], tokens_in: int, tokens_out: int
    ) -> None:
        with self.state.open() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO summaries (app, session_id, chunk_start, chunk_end,"
                " prompt_version, model, summary, intent, outcome, created_at, tokens_in,"
                " tokens_out, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["app"],
                    job["session_id"],
                    job["chunk_start"],
                    job["chunk_end"],
                    self.prompt_version,
                    self.backend.name,
                    parsed["summary"],
                    parsed.get("intent", ""),
                    parsed.get("outcome", ""),
                    self.now().isoformat(),
                    tokens_in,
                    tokens_out,
                    json.dumps(parsed["tags"], ensure_ascii=False, sort_keys=True),
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
        if code in {"budget", "too_large", *BACKEND_STOPS}:
            return ("budget" if code in BACKEND_STOPS else code), code
        if parsed is None:
            return "error", code
        self._store_summary(job, parsed, tokens_in, tokens_out)
        return "done", ""

    def _chunk_summaries(self, job: sqlite3.Row) -> list[str]:
        with self.state.open() as connection:
            rows = connection.execute(
                "SELECT s.summary FROM jobs j JOIN summaries s ON s.app = j.app"
                " AND s.session_id = j.session_id AND s.chunk_start = j.chunk_start"
                " AND s.chunk_end = j.chunk_end AND s.prompt_version = j.prompt_version"
                " WHERE j.app = ? AND j.session_id = ? AND j.fingerprint = ?"
                " AND j.kind = 'chunk_summary' AND j.prompt_version = ? ORDER BY j.chunk_start",
                (job["app"], job["session_id"], job["fingerprint"], self.prompt_version),
            ).fetchall()
        return [str(row["summary"]) for row in rows]

    def _session_row(self, job: sqlite3.Row) -> sqlite3.Row | None:
        with self.state.open() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM summaries WHERE app = ? AND session_id = ? AND chunk_start = -1"
                " AND chunk_end = ? AND prompt_version = ?",
                (job["app"], job["session_id"], job["chunk_end"], self.prompt_version),
            ).fetchone()
        return row

    def _session_summary(self, job: sqlite3.Row, report: RunReport) -> tuple[str, str]:
        # Intent comes from what was asked: harness-injected text (a leading
        # <system-reminder>, AGENTS.md echoes, <user_query> wrappers ...) is
        # stripped first, and a user turn that is nothing but injected text is
        # skipped (MiniMax opens every session with one).
        first_text = ""
        for turn in self._session_turns(job):
            if turn.role != "user" or not turn.text:
                continue
            text, withheld = safe_text(turn.text)
            if withheld:
                continue
            request = strip_structural(text)
            if has_content(request):
                first_text = safe_text(request, 1000)[0]
                break
        data = {"first_user": first_text, "chunks": _bounded(self._chunk_summaries(job))}
        parsed, tokens_in, tokens_out, code = self._call("session_summary", data, report)
        if code in {"budget", "too_large", *BACKEND_STOPS}:
            return ("budget" if code in BACKEND_STOPS else code), code
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
        if code in {"budget", "too_large", *BACKEND_STOPS}:
            return ("budget" if code in BACKEND_STOPS else code), code
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
                if self.verifier is None:  # checked in __init__; attributes are public
                    raise ConversationError("claim verification needs a claim verifier or judge")
                polarity = self.verifier.polarity(claim, text)
            if polarity == "contradict":
                return None
            if polarity == "support" and support is None:
                support = turn
        return support

    def _stage(self, job: sqlite3.Row, verified: Sequence[tuple[str, Turn]]) -> int:
        assert self.vault is not None and self.stager is not None
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
        # The claims are part of the id: a retry with other claims is a new dream.
        claim_ids = sorted(claim_digest for claim_digest, _claim, _turn in fresh)
        dream_id = f"conv-{_digest(app, session_id, job['fingerprint'], claim_ids)[:16]}"
        claims = [
            StagedClaim(
                claim=claim,
                oid=f"conversation-{app}",
                evidence=(
                    ClaimEvidence(
                        ref=f"conv:{turn.ref}", session_id=session_id, timestamp=turn.ts or None
                    ),
                ),
            )
            for _digest_value, claim, turn in fresh
        ]
        report = {
            "kind": "conversation-memory-candidates",
            "backend": self.claims_backend.name,
            "prompt_version": PROMPT_VERSION,
            "evidence_refs": "conv:APP:SESSION@OFFSET, read with memfw conversations get",
            "candidates": len(claims),
        }
        # False: staged by an earlier, interrupted run (idempotent).
        self.stager.stage_claims(
            self.vault, dream_id, claims, report, created_at=self.now().isoformat()
        )
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
            # One pending episode per session: a newer state replaces an unclaimed one.
            connection.execute(
                "UPDATE episodes SET status = 'superseded' WHERE app = ? AND session_id = ?"
                " AND status = 'pending' AND episode_id != ?",
                (info.app, info.session_id, episode_id),
            )
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
