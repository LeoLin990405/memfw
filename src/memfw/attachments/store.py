"""Repository mixin: register, read, verify and fsck attachments (MM1a, MM1b).

Write order (design v2.1, H2): the original is hashed and copied into the blob
store first (outside the vault lock, under a temporary name), then, holding
the vault lock, the blob is linked to its content address, the record
``attachments/<id>.md`` is written and one ``attach`` event is appended. A
crash after the blob and before the record leaves an orphan blob, which
``attach verify`` and ``fsck`` report; only ``attach purge`` (and
``purge --orphans``, in ``memfw.attachments.lifecycle``) deletes a blob.

The reference count of a blob is derived from the records; it is never
stored. Vault writes never read the blob store, so their cost does not grow
with the blobs' total size.

A referenced attachment (MM1b) has no blob: its record holds a location
under a configured root (``memfw.attachments.locations``) and the original's
sha256, which ``verify`` re-hashes in place. Detach, relink, purge and the
missing state are in ``memfw.attachments.lifecycle``.
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
from memfw.storage.evidence import attachment_backlinks
from memfw.storage.transactions import _serialized

from .blobs import (
    DEFAULT_BLOB_ROOT,
    REOWN_HINT,
    BlobLocationError,
    BlobStore,
    StagedBlob,
    hash_stream,
    stage_blob,
)
from .locations import (
    ListingCache,
    Location,
    LocationError,
    configured_roots,
    location_for_path,
    looks_like_location,
    open_location,
    stat_location,
    validate_roots_config,
    walk_root,
)
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
from .scan import scan_root
from .scrub import scrub_stores

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
#: Events that commit a record's bytes (``after_hash``).
_RECORD_KINDS: Final = frozenset(
    {EventKind.ATTACH, EventKind.ATTACH_UPDATE, EventKind.ATTACH_RELINK}
)
#: How many directory entries ``verify`` walks per root looking for a moved original.
CANDIDATE_SCAN_LIMIT: Final = 200_000
#: At most this many relink suggestions per missing attachment.
MAX_CANDIDATES: Final = 5
ROOTS_HINT: Final = "attachments_roots in vault.yaml"
#: A record's previous bytes while a relink rewrites it (``<id>.md.prev``).
PREVIOUS_SUFFIX: Final = ".prev"


def validate_attachment_config(raw: dict[str, Any], *, vault: Path | None = None) -> None:
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
    validate_roots_config(raw, vault=vault, blob_root=_blob_root(raw))


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
    #: ``referenced_missing`` only: locations under the same root with the same sha256.
    candidates: tuple[str, ...] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.attachment_id,
            "status": self.status,
            "sha256": self.sha256,
            "message": self.message,
        }
        if self.candidates is not None:
            payload["candidates"] = list(self.candidates)
        return payload


@dataclass
class AttachmentState:
    """What the event log says about one attachment id."""

    #: The committed record hash; ``None`` once purged.
    committed_hash: str | None = None
    detached: bool = False
    missing: bool = False
    purged: bool = False

    @property
    def label(self) -> str:
        if self.purged:
            return "purged"
        if self.detached:
            return "detached"
        return "missing" if self.missing else "active"


def attachment_states(events: Iterable[Event]) -> dict[str, AttachmentState]:
    """Attachment id → its state from the log, in order of first appearance."""

    states: dict[str, AttachmentState] = {}
    for event in events:
        if event.kind not in ATTACHMENT_EVENT_KINDS:
            continue
        state = states.setdefault(event.memory_id, AttachmentState())
        if event.kind in _RECORD_KINDS and event.after_hash is not None:
            state.committed_hash = event.after_hash
            state.purged = False
            if event.kind == EventKind.ATTACH_RELINK:
                state.missing = False
        elif event.kind == EventKind.DETACH:
            state.detached = True
        elif event.kind == EventKind.ATTACH_MISSING:
            state.missing = True
        elif event.kind == EventKind.ATTACH_FOUND:
            state.missing = False
        elif event.kind == EventKind.ATTACH_PURGE:
            state.committed_hash = None
            state.purged = True
    return states


def _attachments_dir(host: RepositoryHost) -> Path:
    return host.root / ATTACHMENTS_DIR


def _record_path(host: RepositoryHost, attachment_id: str) -> Path:
    return _attachments_dir(host) / f"{check_attachment_id(attachment_id)}.md"


def committed_records(events: Iterable[Event]) -> dict[str, str]:
    """Attachment id → committed record hash, from the event log (purged ids left out)."""

    return {
        identifier: state.committed_hash
        for identifier, state in attachment_states(events).items()
        if state.committed_hash is not None
    }


def blob_owners(events: Iterable[Event]) -> tuple[dict[str, int], set[str], bool]:
    """(size per committed embedded attachment, the ids that own a blob, legacy).

    The ``attach`` event that stored new bytes owns the blob; a purge that
    keeps a shared blob hands it to ``details.heir``. ``legacy`` is true when
    a committed ``attach`` event predates ``details.size``.
    """

    sizes: dict[str, int] = {}
    owners: set[str] = set()
    for event in events:
        details = event.details
        if event.kind == EventKind.ATTACH:
            if details.get("storage", "embedded") != "embedded":
                continue
            size = details.get("size")
            blob = details.get("blob")
            # Anything but a non-negative int size and a known blob value is
            # read as an event without them (an old or hand-edited log).
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(blob, str)
                or blob not in {"new", "shared"}
            ):
                return sizes, owners, True
            sizes[event.memory_id] = size
            if blob == "new":
                owners.add(event.memory_id)
        elif event.kind == EventKind.ATTACH_PURGE:
            sizes.pop(event.memory_id, None)
            owned = event.memory_id in owners
            owners.discard(event.memory_id)
            heir = details.get("heir")
            if owned and isinstance(heir, str) and heir in sizes:
                owners.add(heir)
    return sizes, owners, False


def embedded_total(events: Iterable[Event], store: BlobStore) -> int:
    """Bytes the vault's embedded blobs take, for the ``attachments_max_total_bytes`` check.

    Each blob is counted once, by the ``attach`` event that stored it
    (``details.blob == "new"``) while its attachment is committed (see
    :func:`blob_owners`; a purge that keeps a shared blob passes it on). A log with
    a committed ``attach`` event that predates ``details.size`` (or carries a
    malformed ``size`` / ``blob``) falls back to
    the sizes (``lstat``) of the files in the blob directory; otherwise the
    larger of the log total and those sizes is used (orphans count). Neither
    way parses a record, so the cost of an add does not grow with record
    parses (the ``lstat`` pass is one call per stored blob).
    """

    stored = store.stored_bytes()
    sizes, owners, legacy = blob_owners(events)
    if legacy:
        return stored
    # The log never counts bytes no committed ``attach`` stored as new (an
    # orphan an interrupted add left, then re-added as "shared"; detach and
    # re-add cycles over such blobs). The files on disk bound it from below,
    # so the quota can never be bypassed that way.
    return max(sum(sizes[identifier] for identifier in owners), stored)


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
        source: Path | str,
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
        reference: bool = False,
    ) -> AttachResult:
        """Register ``source`` as an attachment (a human write).

        Embedded (the default) copies the bytes into the blob store.
        ``reference=True`` records only a location under a configured root
        (``attachments_roots``); ``source`` is then a filesystem path inside a
        root or a location ``<root>:<relative path>``. Audio and video default
        to a reference: without ``embed`` they must lie inside a configured
        root. Every check that needs no I/O runs before anything is written.
        """

        if embed and reference:
            raise ValueError("--embed and --reference exclude each other")
        level = privacy_value(privacy) or DEFAULT_ATTACHMENT_PRIVACY
        config = self.read_config() if self.config_path.exists() else {}
        roots = _checked_roots(self, config)
        text = str(source)
        location: Location | None = None
        if reference:
            location = (
                Location.parse(text)
                if looks_like_location(text, roots)
                else location_for_path(Path(source), roots)
            )
        base_name = location.name if location is not None else Path(source).name
        kind = check_media_type(
            media_type
            or (mimetypes.guess_type(base_name, strict=False)[0] or "application/octet-stream")
        )
        if location is None and kind.startswith(REFERENCE_BY_DEFAULT) and not embed:
            try:
                location = location_for_path(Path(source), roots)
            except LocationError:
                raise ValueError(
                    f"{kind} defaults to a referenced attachment, but the file is not inside "
                    f"any configured attachments root ({ROOTS_HINT}); pass --embed to copy "
                    "the bytes"
                ) from None
        retention = check_retention(retention)
        draft = AttachmentRecord(
            id=new_attachment_id(self._now(), b"\0" * 10),
            sha256="0" * 64,
            size=0,
            media_type=kind,
            recorded_at=self._now().isoformat(),
            storage="embedded" if location is None else "referenced",
            location=None if location is None else str(location),
            privacy=level,
            original_name=clean_name(base_name) or None,
            origin=origin.strip() if origin and origin.strip() else None,
            captured_at=captured_at,
            retention=retention,
            rights=rights.strip() if rights and rights.strip() else None,
            tags=tuple(str(tag) for tag in tags),
            note=note,
        )
        # A value that would not read back is refused before anything is written.
        render_record(draft)
        if location is not None:
            return AttachmentStoreMixin._add_reference(self, draft, location, roots)
        source = Path(source)
        max_bytes = _config_int(config, MAX_BYTES_KEY, DEFAULT_MAX_BYTES)
        handle, info = _open_source(source)
        try:
            if info.st_size > max_bytes:
                raise ValueError(
                    f"attachment is {info.st_size} bytes, over {MAX_BYTES_KEY} ({max_bytes}); "
                    f"use --reference (a file under a configured attachments root)"
                )
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

    def _add_reference(
        self: RepositoryHost,
        draft: AttachmentRecord,
        location: Location,
        roots: dict[str, Path],
    ) -> AttachResult:
        """Hash the original in place (no copy) and commit a referenced record."""

        try:
            handle, info = open_location(location, roots)
        except FileNotFoundError:
            raise ValueError(f"no such file: {location}") from None
        if info.st_nlink > 1:
            os.close(handle)
            # A hard link elsewhere (perhaps outside every root) can change the
            # bytes behind the location without touching any path memfw checks.
            raise LocationError(
                f"{location}: refusing a file with {info.st_nlink} hard links; "
                "reference a copy with a single link"
            )
        with os.fdopen(handle, "rb") as stream:
            sha256, size = hash_stream(stream)
        result: AttachResult = cast(Any, self).add_reference_record(
            replace(draft, sha256=sha256, size=size)
        )
        return result

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
                "use --reference (a file under a configured attachments root)"
            )
        attachment_id = _new_id(self, events)
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

    @_serialized
    def add_reference_record(self: RepositoryHost, draft: AttachmentRecord) -> AttachResult:
        """Commit a referenced record plus one ``attach`` event (no blob, no ``vault_id``)."""

        if draft.storage != "referenced":
            raise IntegrityError("add_reference_record takes a referenced record")
        events = self.events()
        record = replace(draft, id=_new_id(self, events))
        raw = render_record(record)
        self._atomic_write(_record_path(self, record.id), raw)
        self._append_event(
            EventKind.ATTACH,
            record.id,
            actor="human",
            after_hash=hashlib.sha256(raw).hexdigest(),
            details={"storage": record.storage, "size": record.size},
        )
        return AttachResult(record, False)

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

    def get_attachment_with_state(
        self: RepositoryHost, attachment_id: str
    ) -> tuple[AttachmentRecord, AttachmentState]:
        """:meth:`get_attachment` plus its log state, reading the log once."""

        identifier = check_attachment_id(attachment_id)
        state = attachment_states(self.events()).get(identifier)
        if state is None or state.committed_hash is None:
            raise NotFoundError(f"Attachment not found: {identifier}")
        return _load_record(self, identifier, state.committed_hash), state

    def attachment_records(
        self: RepositoryHost,
    ) -> list[tuple[str, AttachmentRecord | MemfwError]]:
        """Every committed attachment, oldest first: its record or why it cannot be read.

        Reads the event log once, whatever the number of attachments.
        """

        return _load_committed(self, committed_records(self.events()))

    def attachment_state_map(self: RepositoryHost) -> dict[str, AttachmentState]:
        """Attachment id → its state from the event log (``active``, ``detached``, …)."""

        return attachment_states(self.events())

    def scan_attachment_root(
        self: RepositoryHost, target: str, *, hash_files: bool = False
    ) -> dict[str, Any]:
        """Dry-run inventory of ``target`` under a configured root (writes nothing)."""

        config = self.read_config() if self.config_path.exists() else {}
        roots = _checked_roots(self, config)
        registered = {
            str(record.location)
            for _, record in _load_committed(self, committed_records(self.events()))
            if isinstance(record, AttachmentRecord) and record.location is not None
        }
        return scan_root(
            target,
            roots,
            registered=registered,
            max_bytes=_config_int(config, MAX_BYTES_KEY, DEFAULT_MAX_BYTES),
            hash_files=hash_files,
        )

    def attachment_citations(self: RepositoryHost, attachment_id: str) -> list[str]:
        """Ids of memories whose ``evidence`` names the attachment (read-only).

        ``att:<id>`` with or without a locator, in any spelling, plus the other
        pointers the MM3 gate treats as naming it (its hash, its blob path, its
        id inside another pointer); see ``memfw.storage.evidence``.
        """

        identifier = check_attachment_id(attachment_id)
        return sorted({link.memory_id for link in attachment_backlinks(self, identifier)})

    # -- integrity ----------------------------------------------------------------

    def verify_attachments(
        self: RepositoryHost,
        attachment_ids: Iterable[str] | None = None,
        *,
        events: list[Event] | None = None,
        candidates: bool = True,
        hash_referenced: bool = True,
    ) -> tuple[list[str], list[AttachmentProblem]]:
        """Re-hash records, blobs and referenced originals. Returns (checked ids, problems).

        Read-only. With no ids every committed attachment is checked, untracked
        record files are reported and so are orphan blobs (no record names
        them). A referenced original that is gone is ``referenced_missing``;
        with ``candidates`` the same root tree is searched for files with the
        same sha256, which are suggested for ``attach relink`` (never relinked
        here). A detached referenced attachment's original is not checked.
        Without ``hash_referenced`` referenced originals are only ``lstat``-ed
        (exists, regular, same size): what ``fsck`` does by default.
        ``events`` is the event log when the caller has already read it.
        """

        if events is None:
            events = self.events()
        states = attachment_states(events)
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
            if record.storage == "embedded":
                referenced.add(record.sha256)
        problems.extend(
            _check_references(
                self,
                [
                    record
                    for record in records
                    if record.storage == "referenced" and not states[record.id].detached
                ],
                candidates=candidates,
                hash_files=hash_referenced,
                states=states,
            )
        )
        if store is not None and store.owner_status() == "foreign":
            problems.append(AttachmentProblem(None, "blob_store_foreign_owner", message=REOWN_HINT))
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
                        parsed = parse_record(raw_record, identifier)
                    except (OSError, IntegrityError):
                        continue
                    if parsed.storage == "embedded":
                        referenced.add(parsed.sha256)
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

    def attachment_fsck_issues(
        self: RepositoryHost, *, hash_referenced: bool = False
    ) -> list[dict[str, str | None]]:
        """``fsck`` rows for attachments; empty (and no I/O past the log) without any.

        Referenced originals are only ``lstat``-ed unless ``hash_referenced``
        (``fsck`` holds the vault lock; ``attach verify`` always re-hashes).

        A vault "has attachments" once it has a record file, an ``attach``
        event or a ``vault_id`` (an add may have stopped before its record).
        """

        has_records = _attachments_dir(self).exists()
        events = self.events()
        if not has_records and not committed_records(events) and _vault_id(self) is None:
            return []
        _, problems = AttachmentStoreMixin.verify_attachments(
            self, events=events, candidates=False, hash_referenced=hash_referenced
        )
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
        """Recovery after an interrupted attachment write.

        * ``*.md.prev`` (the record before a relink): restored when the record
          on disk is not the committed one, else removed;
        * a record whose ``attach_purge`` committed: the purge is finished (its
          blob is removed when no other committed record names it);
        * a record no event committed, and temp files: removed.
        """

        directory = _attachments_dir(self)
        removed: list[str] = []
        if not directory.is_dir():
            return removed
        states = attachment_states(events)
        committed = committed_records(events)
        for path in sorted(directory.glob("*.md.tmp")):
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(self.root).as_posix())
        for previous in sorted(directory.glob("*.md" + PREVIOUS_SUFFIX)):
            identifier = previous.name.removesuffix(".md" + PREVIOUS_SUFFIX)
            expected = committed.get(identifier)
            record_file = directory / f"{identifier}.md"
            current = record_file.read_bytes() if record_file.exists() else None
            if expected is not None and (
                current is None or hashlib.sha256(current).hexdigest() != expected
            ):
                if hashlib.sha256(previous.read_bytes()).hexdigest() == expected:
                    self._atomic_write(record_file, previous.read_bytes())
                    removed.append(identifier)
            previous.unlink(missing_ok=True)
            removed.append(previous.relative_to(self.root).as_posix())
        purged = [
            path
            for path in sorted(directory.glob("*.md"))
            if _is_attachment_name(path.stem) and path.stem in states and states[path.stem].purged
        ]
        if purged:
            try:
                store = blob_store(self)
            except BlobLocationError:
                store = None
            # A copy of the vault shares its vault_id: never delete another's blobs.
            if store is not None and store.owner_status() not in {"own", "absent"}:
                store = None
            shared = _committed_embedded_shas(self, committed)
            for path in purged:
                parsed: AttachmentRecord | None
                try:
                    parsed = parse_record(path.read_bytes(), path.stem)
                except IntegrityError:
                    parsed = None
                if (
                    parsed is not None
                    and parsed.storage == "embedded"
                    and store is not None
                    and shared is not None
                    and parsed.sha256 not in shared
                ):
                    _remove_blob(store, parsed.sha256)
                # The same scrub a normal purge runs (index dropped, stores VACUUMed).
                scrub_stores(self, parsed)
        for path in sorted(directory.glob("*.md")):
            if _is_attachment_name(path.stem) and path.stem not in committed:
                path.unlink()
                removed.append(path.stem)
        return removed


def _committed_embedded_shas(host: RepositoryHost, committed: dict[str, str]) -> set[str] | None:
    """sha256 of every committed embedded record; ``None`` if any committed record is unreadable.

    Records are the only place a blob's sha256 is kept, so while one cannot be
    read no blob may be treated as unreferenced.
    """

    shas: set[str] = set()
    for _, record in _load_committed(host, committed):
        if isinstance(record, MemfwError):
            return None
        if record.storage == "embedded":
            shas.add(record.sha256)
    return shas


def _remove_blob(store: BlobStore, sha256: str) -> bool:
    """Delete one blob file (and its shard directory once empty). ``False``: it was not there."""

    path = store.path_for(sha256)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode):
        raise IntegrityError(f"blob address {sha256[:12]} is a directory; remove it by hand")
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass  # other blobs share the shard
    try:
        handle = os.open(store.directory, os.O_RDONLY)
    except OSError:
        return True
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    return True


def _new_id(host: RepositoryHost, events: Iterable[Event]) -> str:
    used = {event.memory_id for event in events}
    for _ in range(16):
        entropy = hashlib.sha256(host.id_factory().encode("utf-8")).digest()
        attachment_id = new_attachment_id(host._now(), entropy)
        if attachment_id not in used and not _record_path(host, attachment_id).exists():
            return attachment_id
    raise IntegrityError("could not allocate an unused attachment id")  # pragma: no cover


def _checked_roots(host: RepositoryHost, config: dict[str, Any]) -> dict[str, Path]:
    """The configured roots; one inside the vault is refused (``IntegrityError``)."""

    validate_roots_config(config, vault=host.root, blob_root=_blob_root(config))
    return configured_roots(config)


def attachment_roots(host: RepositoryHost) -> dict[str, Path]:
    """This vault's configured attachments roots (name → directory)."""

    config = host.read_config() if host.config_path.exists() else {}
    return _checked_roots(host, config)


