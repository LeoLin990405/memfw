"""Where rebuildable per-vault projections live: outside the vault. Stdlib only.

A projection that is derived from (or merely keyed by) a vault but is not
part of it must not sit inside the vault directory: everything there counts
toward the physical-size budget (``memfw.stats.measure_physical_disk_bytes``,
which drives total-mode budget enforcement, where a hard forget deletes
memories) and the transaction fingerprint. So such a file lives in memfw's
local data directory under the home directory, keyed by the vault::

    ~/.local/share/memfw/<kind>/<key>.sqlite3

``<key>`` is :func:`vault_key`: the first 24 hex digits of the sha256 of the
vault's resolved path. The home directory is ``Path.home()`` (the ``HOME``
environment variable on POSIX) unless a caller passes ``home``. The usage
projection (``memfw.usage``, kind ``usage``), the typed-edge projection
(``memfw.edges``, kind ``edges``) and the corroboration projection
(``memfw.corroboration``, kind ``corroboration``) use this rule.

Consequences of keying by the resolved path: a vault reached through a
symlink shares the projection of its target; a moved vault starts without a
projection (the old file stays, orphaned, until deleted); and a vault rooted
at the home directory itself cannot have one, because the file would land
inside the vault (writers refuse that).

**Vault identity.** The path alone does not say *which* vault a projection
was built from: delete a vault and ``memfw init`` a new one at the same path,
and the old projection would answer for the new vault. So a projection
derived from the vault records :func:`vault_identity` in its meta table and
its reader refuses a file whose identity does not match
(:meth:`VaultIdentity.matches`; the message asks for a rebuild). The identity
is the resolved path plus

* ``vault_id``: ``vault.yaml``'s ``vault_id`` when the vault has one (written
  on the first attachment, MM1a; never changed afterwards), and
* ``genesis``: the hash of the vault's first event (from the segment catalog
  once the log has been compacted, else the first line of ``events.jsonl``).
  The hash covers the event's random id and its timestamp, so two vaults
  created separately differ; ``""`` while the log is empty.

Two identities match when the paths are equal and, if both carry a
``vault_id``, the ids are equal; otherwise the genesis hashes must be equal.
(A vault that gets its ``vault_id`` after a build keeps matching through its
genesis.) A vault whose log was still empty at build time no longer matches
once its first event is written: rebuild then, as after any write.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar

from .errors import IntegrityError, MemfwError

_T = TypeVar("_T")

#: Under the home directory: memfw's local data directory.
DATA_ROOT: Final = Path(".local") / "share" / "memfw"
_KIND: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def vault_key(root: Path | str) -> str:
    """The first 24 hex digits of sha256 of the vault's resolved path."""

    resolved = str(Path(root).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def projection_dir(kind: str, *, home: Path | None = None) -> Path:
    """``<home>/.local/share/memfw/<kind>``: the directory one kind of projection uses."""

    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise ValueError("projection kind must be a short lower-case name")
    base = home if home is not None else Path.home()
    return base / DATA_ROOT / kind


def projection_file(root: Path | str, kind: str, *, home: Path | None = None) -> Path:
    """Where the vault's ``kind`` projection lives: outside it, under the home directory."""

    return projection_dir(kind, home=home) / f"{vault_key(root)}.sqlite3"


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) with mode 0700; refuse a symlink or a non-directory.

    An existing directory the current user owns is tightened to 0700 (its
    parents are left alone; a directory owned by someone else is not touched).
    Raises ``NotADirectoryError``.
    """

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise NotADirectoryError("projection directory is not a directory")
    info = os.lstat(path)
    getuid = getattr(os, "getuid", None)
    if stat.S_IMODE(info.st_mode) != 0o700 and (getuid is None or info.st_uid == getuid()):
        os.chmod(path, 0o700)


#: The meta-table keys a projection stores its vault identity under.
IDENTITY_KEYS: Final = ("vault_path", "vault_id", "vault_genesis")
#: The message (and ``rebuild`` hint) for a projection built from another vault.
OTHER_VAULT: Final = "projection belongs to another vault: rebuild"


class VaultHost(Protocol):
    """What :func:`vault_identity` reads from a vault (``Repository`` has it)."""

    @property
    def root(self) -> Path: ...

    @property
    def config_path(self) -> Path: ...

    @property
    def events_path(self) -> Path: ...

    @property
    def log_catalog_path(self) -> Path: ...

    def read_config(self) -> dict[str, Any]: ...

    def events(self) -> Sequence[Any]: ...


@dataclass(frozen=True)
class VaultIdentity:
    """Which vault a projection was built from (see the module docstring)."""

    path: str
    vault_id: str = ""
    genesis: str = ""

    def meta(self) -> dict[str, str]:
        """The rows to store in a projection's meta table (``IDENTITY_KEYS``)."""

        return {"vault_path": self.path, "vault_id": self.vault_id, "vault_genesis": self.genesis}

    def matches(self, meta: Mapping[str, str]) -> bool:
        """Whether a projection whose meta is ``meta`` was built from this vault."""

        if meta.get("vault_path") != self.path:
            return False
        stored_id = meta.get("vault_id") or ""
        if stored_id and self.vault_id:
            return stored_id == self.vault_id
        return meta.get("vault_genesis") == self.genesis


