"""Filesystem primitives the runner builds on: lock, fsync, atomic swap, trust.

All of them work on real paths only. The lock file is opened with
``O_NOFOLLOW`` and the status file is written through an ``O_EXCL`` temporary
file, so a process running as root never follows a symlink someone planted
into its state directory.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import glob
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: Linux ``renameat2`` constants.
AT_FDCWD = -100
RENAME_EXCHANGE = 2
#: macOS ``renamex_np`` flag.
RENAME_SWAP = 0x2
#: errno values meaning "this filesystem/kernel cannot exchange": fall back.
_NO_EXCHANGE = frozenset({errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV})


def private_mkdir(path: Path, mode: int = 0o700) -> None:
    """``path`` as a real directory with ``mode``; every missing parent gets ``mode`` too.

    Existing parents are left as they are (the trust check judges them).
    """
    missing: list[Path] = []
    current = Path(os.path.abspath(path))
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, mode)
            os.chmod(directory, mode)  # not narrowed by the umask
    if path.is_symlink() or not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "not a real directory")
    os.chmod(path, mode)


# --- lock ----------------------------------------------------------------------------------
@contextlib.contextmanager
def single_writer(lock_path: Path, *, wait_seconds: float = 0.0) -> Iterator[bool]:
    """Hold an exclusive, non-blocking ``flock`` on ``lock_path``.

    Yields ``True`` when held, ``False`` when another process still holds it
    after ``wait_seconds`` (retried every 0.1 s, so a ``status`` / ``plan``
    probe that holds the lock for an instant never makes a scheduled run give
    up). The file is opened with ``O_NOFOLLOW``: a symlink planted there raises
    ``OSError``. The lock is released when the process dies, so a killed run
    never wedges the next one. ``flock`` is local to one host: on NFS it may be
    a no-op (``nolock``) or not exclude another host; keep one writer host.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_is_held(lock_path: Path) -> bool | None:
    """Probe the lock without creating anything.

    ``None`` when unknown: no lock file, or one this user cannot open (a non-root
    monitor of a root-run stage). A planted symlink counts as held.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (FileNotFoundError, PermissionError):
        return None
    except OSError:
        return True  # a symlink (ELOOP): treat as not ours to take
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --- fsync ---------------------------------------------------------------------------------
def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    """fsync every regular file and directory under ``root`` (and ``root``).

    Symlinks are neither followed nor synced (``O_NOFOLLOW``).
    """
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(Path(directory))


# --- atomic swap ---------------------------------------------------------------------------
def _platform() -> str:
    return sys.platform


def _load_libc() -> Any:
    """The C library, or ``None`` (tests replace this function)."""
    try:
        return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return None


def exchange(first: Path, second: Path) -> bool:
    """Atomically exchange two directory entries.

    ``True`` when exchanged; ``False`` when this platform, kernel or filesystem
    cannot (the caller falls back). Any other failure raises ``OSError``.
    """
    libc = _load_libc()
    if libc is None:
        return False
    a, b = os.fsencode(first), os.fsencode(second)
    platform = _platform()
    if platform.startswith("linux"):
        fn = getattr(libc, "renameat2", None)
        if fn is None:
            return False
        rc = fn(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE)
    elif platform == "darwin":
        fn = getattr(libc, "renamex_np", None)
        if fn is None:
            return False
        rc = fn(a, b, RENAME_SWAP)
    else:
        return False
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err in _NO_EXCHANGE:
        return False
    raise OSError(err, os.strerror(err))


def new_name(target: Path, kind: str) -> Path:
    """A fresh sibling of ``target``: ``.<name>.<kind>-<pid>-<ns>``."""
    return target.with_name(f".{target.name}.{kind}-{os.getpid()}-{time.monotonic_ns()}")


def swap_in(workdir: Path, target: Path, *, use_exchange: bool = True) -> str:
    """Make ``workdir`` the live ``target``; the previous artifact is removed.

    Returns the method: ``rename`` (no live artifact yet), ``exchange`` (one
    atomic exchange of two real directories) or ``rename_pair`` (two renames;
    a failure of the second puts the old artifact back). On any failure the old
    artifact stays live and ``workdir`` is left for the caller to remove.
    No symlink is used: readers that key on the resolved path see one stable
    directory.
    """
    if not target.exists() and not target.is_symlink():
        os.rename(workdir, target)
        return "rename"
    if target.is_symlink() or not target.is_dir():
        raise OSError(errno.ENOTDIR, "target is not a real directory")
    if use_exchange and exchange(workdir, target):
        fsync_dir(target.parent)  # the exchange is durable before the old copy goes
        shutil.rmtree(workdir, ignore_errors=True)  # workdir now holds the old artifact
        return "exchange"
    old = new_name(target, "old")
    os.rename(target, old)
    try:
        os.rename(workdir, target)
    except OSError:
        os.rename(old, target)
        raise
    fsync_dir(target.parent)  # both renames are durable before the old copy goes
    shutil.rmtree(old, ignore_errors=True)
    return "rename_pair"


def restore_orphaned_old(target: Path) -> str:
    """Put back the previous artifact a crash (or a failed rollback) left as ``.old-*``.

    Only when ``target`` itself is missing: between the two renames of the
    fallback swap the live artifact exists only under that name. Returns
    ``none`` (nothing to do), ``restored``, ``ambiguous`` (more than one
    candidate: a half-removed copy can look newest, and names do not order
    across reboots, so an operator decides) or ``failed`` (the rename failed).
    After ``ambiguous`` or ``failed`` the caller must NOT remove ``.old-*``: one
    of them is the only good copy.
    """
    if target.exists() or target.is_symlink():
        return "none"
    prefix = glob.escape(f".{target.name}.")
    olds = [e for e in target.parent.glob(f"{prefix}old-*") if e.is_dir() and not e.is_symlink()]
    if not olds:
        return "none"
    if len(olds) > 1:
        return "ambiguous"
    try:
        os.rename(olds[0], target)
        fsync_dir(target.parent)
    except OSError:
        return "failed"
    if target.is_symlink() or not target.is_dir():
        return "failed"
    return "restored"


def cleanup_leftovers(target: Path) -> int:
    """Remove ``.<name>.new-*`` / ``.<name>.old-*`` a crashed run left beside ``target``.

    They may hold a full copy of the data. Called under the lock, after
    :func:`restore_orphaned_old`; returns how many entries were removed.
    """
    removed = 0
    prefix = glob.escape(f".{target.name}.")
    for kind in ("new", "old"):
        for entry in target.parent.glob(f"{prefix}{kind}-*"):
            try:
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
                removed += 1
            except OSError:
                pass
    return removed


# --- status file ---------------------------------------------------------------------------
def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from a real file, or ``None`` (missing, symlink, corrupt)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = json.loads(handle.read(1 << 20))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    """Write ``payload`` to ``path`` (``mode``) via an ``O_EXCL`` temp file and a rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)  # not narrowed by the umask
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)


