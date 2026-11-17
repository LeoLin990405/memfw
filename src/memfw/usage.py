"""Usage projection: the ``memory_used`` signals, in a disposable SQLite file (C5).

``SqliteUsageSink`` is the persistent ``memfw.ports.UsageSink``. It writes one
row per (signal, memory id) to ``usage.sqlite3`` at the vault root, next to
``index.sqlite3`` and ``corroboration.sqlite3``:

``memory_id``, ``session_ref``, ``app``, ``at`` (ISO 8601). Ids, an opaque
session token, an app name and a time; never query or body text.

Like every projection it is never the authority for anything and never
writes back into the vault: no memory, no ``events.jsonl`` entry, no
``vault.yaml`` change. Unlike the other projections its rows are not derived
from the vault, so "rebuild" means start empty: the file may be deleted at
any time (``reset()``), and only usage signals are lost. Nothing in memfw
ranks or filters by it; the C4 coverage/waste eval reads it (``counts``,
``rows``). The file is created with mode 0600 (``O_EXCL``/``O_NOFOLLOW``, so a
planted file or symlink is refused) and kept at 0600.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, Final

from memfw.ports.usage import UsageSignal

SCHEMA_VERSION: Final = 1
FILENAME: Final = "usage.sqlite3"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS used ("
    " memory_id TEXT NOT NULL, session_ref TEXT NOT NULL, app TEXT NOT NULL, at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS used_memory ON used (memory_id)",
)


class UsageStoreError(OSError):
    """The usage file is not a private regular file, or not a usage projection."""


def projection_path(root: Path | str) -> Path:
    """Where the projection lives: ``usage.sqlite3`` at the vault root."""

    return Path(root) / FILENAME


def _private_file(path: Path) -> None:
    """Create ``path`` with mode 0600, or check that it is a regular file; keep it 0600."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(path, flags, 0o600))
    except FileExistsError:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise UsageStoreError("usage projection is not a regular file") from None
    os.chmod(path, 0o600)


class SqliteUsageSink:
    """``UsageSink`` over ``usage.sqlite3`` (one row per signal and id)."""

    name = "sqlite"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def record(self, signal: UsageSignal) -> None:
        if not isinstance(signal, UsageSignal):
            raise TypeError("record() takes a UsageSignal")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _private_file(self.path)
        at = signal.at.isoformat()
        rows = [(memory_id, signal.session_ref, signal.app, at) for memory_id in signal.memory_ids]
        try:
            with closing(sqlite3.connect(self.path, timeout=5.0)) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                with connection:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    connection.executemany(
                        "INSERT INTO used (memory_id, session_ref, app, at) VALUES (?, ?, ?, ?)",
                        rows,
                    )
        except sqlite3.Error as exc:
            raise UsageStoreError(f"usage projection not written: {exc}") from exc
        os.chmod(self.path, 0o600)

    def _read(self, sql: str, parameters: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        if not self.path.is_file() or self.path.is_symlink():
            return []
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            try:
                return connection.execute(sql, tuple(parameters)).fetchall()
            except sqlite3.DatabaseError as exc:
                raise UsageStoreError(f"unreadable usage projection: {exc}") from exc

    def rows(self) -> list[dict[str, str]]:
        """Every row, in insertion order (a missing file has none)."""

        return [
            {"memory_id": memory_id, "session_ref": session_ref, "app": app, "at": at}
            for memory_id, session_ref, app, at in self._read(
                "SELECT memory_id, session_ref, app, at FROM used ORDER BY rowid"
            )
        ]

    def counts(self, memory_ids: Iterable[str] | None = None) -> dict[str, int]:
        """How many signals named each id (all ids, or only ``memory_ids``)."""

        rows = self._read("SELECT memory_id, COUNT(*) FROM used GROUP BY memory_id")
        found = {str(memory_id): int(count) for memory_id, count in rows}
        if memory_ids is None:
            return found
        return {memory_id: found.get(memory_id, 0) for memory_id in dict.fromkeys(memory_ids)}

    def reset(self) -> None:
        """Delete the projection (it starts empty on the next signal)."""

        for suffix in ("", "-journal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)


__all__ = ["FILENAME", "SCHEMA_VERSION", "SqliteUsageSink", "UsageStoreError", "projection_path"]
