"""Repository mixin: register, read, verify and fsck attachments (MM1a).

Write order (design v2.1, H2): the original is hashed and copied into the blob
store first (outside the vault lock, under a temporary name), then, holding
the vault lock, the blob is linked to its content address, the record
``attachments/<id>.md`` is written and one ``attach`` event is appended. A
crash after the blob and before the record leaves an orphan blob, which
``attach verify`` and ``fsck`` report; nothing in MM1a deletes a blob
(reclaiming orphans belongs with ``attach purge`` in MM1b).

The reference count of a blob is derived from the records; it is never
stored. Vault writes never read the blob store, so their cost does not grow
with the blobs' total size.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from memfw.core.errors import IntegrityError, MemfwError, NotFoundError
from memfw.core.models import ATTACHMENT_EVENT_KINDS, Event, EventKind
from memfw.ports.privacy import privacy_value
from memfw.storage.transactions import _serialized

from .blobs import DEFAULT_BLOB_ROOT, BlobLocationError, BlobStore, StagedBlob, stage_blob
from .records import (
    ATTACHMENTS_DIR,
    DEFAULT_ATTACHMENT_PRIVACY,
    AttachmentRecord,
    check_attachment_id,
    check_media_type,
    check_retention,
    clean_name,
    new_attachment_id,
    parse_record,
    render_record,
)

if TYPE_CHECKING:
    from memfw.core.context import RepositoryHost

#: ``vault.yaml`` keys (all optional; written only when attachments are used).
VAULT_ID_KEY: Final = "vault_id"
BLOB_ROOT_KEY: Final = "attachments_blob_root"
MAX_BYTES_KEY: Final = "attachments_max_bytes"
MAX_TOTAL_KEY: Final = "attachments_max_total_bytes"
#: Design section 8: 25 MiB per original, 2 GiB per vault.
DEFAULT_MAX_BYTES: Final = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES: Final = 2 * 1024 * 1024 * 1024
#: Media families that default to a referenced attachment (design v2 "上限").
REFERENCE_BY_DEFAULT: Final = ("audio/", "video/")
_RECORD_KINDS: Final = frozenset({EventKind.ATTACH, EventKind.ATTACH_UPDATE})


def validate_attachment_config(raw: dict[str, Any]) -> None:
    """Check the attachment keys of a ``vault.yaml`` mapping (absent keys are fine)."""

    vault_id = raw.get(VAULT_ID_KEY)
    if VAULT_ID_KEY in raw:
        try:
            if not isinstance(vault_id, str) or str(uuid.UUID(vault_id)) != vault_id:
                raise ValueError
        except ValueError:
            raise IntegrityError("vault_id must be a lowercase UUID string") from None
    if BLOB_ROOT_KEY in raw:
        root = raw[BLOB_ROOT_KEY]
        if not isinstance(root, str) or not root.strip():
            raise IntegrityError(f"{BLOB_ROOT_KEY} must be a non-empty path string")
        # A relative root would resolve against the working directory of each command.
        if not (root.startswith("~") or Path(root).is_absolute()):
            raise IntegrityError(f"{BLOB_ROOT_KEY} must be an absolute or ~-prefixed path")
    for key in (MAX_BYTES_KEY, MAX_TOTAL_KEY):
        value = raw.get(key)
        if key in raw and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise IntegrityError(f"{key} must be a positive integer")


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _blob_root(config: dict[str, Any]) -> Path:
    configured = config.get(BLOB_ROOT_KEY)
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / DEFAULT_BLOB_ROOT


@dataclass(frozen=True)
class AttachResult:
    record: AttachmentRecord
    #: The blob was already stored (another record shares it).
    deduplicated: bool


@dataclass(frozen=True)
class AttachmentProblem:
    attachment_id: str | None
    status: str
    sha256: str | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.attachment_id,
            "status": self.status,
            "sha256": self.sha256,
            "message": self.message,
        }


def _attachments_dir(host: RepositoryHost) -> Path:
    return host.root / ATTACHMENTS_DIR


def _record_path(host: RepositoryHost, attachment_id: str) -> Path:
    return _attachments_dir(host) / f"{check_attachment_id(attachment_id)}.md"


def committed_records(events: Iterable[Event]) -> dict[str, str]:
    """Attachment id → committed record hash, from the event log."""

    committed: dict[str, str] = {}
    for event in events:
        if event.kind in _RECORD_KINDS and event.after_hash is not None:
            committed[event.memory_id] = event.after_hash
    return committed


def embedded_total(events: Iterable[Event], store: BlobStore) -> int:
    """Bytes the vault's embedded blobs take, for the ``attachments_max_total_bytes`` check.

    Each blob is counted once, by the ``attach`` event that stored it
    (``details.blob == "new"``) while its attachment is committed. A log with
    a committed ``attach`` event that predates ``details.size`` (or carries a
    malformed ``size`` / ``blob``) falls back to
    the sizes (``lstat``) of the files in the blob directory. Neither way
    parses a record, so the cost of an add does not grow with record parses.
    """

    sizes: dict[str, int] = {}
    owners: set[str] = set()
    legacy = False
    for event in events:
        if event.kind != EventKind.ATTACH:
            continue
        details = event.details
        if details.get("storage", "embedded") != "embedded":
            continue
        size = details.get("size")
        blob = details.get("blob")
        # Anything but a non-negative int size and a known blob value is read
        # as an event without them (an old or hand-edited log): fall back.
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or blob not in {"new", "shared"}
        ):
            legacy = True
            break
        sizes[event.memory_id] = size
        if blob == "new":
            owners.add(event.memory_id)
    if legacy:
        return store.stored_bytes()
    return sum(sizes[identifier] for identifier in owners)


def _vault_id(host: RepositoryHost) -> str | None:
    if not host.config_path.exists():
        return None
    value = host.read_config().get(VAULT_ID_KEY)
    return value if isinstance(value, str) else None


def blob_store(host: RepositoryHost, *, create: bool = False) -> BlobStore | None:
    """This vault's blob store; ``None`` before the vault has a ``vault_id``."""

    vault_id = _vault_id(host)
    if vault_id is None:
        return None
    return BlobStore.open(_blob_root(host.read_config()), vault_id, vault=host.root, create=create)


