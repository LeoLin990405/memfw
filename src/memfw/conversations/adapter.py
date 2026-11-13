"""The one file that knows the unified store's schema and ingest entry point.

The store (tables, readers, write-time redaction) belongs to
:mod:`memfw.history.conversations`. This module reads its
``conversation_turns`` table read-only and drives its ingest; when the store
module changes shape, only this file changes.

Store location: ``conversation_store.path`` in ``vault.yaml``, else
``~/.local/share/memfw/conversations.sqlite``. It is refused inside a git work
tree, the vault or a synced folder.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import (
    ConversationError,
    SessionInfo,
    Turn,
    check_private_location,
)

DATA_DIR = Path(".local") / "share" / "memfw"
STORE_NAME = "conversations.sqlite"
TURNS_TABLE = "conversation_turns"
MAX_TERMS = 8
_SINCE_CHARS = "0123456789-:TZ.+ "


def data_dir() -> Path:
    return Path.home() / DATA_DIR


def store_path(config: Mapping[str, Any] | None, *, vault: Path | None = None) -> Path:
    """Where the unified store lives, checked against the private-location rules."""

    section = (config or {}).get("conversation_store") or {}
    if not isinstance(section, Mapping):
        raise ConversationError("conversation_store must be a mapping")
    raw = section.get("path")
    path = Path(str(raw)).expanduser() if raw else data_dir() / STORE_NAME
    return check_private_location(path, vault=vault, extra_synced=synced_dirs(config))


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
                return self._columns(connection) != set()
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
            columns = self._columns(connection)
            origin = "MAX(origin)" if "origin" in columns else "''"
            rows = connection.execute(
                "SELECT agent, session_id, COUNT(*) AS turns, MIN(offset) AS first_offset,"
                " MAX(offset) AS last_offset, MIN(NULLIF(ts, '')) AS started_at,"
                " MAX(NULLIF(ts, '')) AS updated_at, MAX(cwd) AS cwd, MAX(project) AS project,"
                f" {origin} AS origin FROM {TURNS_TABLE} WHERE {where}"
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
                "SELECT agent, session_id, offset, turn, role, ts, text, tool_calls"
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
        clauses = [where, *(r"text LIKE ? ESCAPE '\'" for _ in terms)]
        params = [*params, *(_like(term) for term in terms)]
        since_value = _since(since)
        if since_value:
            clauses.append("ts >= ?")
            params.append(since_value)
        with self._open() as connection:
            rows = connection.execute(
                "SELECT agent, session_id, offset, turn, role, ts, text, tool_calls"
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
    """An ingestor that drives the store module over ``conversation_sources``.

    ``conversation_sources`` is a list of ``{name, agent, path}`` entries (see
    the store's docs); nothing is discovered or hard-coded here.
    """

    def run() -> dict[str, Any]:
        try:
            store_module = importlib.import_module(STORE_MODULE)
            sources_module = importlib.import_module(SOURCES_MODULE)
        except ImportError:
            return {"status": "unavailable", "reason": "store_module_missing"}
        entries = (config or {}).get("conversation_sources") or []
        if not isinstance(entries, list) or not entries:
            return {"status": "skipped", "reason": "no_conversation_sources"}
        store = store_module.ConversationStore(db)
        report: dict[str, Any] = {"status": "ok", "sources": {}}
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or f"source-{index}")[:64]
            agent = str(entry.get("agent") or "").strip().lower()
            if not agent or agent in skip_apps:
                report["sources"][name] = {"status": "opted_out"}
                continue
            root = Path(str(entry.get("path") or "")).expanduser()
            if not root.is_dir() or root.is_symlink():
                report["sources"][name] = {"status": "missing_root"}
                continue
            try:
                reader = sources_module.get_reader(agent)
                counts = store.ingest(reader, [root])
            except Exception as exc:  # report the kind only: messages may hold paths
                report["sources"][name] = {"status": "error", "error": type(exc).__name__}
                continue
            as_dict = counts.as_dict() if hasattr(counts, "as_dict") else {}
            report["sources"][name] = {
                "status": "ok",
                **{key: value for key, value in as_dict.items() if isinstance(value, int)},
            }
        return report

    return run
