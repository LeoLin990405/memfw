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
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final

from memfw.core.errors import IntegrityError

CHUNK_BYTES: Final = 1024 * 1024
#: Default blob root, relative to the home directory (next to the conversation store).
DEFAULT_BLOB_ROOT: Final = Path(".local/share/memfw/blobs")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
#: Staging area under the blob root (never a vault id: those are UUIDs).
STAGING_DIR: Final = ".staging"
#: A staging file older than this is reported as stale (an interrupted add).
STALE_STAGE_SECONDS: Final = 60 * 60
#: ``.stage-<vault tag>-<random>.tmp``: the tag (see :func:`vault_stage_tag`)
#: says which vault staged it, because ``.staging`` is shared by every vault
#: under the root. A name without a tag (written before tags) is unattributable.
_TAGGED_STAGE_RE: Final = re.compile(r"^\.stage-([0-9a-f]{16})-[^-]+\.tmp$")

#: Synced-folder markers under the home directory, equal to
#: ``memfw.history.transcripts.SYNCED_MARKERS`` (a test keeps them equal; the
#: history package sits above this layer, so it cannot be imported here).
#: ``Documents`` and ``Desktop`` are synced by macOS iCloud "Desktop & Documents".
SYNCED_MARKERS: Final[tuple[tuple[str, ...], ...]] = (
    ("Library", "Mobile Documents"),
    ("Library", "CloudStorage"),
    ("Dropbox",),
    ("Google Drive",),
    ("OneDrive",),
    ("iCloud Drive",),
    ("Nextcloud",),
    ("Syncthing",),
    ("Documents",),
    ("Desktop",),
)


class BlobLocationError(IntegrityError):
    """The blob directory is not a private local location."""


class BlobOwnerError(IntegrityError):
    """The blob directory belongs to another vault (a copy shares its ``vault_id``)."""


#: Owner marker in a vault's blob directory: ``{"vault_id", "vault_tag"}``.
OWNER_FILE: Final = ".owner"
REOWN_HINT: Final = (
    "if this vault is a copy (cp -r, a snapshot, an eval vault), run "
    "`memfw attach reown --new-vault-id --yes` in it; only if it is the original "
    "vault moved to a new path, run `memfw attach reown --yes`"
)


def vault_stage_tag(vault: Path) -> str:
    """The 16-hex tag of ``vault`` in staging file names: sha256 of its resolved root.

    A vault that moves (or is reached by another resolved path) gets a new
    tag, so its own stale stages from before the move are then counted as
    foreign: reported as a number, never deleted by it.
    """

    return hashlib.sha256(str(vault.expanduser().resolve()).encode("utf-8")).hexdigest()[:16]


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
        # Also the marker's target: ``~/Dropbox`` is often a symlink elsewhere.
        for candidate in {synced, synced.resolve()}:
            if resolved == candidate or candidate in resolved.parents:
                raise BlobLocationError("refusing a blob directory inside a synced folder")
    for extra in extra_synced:
        synced = extra.expanduser().resolve()
        if resolved == synced or synced in resolved.parents:
            raise BlobLocationError("refusing a blob directory inside a synced folder")
    return resolved


def _private_dir(directory: Path) -> None:
    """Create ``directory`` 0700 (parents too); refuse a symlink or a non-directory.

    Safe when several processes make the same directory at once (concurrent
    first adds): whoever loses the ``mkdir`` race re-checks what the winner
    made, so a symlink or a file planted there is still refused. Missing
    parents are made as ``Path.mkdir(parents=True)`` makes them.
    """

    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass  # made concurrently; the lstat below checks what is there now
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


