"""Forget residue check (roadmap R5) and the deletion-request listing.

``projection_residue`` answers one question for one memory id: after the
memory was forgotten, does any derived store still carry it? It is read-only:
nothing is rebuilt, deleted or written, not even a SQLite sidecar, and the
output carries ids, table names and counts, never text or host paths.

SQLite files are opened without creating ``-wal``/``-shm``/``-journal``
files, so a store in a read-only directory is read, not reported as
unreadable residue. A file with no ``-wal`` or ``-journal`` beside it is
opened ``immutable=1``; one with a pending ``-wal`` or ``-journal`` is
copied (with that sidecar) into a private temporary directory and the copy
is read, so committed rows still in the WAL are counted. Caveat: either way
the check reads a snapshot and assumes no writer is changing the store at
that moment; run it after a rebuild finishes, not during one. The store
and its sidecars are stat-ed before and after each read; if they changed
the read runs once more, and a second change, like any torn read, fails
closed (``error: "unreadable"``, counted as residue).

The memory's *state* decides what counts as residue:

* ``deleted`` (no ``memories/<id>.md``, and a ``delete`` event for it) and
  ``absent`` (no file and no such event): any row naming the id in any store
  is residue, and so is a leftover ``.history/<id>`` directory;
* ``closed`` (the file is there with ``valid_to`` or ``superseded_by`` set):
  the FTS index may keep its rows (``as_of`` and ``include_history`` read
  them) but must not list it as current (rows whose current state cannot
  be told, with no ``memory_temporal`` row, count as residue); the related projection is
  current-only, so any item or edge is residue. Corroboration and usage
  rows of a closed memory are reported as ``retained``, not residue: the
  memory still exists and those rows hold ids, counts and times only;
* ``live``: nothing was forgotten. The checks still run and are reported, but
  ``clean`` is ``None``.

Stores checked: the FTS index (``index.sqlite3``: ``memory_rows``,
``memory_temporal``, ``memory_access``, ``memory_fts``), the related
projection (``related_items``, ``related_edges`` as source or target), the
corroboration projection (``~/.local/share/memfw/corroboration/<key>.sqlite3``
``mentions``; a vault-root ``corroboration.sqlite3`` from an earlier release,
which memfw no longer reads, is checked too when it exists, as
``corroboration_legacy``), the usage
projection (``~/.local/share/memfw/usage/<key>.sqlite3`` ``used``) and,
when a path is given, the entity projection (``entities``). A store whose file
is missing has nothing to carry and is clean; a file that cannot be read
fails closed (residue, ``error: "unreadable"``). OKF exports on disk are out
of scope.

``list_forget_requests`` reads the ``memfw-http/3`` ``POST /forget``
candidates under ``<vault>/_candidates/forget/``; it never applies one.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar
from urllib.parse import quote

import yaml

from ..core.models import EventKind
from ..core.repository import Repository
from ..corroboration import legacy_path as corroboration_legacy_path
from ..corroboration import projection_path as corroboration_path
from ..usage import projection_path as usage_path

_T = TypeVar("_T")

_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
#: Where ``memfw memory-related-build`` writes by default, relative to the vault.
DEFAULT_RELATED_NAME: Final = ".memfw-related.sqlite"
FORGET_DIRECTORY: Final = ("_candidates", "forget")

#: Stable remedy codes; the CLI prints them, it never runs them.
REMEDIES: Final = {
    "index": "memfw index rebuild",
    "related": "memfw memory-related-build",
    "corroboration": "memfw corroboration rebuild",
    "corroboration_legacy": "delete corroboration.sqlite3 at the vault root (no longer used)",
    "usage": "reset the usage projection (it holds signals only)",
    "entities": "rebuild the entity projection",
    "vault": "memfw fsck",
}


@dataclass
class StoreCheck:
    store: str
    present: bool
    rows: dict[str, int] = field(default_factory=dict)
    residue: bool = False
    retained: bool = False
    error: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "store": self.store,
            "present": self.present,
            "rows": dict(self.rows),
            "residue": self.residue,
            "retained": self.retained,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.detail is not None:
            out["detail"] = self.detail
        if self.residue:
            out["remedy"] = REMEDIES[self.store]
        return out


def _uri_connect(path: Path, query: str) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?{query}"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


@contextmanager
def _open_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection that creates no file beside ``path`` (module docstring)."""

    sidecars = [
        path.with_name(path.name + suffix)
        for suffix in ("-wal", "-journal")
        if path.with_name(path.name + suffix).is_file()
    ]
    if not sidecars:
        with closing(_uri_connect(path, "mode=ro&immutable=1")) as connection:
            yield connection
        return
    with tempfile.TemporaryDirectory(prefix="memfw-residue-") as scratch:
        copy = Path(scratch) / "store.sqlite3"
        shutil.copyfile(path, copy)
        for sidecar in sidecars:
            shutil.copyfile(sidecar, copy.with_name(copy.name + sidecar.name[len(path.name) :]))
        # The private copy may replay its WAL or hot journal; the original is untouched.
        with closing(sqlite3.connect(copy, timeout=5)) as connection:
            connection.execute("PRAGMA query_only=ON")
            yield connection