def _open_source(source: Path) -> tuple[int, os.stat_result]:
    """Open a regular file without following a symlink at its last component."""

    try:
        info = os.lstat(source)
    except FileNotFoundError:
        raise ValueError(f"no such file: {source.name}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("refusing a symlink; attach the file it points to")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("only regular files can be attached")
    handle = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    opened = os.fstat(handle)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(
        opened.st_mode
    ):
        os.close(handle)
        raise ValueError("the file changed while it was being opened")
    return handle, opened


class AttachmentStoreMixin:
    """Attachment records in ``attachments/`` plus their blobs outside the vault."""

    # -- configuration -----------------------------------------------------------

    def ensure_vault_id(self: RepositoryHost) -> str:
        """This vault's ``vault_id``, generated (one ``config_change``) on first use.

        Generation holds the vault lock and re-reads ``vault.yaml`` under it,
        so concurrent first uses agree on one id and write it once.
        """

        existing = _vault_id(self)
        if existing is not None:
            return existing
        with self._locked():
            self.init()
            existing = _vault_id(self)
            if existing is not None:
                return existing
            return AttachmentStoreMixin._write_vault_id(self)

    def _write_vault_id(self: RepositoryHost) -> str:
        config = self.read_config()
        # Always random, never from ``id_factory``: a seeded factory (``--id-seed``)
        # would give vaults that share one blob root the same blob directory.
        config[VAULT_ID_KEY] = str(uuid.uuid4())
        cast(Any, self).configure(config)
        return str(config[VAULT_ID_KEY])

    def attachment_blob_store(self: RepositoryHost, *, create: bool = False) -> BlobStore | None:
        return blob_store(self, create=create)

    # -- writes -------------------------------------------------------------------

    def add_attachment(
        self: RepositoryHost,
        source: Path,
        *,
        privacy: str | None = None,
        media_type: str | None = None,
        origin: str | None = None,
        note: str = "",
        captured_at: str | None = None,
        retention: str = "keep",
        rights: str | None = None,
        tags: Iterable[str] = (),
        embed: bool = False,
    ) -> AttachResult:
        """Register ``source`` as an embedded attachment (a human write).

        Every check that needs no I/O runs before anything is written. Audio
        and video default to a referenced attachment, which this version does
        not support, so they need ``embed=True``.
        """

        level = privacy_value(privacy) or DEFAULT_ATTACHMENT_PRIVACY
        source = Path(source)
        name = clean_name(source.name) or None
        kind = check_media_type(
            media_type
            or (mimetypes.guess_type(source.name, strict=False)[0] or "application/octet-stream")
        )
        if kind.startswith(REFERENCE_BY_DEFAULT) and not embed:
            raise ValueError(
                f"{kind} defaults to a referenced attachment (--reference), which this "
                "version does not support yet; pass --embed to copy the bytes"
            )
        retention = check_retention(retention)
        tag_list = tuple(str(tag) for tag in tags)
        config = self.read_config() if self.config_path.exists() else {}
        max_bytes = _config_int(config, MAX_BYTES_KEY, DEFAULT_MAX_BYTES)
        handle, info = _open_source(source)
        try:
            if info.st_size > max_bytes:
                raise ValueError(
                    f"attachment is {info.st_size} bytes, over {MAX_BYTES_KEY} ({max_bytes}); "
                    "referenced attachments (--reference) are not supported yet"
                )
            draft = AttachmentRecord(
                id=new_attachment_id(self._now(), b"\0" * 10),
                sha256="0" * 64,
                size=0,
                media_type=kind,
                recorded_at=self._now().isoformat(),
                privacy=level,
                original_name=name,
                origin=origin.strip() if origin and origin.strip() else None,
                captured_at=captured_at,
                retention=retention,
                rights=rights.strip() if rights and rights.strip() else None,
                tags=tag_list,
                note=note,
            )
            # A value that would not read back is refused before anything is written.
            render_record(draft)
            # Staged under the blob root, independent of the vault id; the
            # vault's blob directory is resolved under the lock below. Staging
            # checks the location first, so a refused root writes nothing.
            with os.fdopen(handle, "rb", closefd=False) as stream:
                staged = stage_blob(
                    _blob_root(config), stream, vault=self.root, max_bytes=max_bytes
                )
        finally:
            os.close(handle)
        try:
            AttachmentStoreMixin.ensure_vault_id(self)
            result: AttachResult = cast(Any, self).add_attachment_record(draft, staged)
            return result
        finally:
            staged.discard()

    @_serialized
    def add_attachment_record(
        self: RepositoryHost, draft: AttachmentRecord, staged: StagedBlob
    ) -> AttachResult:
        """Link the staged blob and commit the record plus one ``attach`` event."""

        sha256, size = staged.sha256, staged.size
        config = self.read_config()
        if _vault_id(self) is None:
            raise IntegrityError("vault has no vault_id; run the add again")
        # Opened without creating anything: ``link`` refuses a blob staged under
        # another root (the root changed in between) before it makes a directory.
        store = blob_store(self)
        assert store is not None
        if staged.temporary.parent != store.staging:
            staged.discard()
            raise IntegrityError("the blob root changed while the original was staged; retry")
        events = self.events()
        deduplicated = store.has(sha256)
        max_total = _config_int(config, MAX_TOTAL_KEY, DEFAULT_MAX_TOTAL_BYTES)
        # The same bytes again add nothing to the total. Otherwise the total
        # comes from the log (no record is parsed), or from the stored blobs'
        # sizes for a log written before ``attach`` events carried ``size``.
        if not deduplicated and embedded_total(events, store) + size > max_total:
            raise ValueError(
                f"the vault's attachments would exceed {MAX_TOTAL_KEY} ({max_total} bytes); "
                "referenced attachments (--reference) are not supported yet"
            )
        used = {event.memory_id for event in events}
        for _ in range(16):
            entropy = hashlib.sha256(self.id_factory().encode("utf-8")).digest()
            attachment_id = new_attachment_id(self._now(), entropy)
            if attachment_id not in used and not _record_path(self, attachment_id).exists():
                break
        else:  # pragma: no cover - 74 random bits
            raise IntegrityError("could not allocate an unused attachment id")
        record = replace(draft, id=attachment_id, sha256=sha256, size=size)
        # Rendered (and read back) before anything is linked or written.
        raw = render_record(record)
        deduplicated = not store.link(staged)
        self._atomic_write(_record_path(self, attachment_id), raw)
        self._append_event(
            EventKind.ATTACH,
            attachment_id,
            actor="human",
            after_hash=hashlib.sha256(raw).hexdigest(),
            # Event details never carry content or fingerprints (design H7):
            # the size and whether this add stored new bytes are enough to keep
            # the vault's total without reading any record.
            details={
                "storage": record.storage,
                "size": record.size,
                "blob": "shared" if deduplicated else "new",
            },
        )
        return AttachResult(record, deduplicated)

    # -- reads --------------------------------------------------------------------

    def attachment_ids(self: RepositoryHost) -> list[str]:
        """Committed attachment ids, oldest first."""

        return list(committed_records(self.events()))

    def get_attachment(self: RepositoryHost, attachment_id: str) -> AttachmentRecord:
        """A committed record whose bytes match its last event."""

        identifier = check_attachment_id(attachment_id)
        expected = committed_records(self.events()).get(identifier)
        if expected is None:
            raise NotFoundError(f"Attachment not found: {identifier}")
        return _load_record(self, identifier, expected)

    def attachment_records(
        self: RepositoryHost,
    ) -> list[tuple[str, AttachmentRecord | MemfwError]]:
        """Every committed attachment, oldest first: its record or why it cannot be read.

        Reads the event log once, whatever the number of attachments.
        """

        return _load_committed(self, committed_records(self.events()))

    def attachment_citations(self: RepositoryHost, attachment_id: str) -> list[str]:
        """Ids of memories whose ``evidence`` cites ``att:<id>`` (with or without a locator)."""

        identifier = check_attachment_id(attachment_id)
        reference = f"att:{identifier}"
        return sorted(
            document.id
            for document in self.list()
            if any(
                item == reference or item.startswith(reference + "#") for item in document.evidence
            )
        )

    # -- integrity ----------------------------------------------------------------

    def verify_attachments(
        self: RepositoryHost,
        attachment_ids: Iterable[str] | None = None,
        *,
        events: list[Event] | None = None,
    ) -> tuple[list[str], list[AttachmentProblem]]:
        """Re-hash records and blobs. Returns (checked ids, problems). Read-only.

        With no ids every committed attachment is checked, untracked record
        files are reported and so are orphan blobs (no record names them).
        ``events`` is the event log when the caller has already read it.
        """

        if events is None:
            events = self.events()
        committed = committed_records(events)
        everything = attachment_ids is None
        if attachment_ids is None:
            wanted = list(committed)
        else:
            wanted = [check_attachment_id(item) for item in attachment_ids]
        problems: list[AttachmentProblem] = []
        try:
            store = blob_store(self)
        except BlobLocationError as exc:
            store = None
            location_error: str | None = str(exc)
        else:
            location_error = None if store is not None else "vault has no vault_id"
        referenced: set[str] = set()
        records: list[AttachmentRecord] = []
        for identifier in wanted:
            expected = committed.get(identifier)
            if expected is None:
                problems.append(AttachmentProblem(identifier, "not_found"))
                continue
            path = _record_path(self, identifier)
            if not path.exists():
                problems.append(
                    AttachmentProblem(identifier, "record_missing", expected_hash=expected)
                )
                continue
            raw = path.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                problems.append(
                    AttachmentProblem(
                        identifier, "record_modified", expected_hash=expected, actual_hash=actual
                    )
                )
                continue
            try:
                record = parse_record(raw, identifier)
            except IntegrityError as exc:
                problems.append(AttachmentProblem(identifier, "record_invalid", message=str(exc)))
                continue
            records.append(record)
            referenced.add(record.sha256)
        for record in records:
            if record.storage != "embedded":
                continue
            if store is None:
                problems.append(
                    AttachmentProblem(
                        record.id,
                        "blob_unavailable",
                        record.sha256,
                        message=location_error or "",
                    )
                )
                continue
            check = store.check(record.sha256, expected_size=record.size)
            if check.status != "ok":
                status = {
                    "missing": "blob_missing",
                    "not_regular": "blob_not_regular",
                    "size_mismatch": "blob_modified",
                    "modified": "blob_modified",
                }[check.status]
                problems.append(
                    AttachmentProblem(
                        record.id,
                        status,
                        record.sha256,
                        expected_hash=record.sha256,
                        actual_hash=check.actual_sha256,
                        message=(
                            f"size {check.actual_size} != {record.size}"
                            if check.status == "size_mismatch"
                            else ""
                        ),
                    )
                )
            elif check.loose_mode:
                problems.append(AttachmentProblem(record.id, "blob_mode_loose", record.sha256))
        if everything:
            directory = _attachments_dir(self)
            if directory.is_dir():
                for path in sorted(directory.glob("*.md")):
                    if path.stem not in committed:
                        problems.append(
                            AttachmentProblem(
                                path.stem if _is_attachment_name(path.stem) else None,
                                "record_untracked",
                                message=path.name,
                            )
                        )
            if store is not None:
                # Every committed record's blob stays referenced even when its
                # record is damaged, so a damaged record never orphans a blob.
                for identifier in committed:
                    try:
                        raw_record = _record_path(self, identifier).read_bytes()
                        referenced.add(parse_record(raw_record, identifier).sha256)
                    except (OSError, IntegrityError):
                        continue
                for sha in store.stored():
                    if sha not in referenced:
                        problems.append(AttachmentProblem(None, "blob_orphan", sha))
                for name in store.stale_stages():
                    problems.append(AttachmentProblem(None, "blob_stage_stale", message=name))
                # Another vault's (or an untagged) stage is only counted, never named.
                foreign = store.foreign_stale_stages()
                if foreign:
                    problems.append(
                        AttachmentProblem(
                            None,
                            "blob_stage_foreign",
                            message=f"{foreign} stale staging file(s) of other vaults",
                        )
                    )
        return wanted, problems

    def attachment_fsck_issues(self: RepositoryHost) -> list[dict[str, str | None]]:
        """``fsck`` rows for attachments; empty (and no I/O past the log) without any.

        A vault "has attachments" once it has a record file, an ``attach``
        event or a ``vault_id`` (an add may have stopped before its record).
        """

        has_records = _attachments_dir(self).exists()
        events = self.events()
        if not has_records and not committed_records(events) and _vault_id(self) is None:
            return []
        _, problems = AttachmentStoreMixin.verify_attachments(self, events=events)
        return [
            {
                "memory_id": problem.attachment_id,
                "kind": f"attachment_{problem.status}",
                "expected_hash": problem.expected_hash,
                "actual_hash": problem.actual_hash
                or (problem.sha256 if problem.status == "blob_orphan" else None),
                "message": problem.message or None,
            }
            for problem in problems
        ]

    def _reconcile_attachment_records(self: RepositoryHost, events: list[Event]) -> list[str]:
        """Recovery: drop record files no ``attach`` event committed, and temp files."""

        directory = _attachments_dir(self)
        removed: list[str] = []
        if not directory.is_dir():
            return removed
        committed = committed_records(events)
        for path in sorted(directory.glob("*.md.tmp")):
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(self.root).as_posix())
        for path in sorted(directory.glob("*.md")):
            if _is_attachment_name(path.stem) and path.stem not in committed:
                path.unlink()
                removed.append(path.stem)
        return removed


