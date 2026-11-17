"""Usage projection: the ``memory_used`` signals, in a disposable SQLite file (C5).

``SqliteUsageSink`` is the persistent ``memfw.ports.UsageSink``. It writes one
row per (signal, memory id) to a file **outside the vault**, in memfw's local
data directory and keyed by the vault (``projection_path``):
``~/.local/share/memfw/usage/<key>.sqlite3``, where ``<key>`` is the first 24
hex digits of the sha256 of the vault's resolved path. Outside the vault it
never counts toward the physical-size budget (``measure_physical_disk_bytes``,
which drives budget enforcement) or the transaction fingerprint, however many
signals arrive. Columns:

``memory_id``, ``session_ref``, ``app``, ``at`` (ISO 8601). Ids, an opaque
session token, an app name and a time; never query or body text.

Like every projection it is never the authority for anything and never
writes back into the vault: no memory, no ``events.jsonl`` entry, no
``vault.yaml`` change. Unlike the other projections its rows are not derived
from the vault, so "rebuild" means start empty: the file may be deleted at
any time (``reset()``), and only usage signals are lost. Nothing in memfw
ranks or filters by it; the C4 coverage/waste eval reads it (``counts``,
``rows``). The file is created with mode 0600 (``O_EXCL``/``O_NOFOLLOW``, so a
planted file or symlink is refused) and kept at 0600; its directory is
0700 (an existing one the user owns is tightened to 0700 on every write).

**Retention.** Every ``record`` prunes in the same transaction: rows older
than ``max_age_days`` (default ``DEFAULT_MAX_AGE_DAYS`` = 90, measured from
the new signal's time; an index on ``at_epoch`` keeps this a range delete),
then the oldest rows beyond ``max_rows`` (default ``DEFAULT_MAX_ROWS`` =
100000: about 7.4 MB at the cap with short ids, plus about 1.5 MB for the
``at_epoch`` index), as a rowid range below the ``max_rows``-th newest rowid,
so a write never sorts the table. Together with the 100-id cap per signal
this bounds the file.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from memfw.core import local_data
from memfw.ports.usage import UsageSignal

SCHEMA_VERSION: Final = 1
FILENAME: Final = "usage.sqlite3"
#: Under the home directory: memfw's local data directory (``memfw.core.local_data``).
DATA_DIR: Final = local_data.DATA_ROOT / "usage"
DEFAULT_MAX_ROWS: Final = 100_000
DEFAULT_MAX_AGE_DAYS: Final = 90

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS used ("
    " memory_id TEXT NOT NULL, session_ref TEXT NOT NULL, app TEXT NOT NULL, at TEXT NOT NULL,"
    " at_epoch REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS used_memory ON used (memory_id)",
    # Added after schema 1 shipped; IF NOT EXISTS adds it to an existing file.
    "CREATE INDEX IF NOT EXISTS used_at ON used (at_epoch)",
)


class UsageStoreError(OSError):
    """The usage file is not a private regular file, or not a usage projection."""


def vault_key(root: Path | str) -> str:
    """The first 24 hex digits of sha256 of the vault's resolved path."""

    return local_data.vault_key(root)


def projection_path(root: Path | str, *, home: Path | None = None) -> Path:
    """Where the vault's usage projection lives: outside it, under the home directory."""

    return local_data.projection_file(root, "usage", home=home)


def _private_dir(path: Path) -> None:
    """Create the store's directory (and parents) with mode 0700; refuse a symlink.

    An existing directory the current user owns is tightened to 0700 too (its
    parents are left alone; a directory owned by someone else is not touched).
    """

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise UsageStoreError("usage directory is not a directory")
    info = os.lstat(path)
    getuid = getattr(os, "getuid", None)
    if stat.S_IMODE(info.st_mode) != 0o700 and (getuid is None or info.st_uid == getuid()):
        os.chmod(path, 0o700)


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

    def __init__(
        self,
        path: Path | str,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        if type(max_age_days) is not int or max_age_days < 1:
            raise ValueError("max_age_days must be a positive integer")
        self.path = Path(path)
        self.max_rows = max_rows
        self.max_age_days = max_age_days

    def record(self, signal: UsageSignal) -> None:
        if not isinstance(signal, UsageSignal):
            raise TypeError("record() takes a UsageSignal")
        _private_dir(self.path.parent)
        _private_file(self.path)
        at = signal.at.isoformat()
        cutoff = (signal.at - timedelta(days=self.max_age_days)).timestamp()
        epoch = signal.at.timestamp()
        rows = [
            (memory_id, signal.session_ref, signal.app, at, epoch)
            for memory_id in signal.memory_ids
        ]
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
                        "INSERT INTO used (memory_id, session_ref, app, at, at_epoch)"
                        " VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    # Retention: by age, then by count (oldest rows first).
                    connection.execute("DELETE FROM used WHERE at_epoch < ?", (cutoff,))
                    # The same rows as "keep the newest max_rows by rowid", without
                    # a sort: walk back max_rows on the rowid b-tree, drop the rest.
                    connection.execute(
                        "DELETE FROM used WHERE rowid <="
                        " (SELECT rowid FROM used ORDER BY rowid DESC LIMIT 1 OFFSET ?)",
                        (self.max_rows,),
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


__all__ = [
    "DATA_DIR",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MAX_ROWS",
    "FILENAME",
    "SCHEMA_VERSION",
    "SqliteUsageSink",
    "UsageStoreError",
    "projection_path",
    "vault_key",
]