def _signature(path: Path) -> tuple[tuple[int, int] | None, ...]:
    """``(mtime_ns, size)`` of the store and each sidecar (``None`` when absent)."""

    out: list[tuple[int, int] | None] = []
    for candidate in (path, *(path.with_name(path.name + s) for s in ("-wal", "-journal"))):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            out.append(None)
        else:
            out.append((stat.st_mtime_ns, stat.st_size))
    return tuple(out)


def _read_stable(path: Path, read: Callable[[sqlite3.Connection], _T]) -> _T:
    """``read`` on a snapshot of ``path``; retried once if the store changed meanwhile.

    An ``immutable=1`` read assumes no writer, so the store and its sidecars
    are checked before and after; a change (a writer, a new ``-wal``) means
    the read may be torn and it runs once more. A second change raises
    ``OSError``, which callers report as ``unreadable`` (fail closed).
    """

    for _attempt in range(2):
        before = _signature(path)
        with _open_ro(path) as connection:
            result = read(connection)
        if _signature(path) == before:
            return result
    raise OSError("store changed while it was read")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")
    }


def _count_rows(path: Path, queries: dict[str, tuple[str, str, tuple[Any, ...]]]) -> StoreCheck:
    """Count rows per ``label -> (table, where, params)``; missing tables count 0."""

    check = StoreCheck(store="", present=path.is_file())
    if not check.present:
        return check

    def read(connection: sqlite3.Connection) -> dict[str, int]:
        rows: dict[str, int] = {}
        tables = _tables(connection)
        for label, (table, where, params) in queries.items():
            if table not in tables:
                rows[label] = 0
                continue
            row = connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params
            ).fetchone()
            rows[label] = int(row[0]) if row else 0
        return rows

    try:
        check.rows = _read_stable(path, read)
    except (sqlite3.Error, OSError):
        check.error = "unreadable"
    return check


def _index_current(path: Path, memory_id: str) -> bool | None:
    """Whether the index lists ``memory_id`` as current (no ``valid_to``/``superseded_by``).

    ``None`` when that cannot be told: the store is unreadable, or it has no
    ``memory_temporal`` row for the id. Callers that found rows for the id
    treat ``None`` as residue (fail closed).
    """

    def read(connection: sqlite3.Connection) -> tuple[Any, ...] | None:
        if "memory_temporal" not in _tables(connection):
            return None
        row = connection.execute(
            "SELECT valid_to, superseded_by FROM memory_temporal WHERE id = ?", (memory_id,)
        ).fetchone()
        return tuple(row) if row is not None else None

    try:
        row = _read_stable(path, read)
    except (sqlite3.Error, OSError):
        return None
    if row is None:
        return None
    return not (str(row[0] or "") or str(row[1] or ""))


def memory_state(repository: Repository, memory_id: str) -> str:
    """``live``, ``closed``, ``deleted`` or ``absent`` (see the module docstring)."""

    path = repository.memories_dir / f"{memory_id}.md"
    if path.is_file():
        document = repository.get(memory_id)
        if document.valid_to is not None or document.superseded_by:
            return "closed"
        return "live"
    deleted = any(
        event.kind == EventKind.DELETE and event.memory_id == memory_id
        for event in repository.events()
    )
    return "deleted" if deleted else "absent"


