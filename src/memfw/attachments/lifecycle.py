"""Detach, relink, the missing state and purge of attachments (MM1b).

* ``detach`` is a soft delete: one ``detach`` event; the record and the blob
  stay, ``attach list`` hides it, ``verify`` no longer checks a detached
  referenced original.
* ``relink`` points a referenced attachment at a new location whose bytes
  have the same sha256 and size (the original was moved or renamed). It is
  only ever run by a human; ``verify`` suggests candidates, never relinks.
* ``record_missing`` appends ``attach_missing`` for referenced originals that
  ``verify`` found gone; ``relink`` clears the state.
* ``purge`` is the hard delete (design H7). It needs an explicit confirmation
  (``confirm=True``, ``--yes`` on the CLI); without it only the plan is
  returned. A purge removes the record, the blob when no other record names
  it, this vault's stale staging files, the memory index (rebuilt on next
  use) and VACUUMs any other SQLite store in the vault that still holds the
  record's text. Memories that cite the attachment are listed, never
  changed: a human decides what to do with them. A referenced original is
  never touched (it is not memfw's copy).

Every event carries only the attachment id (and, for a purge, what happened
to the blob), never a name, location or fingerprint.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from memfw.core.errors import IntegrityError, MemfwError, NotFoundError
from memfw.core.models import Event, EventKind
from memfw.storage.transactions import _serialized

from .blobs import REOWN_HINT, BlobLocationError, BlobOwnerError, hash_stream, stage_blob
from .locations import Location, location_for_path, looks_like_location, open_location
from .records import AttachmentRecord, check_attachment_id, render_record
from .scrub import scrub_stores, vault_sqlite_stores
from .store import (
    PREVIOUS_SUFFIX,
    VAULT_ID_KEY,
    _committed_embedded_shas,
    _load_committed,
    _load_record,
    _record_path,
    _remove_blob,
    attachment_roots,
    attachment_states,
    blob_owners,
    blob_store,
    committed_records,
    referenced_missing_now,
)

if TYPE_CHECKING:
    from memfw.core.context import RepositoryHost

#: Copies a purge cannot reach; printed with every purge plan (design v2.1 H7).
IRRECOVERABLE_COPIES: Final = (
    "external backups of the vault or the blob root (Time Machine, rsync, cloud backup)",
    "filesystem or NAS snapshots of the vault or the blob root",
    "federation replicas and other hosts' copies of the vault",
    "test or snapshot vaults copied from this one",
    "OKF bundles or other exports already written",
    "conversation transcripts or notes that quote the original",
)
#: ``details`` of every event kind MM1b writes: the forward-compat marker that
#: tells a reader which does not know the kind that it projects onto nothing
#: (harmless for readers that know it; see the event-log forward-compat scheme).
NO_PROJECTION: Final = {"projection": "none"}


@dataclass(frozen=True)
class PurgePlan:
    """What ``purge`` would do (or did) for one attachment."""

    attachment_id: str
    storage: str | None
    state: str
    #: ``delete`` | ``keep_shared`` | ``keep_unverified`` | ``absent`` | ``none`` (referenced)
    blob: str
    shared_with: tuple[str, ...] = ()
    cited_by: tuple[str, ...] = ()
    mentioned_by: tuple[str, ...] = ()
    record_readable: bool = True
    stores: tuple[str, ...] = ()
    #: The id that owns the kept blob from now on (quota accounting), if any.
    heir: str | None = None
    notices: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.attachment_id,
            "storage": self.storage,
            "state": self.state,
            "blob": self.blob,
            "shared_with": list(self.shared_with),
            "cited_by": list(self.cited_by),
            "mentioned_by": list(self.mentioned_by),
            "record_readable": self.record_readable,
            "stores": list(self.stores),
            "notices": list(self.notices),
            "irrecoverable_copies": list(IRRECOVERABLE_COPIES),
        }


@dataclass
class PurgeResult:
    plan: PurgePlan
    removed: list[str] = field(default_factory=list)
    vacuumed: list[str] = field(default_factory=list)
    still_containing: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        payload = self.plan.to_json()
        payload.update(
            purged=True,
            removed=self.removed,
            vacuumed=self.vacuumed,
            still_containing=self.still_containing,
        )
        return payload


@dataclass(frozen=True)
class OrphanPlan:
    orphans: tuple[str, ...]
    stale_stages: tuple[str, ...]
    #: Why orphans cannot be reclaimed now (a committed record is unreadable).
    blocked: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "orphans": list(self.orphans),
            "stale_stages": list(self.stale_stages),
            "blocked": self.blocked,
        }


def _target(text: str | Path, roots: dict[str, Path]) -> Location:
    raw = str(text)
    if looks_like_location(raw, roots):
        return Location.parse(raw)
    return location_for_path(Path(text), roots)


class AttachmentLifecycleMixin:
    """Detach, relink, missing and purge (``AttachmentStoreMixin`` holds add/read/verify)."""

    # -- detach -------------------------------------------------------------------

    @_serialized
    def detach_attachment(self: RepositoryHost, attachment_id: str) -> AttachmentRecord:
        """Soft-delete: one ``detach`` event. Record and blob are kept (``purge`` removes them)."""

        identifier = check_attachment_id(attachment_id)
        states = attachment_states(self.events())
        state = states.get(identifier)
        if state is None or state.committed_hash is None:
            raise NotFoundError(f"Attachment not found: {identifier}")
        if state.detached:
            raise IntegrityError(f"Attachment is already detached: {identifier}")
        record = _load_record(self, identifier, state.committed_hash)
        self._append_event(
            EventKind.DETACH,
            identifier,
            actor="human",
            after_hash=state.committed_hash,
            details=dict(NO_PROJECTION),
        )
        return record

    # -- missing / relink ---------------------------------------------------------

    @_serialized
    def record_missing_attachments(self: RepositoryHost, attachment_ids: list[str]) -> list[str]:
        """Append ``attach_missing`` for each referenced id still missing. Returns those.

        Each original is checked again under the vault lock: one that is back,
        or whose root is not mounted (missing, empty or unconfigured), gets no
        event. See :meth:`record_found_attachments` for the way back.
        """

        states = attachment_states(self.events())
        written: list[str] = []
        for attachment_id in attachment_ids:
            identifier = check_attachment_id(attachment_id)
            state = states.get(identifier)
            if state is None or state.committed_hash is None or state.missing or state.detached:
                continue
            record = _load_record(self, identifier, state.committed_hash)
            if record.storage != "referenced" or referenced_missing_now(self, record) is not True:
                continue
            self._append_event(
                EventKind.ATTACH_MISSING,
                identifier,
                actor="human",
                after_hash=state.committed_hash,
                details=dict(NO_PROJECTION),
            )
            state.missing = True
            written.append(identifier)
        return written

    @_serialized
    def record_found_attachments(self: RepositoryHost) -> list[str]:
        """Append ``attach_found`` for each attachment recorded missing whose original is back.

        "Back" means present at its location again (checked under the vault
        lock; the bytes are compared by ``verify``). Returns those ids.
        """

        states = attachment_states(self.events())
        written: list[str] = []
        for identifier, state in states.items():
            if state.committed_hash is None or not state.missing:
                continue
            record = _load_record(self, identifier, state.committed_hash)
            if record.storage != "referenced" or referenced_missing_now(self, record) is not False:
                continue
            self._append_event(
                EventKind.ATTACH_FOUND,
                identifier,
                actor="human",
                after_hash=state.committed_hash,
                details=dict(NO_PROJECTION),
            )
            written.append(identifier)
        return written

    # -- blob directory ownership ----------------------------------------------------

    @_serialized
    def reown_blob_store(
        self: RepositoryHost, *, new_vault_id: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Make this vault's blob directory its own again (needs ``confirm``).

        * default: mark ``<blob root>/<vault_id>`` as this vault's (the vault
          moved to a new path, or the directory predates owner markers);
        * ``new_vault_id``: for a copy of a vault (snapshot, eval vault). A new
          ``vault_id`` is written and every blob this vault's committed
          records name is **copied** from the shared directory into the new
          one, which is marked as this vault's. The shared directory is not
          changed, so the original vault keeps all of its blobs.
        """

        if not confirm:
            raise IntegrityError("reown changes which vault may delete blobs; confirm it")
        old = blob_store(self)
        if not new_vault_id:
            if old is None:
                raise IntegrityError("vault has no vault_id; nothing to re-own")
            if old.owner_status() == "absent":
                old.ensure()
            else:
                old.write_owner()
            return {"vault_id": old.directory.name, "copied": 0, "missing": []}
        committed = committed_records(self.events())
        shas = _committed_embedded_shas(self, committed)
        if shas is None:
            raise IntegrityError("a committed record is missing or unreadable; fix it first")
        config = self.read_config()
        config[VAULT_ID_KEY] = str(uuid.uuid4())
        cast(Any, self).configure(config)
        new = blob_store(self, create=True)
        assert new is not None
        copied = 0
        missing: list[str] = []
        for sha in sorted(shas):
            source = None if old is None else old.path_for(sha)
            if source is None or not source.is_file() or source.is_symlink():
                missing.append(sha)
                continue
            with source.open("rb") as stream:
                staged = stage_blob(
                    new.directory.parent, stream, vault=self.root, max_bytes=1 << 62
                )
            try:
                new.link(staged)
            finally:
                staged.discard()
            copied += 1
        return {"vault_id": new.directory.name, "copied": copied, "missing": missing}

    @_serialized
    def relink_attachment(
        self: RepositoryHost, attachment_id: str, target: str | Path
    ) -> AttachmentRecord:
        """Point a referenced attachment at ``target`` (a path in a root, or ``<root>:<path>``).

        The file there must have the record's sha256 and size; anything else
        is a different original and is refused. One ``attach_relink`` event.
        """

        identifier = check_attachment_id(attachment_id)
        states = attachment_states(self.events())
        state = states.get(identifier)
        if state is None or state.committed_hash is None:
            raise NotFoundError(f"Attachment not found: {identifier}")
        if state.detached:
            raise IntegrityError(f"Attachment is detached: {identifier}")
        record = _load_record(self, identifier, state.committed_hash)
        if record.storage != "referenced":
            raise IntegrityError("only a referenced attachment can be relinked")
        roots = attachment_roots(self)
        location = _target(target, roots)
        try:
            handle, _ = open_location(location, roots)
        except FileNotFoundError:
            raise ValueError(f"no such file: {location}") from None
        with os.fdopen(handle, "rb") as stream:
            sha256, size = hash_stream(stream)
        if sha256 != record.sha256 or size != record.size:
            raise ValueError(
                f"{location} is not the same original (sha256 or size differ); "
                "register it as a new attachment instead"
            )
        updated = replace(record, location=str(location))
        raw = render_record(updated)
        path = _record_path(self, identifier)
        previous = path.with_name(path.name + PREVIOUS_SUFFIX)
        # The old bytes stay next to the record until the event commits, so
        # recovery can put them back (records have no version history).
        self._atomic_write(previous, path.read_bytes())
        self._atomic_write(path, raw)
        self._append_event(
            EventKind.ATTACH_RELINK,
            identifier,
            actor="human",
            before_hash=state.committed_hash,
            after_hash=hashlib.sha256(raw).hexdigest(),
            details=dict(NO_PROJECTION),
        )
        previous.unlink(missing_ok=True)
        return updated

    # -- purge --------------------------------------------------------------------

    def attachment_purge_plan(
        self: RepositoryHost, attachment_id: str, *, events: list[Event] | None = None
    ) -> PurgePlan:
        """What ``purge_attachment`` would do. Read-only."""

        identifier = check_attachment_id(attachment_id)
        if events is None:
            events = self.events()
        states = attachment_states(events)
        state = states.get(identifier)
        if state is None or state.committed_hash is None:
            raise NotFoundError(f"Attachment not found: {identifier}")
        committed = committed_records(events)
        try:
            record: AttachmentRecord | None = _load_record(self, identifier, state.committed_hash)
        except (IntegrityError, NotFoundError):
            record = None
        reference = f"att:{identifier}"
        cited: list[str] = []
        mentioned: list[str] = []
        for document in self.list():
            if any(
                item == reference or item.startswith(reference + "#") for item in document.evidence
            ):
                cited.append(document.id)
            elif reference in document.body or reference in document.title:
                mentioned.append(document.id)
        notices: list[str] = []
        shared: list[str] = []
        heir: str | None = None
        if record is None:
            blob = "keep_unverified"
            storage = None
            notices.append(
                "the record cannot be read, so its blob cannot be identified; the blob is "
                "kept (an orphan `attach verify` reports; `attach purge --orphans` reclaims "
                "it once every other record reads)"
            )
        elif record.storage == "referenced":
            blob, storage = "none", "referenced"
            notices.append(
                f"the original at {record.location} is not memfw's copy and is not touched"
            )
        else:
            storage = "embedded"
            others = {key: value for key, value in committed.items() if key != identifier}
            unreadable = False
            for other_id, other in _load_committed(self, others):
                if isinstance(other, MemfwError):
                    unreadable = True
                elif other.storage == "embedded" and other.sha256 == record.sha256:
                    shared.append(other_id)
            try:
                store = blob_store(self)
            except BlobLocationError as exc:
                store = None
                notices.append(f"the blob directory cannot be used: {exc}")
            if shared:
                blob = "keep_shared"
                notices.append(
                    f"the original is not deleted: {len(shared)} other attachment(s) still "
                    "use the same blob"
                )
                _, owners, _ = blob_owners(events)
                if identifier in owners:
                    heir = shared[0]
            elif unreadable:
                blob = "keep_unverified"
                notices.append(
                    "another committed record cannot be read, so the blob is kept; fix it "
                    "and run `attach purge --orphans`"
                )
            elif store is None or not store.has(record.sha256):
                blob = "absent"
            elif store.owner_status() != "own":
                blob = "refused_owner"
                notices.append(
                    "the blob directory is not marked as this vault's (a copy of a vault "
                    f"shares its vault_id), so nothing is purged; {REOWN_HINT}"
                )
            else:
                blob = "delete"
        if cited or mentioned:
            notices.append(
                "memories cite this attachment; purge does not change them: review "
                + ", ".join(cited + mentioned)
            )
        stores = tuple(path.relative_to(self.root).as_posix() for path in vault_sqlite_stores(self))
        return PurgePlan(
            attachment_id=identifier,
            storage=storage,
            state=state.label,
            blob=blob,
            shared_with=tuple(shared),
            cited_by=tuple(cited),
            mentioned_by=tuple(mentioned),
            record_readable=record is not None,
            stores=stores,
            heir=heir,
            notices=tuple(notices),
        )

    @_serialized
    def purge_attachment(
        self: RepositoryHost, attachment_id: str, *, confirm: bool = False
    ) -> PurgeResult:
        """Hard-delete one attachment (needs ``confirm=True``; see the module docstring)."""

        if not confirm:
            raise IntegrityError("purge is a hard delete; it needs an explicit confirmation")
        events = self.events()
        plan = AttachmentLifecycleMixin.attachment_purge_plan(self, attachment_id, events=events)
        if plan.blob == "refused_owner":
            raise BlobOwnerError(
                "the blob directory is not marked as this vault's; nothing was purged; "
                + REOWN_HINT
            )
        identifier = plan.attachment_id
        state = attachment_states(events)[identifier]
        path = _record_path(self, identifier)
        try:
            record: AttachmentRecord | None = _load_record(
                self, identifier, str(state.committed_hash)
            )
        except (IntegrityError, NotFoundError):
            record = None
        details: dict[str, Any] = {
            **NO_PROJECTION,
            "blob": {"delete": "deleted", "absent": "absent", "none": "none"}.get(
                plan.blob, "kept"
            ),
        }
        if plan.heir is not None:
            details["heir"] = plan.heir
        # The event commits the purge; everything after it is finished by
        # `memfw recover` if interrupted (the record file still says which blob).
        self._append_event(
            EventKind.ATTACH_PURGE,
            identifier,
            actor="human",
            before_hash=state.committed_hash,
            details=details,
        )
        result = PurgeResult(plan)
        store = None
        try:
            store = blob_store(self)
        except BlobLocationError:
            store = None
        if plan.blob == "delete" and record is not None and store is not None:
            store.require_owner()
            if _remove_blob(store, record.sha256):
                result.removed.append("blob")
        for candidate in (path, path.with_name(path.name + PREVIOUS_SUFFIX)):
            if candidate.exists():
                candidate.unlink()
                result.removed.append(candidate.relative_to(self.root).as_posix())
        if store is not None:
            result.removed.extend(store.reclaim_stale_stages())
        scrubbed = scrub_stores(self, record)
        result.removed.extend(scrubbed.removed)
        result.vacuumed.extend(scrubbed.vacuumed)
        result.still_containing.extend(scrubbed.still_containing)
        return result

    def attachment_orphan_plan(self: RepositoryHost) -> OrphanPlan:
        """Orphan blobs and this vault's stale staging files ``purge --orphans`` would remove."""

        committed = committed_records(self.events())
        try:
            store = blob_store(self)
        except BlobLocationError as exc:
            return OrphanPlan((), (), blocked=str(exc))
        if store is None:
            return OrphanPlan((), ())
        stages = tuple(store.stale_stages())
        if store.owner_status() not in {"own", "absent"}:
            return OrphanPlan(
                (), stages, blocked=f"the blob directory is not this vault's; {REOWN_HINT}"
            )
        shas = _committed_embedded_shas(self, committed)
        if shas is None:
            return OrphanPlan(
                (),
                stages,
                blocked="a committed record is missing or unreadable; orphans are kept",
            )
        return OrphanPlan(tuple(sha for sha in store.stored() if sha not in shas), stages)

    @_serialized
    def purge_orphan_blobs(self: RepositoryHost, *, confirm: bool = False) -> OrphanPlan:
        """Delete blobs no committed record names and this vault's stale stages."""

        if not confirm:
            raise IntegrityError("purge is a hard delete; it needs an explicit confirmation")
        plan = AttachmentLifecycleMixin.attachment_orphan_plan(self)
        store = blob_store(self)
        if store is None:
            return plan
        if plan.orphans:
            store.require_owner()
        for sha in plan.orphans:
            _remove_blob(store, sha)
        reclaimed = tuple(store.reclaim_stale_stages())
        return OrphanPlan(plan.orphans, reclaimed, plan.blocked)


__all__ = [
    "IRRECOVERABLE_COPIES",
    "AttachmentLifecycleMixin",
    "OrphanPlan",
    "PurgePlan",
    "PurgeResult",
]
