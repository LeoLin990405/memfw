"""Referenced attachments: configured roots and safe locations under them (MM1b).

A referenced attachment keeps no copy of the original. Its record stores a
**location** ``<root name>:<relative path>`` (``kh:papers/scan.pdf``) and the
original's sha256 and size. Root names map to absolute directories in
``vault.yaml`` (``attachments_roots``), so each host maps ``kh`` to wherever
that host mounts the tree; a record never holds an absolute path.

Path safety (design H6), checked on every add, verify, relink and scan:

* a relative path is POSIX (``/``-separated), never absolute, never empty,
  and has no ``.`` / ``..`` / empty component, backslash, NUL or control
  character;
* a file given by its filesystem path must lie lexically inside a configured
  root (the path is never resolved first, so ``..`` or a symlink cannot carry
  it out); a path with a ``..`` component is refused outright;
* opening walks the path one component at a time from the root with
  ``dir_fd`` and ``O_NOFOLLOW``: any component that is a symlink is refused,
  the last one must be a regular file;
* every component must match a directory entry **exactly** (same case, same
  Unicode normalization). On a case-insensitive filesystem ``Scan.PDF`` would
  open ``scan.pdf``; such a spelling is refused, so a location means the same
  file on macOS and on a case-sensitive Linux host.

Standard library only.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from memfw.core.errors import IntegrityError

#: ``vault.yaml`` key: ``{name: absolute or ~-prefixed directory}``.
ROOTS_KEY: Final = "attachments_roots"
_ROOT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: Names that would read as another kind of reference (or a URL scheme).
RESERVED_ROOT_NAMES: Final = frozenset(
    {"att", "data", "file", "ftp", "http", "https", "mem", "memory", "sha256", "urn"}
)
_MAX_COMPONENT_BYTES: Final = 255
_MAX_LOCATION_BYTES: Final = 4096


class LocationError(IntegrityError):
    """A location or path that is refused (outside the roots, a symlink, malformed)."""


def _check_root_name(name: Any) -> str:
    if not isinstance(name, str) or not _ROOT_NAME_RE.fullmatch(name):
        raise IntegrityError(
            f"{ROOTS_KEY}: root name {name!r} must match [a-z][a-z0-9_-]* (at most 32)"
        )
    if name in RESERVED_ROOT_NAMES:
        raise IntegrityError(f"{ROOTS_KEY}: root name {name!r} is reserved")
    return name


def validate_roots_config(
    raw: Mapping[str, Any], *, vault: Path | None = None, blob_root: Path | None = None
) -> None:
    """Check ``attachments_roots`` in a ``vault.yaml`` mapping (absent is fine).

    A root may not be ``/``, the home directory, inside or containing the vault,
    or containing the blob root (``vault`` / ``blob_root`` when known).
    """

    if ROOTS_KEY not in raw:
        return
    roots = raw[ROOTS_KEY]
    if not isinstance(roots, dict):
        raise IntegrityError(f"{ROOTS_KEY} must be a mapping of root names to directories")
    for name, directory in roots.items():
        _check_root_name(name)
        if not isinstance(directory, str) or not directory.strip():
            raise IntegrityError(f"{ROOTS_KEY}.{name} must be a non-empty path string")
        # A relative root would resolve against the working directory of each command.
        if not (directory.startswith("~") or Path(directory).is_absolute()):
            raise IntegrityError(f"{ROOTS_KEY}.{name} must be an absolute or ~-prefixed path")
        if ".." in PurePosixPath(directory).parts:
            raise IntegrityError(f"{ROOTS_KEY}.{name} must not contain '..'")
        root = Path(directory).expanduser().resolve()
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise IntegrityError(f"{ROOTS_KEY}.{name} must not be / or the home directory")
        if vault is not None:
            vault_root = vault.expanduser().resolve()
            if root == vault_root or vault_root in root.parents:
                raise IntegrityError(f"{ROOTS_KEY}.{name} must not be inside the vault")
            if root in vault_root.parents:
                raise IntegrityError(f"{ROOTS_KEY}.{name} must not contain the vault")
        if blob_root is not None:
            blobs = blob_root.expanduser().resolve()
            if root == blobs or root in blobs.parents:
                raise IntegrityError(f"{ROOTS_KEY}.{name} must not contain the blob root")


def configured_roots(config: Mapping[str, Any]) -> dict[str, Path]:
    """Root name → directory (``~`` expanded, not resolved). Invalid entries are skipped."""

    roots = config.get(ROOTS_KEY)
    if not isinstance(roots, dict):
        return {}
    found: dict[str, Path] = {}
    for name, directory in roots.items():
        try:
            _check_root_name(name)
        except IntegrityError:
            continue
        if isinstance(directory, str) and directory.strip():
            path = Path(directory).expanduser()
            if path.is_absolute():
                found[name] = path
    return found


def check_relative(relative: str) -> tuple[str, ...]:
    """The components of a location's relative path; anything unsafe raises ``LocationError``."""

    if not isinstance(relative, str) or not relative:
        raise LocationError("a location needs a relative path after the root name")
    if len(relative.encode("utf-8", "surrogatepass")) > _MAX_LOCATION_BYTES:
        raise LocationError("location is too long")
    if relative.startswith("/"):
        raise LocationError("a location's path must be relative to its root")
    if "\\" in relative:
        raise LocationError("a location's path uses '/' only (no backslash)")
    if any(not char.isprintable() for char in relative):
        raise LocationError("a location's path may not contain control characters")
    parts = tuple(relative.split("/"))
    for part in parts:
        if part in {"", ".", ".."}:
            raise LocationError("a location's path may not contain '', '.' or '..' components")
        if len(part.encode("utf-8", "surrogatepass")) > _MAX_COMPONENT_BYTES:
            raise LocationError("a location's path component is too long")
    return parts