def _stage_into(directory: Path, source: BinaryIO, *, max_bytes: int, tag: str) -> StagedBlob:
    _private_dir(directory)
    handle, name = tempfile.mkstemp(prefix=f".stage-{tag}-", suffix=".tmp", dir=directory)
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
    it into the vault's blob directory under the same root. The file name
    carries :func:`vault_stage_tag` of ``vault``, so a stale stage is reported
    (and reclaimed) only by the vault that made it.
    Raises ``ValueError`` (and leaves nothing behind) past ``max_bytes``.
    """

    resolved_root = checked_root(root, vault=vault, extra_synced=extra_synced)
    _private_dir(resolved_root)
    return _stage_into(
        resolved_root / STAGING_DIR, source, max_bytes=max_bytes, tag=vault_stage_tag(vault)
    )


class BlobStore:
    """The blob directory of one vault (``<root>/<vault id>``).

    ``stage_tag`` is the vault's :func:`vault_stage_tag`: staging files with
    another tag belong to another vault under the same root.
    """

    def __init__(self, directory: Path, stage_tag: str, vault_path: str | None = None) -> None:
        self.directory = directory
        self.stage_tag = stage_tag
        #: The owning vault's resolved path, written into the owner marker.
        self.vault_path = vault_path

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
        on macOS). With ``create`` the directories are made 0700; otherwise
        :meth:`link` makes them when it first stores a blob.
        """

        resolved_root = checked_root(root, vault=vault, extra_synced=extra_synced)
        directory = resolved_root / vault_id
        store = cls(directory, vault_stage_tag(vault), str(vault.expanduser().resolve()))
        if create:
            store.ensure()
        elif directory.is_symlink():
            raise BlobLocationError("blob directory is a symlink")
        return store

    def ensure(self) -> None:
        """Create the root and this vault's blob directory (0700); refuse symlinks.

        The first write marks the directory as this vault's (``.owner``); a
        directory another vault marked (a copy of a vault shares its
        ``vault_id``) raises :class:`BlobOwnerError`.
        """

        _private_dir(self.directory.parent)
        _private_dir(self.directory)
        status = self.owner_status()
        if status == "unmarked":
            self.write_owner()
        elif status == "foreign":
            raise BlobOwnerError(f"the blob directory belongs to another vault; {REOWN_HINT}")

    @property
    def owner_path(self) -> Path:
        return self.directory / OWNER_FILE

    def owner_status(self) -> str:
        """``own``, ``foreign`` (another vault's tag), ``unmarked`` or ``absent`` (no directory)."""

        if not self.directory.is_dir() or self.directory.is_symlink():
            return "absent"
        try:
            info = os.lstat(self.owner_path)
        except FileNotFoundError:
            return "unmarked"
        if not stat.S_ISREG(info.st_mode):
            return "foreign"
        try:
            marker = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "foreign"
        if (
            isinstance(marker, dict)
            and marker.get("vault_tag") == self.stage_tag
            and marker.get("vault_id") == self.directory.name
        ):
            return "own"
        return "foreign"

    def write_owner(self) -> None:
        """Mark the directory as this vault's (atomic, 0600)."""

        marker: dict[str, str] = {"vault_id": self.directory.name, "vault_tag": self.stage_tag}
        if self.vault_path is not None:
            marker["vault_path"] = self.vault_path
        data = json.dumps(marker, sort_keys=True).encode("utf-8")
        handle, name = tempfile.mkstemp(prefix=".owner-", suffix=".tmp", dir=self.directory)
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.owner_path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def owner_marker(self) -> dict[str, Any] | None:
        """The owner marker's contents, or ``None`` when absent or unreadable."""

        try:
            marker = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return marker if isinstance(marker, dict) else None

    def require_owner(self) -> None:
        """Raise :class:`BlobOwnerError` unless this vault marked the directory.

        Every delete (purge, ``purge --orphans``, purge recovery) checks it
        first, so a copy of a vault never removes the original's blobs.
        """

        status = self.owner_status()
        if status == "absent":
            return
        if status != "own":
            what = (
                "is not marked as this vault's"
                if status == "unmarked"
                else ("belongs to another vault")
            )
            raise BlobOwnerError(f"the blob directory {what}; {REOWN_HINT}")

    def path_for(self, sha256: str) -> Path:
        sha = check_sha(sha256)
        return self.directory / sha[:2] / sha

    def has(self, sha256: str) -> bool:
        """A file (of any kind) is at the address of ``sha256``; nothing is hashed."""

        try:
            os.lstat(self.path_for(sha256))
        except FileNotFoundError:
            return False
        return True

    def link(self, staged: StagedBlob) -> bool:
        """Move ``staged`` to its content address. ``False``: the blob was already there.

        An existing blob is re-hashed first (only that one blob); one that no
        longer matches its address raises ``IntegrityError`` instead of being
        reused or overwritten. A blob staged under another root (the root
        changed in between) is refused before any directory is created.
        """

        target = self.path_for(staged.sha256)
        if staged.temporary.parent != self.staging:
            staged.discard()
            raise IntegrityError("the blob root changed while the original was staged; retry")
        self.ensure()
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

    def _stale(self, older_than: float) -> tuple[list[Path], int]:
        cutoff = time.time() - older_than
        own: list[Path] = []
        foreign = 0
        for directory in (self.staging, self.directory):
            if directory.is_symlink() or not directory.is_dir():
                continue
            for item in sorted(directory.glob(".stage-*.tmp")):
                try:
                    info = os.lstat(item)
                except FileNotFoundError:
                    continue
                if info.st_mtime >= cutoff or not stat.S_ISREG(info.st_mode):
                    continue
                tagged = _TAGGED_STAGE_RE.fullmatch(item.name)
                # The vault's own blob directory holds only this vault's files.
                if directory == self.directory or (tagged and tagged.group(1) == self.stage_tag):
                    own.append(item)
                else:
                    foreign += 1
        return own, foreign

    def stale_stages(self, *, older_than: float = STALE_STAGE_SECONDS) -> list[str]:
        """This vault's staging temp files left by an interrupted add, older than ``older_than``.

        Looks in the root's staging area (files tagged with this vault) and in
        the blob directory itself. Younger files may belong to an add still
        running, so they are skipped. Names are relative to the blob root;
        nothing is deleted.
        """

        own, _ = self._stale(older_than)
        return [f"{item.parent.name}/{item.name}" for item in own]

    def foreign_stale_stages(self, *, older_than: float = STALE_STAGE_SECONDS) -> int:
        """How many stale staging files under the root belong to other vaults (or to none).

        Only a count: their names would say nothing useful to this vault, and
        this vault never deletes them.
        """

        _, foreign = self._stale(older_than)
        return foreign

    def reclaim_stale_stages(self, *, older_than: float = STALE_STAGE_SECONDS) -> list[str]:
        """Delete this vault's stale staging files (never another vault's). Returns their names."""

        own, _ = self._stale(older_than)
        removed: list[str] = []
        for item in own:
            item.unlink(missing_ok=True)
            removed.append(f"{item.parent.name}/{item.name}")
        return removed

    def stored_bytes(self) -> int:
        """Total size of the files at blob addresses (``lstat`` only, nothing hashed)."""

        total = 0
        for sha in self.stored():
            try:
                total += os.lstat(self.path_for(sha)).st_size
            except FileNotFoundError:
                continue
        return total

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
    "STALE_STAGE_SECONDS",
    "SYNCED_MARKERS",
    "BlobCheck",
    "OWNER_FILE",
    "BlobLocationError",
    "BlobOwnerError",
    "BlobStore",
    "StagedBlob",
    "check_blob_location",
    "checked_root",
    "check_sha",
    "hash_stream",
    "stage_blob",
    "vault_stage_tag",
]
