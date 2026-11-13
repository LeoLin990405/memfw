"""Where the conversation store may live, and its permissions.

The store keeps redacted conversation text, so it stays local and private:
by default ``~/.local/share/memfw/conversations.sqlite`` (``vault.yaml``
``conversation_store.path`` overrides it), directory ``0700`` and file
``0600``. A path inside a git worktree, inside the vault, or inside a synced
folder (iCloud, Dropbox, Google Drive, OneDrive, Syncthing ...) is refused.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .index import HistoryError

CONFIG_KEY = "conversation_store"
DEFAULT_RELATIVE = Path(".local/share/memfw/conversations.sqlite")
_SYNCED_PARTS = (
    ("Library", "Mobile Documents"),
    ("Library", "CloudStorage"),
    ("iCloud Drive",),
    ("Dropbox",),
    ("Google Drive",),
    ("GoogleDrive",),
    ("OneDrive",),
    ("Box Sync",),
    ("pCloud Drive",),
)
_SYNC_MARKERS = (".stfolder", ".dropbox", ".dropbox.cache", ".sync")


def default_store_path(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / DEFAULT_RELATIVE


def store_path_from_config(config: dict[str, Any], home: Path | None = None) -> Path:
    """``conversation_store.path`` from a vault config, else the default."""

    raw = config.get(CONFIG_KEY)
    if raw is None:
        return default_store_path(home)
    if not isinstance(raw, dict):
        raise HistoryError(f"{CONFIG_KEY} must be a mapping")
    value = raw.get("path")
    if value is None:
        return default_store_path(home)
    if not isinstance(value, str) or not value.strip():
        raise HistoryError(f"{CONFIG_KEY}.path must be a non-empty string")
    return Path(value).expanduser()


def _ancestors(path: Path) -> list[Path]:
    return [path, *path.parents]


def _inside_git(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in _ancestors(path.parent))


def _synced(path: Path) -> bool:
    parts = path.parts
    for pattern in _SYNCED_PARTS:
        size = len(pattern)
        if any(parts[i : i + size] == pattern for i in range(len(parts) - size + 1)):
            return True
    return any(
        (parent / marker).exists() for parent in _ancestors(path.parent) for marker in _SYNC_MARKERS
    )


def check_store_path(path: Path, *, vault_root: Path | None = None) -> Path:
    """The resolved store path, or :class:`HistoryError` when the location is refused."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    if vault_root is not None:
        vault = Path(vault_root).expanduser().resolve()
        if resolved == vault or vault in resolved.parents:
            raise HistoryError("conversation store must not be inside the vault")
    if _inside_git(resolved):
        raise HistoryError("conversation store must not be inside a git worktree")
    if _synced(resolved):
        raise HistoryError("conversation store must not be inside a synced folder")
    return resolved


def prepare_store_path(path: Path) -> None:
    """Create the store's directory (``0700``) and file (``0600``); tighten looser modes."""

    directory = path.parent
    if directory.is_symlink():
        raise HistoryError("conversation store directory must not be a symlink")
    if not directory.exists():
        try:
            directory.mkdir(mode=0o700, parents=True)
            os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask
        except OSError as exc:
            raise HistoryError(f"cannot create conversation store directory: {exc}") from exc
    # An existing directory is left as it is: it may be shared (only the file is 0600).
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HistoryError(f"cannot open conversation store: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HistoryError("conversation store is not a regular file")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_file() and not sidecar.is_symlink():
            os.chmod(sidecar, 0o600)