@dataclass(frozen=True)
class Location:
    """``<root>:<relative path>``, checked lexically (nothing on disk)."""

    root: str
    parts: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> Location:
        if not isinstance(text, str) or ":" not in text:
            raise LocationError("a location is <root name>:<relative path>")
        root, _, relative = text.partition(":")
        try:
            _check_root_name(root)
        except IntegrityError as exc:
            raise LocationError(str(exc)) from None
        return cls(root, check_relative(relative))

    @property
    def relative(self) -> str:
        return "/".join(self.parts)

    @property
    def name(self) -> str:
        return self.parts[-1]

    def __str__(self) -> str:
        return f"{self.root}:{self.relative}"


def looks_like_location(text: str, roots: Mapping[str, Path]) -> bool:
    """``text`` starts with ``<a configured root name>:``."""

    root, sep, _ = text.partition(":")
    return bool(sep) and root in roots


def location_for_path(path: Path, roots: Mapping[str, Path]) -> Location:
    """The location of a filesystem ``path`` under the configured ``roots``.

    Purely lexical: ``path`` is made absolute against the working directory
    but never resolved, so neither ``..`` nor a symlink can move it into or
    out of a root. A ``..`` component is refused. Each root matches as
    configured and as resolved (``/tmp`` is ``/private/tmp`` on macOS); the
    most specific root wins.
    """

    raw = str(path)
    if "\\" in raw:
        raise LocationError("a path to reference uses '/' only (no backslash)")
    candidate = PurePosixPath(os.path.join(os.getcwd(), raw) if not os.path.isabs(raw) else raw)
    if ".." in candidate.parts:
        raise LocationError("refusing a path with '..'; give the path inside the root directly")
    parts = tuple(part for part in candidate.parts if part != ".")
    best: tuple[int, str, tuple[str, ...]] | None = None
    for name, directory in roots.items():
        for form in {directory, directory.resolve()}:
            root_parts = PurePosixPath(form).parts
            if parts[: len(root_parts)] == root_parts and len(parts) > len(root_parts):
                relative = parts[len(root_parts) :]
                if best is None or len(root_parts) > best[0]:
                    best = (len(root_parts), name, relative)
    if best is None:
        raise LocationError("the file is not inside any configured attachments root")
    _, name, relative = best
    return Location(name, check_relative("/".join(relative)))