def _check_references(
    host: RepositoryHost,
    records: list[AttachmentRecord],
    *,
    candidates: bool,
    hash_files: bool = True,
    states: dict[str, AttachmentState] | None = None,
) -> list[AttachmentProblem]:
    """Check each referenced original in place; suggest candidates for missing ones.

    ``hash_files`` re-hashes; otherwise only ``lstat`` (exists, regular file,
    same size). A root whose every lookup fails (two or more originals) or
    that is an empty directory is treated as not mounted: every original
    under it is ``referenced_unavailable``, never ``referenced_missing``.
    """

    if not records:
        return []
    try:
        roots = attachment_roots(host)
    except IntegrityError as exc:
        return [
            AttachmentProblem(record.id, "referenced_unavailable", record.sha256, message=str(exc))
            for record in records
        ]
    cache: ListingCache = {}
    problems: list[AttachmentProblem] = []
    missing: list[tuple[AttachmentRecord, Location]] = []
    seen: dict[str, int] = {}
    for record in records:
        location = Location.parse(str(record.location))
        seen[location.root] = seen.get(location.root, 0) + 1
        try:
            if hash_files:
                handle, info = open_location(location, roots, cache=cache)
            else:
                info = stat_location(location, roots, cache=cache)
                handle = None
        except FileNotFoundError as exc:
            if location.root in roots and roots[location.root].is_dir():
                missing.append((record, location))
            else:
                problems.append(
                    AttachmentProblem(
                        record.id, "referenced_unavailable", record.sha256, message=str(exc)
                    )
                )
            continue
        except LocationError as exc:
            status = "referenced_unavailable" if location.root not in roots else "referenced_unsafe"
            problems.append(AttachmentProblem(record.id, status, record.sha256, message=str(exc)))
            continue
        actual: str | None = record.sha256
        size = info.st_size
        if handle is not None:
            with os.fdopen(handle, "rb") as stream:
                if info.st_size != record.size:
                    actual = None
                else:
                    actual, size = hash_stream(stream)
        elif info.st_size != record.size:
            actual = None
        if actual != record.sha256:
            problems.append(
                AttachmentProblem(
                    record.id,
                    "referenced_modified",
                    record.sha256,
                    expected_hash=record.sha256,
                    actual_hash=actual,
                    message=f"size {size} != {record.size}" if size != record.size else "",
                )
            )
    unmounted = _unmounted_roots(missing, seen, roots)
    real = [(record, location) for record, location in missing if location.root not in unmounted]
    found = _find_candidates(real, roots) if candidates and real else {}
    for record, location in missing:
        if location.root in unmounted:
            problems.append(
                AttachmentProblem(
                    record.id,
                    "referenced_unavailable",
                    record.sha256,
                    message=f"{location.root}: {unmounted[location.root]}",
                )
            )
            continue
        problems.append(
            AttachmentProblem(
                record.id,
                "referenced_missing",
                record.sha256,
                message=f"{location}: no such file",
                candidates=tuple(found.get(record.id, ())) if candidates else None,
            )
        )
    return problems


