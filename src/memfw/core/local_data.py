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
projection (``memfw.usage``, kind ``usage``) and the typed-edge projection
(``memfw.edges``, kind ``edges``) use this rule.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Final

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


__all__ = [
    "DATA_ROOT",
    "ensure_private_dir",
    "projection_dir",
    "projection_file",
    "vault_key",
]