# --- path trust ----------------------------------------------------------------------------
def _geteuid() -> int:
    return os.geteuid()


def path_is_trusted(path: Path) -> bool:
    """``path`` and every ancestor: no symlink, owned by root or this user, not
    group/other-writable.

    Missing components (not created yet) are skipped. One exception: a
    root-owned ancestor directory with the sticky bit (``/tmp``) may be
    world-writable. Run as root, a path another user can write would let
    that user swap the artifact, the config or the stage code under the runner.
    """
    euid = _geteuid()
    path = Path(os.path.abspath(path))
    chain = [path, *path.parents]
    try:
        for component in chain:
            try:
                info = os.lstat(component)
            except FileNotFoundError:
                continue  # not created yet: judged by the ancestors that exist
            if stat.S_ISLNK(info.st_mode):
                return False
            if info.st_uid not in (0, euid):
                return False
            if info.st_mode & 0o022:
                sticky_root = (
                    component != path
                    and stat.S_ISDIR(info.st_mode)
                    and bool(info.st_mode & stat.S_ISVTX)
                    and info.st_uid == 0
                )
                if not sticky_root:
                    return False
    except OSError:
        return False
    return True


__all__ = [
    "cleanup_leftovers",
    "exchange",
    "fsync_dir",
    "fsync_tree",
    "lock_is_held",
    "new_name",
    "path_is_trusted",
    "private_mkdir",
    "read_json",
    "restore_orphaned_old",
    "single_writer",
    "swap_in",
    "write_json_atomic",
]