def _unmounted_roots(
    missing: list[tuple[AttachmentRecord, Location]],
    seen: dict[str, int],
    roots: dict[str, Path],
) -> dict[str, str]:
    """Roots that look unmounted: an empty directory, or every lookup (two or more) failed."""

    gone: dict[str, int] = {}
    for _, location in missing:
        gone[location.root] = gone.get(location.root, 0) + 1
    unmounted: dict[str, str] = {}
    for root_name, count in gone.items():
        try:
            empty = not any(os.scandir(roots[root_name]))
        except OSError:
            empty = True
        if empty:
            unmounted[root_name] = "the root is an empty directory (not mounted?)"
        elif count >= 2 and count == seen.get(root_name):
            unmounted[root_name] = "every original under the root is gone (not mounted?)"
    return unmounted


def referenced_missing_now(host: RepositoryHost, record: AttachmentRecord) -> bool | None:
    """Re-check one referenced original: ``True`` missing, ``False`` present, ``None`` unknown.

    ``None`` when the root is not configured, not a directory or empty (not
    mounted), or the location is refused; used under the vault lock before
    the missing state changes.
    """

    try:
        roots = attachment_roots(host)
    except IntegrityError:
        return None
    location = Location.parse(str(record.location))
    root = roots.get(location.root)
    if root is None or not root.is_dir():
        return None
    try:
        if not any(os.scandir(root)):
            return None
        stat_location(location, roots)
    except FileNotFoundError:
        return True
    except (OSError, LocationError):
        return None
    return False


