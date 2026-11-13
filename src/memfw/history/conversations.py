"""Incremental, redacted local store of normalised conversation turns.

Readers (:mod:`memfw.history.sources`) yield raw turns; this module is the
only place they are written. Before a write every text field (turn text,
tool-call names and summaries, cwd, project, model) is scanned with
:func:`memfw.history.redact.has_secret`: a hit withholds the whole turn
(``withheld = 1``, empty text and tool calls); otherwise the values are
passed through :func:`redact`. The store never holds an unredacted value and
raw lines are never copied. The location rules live in
:mod:`memfw.history.store_location`.

Rows are keyed by ``(agent, session_id, offset)``: re-ingesting the same turn
is a no-op, a changed turn is updated in place. Per locator a cursor keeps the
store's fingerprint and the last offset seen:

* unchanged fingerprint: skipped;
* append-only reader, same inode, not smaller: re-read from the last stored
  turn (it may have been mid-stream);
* anything else (non-append-only reader, rewrite, truncation, new inode):
  the whole session is re-read first and, only once that read finished,
  offsets that are no longer present are dropped.

A re-read that yields nothing although the locator had rows (store locked,
vanished mid-read) changes nothing and leaves the cursor as it was.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .index import HistoryError
from .redact import has_secret, redact
from .sources.base import ConversationRecord, Reader, SessionLocator, StoreRoot
from .store_location import prepare_store_path

SCHEMA_VERSION = "1"
TEXT_CHARS = 16_000
FIELD_CHARS = 512
SUMMARY_CHARS = 200
OPTOUT_KEY = "conversation_optout"
OPTOUT_MARKER = ".memfw-no-history"
ORIGINS = ("interactive", "worker")


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


@dataclass
class IngestCounts:
    sessions_seen: int = 0
    sessions_unchanged: int = 0
    sessions_read: int = 0
    sessions_failed: int = 0
    sessions_busy: int = 0
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
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO conversation_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        return connection

    @staticmethod
    def _row(
        record: ConversationRecord, agent: str, origin: str, version: str, locator: str
    ) -> tuple[Any, ...]:
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
        # Origin is not part of the digest: the same turn seen from another root
        # (a Worker home mirroring a session) is a duplicate, not a rewrite.
        content = [record.turn, *values[:7], *values[8:]]
        digest = hashlib.sha256(json.dumps(content).encode()).hexdigest()[:24]
        return (
            agent,
            _clean(record.session_id),
            record.offset,
            record.turn,
            *values,
            locator,
            digest,
        )

    def ingest(self, reader: Reader, roots: Iterable[StoreRoot | Path]) -> IngestCounts:
        """Ingest every session of ``reader`` under ``roots`` incrementally."""

        counts = IngestCounts()
        with self._open() as connection:
            if reader.agent in self.optout.agents:
                # Opting out also forgets what was stored before.
                counts.records_dropped += connection.execute(
                    "DELETE FROM conversation_turns WHERE agent = ?", (reader.agent,)
                ).rowcount
                connection.execute(
                    "DELETE FROM conversation_cursors WHERE agent = ?", (reader.agent,)
                )
                counts.extra["opted_out"] = True
                return counts
            self._force = self._optout_changed(connection, reader.agent)
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
        digest = hashlib.sha256(locator.key.encode("utf-8")).hexdigest()[:20]
        key = f"{root.origin}:{root.label}/{digest}"
        agent = reader.agent
        version = str(getattr(reader, "parser_version", "1"))
        append_only = bool(getattr(reader, "append_only", True))
        fingerprint = _fingerprint(locator.path)
        cursor = connection.execute(
            "SELECT fingerprint, last_offset, parser_version FROM conversation_cursors"
            " WHERE agent = ? AND locator = ?",
            (agent, key),
        ).fetchone()
        after = -1
        if cursor is not None:
            previous = json.loads(cursor["fingerprint"])
            same_version = cursor["parser_version"] == version and not self._force
            if previous == fingerprint and same_version:
                counts.sessions_unchanged += 1
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
        excluded = False
        for record in reader.read(locator, after=after):
            if record.offset <= after:
                continue
            if self.optout.cwd_excluded(record.cwd):
                excluded = True
                break
            row = self._row(record, agent, root.origin, version, key)
            rows.append(row)
            seen.add((row[1], row[2]))
        if excluded:
            counts.sessions_opted_out += 1
            counts.records_dropped += connection.execute(
                "DELETE FROM conversation_turns WHERE agent = ? AND locator = ?", (agent, key)
            ).rowcount
            self._save_cursor(connection, agent, key, fingerprint, -1, version)
            return
        if after < 0 and not rows:
            had = connection.execute(
                "SELECT 1 FROM conversation_turns WHERE agent = ? AND locator = ? LIMIT 1",
                (agent, key),
            ).fetchone()
            if had is not None:
                # Nothing readable now although rows exist: likely locked or mid-rewrite.
                counts.sessions_busy += 1
                return
        for row in rows:
            existing = connection.execute(
                "SELECT digest FROM conversation_turns"
                " WHERE agent = ? AND session_id = ? AND offset = ?",
                row[:3],
            ).fetchone()
            last = max(last, row[2])
            if existing is not None and existing["digest"] == row[-1]:
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
                " parser_version, locator, digest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        if after < 0:
            # Full re-read finished: drop this locator's offsets that are gone.
            stale = [
                (agent, r["session_id"], r["offset"])
                for r in connection.execute(
                    "SELECT session_id, offset FROM conversation_turns"
                    " WHERE agent = ? AND locator = ?",
                    (agent, key),
                )
                if (r["session_id"], r["offset"]) not in seen
            ]
            connection.executemany(
                "DELETE FROM conversation_turns WHERE agent = ? AND session_id = ? AND offset = ?",
                stale,
            )
            counts.records_dropped += len(stale)
        self._save_cursor(connection, agent, key, fingerprint, last, version)

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
        fingerprint: dict[str, int],
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