def _load_record(host: RepositoryHost, identifier: str, expected: str) -> AttachmentRecord:
    """The record file of ``identifier``, checked against its committed hash."""

    try:
        raw = _record_path(host, identifier).read_bytes()
    except FileNotFoundError:
        raise NotFoundError(f"Attachment record file is missing: {identifier}") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise IntegrityError(f"Attachment record hash mismatch: {identifier}")
    return parse_record(raw, identifier)


def _load_committed(
    host: RepositoryHost, committed: dict[str, str]
) -> list[tuple[str, AttachmentRecord | MemfwError]]:
    loaded: list[tuple[str, AttachmentRecord | MemfwError]] = []
    for identifier, expected in committed.items():
        try:
            loaded.append((identifier, _load_record(host, identifier, expected)))
        except (IntegrityError, NotFoundError) as exc:
            loaded.append((identifier, exc))
    return loaded


def _is_attachment_name(stem: str) -> bool:
    try:
        check_attachment_id(stem)
    except IntegrityError:
        return False
    return True


__all__ = [
    "ATTACHMENT_EVENT_KINDS",
    "BLOB_ROOT_KEY",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_BYTES_KEY",
    "MAX_TOTAL_KEY",
    "VAULT_ID_KEY",
    "AttachResult",
    "AttachmentProblem",
    "AttachmentStoreMixin",
    "blob_store",
    "committed_records",
    "embedded_total",
    "validate_attachment_config",
]
