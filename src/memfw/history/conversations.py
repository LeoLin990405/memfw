"""Incremental, redacted local store of normalised conversation turns.

Readers (:mod:`memfw.history.sources`) yield raw turns; this module is the
only place they are written. Before a write every text field (turn text,
tool-call names and summaries, cwd, project, model) is scanned with
:func:`memfw.history.redact.has_secret`: a hit withholds the whole turn
(``withheld = 1``, empty text and tool calls); otherwise the values are
passed through :func:`redact`. The store never holds an unredacted value and
raw lines are never copied. The location rules live in
:mod:`memfw.history.store_location`.

Writers are serialised by ``<store>.lock`` (:func:`store_writer_lock`): an
ingest waits up to :data:`WRITER_LOCK_WAIT` seconds for it, then fails;
``memfw conversations reredact --apply`` (:mod:`memfw.history.reredact`) takes
it without waiting.

Rows are keyed by ``(agent, session_id, offset)``: re-ingesting the same turn
is a no-op, a changed turn is updated in place. The same session seen under
two roots (a Worker home mirroring it) is stored once; ``conversation_turn_locators``
records every locator that holds each turn, so a full re-read of one copy only
drops a turn no other locator still holds. Per locator a cursor keeps the
store's fingerprint and the last offset seen:

* unchanged fingerprint: skipped;
* append-only reader, same inode, not smaller: re-read from the last stored
  turn (it may have been mid-stream);
* anything else (non-append-only reader, rewrite, truncation, new inode):
  the whole session is re-read first and, only once that read finished,
  offsets that are no longer present are dropped.

Origin is a session label: the root's origin, the reader's
:attr:`ConversationRecord.origin` and a Conductor dispatch marker in the
session's first real prompt (:func:`memfw.history.sources.labels.prompt_origin`)
are merged once per session and written to every turn of it. An incremental
read keeps the label already stored. When :data:`LABELS_VERSION` changes, the
next ingest re-reads every session once (the re-label pass).

A re-read that yields nothing although the locator had rows (store locked,
vanished mid-read) changes nothing and leaves the cursor as it was, at most
:data:`BUSY_RETRIES` times for the same fingerprint: then the stored rows are
kept, the cursor is saved (``sessions_stale``) and the next change re-reads the
whole session. A session file over the reader's size limit
(:class:`~memfw.history.sources.base.SessionTooLarge`) keeps its stored rows and
is not retried until the file changes (``sessions_too_large``).

Text is scrubbed before it is written: lone UTF-16 surrogates (a JavaScript
writer that cut a pair) become U+FFFD, which SQLite can encode. Timestamps
with a UTC offset are stored as UTC ``...Z`` so text comparison orders them.

Settled ingest (``settled_minutes``; absent means every change is ingested as
before). A locator whose last activity is within ``settled_minutes`` of now is
skipped this round (``sessions_unsettled``): no turn is written and its cursor
is left as it was, so the next round reads it again (an archived session that
became active again loses its archive row). Last activity of a locator with a
file of its own is the later of the newest turn timestamp read this round and
the file's mtime (some readers stamp replies with the prompt time or skip
lines without a timestamp); for a session in a shared database (``path#id``
key or a per-session marker) it is the newest turn timestamp, else the mtime.
A timestamp more than :data:`CLOCK_SKEW` in the future is ignored. A locator
holding several sessions settles once all of them are quiet. A forced pass
(re-label, opt-out change) does not wait for unsettled locators: each keeps a
``force:`` marker and is re-read in full once it settles. The
cwd opt-out still applies to an unsettled locator (its stored rows are dropped).

When a settled locator's content is in the store (just written, or unchanged
since an earlier ingest), each of its sessions is archived:
``conversation_archive`` records ``archived_at`` and ``archived_fingerprint``
(:func:`memfw.history.transcripts.session_fingerprint`, the same value the
conversation worker keys its jobs by). A session is archived while that
fingerprint matches its stored turns; new turns change the fingerprint, so it
reads as not archived until the next settled ingest archives it again. The
table and the cursor's ``settled_fingerprint`` column are added on open
(additive, no cursor reset): rows written before them are not archived until
the next settled ingest.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .index import HistoryError
from .injected import has_content, strip_structural
from .redact import has_secret, redact
from .sources.base import (
    ConversationRecord,
    Reader,
    SessionLocator,
    SessionTooLarge,
    StoreRoot,
    utc_timestamp,
)
from .sources.labels import merge_origin, prompt_origin
from .store_location import prepare_store_path
from .transcripts import session_fingerprint, ts_display, ts_key

SCHEMA_VERSION = "2"
"""2: ``conversation_turn_locators`` (turn ownership per locator) was added."""
TEXT_CHARS = 16_000
FIELD_CHARS = 512
SUMMARY_CHARS = 200
OPTOUT_KEY = "conversation_optout"
OPTOUT_MARKER = ".memfw-no-history"
ORIGINS = ("interactive", "worker")
"""Origins of a store root. A session's stored origin may also be ``automated``
or ``subagent`` (:data:`memfw.history.sources.labels.SESSION_ORIGINS`)."""
LABELS_VERSION = "2"
"""Bumped when session labelling (origin, parent) changes: the next ingest of
each agent re-reads every session once, so existing stores are re-labelled."""
BUSY_RETRIES = 3
CLOCK_SKEW = timedelta(minutes=5)
"""Settled ingest: a turn timestamp later than now plus this is not trusted."""
ARCHIVE_TABLE = "conversation_archive"
TS_FUNCTION = "memfw_ts_key"
"""Consecutive empty re-reads of one unchanged fingerprint before ingest gives up."""
LOCK_SUFFIX = ".lock"
"""The store's writer lock file is ``<store>.lock`` (see :func:`store_writer_lock`)."""
WRITER_LOCK_WAIT = 60.0
"""Seconds an ingest waits for another writer (a running re-redaction) before failing."""
_SURROGATES = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class OptOut:
    """Agents and cwd globs whose sessions are never stored."""

    agents: frozenset[str] = frozenset()
    cwd_globs: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> OptOut:
        raw = config.get(OPTOUT_KEY) or {}
        if not isinstance(raw, dict):
            raise HistoryError(f"{OPTOUT_KEY} must be a mapping")
        agents = raw.get("agents") or []
        globs = raw.get("cwd_globs") or []
        if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
            raise HistoryError(f"{OPTOUT_KEY}.agents must be a list of names")
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise HistoryError(f"{OPTOUT_KEY}.cwd_globs must be a list of globs")
        return cls(frozenset(a.strip().lower() for a in agents), tuple(globs))

    def cwd_excluded(self, cwd: str) -> bool:
        if not cwd:
            return False
        if any(fnmatch.fnmatch(cwd, pattern) for pattern in self.cwd_globs):
            return True
        try:
            return (Path(cwd) / OPTOUT_MARKER).exists()
        except OSError:
            return False