#: One directory listing per directory (``(st_dev, st_ino)``) for one pass.
ListingCache = dict[tuple[int, int], frozenset[str]]


def _exact_entry(directory_fd: int, name: str, cache: ListingCache | None) -> bool:
    """``name`` is spelled exactly as a directory entry (case and normalization)."""

    if cache is None:
        return name in os.listdir(directory_fd)
    info = os.fstat(directory_fd)
    key = (info.st_dev, info.st_ino)
    names = cache.get(key)
    if names is None:
        names = cache[key] = frozenset(os.listdir(directory_fd))
    return name in names


def _walk_to(
    location: Location, roots: Mapping[str, Path], cache: ListingCache | None
) -> tuple[int, os.stat_result]:
    """(fd of the last component's directory, ``lstat`` of the last component).

    The configured root itself is trusted (a mount point or a symlink a human
    configured, such as ``/tmp`` on macOS, is followed); every component below
    it is opened with ``O_NOFOLLOW``. The caller closes the fd.
    """

    root = roots.get(location.root)
    if root is None:
        raise LocationError(f"attachments root {location.root!r} is not configured on this host")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        current = os.open(root, os.O_RDONLY | directory_flag)
    except FileNotFoundError:
        raise FileNotFoundError(f"attachments root {location.root!r} is not available") from None
    try:
        for index, part in enumerate(location.parts):
            if not _exact_entry(current, part, cache):
                raise FileNotFoundError(f"{location}: no such file")
            info = os.lstat(part, dir_fd=current)
            if stat.S_ISLNK(info.st_mode):
                raise LocationError(f"{location}: refusing a symlink in the path")
            if index == len(location.parts) - 1:
                return current, info
            if not stat.S_ISDIR(info.st_mode):
                raise FileNotFoundError(f"{location}: no such file")
            following = os.open(part, os.O_RDONLY | directory_flag | nofollow, dir_fd=current)
            os.close(current)
            current = following
    except OSError as exc:
        os.close(current)
        if isinstance(exc, FileNotFoundError):
            raise
        if exc.errno in {getattr(os, "ELOOP", -1), getattr(os, "ENOTDIR", -1)}:
            raise LocationError(f"{location}: refusing a symlink in the path") from None
        raise
    except BaseException:
        os.close(current)
        raise
    os.close(current)  # pragma: no cover - parts is non-empty
    raise LocationError(f"{location}: empty location")  # pragma: no cover


def stat_location(
    location: Location, roots: Mapping[str, Path], *, cache: ListingCache | None = None
) -> os.stat_result:
    """``lstat`` of the regular file at ``location`` (same rules as :func:`open_location`)."""

    directory, info = _walk_to(location, roots, cache)
    os.close(directory)
    if not stat.S_ISREG(info.st_mode):
        raise LocationError(f"{location}: only regular files can be referenced")
    return info


def open_location(
    location: Location, roots: Mapping[str, Path], *, cache: ListingCache | None = None
) -> tuple[int, os.stat_result]:
    """Open the regular file at ``location`` read-only, walking from its root without symlinks.

    Returns ``(fd, fstat)``; the caller closes ``fd``. Raises
    ``FileNotFoundError`` when a component is absent (also when only a
    different spelling exists: the location is then missing, not
    redirected), ``LocationError`` for a symlink, a non-regular file or an
    unconfigured root. ``cache`` shares directory listings across calls.
    """

    directory, info = _walk_to(location, roots, cache)
    try:
        if not stat.S_ISREG(info.st_mode):
            raise LocationError(f"{location}: only regular files can be referenced")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            # O_NONBLOCK: a FIFO swapped in after the lstat does not hang the open.
            handle = os.open(
                location.name, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=directory
            )
        except OSError as exc:
            if exc.errno == getattr(os, "ELOOP", -1):
                raise LocationError(f"{location}: refusing a symlink in the path") from None
            raise
        opened = os.fstat(handle)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            os.close(handle)
            raise LocationError(f"{location}: the file changed while it was being opened")
        os.set_blocking(handle, True)
        return handle, opened
    finally:
        os.close(directory)


