"""Read one session from the conversation store, read-only, for a handoff pack.

The store (:mod:`memfw.history.conversations`, table ``conversation_turns``)
already holds redacted text only; ``withheld = 1`` turns have no text and
stay withheld here. The file is opened with ``mode=ro`` and
``query_only``; nothing in this package writes to it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from memfw.conversations.store import GOAL_SCAN_TURNS, goal_text, ts_display, ts_key

TABLE = "conversation_turns"
TS_FUNCTION = "memfw_ts_key"
REQUIRED = frozenset(
    {"agent", "session_id", "offset", "turn", "role", "ts", "text", "tool_calls"}
    | {"cwd", "project", "origin", "withheld", "parser_version"}
)
TAIL_ROWS = 600
"""At most this many of the latest turns are read for ``recent_turns``."""
CALL_ROWS = 5000
"""At most this many turns with tool calls are scanned for ``files_touched``."""


class StoreUnavailable(RuntimeError):
    """The store file is missing, not a store, or locked (message is a short code)."""


@dataclass
class StoredTurn:
    offset: int
    turn: int
    role: str
    ts: str
    text: str
    withheld: bool
    tool_calls: list[dict[str, str]] = field(default_factory=list)


@dataclass
class StoredSession:
    agent: str
    session_id: str
    origin: str
    started_at: str
    updated_at: str
    turns: int
    withheld: int
    parser_version: str
    cwd: str
    project: str
    first_user: str
    """The goal text: the first user turn with content once harness markers are removed."""
    tail: list[StoredTurn]
    calls: list[dict[str, str]]


def _calls(raw: Any) -> list[dict[str, str]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [
        {"name": str(item.get("name") or ""), "summary": str(item.get("summary") or "")}
        for item in value
        if isinstance(item, dict)
    ]


@contextmanager
def _open(db: Path) -> Iterator[sqlite3.Connection]:
    if db.is_symlink() or not db.is_file():
        raise StoreUnavailable("store_missing")
    uri = f"file:{quote(str(db))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
    except sqlite3.Error as exc:
        raise StoreUnavailable("store_unreadable") from exc
    with closing(connection):
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = 1")
            # Timestamps may be stored with offsets: MIN/MAX through the UTC key.
            connection.create_function(TS_FUNCTION, 1, ts_key, deterministic=True)
            columns = {str(r["name"]) for r in connection.execute(f"PRAGMA table_info({TABLE})")}
        except sqlite3.Error as exc:
            raise StoreUnavailable("store_unreadable") from exc
        if not REQUIRED <= columns:
            raise StoreUnavailable("store_schema_mismatch")
        yield connection


def _turn(row: sqlite3.Row) -> StoredTurn:
    withheld = bool(int(row["withheld"] or 0))
    return StoredTurn(
        offset=int(row["offset"]),
        turn=int(row["turn"]),
        role=str(row["role"] or ""),
        ts=ts_display(ts_key(str(row["ts"] or ""))),
        text="" if withheld else str(row["text"] or ""),
        withheld=withheld,
        tool_calls=[] if withheld else _calls(row["tool_calls"]),
    )


def load_session(db: Path | str, agent: str, session_id: str) -> StoredSession | None:
    """The stored session, or ``None`` when the store has no turn for it."""

    key = (agent, session_id)
    try:
        with _open(Path(db)) as connection:
            head = connection.execute(
                "SELECT COUNT(*) AS turns, SUM(withheld) AS withheld,"
                f" MIN({TS_FUNCTION}(NULLIF(ts, ''))) AS started_at,"
                f" MAX({TS_FUNCTION}(NULLIF(ts, ''))) AS updated_at,"
                " MAX(origin) AS origin, MAX(parser_version) AS parser_version"
                f" FROM {TABLE} WHERE agent = ? AND session_id = ?",
                key,
            ).fetchone()
            if head is None or not int(head["turns"] or 0):
                return None
            place = connection.execute(
                f"SELECT cwd, project FROM {TABLE} WHERE agent = ? AND session_id = ?"
                " AND withheld = 0 AND (cwd != '' OR project != '') ORDER BY offset DESC LIMIT 1",
                key,
            ).fetchone()
            first_rows = connection.execute(
                f"SELECT text FROM {TABLE} WHERE agent = ? AND session_id = ? AND role = 'user'"
                " AND withheld = 0 AND TRIM(text) != '' ORDER BY offset LIMIT ?",
                (*key, GOAL_SCAN_TURNS),
            ).fetchall()
            tail_rows = connection.execute(
                "SELECT offset, turn, role, ts, text, tool_calls, withheld"
                f" FROM {TABLE} WHERE agent = ? AND session_id = ? ORDER BY offset DESC LIMIT ?",
                (*key, TAIL_ROWS),
            ).fetchall()
            call_rows = connection.execute(
                f"SELECT tool_calls FROM {TABLE} WHERE agent = ? AND session_id = ?"
                " AND withheld = 0 AND tool_calls NOT IN ('', '[]') ORDER BY offset LIMIT ?",
                (*key, CALL_ROWS),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        raise StoreUnavailable("store_busy") from exc
    except sqlite3.Error as exc:
        raise StoreUnavailable("store_unreadable") from exc
    calls = [call for row in call_rows for call in _calls(row["tool_calls"])]
    return StoredSession(
        agent=agent,
        session_id=session_id,
        origin=str(head["origin"] or ""),
        started_at=ts_display(head["started_at"]),
        updated_at=ts_display(head["updated_at"]),
        turns=int(head["turns"]),
        withheld=int(head["withheld"] or 0),
        parser_version=str(head["parser_version"] or ""),
        cwd=str(place["cwd"] or "") if place is not None else "",
        project=str(place["project"] or "") if place is not None else "",
        first_user=goal_text([str(row["text"] or "") for row in first_rows]),
        tail=[_turn(row) for row in reversed(tail_rows)],
        calls=calls,
    )
