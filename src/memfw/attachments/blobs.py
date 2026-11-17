"""Content-addressed blob storage for attachment originals, outside the vault (MM1a).

A blob is the exact bytes of one original, stored once per sha256 at
``<blob dir>/<sha[:2]>/<sha>``. The blob dir is ``<blob root>/<vault id>``;
the blob root defaults to ``~/.local/share/memfw/blobs`` (the memfw data dir)
and is set with ``attachments_blob_root`` in ``vault.yaml``. Directories are
0700 and files 0600; a symlink anywhere this module creates or opens is
refused. Nothing here reads or writes the vault: the vault keeps only the
small attachment records (``memfw.attachments.store``), so vault writes,
``_tree_fingerprint``, temp-file cleanup and the cold archive never see blob
bytes.

Hashing and copying stream in 1 MiB chunks. Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from memfw.core.errors import IntegrityError

CHUNK_BYTES: Final = 1024 * 1024
#: Default blob root, relative to the home directory (next to the conversation store).
DEFAULT_BLOB_ROOT: Final = Path(".local/share/memfw/blobs")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
#: Staging area under the blob root (never a vault id: those are UUIDs).
STAGING_DIR: Final = ".staging"

#: Synced-folder markers under the home directory. Same list as
#: ``memfw.history.transcripts.SYNCED_MARKERS`` (kept equal by a test; the
#: history package sits above this layer, so it cannot be imported here).
SYNCED_MARKERS: Final[tuple[tuple[str, ...], ...]] = (
    ("Library", "Mobile Documents"),
    ("Library", "CloudStorage"),
    ("Dropbox",),
    ("Google Drive",),
    ("OneDrive",),
    ("iCloud Drive",),
    ("Nextcloud",),
    ("Syncthing",),
)


class BlobLocationError(IntegrityError):
    """The blob directory is not a private local location."""


def check_sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise IntegrityError("Invalid sha256")
    return value


def check_blob_location(path: Path, *, vault: Path, extra_synced: Sequence[Path] = ()) -> Path:
    """``path`` resolved; refuse a git work tree, the vault or a synced folder.

    The rules of ``memfw.history.transcripts.check_private_location``.
    """

    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    for ancestor in [resolved, *resolved.parents]:
        if (ancestor / ".git").exists():
            raise BlobLocationError("refusing a blob directory inside a git work tree")
    vault_root = vault.expanduser().resolve()
    if resolved == vault_root or vault_root in resolved.parents or resolved in vault_root.parents:
        raise BlobLocationError("refusing a blob directory inside (or containing) the vault")
    for marker in SYNCED_MARKERS:
        synced = home.joinpath(*marker)
        if resolved == synced or synced in resolved.parents:
            raise BlobLocationError("refusing a blob directory inside a synced folder")
    for extra in extra_synced:
        synced = extra.expanduser().resolve()
        if resolved == synced or synced in resolved.parents:
            raise BlobLocationError("refusing a blob directory inside a synced folder")
    return resolved


def _private_dir(directory: Path) -> None:
    """Create ``directory`` 0700 (parents too); refuse a symlink or a non-directory."""

    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700)
        info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BlobLocationError(f"blob directory is not a plain directory: {directory.name}")
    if info.st_mode & 0o077:
        os.chmod(directory, 0o700)


def hash_stream(stream: BinaryIO) -> tuple[str, int]:
    """sha256 and length of ``stream`` read to the end in 1 MiB chunks."""

    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class StagedBlob:
    """Bytes copied into the blob dir under a temporary name, not yet linked."""

    sha256: str
    size: int
    temporary: Path

    def discard(self) -> None:
        self.temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class BlobCheck:
    """What :meth:`BlobStore.check` found for one sha256."""

    sha256: str
    status: str  # ok | missing | not_regular | size_mismatch | modified
    actual_sha256: str | None = None
    actual_size: int | None = None
    loose_mode: bool = False


def checked_root(root: Path, *, vault: Path, extra_synced: Sequence[Path] = ()) -> Path:
    """``root`` resolved and location-checked; nothing is created.

    ``root`` itself may not be a symlink (its parents may: ``/tmp`` is one on macOS).
    """

    expanded = root.expanduser()
    if expanded.is_symlink():
        raise BlobLocationError("blob root is a symlink")
    return check_blob_location(expanded, vault=vault, extra_synced=extra_synced)


def _stage_into(directory: Path, source: BinaryIO, *, max_bytes: int) -> StagedBlob:
    _private_dir(directory)
    handle, name = tempfile.mkstemp(prefix=".stage-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as target:
            while chunk := source.read(CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"attachment is larger than {max_bytes} bytes")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return StagedBlob(digest.hexdigest(), size, temporary)


def stage_blob(
    root: Path,
    source: BinaryIO,
    *,
    vault: Path,
    max_bytes: int,
    extra_synced: Sequence[Path] = (),
) -> StagedBlob:
    """Copy ``source`` into ``<root>/.staging`` (0600) while hashing it.

    The staging area does not depend on a vault id, so an original can be
    staged before the vault lock is taken; :meth:`BlobStore.link` then moves
    it into the vault's blob directory under the same root.
    Raises ``ValueError`` (and leaves nothing behind) past ``max_bytes``.
    """

    resolved_root = checked_root(root, vault=vault, extra_synced=extra_synced)
    _private_dir(resolved_root)
    return _stage_into(resolved_root / STAGING_DIR, source, max_bytes=max_bytes)


class BlobStore:
    """The blob directory of one vault (``<root>/<vault id>``)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def staging(self) -> Path:
        """The root's staging area (shared by every vault under the root)."""

        return self.directory.parent / STAGING_DIR

    @classmethod
    def open(
        cls,
        root: Path,
        vault_id: str,
        *,
        vault: Path,
        extra_synced: Sequence[Path] = (),
        create: bool = False,
    ) -> BlobStore:
        """The blob dir for ``vault_id`` under ``root``, location-checked.

        ``root`` itself may not be a symlink (its parents may: ``/tmp`` is one
        on macOS). With ``create`` the directories are made 0700.
        """

        resolved_root = checked_root(root, vault=vault, extra_synced=extra_synced)
        directory = resolved_root / vault_id
        if create:
            _private_dir(resolved_root)
            _private_dir(directory)
        elif directory.is_symlink():
            raise BlobLocationError("blob directory is a symlink")
        return cls(directory)

    def path_for(self, sha256: str) -> Path:
        sha = check_sha(sha256)
        return self.directory / sha[:2] / sha

    def stage(self, source: BinaryIO, *, max_bytes: int) -> StagedBlob:
        """Copy ``source`` into the root's staging area while hashing it."""

        _private_dir(self.directory.parent)
        return _stage_into(self.staging, source, max_bytes=max_bytes)

    def link(self, staged: StagedBlob) -> bool:
        """Move ``staged`` to its content address. ``False``: the blob was already there.

        An existing blob is re-hashed first (only that one blob); one that no
        longer matches its address raises ``IntegrityError`` instead of being
        reused or overwritten.
        """

        target = self.path_for(staged.sha256)
        if staged.temporary.parent != self.staging:
            staged.discard()
            raise IntegrityError("the blob root changed while the original was staged; retry")
        _private_dir(target.parent)
        existing = self.check(staged.sha256, expected_size=staged.size)
        if existing.status == "ok":
            staged.discard()
            return False
        if existing.status != "missing":
            staged.discard()
            raise IntegrityError(
                f"stored blob {staged.sha256[:12]} is {existing.status}; run `memfw attach verify`"
            )
        os.replace(staged.temporary, target)
        os.chmod(target, 0o600)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True

    def check(self, sha256: str, *, expected_size: int | None = None) -> BlobCheck:
        """Re-hash one blob (streamed). Never follows a symlink."""

        path = self.path_for(sha256)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return BlobCheck(sha256, "missing")
        if not stat.S_ISREG(info.st_mode):
            return BlobCheck(sha256, "not_regular")
        loose = bool(info.st_mode & 0o077)
        if expected_size is not None and info.st_size != expected_size:
            return BlobCheck(sha256, "size_mismatch", actual_size=info.st_size, loose_mode=loose)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            actual, size = hash_stream(stream)
        if actual != sha256:
            return BlobCheck(sha256, "modified", actual, size, loose)
        return BlobCheck(sha256, "ok", actual, size, loose)

    def stored(self) -> Iterator[str]:
        """Every sha256 with a file at its address (no hashing). Temp files skipped."""

        if not self.directory.is_dir() or self.directory.is_symlink():
            return
        for shard in sorted(self.directory.iterdir()):
            if shard.is_symlink() or not shard.is_dir() or len(shard.name) != 2:
                continue
            for item in sorted(shard.iterdir()):
                if _SHA_RE.fullmatch(item.name) and item.name[:2] == shard.name:
                    yield item.name


__all__ = [
    "CHUNK_BYTES",
    "DEFAULT_BLOB_ROOT",
    "STAGING_DIR",
    "SYNCED_MARKERS",
    "BlobCheck",
    "BlobLocationError",
    "BlobStore",
    "StagedBlob",
    "check_blob_location",
    "checked_root",
    "check_sha",
    "hash_stream",
    "stage_blob",
]