def _find_candidates(
    missing: list[tuple[AttachmentRecord, Location]], roots: dict[str, Path]
) -> dict[str, list[str]]:
    """Locations under each missing original's root whose size and sha256 match it.

    One bounded walk per root (``CANDIDATE_SCAN_LIMIT`` entries, symlinks never
    followed); only files of a wanted size are hashed, each at most once.
    """

    found: dict[str, list[str]] = {}
    by_root: dict[str, list[AttachmentRecord]] = {}
    for record, location in missing:
        by_root.setdefault(location.root, []).append(record)
    for root_name, wanted in by_root.items():
        sizes = {record.size for record in wanted}
        hashes: dict[str, str | None] = {}
        for item in walk_root(root_name, roots, max_entries=CANDIDATE_SCAN_LIMIT):
            if item.size not in sizes:
                continue
            key = str(item.location)
            if key not in hashes:
                try:
                    handle, _ = open_location(item.location, roots)
                except (OSError, LocationError):
                    hashes[key] = None
                    continue
                with os.fdopen(handle, "rb") as stream:
                    hashes[key] = hash_stream(stream)[0]
            for record in wanted:
                matches = found.setdefault(record.id, [])
                if (
                    hashes[key] == record.sha256
                    and item.size == record.size
                    and len(matches) < MAX_CANDIDATES
                ):
                    matches.append(key)
    for matches in found.values():
        matches.sort()
    return found


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
    "AttachmentState",
    "attachment_roots",
    "attachment_states",
    "referenced_missing_now",
    "blob_owners",
    "blob_store",
    "committed_records",
    "embedded_total",
    "validate_attachment_config",
]