def _first_line_hash(path: Path) -> str | None:
    """``event_hash`` of the first line of ``events.jsonl``; ``""`` when empty, else ``None``."""

    try:
        with path.open("rb") as stream:
            line = stream.readline()
    except FileNotFoundError:
        return ""
    if not line.strip():
        return ""
    try:
        value = json.loads(line).get("event_hash")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _catalog_first_hash(path: Path) -> str | None:
    """The first archived event's hash from the segment catalog, else ``None``."""

    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    segments = catalog.get("segments") if isinstance(catalog, dict) else None
    if not isinstance(segments, list) or not segments or not isinstance(segments[0], dict):
        return None
    value = segments[0].get("first_hash")
    return value if isinstance(value, str) and value else None


class LockingHost(Protocol):
    """What :func:`read_vault` needs from a vault (``Repository`` has it)."""

    def _locked(self) -> AbstractContextManager[None]: ...

    def _tree_fingerprint(self) -> str: ...


class VaultBusyError(MemfwError):
    """An unlocked read kept seeing the vault change (message is safe to show)."""


#: Lock-open errors that mean "may not write the lock file", not "broken".
_LOCK_DENIED: Final = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
#: Unlocked reads tried before giving up (each between two fingerprints).
UNLOCKED_ATTEMPTS: Final = 3


def read_vault(
    host: LockingHost, read: Callable[[], _T], *, attempts: int = UNLOCKED_ATTEMPTS
) -> _T:
    """Run ``read`` (read-only on the vault) on a consistent view of the vault.

    Normally under the vault lock, the one writers hold. When the lock file
    cannot be opened for permission reasons (a read-only vault directory or
    file system, a lock file owned by another user), ``read`` runs unlocked
    between two transaction fingerprints, and is retried (up to ``attempts``
    times, with a short back-off) when they differ or a write was pending;
    then :class:`VaultBusyError`. Other lock errors propagate.
    """

    stack = ExitStack()
    try:
        stack.enter_context(host._locked())
    except OSError as exc:
        stack.close()
        if exc.errno not in _LOCK_DENIED:
            raise
    else:
        with stack:
            return read()
    for attempt in range(attempts):
        before = host._tree_fingerprint()
        try:
            result = read()
        except IntegrityError:
            result = None
            changed = True  # e.g. a write was pending: try again
        else:
            changed = host._tree_fingerprint() != before
        if not changed:
            return result  # type: ignore[return-value]
        time.sleep(0.05 * 2**attempt)
    raise VaultBusyError(
        "the vault changed during every unlocked read (its lock file is not writable): try again"
    )


def vault_identity(host: VaultHost) -> VaultIdentity:
    """The identity of the vault ``host`` opens (see the module docstring).

    Reads ``vault.yaml`` and the start of the event log; when neither the
    catalog nor the first line names the first event (an old catalog, a torn
    line), the verified log is read instead, and its errors propagate.
    """

    config = host.read_config() if host.config_path.exists() else {}
    raw_id = config.get("vault_id")
    vault_id = raw_id if isinstance(raw_id, str) else ""
    genesis: str | None = None
    if host.log_catalog_path.is_file():
        genesis = _catalog_first_hash(host.log_catalog_path)
    else:
        genesis = _first_line_hash(host.events_path)
    if genesis is None:
        events = host.events()
        genesis = str(events[0].event_hash) if events else ""
    return VaultIdentity(str(Path(host.root).resolve()), vault_id, genesis)


__all__ = [
    "DATA_ROOT",
    "IDENTITY_KEYS",
    "OTHER_VAULT",
    "UNLOCKED_ATTEMPTS",
    "LockingHost",
    "VaultBusyError",
    "read_vault",
    "VaultHost",
    "VaultIdentity",
    "vault_identity",
    "ensure_private_dir",
    "projection_dir",
    "projection_file",
    "vault_key",
]