@dataclass(frozen=True)
class TreeFile:
    """One regular file found by :func:`walk_root` (never through a symlink)."""

    location: Location
    size: int
    #: ``st_nlink``: more than one means the bytes are reachable under another path.
    links: int = 1


@dataclass
class WalkStats:
    symlinks: int = 0
    unsafe_names: int = 0
    other: int = 0
    unreadable_dirs: int = 0
    truncated: bool = False


def walk_root(
    root_name: str,
    roots: Mapping[str, Path],
    *,
    start: tuple[str, ...] = (),
    stats: WalkStats | None = None,
    max_entries: int | None = None,
) -> Iterator[TreeFile]:
    """Every regular file under ``<root>/<start>``; symlinks are counted and never followed.

    Each directory is opened with ``O_NOFOLLOW`` relative to its parent's fd
    and listed through that fd, so a directory swapped for a symlink while
    walking is not entered. Names that a location could not hold (backslash,
    control characters) are counted in ``stats.unsafe_names`` and skipped.
    Read-only.
    """

    root = roots.get(root_name)
    if root is None:
        raise LocationError(f"attachments root {root_name!r} is not configured on this host")
    stats = stats if stats is not None else WalkStats()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | directory_flag | nofollow
    label = f"{root_name}:{'/'.join(start)}"
    try:
        base = os.open(root, os.O_RDONLY | directory_flag)
    except FileNotFoundError:
        raise FileNotFoundError(f"{label}: no such directory") from None
    try:
        for part in start:
            if part not in os.listdir(base):
                raise FileNotFoundError(part)
            if stat.S_ISLNK(os.lstat(part, dir_fd=base).st_mode):
                raise LocationError(f"{label}: refusing a symlink in the path")
            following = os.open(part, flags, dir_fd=base)
            os.close(base)
            base = following
    except FileNotFoundError:
        os.close(base)
        raise FileNotFoundError(f"{label}: no such directory") from None
    except NotADirectoryError:
        os.close(base)
        raise FileNotFoundError(f"{label}: no such directory") from None
    except BaseException:
        os.close(base)
        raise
    seen = 0
    pending: list[tuple[int, tuple[str, ...]]] = [(base, start)]
    try:
        while pending:
            directory, prefix = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name, reverse=True)
                for entry in entries:
                    seen += 1
                    if max_entries is not None and seen > max_entries:
                        stats.truncated = True
                        return
                    try:
                        info = os.lstat(entry.name, dir_fd=directory)
                    except OSError:
                        stats.other += 1
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        stats.symlinks += 1
                        continue
                    parts = (*prefix, entry.name)
                    try:
                        check_relative("/".join(parts))
                    except LocationError:
                        stats.unsafe_names += 1
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child = os.open(entry.name, flags, dir_fd=directory)
                        except OSError:
                            stats.unreadable_dirs += 1
                            continue
                        pending.append((child, parts))
                    elif stat.S_ISREG(info.st_mode):
                        yield TreeFile(Location(root_name, parts), info.st_size, info.st_nlink)
                    else:
                        stats.other += 1
            except OSError:
                stats.unreadable_dirs += 1
            finally:
                os.close(directory)
    finally:
        for directory, _ in pending:
            os.close(directory)


__all__ = [
    "RESERVED_ROOT_NAMES",
    "ROOTS_KEY",
    "Location",
    "ListingCache",
    "LocationError",
    "TreeFile",
    "WalkStats",
    "check_relative",
    "configured_roots",
    "location_for_path",
    "looks_like_location",
    "open_location",
    "stat_location",
    "validate_roots_config",
    "walk_root",
]
