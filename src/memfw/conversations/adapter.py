"""The one file that knows the unified store's schema and ingest entry point.

The store (tables, readers, write-time redaction) belongs to
:mod:`memfw.history.conversations` (schema version ``SCHEMA_VERSION``). This
module reads its ``conversation_turns`` table read-only and drives its ingest
(``ConversationStore.ingest`` with the store's own ``OptOut``); when the store
module changes shape, only this file changes.

Store schema used here: ``conversation_turns(agent, session_id, offset, turn,
role, ts, text, tool_calls, cwd, project, model, origin, withheld, ...)``.
``withheld = 1`` marks a turn the store refused to keep (secret-shaped): its
text and tool calls are empty and it is reported as withheld, never as text.

Store location: ``conversation_store.path`` in ``vault.yaml``, else
``~/.local/share/memfw/conversations.sqlite`` (the store module's own
default). It is refused inside a git work tree, the vault or a synced folder:
both this package's rules and the store module's
(:func:`memfw.history.store_location.check_store_path`) apply.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from memfw.history.index import HistoryError
from memfw.history.store_location import (
    DEFAULT_RELATIVE,
    check_store_path,
    store_path_from_config,
)

from .store import (
    WITHHELD,
    ConversationError,
    SessionInfo,
    Turn,
    check_private_location,
)

DATA_DIR = DEFAULT_RELATIVE.parent
STORE_NAME = DEFAULT_RELATIVE.name
TURNS_TABLE = "conversation_turns"
REQUIRED_COLUMNS = frozenset(
    {"agent", "session_id", "offset", "turn", "role", "ts", "text", "tool_calls"}
    | {"cwd", "project", "origin", "withheld"}
)
MAX_TERMS = 8
_SINCE_CHARS = "0123456789-:TZ.+ "


def data_dir() -> Path:
    return Path.home() / DATA_DIR


def store_path(config: Mapping[str, Any] | None, *, vault: Path | None = None) -> Path:
    """Where the unified store lives, checked against the private-location rules."""

    section = (config or {}).get("conversation_store") or {}
    if not isinstance(section, Mapping):
        raise ConversationError("conversation_store must be a mapping")
    try:
        path = store_path_from_config(dict(config or {}))
    except HistoryError as exc:
        raise ConversationError(str(exc)) from exc
    return checked_location(path, config, vault=vault)


def checked_location(
    path: Path, config: Mapping[str, Any] | None, *, vault: Path | None = None
) -> Path:
    """``path`` resolved, or :class:`ConversationError` when either rule set refuses it."""

    resolved = check_private_location(path, vault=vault, extra_synced=synced_dirs(config))
    try:
        return check_store_path(resolved, vault_root=vault)
    except HistoryError as exc:
        raise ConversationError(str(exc)) from exc


def synced_dirs(config: Mapping[str, Any] | None) -> list[Path]:
    section = (config or {}).get("conversation_store") or {}
    raw = (section.get("synced_dirs") or []) if isinstance(section, Mapping) else []
    if not isinstance(raw, (list, tuple)):
        raise ConversationError("conversation_store.synced_dirs must be a list")
    return [Path(str(item)).expanduser() for item in raw]


def _since(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) > 40 or any(char not in _SINCE_CHARS for char in text):
        raise ConversationError("since must be an ISO-8601 timestamp or date")
    return text


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SqliteStoreSource:
    """:class:`~memfw.conversations.store.ConversationSource` over the store file."""

    def __init__(self, db: Path) -> None:
        self.db = Path(db)

    def available(self) -> bool:
        if not self.db.is_file() or self.db.is_symlink():
            return False
        try:
            with self._open() as connection:
                return REQUIRED_COLUMNS <= self._columns(connection)
        except sqlite3.Error:
            return False

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        if self.db.is_symlink():
            raise ConversationError("conversation store is a symlink")
        uri = f"file:{quote(str(self.db))}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = 1")
            yield connection

    @staticmethod
    def _columns(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({TURNS_TABLE})").fetchall()
        return {str(row["name"]) for row in rows}

    @staticmethod
    def _scope(apps: Sequence[str] | None, exclude: Sequence[str]) -> tuple[str, list[Any]] | None:
        """SQL filter for the app scope; ``None`` when the scope is empty."""

        clauses: list[str] = []
        params: list[Any] = []
        if apps is not None:
            if not apps:
                return None
            clauses.append(f"agent IN ({','.join('?' * len(apps))})")
            params.extend(apps)
        if exclude:
            clauses.append(f"agent NOT IN ({','.join('?' * len(exclude))})")
            params.extend(exclude)
        return (" AND ".join(clauses) or "1"), params

    def _session_rows(
        self, where: str, params: list[Any], having: str, limit: int, offset: int
    ) -> list[SessionInfo]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT agent, session_id, COUNT(*) AS turns, MIN(offset) AS first_offset,"
                " MAX(offset) AS last_offset, MIN(NULLIF(ts, '')) AS started_at,"
                " MAX(NULLIF(ts, '')) AS updated_at, MAX(cwd) AS cwd, MAX(project) AS project,"
                f" MAX(origin) AS origin FROM {TURNS_TABLE} WHERE {where}"
                f" GROUP BY agent, session_id {having}"
                " ORDER BY updated_at DESC, agent, session_id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [
            SessionInfo(
                app=str(row["agent"]),
                session_id=str(row["session_id"]),
                turns=int(row["turns"]),
                first_offset=int(row["first_offset"]),
                last_offset=int(row["last_offset"]),
                started_at=str(row["started_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                cwd=str(row["cwd"] or ""),
                project=str(row["project"] or ""),
                origin=str(row["origin"] or ""),
            )
            for row in rows
        ]

    def sessions(
        self,
        *,
        apps: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionInfo]:
        scope = self._scope(apps, exclude)
        if scope is None:
            return []
        where, params = scope
        having = ""
        since_value = _since(since)
        if since_value:
            having = "HAVING MAX(ts) >= ?"
            params = [*params, since_value]
        return self._session_rows(where, params, having, limit, offset)

    def session(self, app: str, session_id: str) -> SessionInfo | None:
        rows = self._session_rows("agent = ? AND session_id = ?", [app, session_id], "", 1, 0)
        return rows[0] if rows else None

    def find_session(
        self, session_id: str, apps: Sequence[str] | None, exclude: Sequence[str] = ()
    ) -> list[SessionInfo]:
        scope = self._scope(apps, exclude)
        if scope is None:
            return []
        where, params = scope
        return self._session_rows(f"session_id = ? AND {where}", [session_id, *params], "", 20, 0)

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        if int(row["withheld"] or 0):
            # The store kept no text for it: report the turn, never its content.
            return Turn(
                app=str(row["agent"]),
                session_id=str(row["session_id"]),
                offset=int(row["offset"]),
                turn=int(row["turn"]),
                role=str(row["role"] or ""),
                ts=str(row["ts"] or ""),
                text=WITHHELD,
                withheld=True,
            )
        try:
            calls_raw = json.loads(row["tool_calls"] or "[]")
        except ValueError:
            calls_raw = []
        calls = tuple(
            {"name": str(item.get("name") or ""), "summary": str(item.get("summary") or "")}
            for item in calls_raw
            if isinstance(item, dict)
        )
        return Turn(
            app=str(row["agent"]),
            session_id=str(row["session_id"]),
            offset=int(row["offset"]),
            turn=int(row["turn"]),
            role=str(row["role"] or ""),
            ts=str(row["ts"] or ""),
            text=str(row["text"] or ""),
            tool_calls=calls,
        )

    def turns(self, app: str, session_id: str, *, start: int = 0, limit: int = 200) -> list[Turn]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT agent, session_id, offset, turn, role, ts, text, tool_calls, withheld"
                f" FROM {TURNS_TABLE} WHERE agent = ? AND session_id = ?"
                " ORDER BY offset LIMIT ? OFFSET ?",
                (app, session_id, max(0, limit), max(0, start)),
            ).fetchall()
        return [self._turn(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        apps: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        since: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[Turn]:
        terms = [term for term in query.split() if term][:MAX_TERMS]
        if not terms:
            return []
        scope = self._scope(apps, exclude)
        if scope is None:
            return []
        where, params = scope
        clauses = [
            where,
            "withheld = 0",
            "text != ''",
            *(r"text LIKE ? ESCAPE '\'" for _ in terms),
        ]
        params = [*params, *(_like(term) for term in terms)]
        since_value = _since(since)
        if since_value:
            clauses.append("ts >= ?")
            params.append(since_value)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT agent, session_id, offset, turn, role, ts, text, tool_calls, withheld"
                f" FROM {TURNS_TABLE} WHERE {' AND '.join(clauses)}"
                " ORDER BY ts DESC, agent, session_id, offset LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._turn(row) for row in rows]


# -- ingest -----------------------------------------------------------------------

Ingestor = Callable[[], dict[str, Any]]
"""Run one incremental ingest; return counts only (never text or paths)."""

STORE_MODULE = "memfw.history.conversations"
SOURCES_MODULE = "memfw.history.sources"


def store_ingestor(
    config: Mapping[str, Any] | None, db: Path, *, skip_apps: Sequence[str] = ()
) -> Ingestor:
    """An ingestor that drives the store module the way ``memfw history ingest`` does.

    Every registered reader is ingested from its existing roots (the
    interactive home plus every Worker home, ``store_roots``) through
    ``ConversationStore.ingest``, with the store's own opt-out
    (``conversation_optout``: agents, cwd globs, marker files) applied.
    ``skip_apps`` are added to that opt-out: the store then also forgets what
    it held for them, exactly as for a configured opt-out. Names are compared
    with each reader's canonical name, so an alias cannot bypass them.
    """

    def run() -> dict[str, Any]:
        try:
            store_module = importlib.import_module(STORE_MODULE)
            sources_module = importlib.import_module(SOURCES_MODULE)
        except ImportError:
            return {"status": "unavailable", "reason": "store_module_missing"}
        readers = getattr(sources_module, "READERS", None)
        store_roots = getattr(sources_module, "store_roots", None)
        store_type = getattr(store_module, "ConversationStore", None)
        optout_type = getattr(store_module, "OptOut", None)
        if (
            not isinstance(readers, Mapping)
            or not callable(store_roots)
            or store_type is None
            or optout_type is None
        ):
            return {"status": "unavailable", "reason": "store_api_mismatch"}
        skip = {str(app).strip().lower() for app in skip_apps if str(app).strip()}
        try:
            optout = optout_type.from_config(dict(config or {}))
        except Exception as exc:  # a bad opt-out section: report the kind only
            return {"status": "error", "error": type(exc).__name__}
        if skip:
            optout = dataclasses.replace(optout, agents=frozenset(optout.agents) | skip)
        store = store_type(db, optout=optout)
        report: dict[str, Any] = {"status": "ok", "agents": {}}
        for reader in readers.values():
            agent = str(getattr(reader, "agent", "") or "")[:64]
            if not agent:
                continue
            try:
                roots = [] if agent in optout.agents else list(store_roots(reader))
                counts = store.ingest(reader, roots)
            except Exception as exc:  # report the kind only: messages may hold paths
                report["agents"][agent] = {"status": "error", "error": type(exc).__name__}
                continue
            as_dict = counts.as_dict() if hasattr(counts, "as_dict") else {}
            numbers = {
                key: value
                for key, value in as_dict.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            if agent in optout.agents or as_dict.get("opted_out") is True:
                report["agents"][agent] = {"status": "opted_out", **numbers}
                continue
            report["agents"][agent] = {"status": "ok", "roots": len(roots), **numbers}
        return report

    return run