def projection_residue(
    root: Path | str,
    memory_id: str,
    *,
    related_db: Path | str | None = None,
    entities_db: Path | str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """The residue report for ``memory_id`` (read-only; see the module docstring).

    ``related_db`` defaults to ``<root>/.memfw-related.sqlite``; ``home`` is
    where the usage projection lives (default the user's home directory).
    """

    if not isinstance(memory_id, str) or not _ID_RE.fullmatch(memory_id):
        raise ValueError("memory id is invalid")
    repository = Repository(root)
    if not repository.config_path.is_file():
        raise ValueError("vault root does not exist")
    state = memory_state(repository, memory_id)
    gone = state in ("deleted", "absent")
    checks: list[StoreCheck] = []

    index = _count_rows(
        repository.index_path,
        {
            "memory_rows": ("memory_rows", "id = ?", (memory_id,)),
            "memory_temporal": ("memory_temporal", "id = ?", (memory_id,)),
            "memory_access": ("memory_access", "id = ?", (memory_id,)),
            "memory_fts": ("memory_fts", "id = ?", (memory_id,)),
        },
    )
    index.store = "index"
    if index.present and index.error is None:
        if gone:
            index.residue = any(index.rows.values())
        elif state == "closed" and any(index.rows.values()):
            current = _index_current(repository.index_path, memory_id)
            if current is None:
                # Rows name the id but whether they list it as current cannot
                # be told: fail closed.
                index.residue = True
                index.detail = "index state of the memory is unknown"
            elif current:
                index.residue = True
                index.detail = "index lists the memory as current"
    checks.append(index)

    related_path = (
        Path(related_db) if related_db is not None else repository.root / DEFAULT_RELATED_NAME
    )
    related = _count_rows(
        related_path,
        {
            "related_items": ("related_items", "source = 'memory' AND id = ?", (memory_id,)),
            "related_edges": (
                "related_edges",
                "(src_source = 'memory' AND src_id = ?) OR (dst_source = 'memory' AND dst_id = ?)",
                (memory_id, memory_id),
            ),
        },
    )
    related.store = "related"
    if related.error is None and state != "live":
        related.residue = any(related.rows.values())
    checks.append(related)

    mentions = {"mentions": ("mentions", "memory_id = ?", (memory_id,))}
    stores: list[tuple[str, Path, dict[str, tuple[str, str, tuple[Any, ...]]]]] = [
        ("corroboration", corroboration_path(repository.root, home=home), mentions),
    ]
    legacy = corroboration_legacy_path(repository.root)
    if legacy.is_file() or legacy.is_symlink():
        stores.append(("corroboration_legacy", legacy, mentions))
    stores.append(
        (
            "usage",
            usage_path(repository.root, home=home),
            {"used": ("used", "memory_id = ?", (memory_id,))},
        )
    )
    for store, path, queries in stores:
        check = _count_rows(path, queries)
        check.store = store
        if check.error is None:
            if gone:
                check.residue = any(check.rows.values())
            elif state == "closed":
                check.retained = any(check.rows.values())
        checks.append(check)

    if entities_db is not None:
        entities = _count_rows(
            Path(entities_db), {"entities": ("entities", "memory_id = ?", (memory_id,))}
        )
        entities.store = "entities"
        if entities.error is None:
            if gone:
                entities.residue = any(entities.rows.values())
            elif state == "closed":
                entities.retained = any(entities.rows.values())
        checks.append(entities)

    vault = StoreCheck(store="vault", present=True)
    history = repository.history_dir / memory_id
    vault.rows = {"history": 1 if history.exists() else 0}
    vault.residue = gone and history.exists()
    checks.append(vault)

    for check in checks:
        if check.error is not None:
            # Fail closed: a store that cannot be read is not known to be clean.
            check.residue = True
    residue = [check.store for check in checks if check.residue]
    return {
        "memory_id": memory_id,
        "state": state,
        "clean": None if state == "live" else not residue,
        "residue": residue,
        "checks": [check.as_dict() for check in checks],
    }


def _inside(path: Path, vault: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return resolved == vault or vault in resolved.parents


def list_forget_requests(root: Path | str) -> list[dict[str, Any]]:
    """Deletion requests staged by ``POST /forget``, oldest first. Never applies one.

    A file that is a symlink, resolves outside the vault, or has no readable
    ``forget-request`` header is skipped. ``target_privacy`` is the memory's
    privacy level recorded with the request (``None`` for a request staged
    before it was recorded). ``target_present`` says whether the
    named memory file still exists; ``target_changed`` whether its bytes
    differ from the hash recorded with the request.
    """

    vault = Path(root).expanduser().resolve()
    directory = vault.joinpath(*FORGET_DIRECTORY)
    current = vault
    for part in FORGET_DIRECTORY:
        current = current / part
        if current.is_symlink():
            return []
    if not directory.is_dir() or not _inside(directory, vault):
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("forget-*.md")):
        if path.is_symlink() or not path.is_file() or not _inside(path, vault):
            continue
        try:
            text = path.read_bytes().decode("utf-8")
            _, header, _body = text.split("---\n", 2)
            metadata = yaml.safe_load(header)
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(metadata, dict) or metadata.get("type") != "forget-request":
            continue
        memory_id = str(metadata.get("memory_id") or "")
        target = vault / "memories" / f"{memory_id}.md"
        present = bool(_ID_RE.fullmatch(memory_id)) and target.is_file()
        changed: bool | None = None
        if present:
            digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            changed = digest != str(metadata.get("target_content_hash") or "")
        rows.append(
            {
                "request_id": str(metadata.get("request_id") or ""),
                "memory_id": memory_id,
                "tenant": str(metadata.get("tenant") or ""),
                "source": str(metadata.get("source") or ""),
                "reason": str(metadata.get("reason") or ""),
                "status": str(metadata.get("status") or ""),
                "recorded_at": str(metadata.get("recorded_at") or ""),
                # ``None`` for a request staged before the field existed.
                "target_privacy": (
                    str(metadata["target_privacy"]) if metadata.get("target_privacy") else None
                ),
                "path": "/".join((*FORGET_DIRECTORY, path.name)),
                "target_present": present,
                "target_changed": changed,
            }
        )
    rows.sort(key=lambda row: (row["recorded_at"], row["request_id"]))
    return rows


__all__ = [
    "DEFAULT_RELATED_NAME",
    "FORGET_DIRECTORY",
    "REMEDIES",
    "list_forget_requests",
    "memory_state",
    "projection_residue",
]
