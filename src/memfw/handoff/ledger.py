"""Where generated packs are kept, and the record of each handoff.

Packs are written next to the conversation store, in ``handoffs/``
(``~/.local/share/memfw/handoffs/`` by default): directory 0700, files 0600,
``<agent>-<session>-<n>.<md|json>``. ``handoffs.sqlite`` in the same folder
keeps one row per generated pack (the store's ``handoff_from`` link: from
which session, the sequence number, for which agent, size and digest), so
the next pack of the same session lists its predecessors in ``lineage``.
This is local derived data; the store itself is never written.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memfw.conversations.store import ensure_private_dir, ensure_private_file

DIR_NAME = "handoffs"
DB_NAME = "handoffs.sqlite"
MAX_PREVIOUS = 20
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def handoff_dir(store: Path) -> Path:
    return Path(store).parent / DIR_NAME


def file_stem(agent: str, session_id: str, n: int) -> str:
    """A file-name-safe stem; unsafe or long session ids get a digest suffix."""

    safe = _UNSAFE.sub("_", session_id)[:80]
    if safe != session_id:
        safe += "-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    return f"{_UNSAFE.sub('_', agent)[:40]}-{safe}-{n}"


@dataclass(frozen=True)
class Reservation:
    n: int
    path: Path


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.db = self.directory / DB_NAME

    def _connect(self) -> sqlite3.Connection:
        ensure_private_dir(self.directory)
        ensure_private_file(self.db)
        connection = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE IF NOT EXISTS handoffs (from_agent TEXT NOT NULL,"
            " from_session TEXT NOT NULL, n INTEGER NOT NULL, for_agent TEXT,"
            " created_at TEXT NOT NULL, format TEXT NOT NULL, file TEXT NOT NULL,"
            " bytes INTEGER, sha256 TEXT, PRIMARY KEY (from_agent, from_session, n))"
        )
        return connection

    def previous(self, agent: str, session_id: str) -> list[dict[str, Any]]:
        """Earlier packs of this session, oldest first (no paths, no content)."""

        if not self.db.is_file():
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT n, for_agent, created_at FROM handoffs WHERE from_agent = ?"
                " AND from_session = ? AND bytes IS NOT NULL ORDER BY n DESC LIMIT ?",
                (agent, session_id, MAX_PREVIOUS),
            ).fetchall()
        return [
            {"n": int(r["n"]), "for_agent": r["for_agent"], "created_at": str(r["created_at"])}
            for r in reversed(rows)
        ]

    def reserve(self, agent: str, session_id: str, for_agent: str | None, fmt: str) -> Reservation:
        """Take the next sequence number for this session (atomic across processes)."""

        ext = "json" if fmt == "json" else "md"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(n), 0) AS n FROM handoffs"
                    " WHERE from_agent = ? AND from_session = ?",
                    (agent, session_id),
                ).fetchone()
                n = int(row["n"]) + 1
                name = f"{file_stem(agent, session_id, n)}.{ext}"
                connection.execute(
                    "INSERT INTO handoffs (from_agent, from_session, n, for_agent, created_at,"
                    " format, file) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (agent, session_id, n, for_agent, _now(), ext, name),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return Reservation(n=n, path=self.directory / name)

    def commit(self, agent: str, session_id: str, reservation: Reservation, body: str) -> None:
        """Write the pack (0600, atomically) and complete its ledger row."""

        data = body.encode("utf-8")
        temporary = reservation.path.with_name(f".{reservation.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, reservation.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE handoffs SET bytes = ?, sha256 = ? WHERE from_agent = ?"
                " AND from_session = ? AND n = ?",
                (len(data), hashlib.sha256(data).hexdigest(), agent, session_id, reservation.n),
            )

    def release(self, agent: str, session_id: str, reservation: Reservation) -> None:
        """Forget a reservation whose pack was never written."""

        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM handoffs WHERE from_agent = ? AND from_session = ? AND n = ?"
                " AND bytes IS NULL",
                (agent, session_id, reservation.n),
            )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