def _parse_ts(value: str | None) -> datetime | None:
    """A stored timestamp as an aware datetime (naive values are taken as UTC)."""

    key = ts_key(value) if value else None
    if not key:
        return None
    try:
        parsed = datetime.fromisoformat(key.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _file_time(info: os.stat_result | None, field: str) -> datetime | None:
    """``info.<field>`` (a POSIX time) as a UTC datetime, or None."""

    if info is None:
        return None
    try:
        return datetime.fromtimestamp(getattr(info, field), UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(value: str) -> str:
    """``value`` with lone surrogates replaced by U+FFFD (SQLite cannot encode them)."""

    return _SURROGATES.sub("\ufffd", value) if isinstance(value, str) and value else ""


def _clean(value: str, limit: int = FIELD_CHARS) -> str:
    return redact(value)[:limit] if value else ""


def _fingerprint(path: Path) -> dict[str, int]:
    """Size, mtime and inode of the store (plus its SQLite WAL, when there is one)."""

    out: dict[str, int] = {}
    for label, candidate in (("", path), ("wal_", Path(str(path) + "-wal"))):
        try:
            info = os.stat(candidate, follow_symlinks=False)
        except OSError:
            continue
        out[label + "size"] = info.st_size
        out[label + "mtime_ns"] = info.st_mtime_ns
        out[label + "ino"] = info.st_ino
    return out


def _session_fingerprint(locator: SessionLocator) -> dict[str, Any]:
    """The reader's per-session marker when it provides one, else the file's stat."""

    marker = getattr(locator, "fingerprint", None)
    if isinstance(marker, str):
        return {"session": marker}
    return _fingerprint(locator.path)


def turn_digest(turn: int, values: tuple[Any, ...], parent: str) -> str:
    """Digest of one stored turn's content: the ingest dedupe key (see ``_row``).

    ``values`` are the stored ``(role, ts, text, tool_calls, cwd, project, model,
    origin, withheld, parser_version)``. Origin is not part of the digest: the same
    turn seen from another root (a Worker home mirroring a session) is a
    duplicate, not a rewrite. :mod:`memfw.history.reredact` recomputes it after
    re-redacting a row, so the digest always describes what is stored.
    """

    content: list[Any] = [turn, *values[:7], *values[8:]]
    if parent:
        # Only when set, so turns stored before parent links keep their digest.
        content.append(parent)
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()[:24]


@contextmanager
def store_writer_lock(db: Path | str, *, wait: float | None = None) -> Iterator[bool]:
    """``True`` while this process holds the store's writer lock, ``False`` if it timed out.

    An exclusive ``flock`` on ``<store>.lock`` (``0600``, ``O_NOFOLLOW``, never
    truncated). :meth:`ConversationStore.ingest` waits up to
    :data:`WRITER_LOCK_WAIT` seconds for it; ``memfw conversations reredact
    --apply`` does not wait (``wait=0``) and refuses to run while another
    writer holds it. ``wait=None`` means :data:`WRITER_LOCK_WAIT`.
    """

    path = Path(str(db) + LOCK_SUFFIX)
    limit = WRITER_LOCK_WAIT if wait is None else max(0.0, float(wait))
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise HistoryError(f"cannot open conversation store lock: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HistoryError("conversation store lock is not a regular file")
        deadline = time.monotonic() + limit
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@dataclass
class IngestCounts:
    sessions_seen: int = 0
    sessions_unchanged: int = 0
    sessions_read: int = 0
    sessions_failed: int = 0
    sessions_busy: int = 0
    sessions_stale: int = 0
    sessions_too_large: int = 0
    sessions_opted_out: int = 0
    records_new: int = 0
    records_updated: int = 0
    records_duplicate: int = 0
    records_withheld: int = 0
    records_dropped: int = 0
    roots_failed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {key: value for key, value in self.__dict__.items() if key != "extra"}
        return out | self.extra


class ConversationStore:
    """SQLite store of redacted conversation turns (local derived data)."""

    def __init__(self, db: Path | str, *, optout: OptOut | None = None) -> None:
        self.db = Path(db)
        self.optout = optout or OptOut()
        self._force = False
        self._settled: datetime | None = None
        self._now = datetime.now(UTC)
        self._pending: frozenset[str] = frozenset()

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and is always closed."""

        with closing(self._connect()) as connection, connection:
            yield connection

    def _connect(self) -> sqlite3.Connection:
        prepare_store_path(self.db)
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS conversation_turns (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                offset INTEGER NOT NULL,
                turn INTEGER NOT NULL,
                role TEXT NOT NULL,
                ts TEXT NOT NULL,
                text TEXT NOT NULL,
                tool_calls TEXT NOT NULL,
                cwd TEXT NOT NULL,
                project TEXT NOT NULL,
                model TEXT NOT NULL,
                origin TEXT NOT NULL,
                withheld INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                locator TEXT NOT NULL,
                digest TEXT NOT NULL,
                parent_session TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (agent, session_id, offset)
            );
            CREATE INDEX IF NOT EXISTS conversation_turns_locator
                ON conversation_turns (agent, locator);
            CREATE INDEX IF NOT EXISTS conversation_turns_ts ON conversation_turns (agent, ts);
            CREATE TABLE IF NOT EXISTS conversation_cursors (
                agent TEXT NOT NULL,
                locator TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                last_offset INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                PRIMARY KEY (agent, locator)
            );
            CREATE TABLE IF NOT EXISTS conversation_archive (
                agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                archived_fingerprint TEXT NOT NULL,
                PRIMARY KEY (agent, session_id)
            );
            """
        )
        connection.create_function(TS_FUNCTION, 1, ts_key, deterministic=True)
        self._migrate(connection)
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(conversation_turns)")
        }
        if "parent_session" not in columns:
            # Additive: stores written before parent links existed get the column.
            connection.execute(
                "ALTER TABLE conversation_turns ADD COLUMN parent_session TEXT NOT NULL DEFAULT ''"
            )
        cursor_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(conversation_cursors)")
        }
        if "settled_fingerprint" not in cursor_columns:
            # Additive: the locator fingerprint at which settled ingest last archived it.
            connection.execute(
                "ALTER TABLE conversation_cursors"
                " ADD COLUMN settled_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        self._repair_owners(connection)
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Schema 1 -> 2 in one transaction, decided by ``schema_version`` (not by a table).

        Every step is idempotent and ``schema_version`` is written in the same
        transaction, so a failure rolls everything back and the next open retries.
        """

        row = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = 'schema_version'"
        ).fetchone()
        try:
            version = int(row[0]) if row is not None else 0
        except (TypeError, ValueError):
            version = 0
        if version >= int(SCHEMA_VERSION):
            return
        connection.isolation_level = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Re-check under the write lock: another process may have migrated.
                row = connection.execute(
                    "SELECT value FROM conversation_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None and str(row[0]).isdigit() and int(row[0]) >= int(SCHEMA_VERSION):
                    connection.execute("COMMIT")
                    return
                # Schema 2: every locator holding a turn. Existing turns get the locator
                # that wrote them; cursors are cleared so every session is re-read once
                # and mirrored copies register (the store is derived data).
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS conversation_turn_locators ("
                    " agent TEXT NOT NULL, session_id TEXT NOT NULL, offset INTEGER NOT NULL,"
                    " locator TEXT NOT NULL,"
                    " PRIMARY KEY (agent, session_id, offset, locator))"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS conversation_turn_locators_locator"
                    " ON conversation_turn_locators (agent, locator)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO conversation_turn_locators"
                    " (agent, session_id, offset, locator)"
                    " SELECT agent, session_id, offset, locator FROM conversation_turns"
                )
                connection.execute("DELETE FROM conversation_cursors")
                connection.execute(
                    "INSERT OR REPLACE INTO conversation_meta (key, value)"
                    " VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.isolation_level = "DEFERRED"

    @staticmethod
    def _repair_owners(connection: sqlite3.Connection) -> None:
        """Reconcile ownership rows with turns (older code wrote or deleted turns without them).

        A cheap ``EXISTS`` check first: nothing is written on a consistent store, and a
        read-only store is left alone (the repair is skipped, not an error).
        """

        try:
            missing = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM conversation_turns t WHERE NOT EXISTS"
                " (SELECT 1 FROM conversation_turn_locators o WHERE o.agent = t.agent"
                " AND o.session_id = t.session_id AND o.offset = t.offset))"
            ).fetchone()[0]
            orphan = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM conversation_turn_locators o WHERE NOT EXISTS"
                " (SELECT 1 FROM conversation_turns t WHERE t.agent = o.agent"
                " AND t.session_id = o.session_id AND t.offset = o.offset))"
            ).fetchone()[0]
            if not missing and not orphan:
                return
            if missing:
                connection.execute(
                    "INSERT OR IGNORE INTO conversation_turn_locators"
                    " (agent, session_id, offset, locator)"
                    " SELECT t.agent, t.session_id, t.offset, t.locator FROM conversation_turns t"
                    " WHERE NOT EXISTS (SELECT 1 FROM conversation_turn_locators o"
                    " WHERE o.agent = t.agent AND o.session_id = t.session_id"
                    " AND o.offset = t.offset)"
                )
            if orphan:
                connection.execute(
                    "DELETE FROM conversation_turn_locators WHERE NOT EXISTS"
                    " (SELECT 1 FROM conversation_turns t"
                    " WHERE t.agent = conversation_turn_locators.agent"
                    " AND t.session_id = conversation_turn_locators.session_id"
                    " AND t.offset = conversation_turn_locators.offset)"
                )
            connection.commit()
        except sqlite3.OperationalError:
            connection.rollback()  # read-only or busy: repair next time

    @staticmethod
    def _row(
        record: ConversationRecord, agent: str, origin: str, version: str, locator: str
    ) -> tuple[Any, ...]:
        record = ConversationRecord(
            agent=record.agent,
            session_id=_scrub(record.session_id),
            offset=record.offset,
            turn=record.turn,
            role=_scrub(record.role),
            text=_scrub(record.text),
            timestamp=utc_timestamp(_scrub(record.timestamp)),
            tool_calls=tuple(
                type(call)(_scrub(call.name), _scrub(call.summary)) for call in record.tool_calls
            ),
            cwd=_scrub(record.cwd),
            project=_scrub(record.project),
            model=_scrub(record.model),
            origin=record.origin,
            parent_session=_scrub(record.parent_session),
        )
        scanned = [record.text, record.cwd, record.project, record.model]
        for call in record.tool_calls:
            scanned.extend((call.name, call.summary))
        withheld = any(value and has_secret(value) for value in scanned)
        if withheld:
            text, calls = "", "[]"
        else:
            text = redact(record.text)[:TEXT_CHARS]
            calls = json.dumps(
                [
                    {"name": _clean(c.name), "summary": _clean(c.summary, SUMMARY_CHARS)}
                    for c in record.tool_calls
                ],
                ensure_ascii=False,
            )
        values = (
            _clean(record.role),
            _clean(record.timestamp),
            text,
            calls,
            "" if withheld else _clean(record.cwd),
            "" if withheld else _clean(record.project),
            "" if withheld else _clean(record.model),
            origin,
            int(withheld),
            version,
        )
        parent = "" if withheld else _clean(record.parent_session)
        digest = turn_digest(record.turn, values, parent)
        return (
            agent,
            _clean(record.session_id),
            record.offset,
            record.turn,
            *values,
            locator,
            digest,
            parent,
        )

    def ingest(
        self,
        reader: Reader,
        roots: Iterable[StoreRoot | Path],
        *,
        settled_minutes: float | None = None,
        now: datetime | None = None,
    ) -> IngestCounts:
        """Ingest every session of ``reader`` under ``roots`` incrementally.

        With ``settled_minutes`` only locators quiet for that long are ingested,
        and their sessions are archived (see the module docstring). ``now``
        defaults to the current UTC time.
        """

        if settled_minutes is not None and (
            isinstance(settled_minutes, bool)
            or not isinstance(settled_minutes, (int, float))
            or settled_minutes < 0
        ):
            raise HistoryError("settled_minutes must be a number >= 0")
        counts = IngestCounts()
        self._settled = None
        self._now = now or datetime.now(UTC)
        if settled_minutes is not None:
            self._settled = self._now - timedelta(minutes=float(settled_minutes))
            counts.extra["sessions_unsettled"] = 0
            counts.extra["sessions_archived"] = 0
        prepare_store_path(self.db)  # the lock file lives next to the store
        with store_writer_lock(self.db) as held:
            if not held:
                raise HistoryError("conversation store is locked by another writer")
            return self._ingest_locked(reader, roots, counts)

    def _ingest_locked(
        self,
        reader: Reader,
        roots: Iterable[StoreRoot | Path],
        counts: IngestCounts,
    ) -> IngestCounts:
        with self._open() as connection:
            if reader.agent in self.optout.agents:
                # Opting out also forgets what was stored before.
                counts.records_dropped += connection.execute(
                    "DELETE FROM conversation_turns WHERE agent = ?", (reader.agent,)
                ).rowcount
                connection.execute(
                    "DELETE FROM conversation_turn_locators WHERE agent = ?", (reader.agent,)
                )
                connection.execute(
                    "DELETE FROM conversation_cursors WHERE agent = ?", (reader.agent,)
                )
                connection.execute(f"DELETE FROM {ARCHIVE_TABLE} WHERE agent = ?", (reader.agent,))
                prefix = f"force:{reader.agent}:"
                connection.execute(
                    "DELETE FROM conversation_meta WHERE substr(key, 1, ?) = ?",
                    (len(prefix), prefix),
                )
                counts.extra["opted_out"] = True
                return counts
            self._force = self._optout_changed(connection, reader.agent)
            relabel = self._labels_changed(connection, reader.agent)
            self._force = self._force or relabel
            self._pending = self._pending_force(connection, reader.agent)
            for item in roots:
                root = item if isinstance(item, StoreRoot) else StoreRoot(Path(item).resolve())
                try:
                    locators = iter(reader.sessions(root.path))
                    while True:
                        try:
                            locator = next(locators)
                        except StopIteration:
                            break
                        self._guarded(connection, reader, root, locator, counts)
                except Exception:
                    connection.rollback()
                    counts.roots_failed += 1
            done = not (counts.roots_failed or counts.sessions_failed or counts.sessions_busy)
            if relabel and done:
                # Recorded only once every session was re-read (none failed or was
                # skipped as busy), so an interrupted pass runs again next ingest.
                # A locator held back as unsettled carries its own force marker
                # and is re-read once it settles, so it does not keep the pass open.
                self._save_labels_version(connection, reader.agent)
            counts.extra["relabelled"] = relabel
        return counts

    def _guarded(
        self,
        connection: sqlite3.Connection,
        reader: Reader,
        root: StoreRoot,
        locator: SessionLocator,
        counts: IngestCounts,
    ) -> None:
        counts.sessions_seen += 1
        try:
            self._ingest_session(connection, reader, root, locator, counts)
        except Exception:
            connection.rollback()
            counts.sessions_failed += 1
        else:
            connection.commit()

    def _ingest_session(
        self,
        connection: sqlite3.Connection,
        reader: Reader,
        root: StoreRoot,
        locator: SessionLocator,
        counts: IngestCounts,
    ) -> None:
        # Opaque and collision-free: the raw relative path is never stored.
        # fsencode: a non-UTF-8 filename (surrogateescape) still hashes.
        digest = hashlib.sha256(os.fsencode(locator.key)).hexdigest()[:20]
        key = f"{root.origin}:{root.label}/{digest}"
        agent = reader.agent
        version = str(getattr(reader, "parser_version", "1"))
        append_only = bool(getattr(reader, "append_only", True))
        fingerprint = _session_fingerprint(locator)
        cursor = connection.execute(
            "SELECT fingerprint, last_offset, parser_version, settled_fingerprint"
            " FROM conversation_cursors WHERE agent = ? AND locator = ?",
            (agent, key),
        ).fetchone()
        after = -1
        forced = self._force or key in self._pending
        if cursor is not None:
            previous = json.loads(cursor["fingerprint"])
            same_version = cursor["parser_version"] == version and not forced
            if previous == fingerprint and same_version:
                counts.sessions_unchanged += 1
                if (
                    self._settled is not None
                    and int(cursor["last_offset"]) >= 0
                    and cursor["settled_fingerprint"] != cursor["fingerprint"]
                    and self._is_settled(self._stored_activity(connection, agent, key), locator)
                ):
                    # Stored earlier (by a plain ingest, or before it was quiet): archive now.
                    self._archive(connection, agent, key, counts)
                    self._mark_settled(connection, agent, key, cursor["fingerprint"])
                return
            grew = (
                append_only
                and same_version
                and previous.get("ino") == fingerprint.get("ino")
                and fingerprint.get("size", 0) >= previous.get("size", 0)
            )
            if grew and int(cursor["last_offset"]) >= 0:
                # Re-read the last turn: it may have been written mid-stream.
                after = int(cursor["last_offset"]) - 1
        counts.sessions_read += 1
        last = int(cursor["last_offset"]) if cursor is not None and after >= 0 else -1
        seen: set[tuple[str, int]] = set()
        rows: list[tuple[Any, ...]] = []
        labels: dict[str, list[str]] = {}
        prompted: set[str] = set()
        excluded = False
        try:
            for record in reader.read(locator, after=after):
                if record.offset <= after:
                    continue
                if self.optout.cwd_excluded(record.cwd):
                    excluded = True
                    break
                row = self._row(record, agent, root.origin, version, key)
                rows.append(row)
                seen.add((row[1], row[2]))
                found = labels.setdefault(row[1], [])
                if record.origin:
                    found.append(record.origin)
                if (
                    after < 0
                    and row[1] not in prompted
                    and record.role == "user"
                    and has_content(strip_structural(record.text))
                ):
                    # The session's first real prompt: a dispatch marker labels it.
                    prompted.add(row[1])
                    found.append(prompt_origin(record.text))
        except SessionTooLarge:
            # Over the reader's limit: keep what was stored, wait for the file to change.
            counts.sessions_too_large += 1
            self._clear_force(connection, agent, key)
            self._clear_busy(connection, agent, key)
            self._save_cursor(connection, agent, key, fingerprint, -1, version)
            return
        if excluded:
            counts.sessions_opted_out += 1
            owned = connection.execute(
                "SELECT session_id, offset FROM conversation_turn_locators"
                " WHERE agent = ? AND locator = ?",
                (agent, key),
            ).fetchall()
            dropped = connection.execute(
                "DELETE FROM conversation_turns WHERE agent = ? AND locator = ?", (agent, key)
            ).rowcount
            for item in owned:
                # Every copy of an excluded turn goes, whichever locator wrote it.
                dropped += connection.execute(
                    "DELETE FROM conversation_turns"
                    " WHERE agent = ? AND session_id = ? AND offset = ?",
                    (agent, item["session_id"], item["offset"]),
                ).rowcount
                connection.execute(
                    "DELETE FROM conversation_turn_locators"
                    " WHERE agent = ? AND session_id = ? AND offset = ?",
                    (agent, item["session_id"], item["offset"]),
                )
            connection.execute(
                "DELETE FROM conversation_turn_locators WHERE agent = ? AND locator = ?",
                (agent, key),
            )
            counts.records_dropped += dropped
            for session in {item["session_id"] for item in owned}:
                connection.execute(
                    f"DELETE FROM {ARCHIVE_TABLE} WHERE agent = ? AND session_id = ?",
                    (agent, session),
                )
            self._clear_force(connection, agent, key)
            self._clear_busy(connection, agent, key)
            self._save_cursor(connection, agent, key, fingerprint, -1, version)
            return
        if self._settled is not None and not self._is_settled(
            self._newest([_parse_ts(row[5]) for row in rows]), locator
        ):
            # Still active: no turn is written and the cursor stays, so the next
            # round reads it again. A session resumed after it was archived is
            # no longer archived (its stored turns are not its final form): that
            # covers the sessions this locator holds and those it just yielded
            # (new turns of a session stored under another locator).
            counts.sessions_read -= 1
            counts.extra["sessions_unsettled"] += 1
            connection.execute(
                f"DELETE FROM {ARCHIVE_TABLE} WHERE agent = ? AND session_id IN"
                " (SELECT session_id FROM conversation_turn_locators"
                " WHERE agent = ? AND locator = ?)",
                (agent, agent, key),
            )
            for session in labels:
                connection.execute(
                    f"DELETE FROM {ARCHIVE_TABLE} WHERE agent = ? AND session_id = ?",
                    (agent, session),
                )
            self._mark_settled(connection, agent, key, "")
            if forced and key not in self._pending:
                # A forced pass (re-label, opt-out change) still owes this locator a
                # full re-read: keep a marker so it is re-read once it settles.
                self._note_force(connection, agent, key)
            return
        if after < 0 and not rows:
            had = connection.execute(
                "SELECT 1 FROM conversation_turn_locators WHERE agent = ? AND locator = ? LIMIT 1",
                (agent, key),
            ).fetchone()
            if had is not None:
                # Nothing readable now although rows exist: likely locked or mid-rewrite.
                # A force marker stays: the owed full re-read has not happened yet.
                if self._note_busy(connection, agent, key, fingerprint) < BUSY_RETRIES:
                    counts.sessions_busy += 1
                    if forced and key not in self._pending:
                        # A forced pass (re-label, opt-out change) that found it
                        # locked: keep a marker so the full re-read still happens.
                        self._note_force(connection, agent, key)
                    return
                # Same empty result too often: keep the rows, stop retrying until
                # the file changes (then the whole session is re-read).
                counts.sessions_stale += 1
                self._clear_busy(connection, agent, key)
                self._save_cursor(connection, agent, key, fingerprint, -1, version)
                self._clear_force(connection, agent, key)
                return
        self._clear_busy(connection, agent, key)
        origins = {
            session: self._session_origin(
                connection, agent, key, session, root.origin, found, after >= 0
            )
            for session, found in labels.items()
        }
        rows = [(*row[:11], origins[row[1]], *row[12:]) for row in rows]
        for row in rows:
            existing = connection.execute(
                "SELECT digest FROM conversation_turns"
                " WHERE agent = ? AND session_id = ? AND offset = ?",
                row[:3],
            ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO conversation_turn_locators (agent, session_id, offset,"
                " locator) VALUES (?, ?, ?, ?)",
                (*row[:3], key),
            )
            last = max(last, row[2])
            if existing is not None and existing["digest"] == row[15]:
                counts.records_duplicate += 1
                continue
            if existing is None:
                counts.records_new += 1
            else:
                counts.records_updated += 1
            counts.records_withheld += row[12]
            connection.execute(
                "INSERT OR REPLACE INTO conversation_turns (agent, session_id, offset, turn,"
                " role, ts, text, tool_calls, cwd, project, model, origin, withheld,"
                " parser_version, locator, digest, parent_session)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        for session, origin in origins.items():
            # Origin is not in the digest: unchanged turns and turns from earlier
            # reads of this session get the session label here.
            connection.execute(
                "UPDATE conversation_turns SET origin = ?"
                " WHERE agent = ? AND locator = ? AND session_id = ? AND origin <> ?",
                (origin, agent, key, session, origin),
            )
        if after < 0:
            # Full re-read finished: this locator no longer holds the offsets that are
            # gone. A turn is dropped only when no other locator still holds it.
            stale = [
                (agent, r["session_id"], r["offset"])
                for r in connection.execute(
                    "SELECT session_id, offset FROM conversation_turn_locators"
                    " WHERE agent = ? AND locator = ?",
                    (agent, key),
                )
                if (r["session_id"], r["offset"]) not in seen
            ]
            for item in stale:
                connection.execute(
                    "DELETE FROM conversation_turn_locators"
                    " WHERE agent = ? AND session_id = ? AND offset = ? AND locator = ?",
                    (*item, key),
                )
                other = connection.execute(
                    "SELECT locator FROM conversation_turn_locators"
                    " WHERE agent = ? AND session_id = ? AND offset = ? ORDER BY locator LIMIT 1",
                    item,
                ).fetchone()
                if other is None:
                    counts.records_dropped += connection.execute(
                        "DELETE FROM conversation_turns"
                        " WHERE agent = ? AND session_id = ? AND offset = ?",
                        item,
                    ).rowcount
                else:
                    connection.execute(
                        "UPDATE conversation_turns SET locator = ?"
                        " WHERE agent = ? AND session_id = ? AND offset = ? AND locator = ?",
                        (other["locator"], *item, key),
                    )
        self._save_cursor(connection, agent, key, fingerprint, last, version)
        self._clear_force(connection, agent, key)
        if self._settled is not None and last >= 0:
            self._archive(connection, agent, key, counts)
            self._mark_settled(connection, agent, key, json.dumps(fingerprint, sort_keys=True))

    # -- settled ingest and archive state --------------------------------------------

    def _newest(self, stamps: Iterable[datetime | None]) -> datetime | None:
        """The newest trusted timestamp: one beyond now plus :data:`CLOCK_SKEW` is ignored."""

        limit = self._now + CLOCK_SKEW
        return max((stamp for stamp in stamps if stamp and stamp <= limit), default=None)

    def _is_settled(self, activity: datetime | None, locator: SessionLocator) -> bool:
        """Whether the locator's last activity is before the settled cutoff.

        A locator with a file of its own: the later of ``activity`` (newest
        trusted turn timestamp) and the file's mtime, since some readers stamp
        replies with the prompt time or skip lines without a timestamp. A
        session inside a shared database (``path#session`` key, or a reader's
        per-session marker): the file's mtime moves with every other session,
        so ``activity`` decides and the mtime is only the fallback. An mtime
        beyond now plus :data:`CLOCK_SKEW` is not trusted; with no trusted
        timestamp either, the file's ctime stands in (setting the mtime moves
        the ctime to the present, so it is not stuck in the future; not on
        Windows, where st_ctime is the creation time). Unknown
        activity (no trusted timestamp, no readable or trusted mtime or ctime)
        is never settled.
        """

        assert self._settled is not None
        try:
            info: os.stat_result | None = os.stat(locator.path, follow_symlinks=False)
        except (OSError, ValueError):
            info = None
        mtime = self._newest([_file_time(info, "st_mtime")])
        if mtime is None and info is not None and activity is None and os.name != "nt":
            # A future (or unreadable) mtime and no usable timestamp: the ctime
            # (inode change time; on Windows st_ctime is the creation time).
            mtime = self._newest([_file_time(info, "st_ctime")])
        shared = "#" in locator.key or getattr(locator, "fingerprint", None) is not None
        if shared:
            last = activity or mtime
        else:
            last = max((value for value in (activity, mtime) if value), default=None)
        return last is not None and last <= self._settled

    def _stored_activity(
        self, connection: sqlite3.Connection, agent: str, key: str
    ) -> datetime | None:
        """The newest trusted stored timestamp among the turns this locator holds."""

        bound = ts_key(_stamp(self._now + CLOCK_SKEW))
        row = connection.execute(
            f"SELECT MAX({TS_FUNCTION}(NULLIF(t.ts, ''))) FROM conversation_turns t"
            " JOIN conversation_turn_locators o ON o.agent = t.agent"
            " AND o.session_id = t.session_id AND o.offset = t.offset"
            f" WHERE o.agent = ? AND o.locator = ? AND {TS_FUNCTION}(NULLIF(t.ts, '')) <= ?",
            (agent, key, bound),
        ).fetchone()
        return self._newest([_parse_ts(row[0])]) if row is not None and row[0] else None

    @staticmethod
    def _pending_force(connection: sqlite3.Connection, agent: str) -> frozenset[str]:
        """Locators a forced pass skipped as unsettled (re-read in full once settled)."""

        prefix = f"force:{agent}:"
        return frozenset(
            str(row[0])[len(prefix) :]
            for row in connection.execute(
                "SELECT key FROM conversation_meta WHERE substr(key, 1, ?) = ?",
                (len(prefix), prefix),
            )
        )

    @staticmethod
    def _note_force(connection: sqlite3.Connection, agent: str, key: str) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO conversation_meta (key, value) VALUES (?, '1')",
            (f"force:{agent}:{key}",),
        )

    def _clear_force(self, connection: sqlite3.Connection, agent: str, key: str) -> None:
        if key in self._pending:
            connection.execute(
                "DELETE FROM conversation_meta WHERE key = ?", (f"force:{agent}:{key}",)
            )

    def _archive(
        self, connection: sqlite3.Connection, agent: str, key: str, counts: IngestCounts
    ) -> None:
        """Record every session this locator holds as archived in its current shape."""

        stamp = _stamp(self._now)
        sessions = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT session_id FROM conversation_turn_locators"
                " WHERE agent = ? AND locator = ?",
                (agent, key),
            )
        ]
        for session in sessions:
            mark = self.session_fingerprint(connection, agent, session)
            if mark is None:
                continue
            existing = connection.execute(
                f"SELECT archived_fingerprint FROM {ARCHIVE_TABLE}"
                " WHERE agent = ? AND session_id = ?",
                (agent, session),
            ).fetchone()
            if existing is not None and existing[0] == mark:
                continue  # already archived in this shape: keep its archived_at
            connection.execute(
                f"INSERT OR REPLACE INTO {ARCHIVE_TABLE}"
                " (agent, session_id, archived_at, archived_fingerprint) VALUES (?, ?, ?, ?)",
                (agent, session, stamp, mark),
            )
            counts.extra["sessions_archived"] += 1

    @staticmethod
    def session_fingerprint(
        connection: sqlite3.Connection, agent: str, session_id: str
    ) -> str | None:
        """:func:`~memfw.history.transcripts.session_fingerprint` of the stored turns."""

        row = connection.execute(
            f"SELECT COUNT(*), MAX(offset), MAX({TS_FUNCTION}(NULLIF(ts, '')))"
            " FROM conversation_turns WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        ).fetchone()
        if row is None or not row[0]:
            return None
        return session_fingerprint(int(row[0]), int(row[1]), ts_display(row[2]))

    @staticmethod
    def _mark_settled(
        connection: sqlite3.Connection, agent: str, key: str, fingerprint: str
    ) -> None:
        connection.execute(
            "UPDATE conversation_cursors SET settled_fingerprint = ?"
            " WHERE agent = ? AND locator = ?",
            (fingerprint, agent, key),
        )

    def archive_state(self, agent: str, session_id: str) -> dict[str, Any]:
        """``{"archived": bool, "archived_at": str}`` of one stored session."""

        with self._open() as connection:
            mark = self.session_fingerprint(connection, agent, session_id)
            row = connection.execute(
                f"SELECT archived_at, archived_fingerprint FROM {ARCHIVE_TABLE}"
                " WHERE agent = ? AND session_id = ?",
                (agent, session_id),
            ).fetchone()
        archived = mark is not None and row is not None and row[1] == mark
        return {"archived": archived, "archived_at": str(row[0]) if archived else ""}

    @staticmethod
    def _session_origin(
        connection: sqlite3.Connection,
        agent: str,
        key: str,
        session: str,
        root_origin: str,
        found: list[str],
        incremental: bool,
    ) -> str:
        """Merged origin of one session; an incremental read keeps the stored label."""

        previous: list[str] = []
        if incremental:
            # The first prompt was not re-read: keep what the full read found.
            stored = connection.execute(
                "SELECT origin FROM conversation_turns"
                " WHERE agent = ? AND locator = ? AND session_id = ? ORDER BY offset LIMIT 1",
                (agent, key, session),
            ).fetchone()
            if stored is not None:
                previous.append(str(stored["origin"]))
        return merge_origin(root_origin, *found, *previous)

    @staticmethod
    def _note_busy(
        connection: sqlite3.Connection, agent: str, key: str, fingerprint: dict[str, Any]
    ) -> int:
        """Count one more empty re-read of ``fingerprint``; returns the running count."""

        name = f"busy:{agent}:{key}"
        marker = json.dumps(fingerprint, sort_keys=True)
        row = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = ?", (name,)
        ).fetchone()
        count = 1
        if row is not None:
            try:
                previous = json.loads(row["value"])
                if previous.get("fingerprint") == marker:
                    count = int(previous.get("count", 0)) + 1
            except (ValueError, TypeError, AttributeError):
                count = 1
        connection.execute(
            "INSERT OR REPLACE INTO conversation_meta (key, value) VALUES (?, ?)",
            (name, json.dumps({"fingerprint": marker, "count": count})),
        )
        return count

    @staticmethod
    def _clear_busy(connection: sqlite3.Connection, agent: str, key: str) -> None:
        connection.execute("DELETE FROM conversation_meta WHERE key = ?", (f"busy:{agent}:{key}",))

    def _labels_changed(self, connection: sqlite3.Connection, agent: str) -> bool:
        """True (once) when :data:`LABELS_VERSION` differs from this agent's last ingest."""

        row = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = ?", (f"labels_version:{agent}",)
        ).fetchone()
        return row is None or row["value"] != LABELS_VERSION

    @staticmethod
    def _save_labels_version(connection: sqlite3.Connection, agent: str) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO conversation_meta (key, value) VALUES (?, ?)",
            (f"labels_version:{agent}", LABELS_VERSION),
        )

    def _optout_changed(self, connection: sqlite3.Connection, agent: str) -> bool:
        """True (once) when the cwd opt-out rules changed since this agent's last ingest."""

        rules = json.dumps(sorted(self.optout.cwd_globs))
        key = f"optout_cwd:{agent}"
        row = connection.execute(
            "SELECT value FROM conversation_meta WHERE key = ?", (key,)
        ).fetchone()
        connection.execute(
            "INSERT OR REPLACE INTO conversation_meta (key, value) VALUES (?, ?)", (key, rules)
        )
        # The no-history marker is checked on every read; a full re-read picks up new ones.
        return row is not None and row["value"] != rules

    @staticmethod
    def _save_cursor(
        connection: sqlite3.Connection,
        agent: str,
        key: str,
        fingerprint: dict[str, Any],
        last: int,
        version: str,
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO conversation_cursors (agent, locator, fingerprint,"
            " last_offset, parser_version) VALUES (?, ?, ?, ?, ?)",
            (agent, key, json.dumps(fingerprint, sort_keys=True), last, version),
        )

    def stats(self) -> dict[str, Any]:
        """Per agent: sessions, turns and withheld turns. Never any text."""

        with self._open() as connection:
            rows = connection.execute(
                "SELECT agent, COUNT(*) AS turns, COUNT(DISTINCT session_id) AS sessions,"
                " SUM(withheld) AS withheld FROM conversation_turns GROUP BY agent ORDER BY agent"
            ).fetchall()
        return {
            row["agent"]: {
                "turns": row["turns"],
                "sessions": row["sessions"],
                "withheld": int(row["withheld"] or 0),
            }
            for row in rows
        }

    def turns(self, agent: str, session_id: str) -> list[dict[str, Any]]:
        """Stored (redacted) turns of one session, in offset order."""

        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_turns WHERE agent = ? AND session_id = ?"
                " ORDER BY offset",
                (agent, session_id),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["tool_calls"] = json.loads(item["tool_calls"])
            out.append(item)
        return out
