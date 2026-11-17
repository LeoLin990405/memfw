"""Scrubbing a purged attachment's text out of the vault's SQLite stores (MM1b, H7).

Used by ``attach purge`` and by ``memfw recover`` when it finishes an
interrupted purge. The memory index is disposable and simply dropped; any
other SQLite database in the vault that still holds the record's name, note,
origin, rights or location is VACUUMed and checked again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .records import AttachmentRecord

if TYPE_CHECKING:
    from memfw.core.context import RepositoryHost

#: Record fields searched for in SQLite stores after a purge (only values this long).
_MIN_NEEDLE: Final = 6


@dataclass
class ScrubResult:
    removed: list[str] = field(default_factory=list)
    vacuumed: list[str] = field(default_factory=list)
    still_containing: list[str] = field(default_factory=list)


def vault_sqlite_stores(host: RepositoryHost) -> list[Path]:
    """SQLite databases in the vault (not their ``-wal``/``-shm``), the index first."""

    found: list[Path] = []
    for path in sorted(host.root.rglob("*")):
        name = path.name
        if not path.is_file() or path.is_symlink():
            continue
        if name.endswith(("-wal", "-shm", "-journal")):
            continue
        try:
            with path.open("rb") as stream:
                header = stream.read(16)
        except OSError:
            continue
        if header == b"SQLite format 3\0":
            found.append(path)
    index = Path(host.index_path)
    found.sort(key=lambda item: (item != index, item.as_posix()))
    return found


def needles_of(record: AttachmentRecord | None) -> list[bytes]:
    if record is None:
        return []
    values = [record.original_name, record.note, record.origin, record.rights]
    if record.location is not None:
        values.append(record.location.partition(":")[2])
    return [
        value.encode("utf-8")
        for value in values
        if isinstance(value, str) and len(value.strip()) >= _MIN_NEEDLE
    ]


def store_contains(path: Path, needles: list[bytes]) -> bool:
    for candidate in (path, Path(f"{path}-wal")):
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        if any(needle in data for needle in needles):
            return True
    return False


def vacuum(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def scrub_stores(host: RepositoryHost, record: AttachmentRecord | None) -> ScrubResult:
    """Drop the memory index and VACUUM every vault SQLite store still holding ``record``'s text."""

    result = ScrubResult()
    for suffix in ("", "-wal", "-shm"):
        index = Path(f"{host.index_path}{suffix}")
        if index.exists():
            index.unlink()
            result.removed.append(index.relative_to(host.root).as_posix())
    needles = needles_of(record)
    if not needles:
        return result
    for database in vault_sqlite_stores(host):
        if not store_contains(database, needles):
            continue
        name = database.relative_to(host.root).as_posix()
        try:
            vacuum(database)
        except sqlite3.Error:
            result.still_containing.append(name)
            continue
        result.vacuumed.append(name)
        if store_contains(database, needles):
            result.still_containing.append(name)
    return result


__all__ = ["ScrubResult", "needles_of", "scrub_stores", "vault_sqlite_stores"]
