"""Memory lifecycle, archive, rollback, and fsck operations."""

from __future__ import annotations

import builtins
import hashlib
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ..core.common import sha256_text
from ..core.context import RepositoryHost
from ..core.epistemic import (
    EPISTEMIC_FIELDS,
    OPEN_QUESTION_TYPE,
    CausalLevel,
    CausalRecord,
    EpistemicType,
    ExperiencePoint,
    OpenQuestion,
    validate_epistemic_write,
    validate_open_question_references,
)
from ..core.errors import DeletedContentUnavailable, IntegrityError, NotFoundError
from ..core.models import (
    EventKind,
    Layer,
    MemoryDocument,
    Tier,
    _same_instant,
    fold_temporal_aliases,
)
from ..storage import with_archived_body
from ..storage.transactions import _serialized

_LOG = logging.getLogger(__name__)


class MemoryOperationsMixin:
    @_serialized
    def add_many(self: RepositoryHost, /, documents: list[dict[str, Any]]) -> list[MemoryDocument]:
        """Create a batch under one recoverable repository transaction."""
        return [self.add(**document) for document in documents]

    @_serialized
    def add(
        self: RepositoryHost,
        /,
        title: str,
        body: str,
        *,
        memory_id: str | None = None,
        layer: Layer = Layer.CURRENT,
        tier: Tier = Tier.HOT,
        memory_type: str | None = None,
        tenant: str = "",
        related: builtins.list[str] | None = None,
        source: str = "manual",
        provenance: str = "human-authored",
        anchor: str | None = None,
        tags: builtins.list[str] | None = None,
        actor: str = "human",
        agent_id: str | None = None,
        signature: str | None = None,
        epistemic: EpistemicType | None = None,
        causal: CausalLevel = "none",
        causal_record: CausalRecord | dict[str, Any] | None = None,
        limitations: builtins.list[str] | None = None,
        experience: ExperiencePoint | dict[str, Any] | None = None,
        evidence: builtins.list[str] | None = None,
        about: builtins.list[str] | None = None,
        applies_to: builtins.list[str] | None = None,
        group_key: str | None = None,
        manually_marked: bool = False,
        open_question: OpenQuestion | dict[str, Any] | None = None,
    ) -> MemoryDocument:
        anchor_always_hot = (
            self.read_config().get("anchor_always_hot", True) if self.config_path.exists() else True
        )
        if anchor_always_hot and (layer == Layer.ANCHOR or bool(anchor)):
            tier = Tier.HOT
        if not self._can_mutate(
            actor, layer=layer, anchor=anchor, signature=signature, agent_id=agent_id
        ):
            raise IntegrityError("Actor lacks permission or a required agent signature")
        gate_values: dict[str, Any] = {
            "id": memory_id,
            "type": memory_type,
            "tenant": tenant,
            "epistemic": epistemic,
            "causal": causal,
            "causal_record": causal_record,
            "limitations": limitations or [],
            "experience": experience,
            "open_question": open_question,
            "evidence": evidence or [],
            "source": source,
            "provenance": provenance,
            "body": body,
        }
        validate_epistemic_write(gate_values, actor=actor)
        if memory_type == OPEN_QUESTION_TYPE and actor != "human":
            # Mechanisms (dream, the conversation worker) and agents stage an
            # open question as a candidate; a human applies it.
            raise IntegrityError(
                "type=" + OPEN_QUESTION_TYPE + " is created by a human or through a "
                "reviewed candidate, not by actor " + actor
            )
        self.init()
        identifier = self._check_id(memory_id or self.id_factory())
        if memory_type == OPEN_QUESTION_TYPE:
            validate_open_question_references(
                {**gate_values, "id": identifier}, lookup=lambda ref: lookup_current(self, ref)
            )
        path = self._current_path(identifier)
        known_ids = getattr(self._mutation_local, "event_memory_ids", None)
        if path.exists() or (
            identifier in known_ids
            if known_ids is not None
            else any(e.memory_id == identifier for e in self.events())
        ):
            raise IntegrityError(f"Memory id already used: {identifier}")
        now = self._now()
        document = MemoryDocument(
            id=identifier,
            title=title,
            body=body,
            layer=layer,
            tier=tier,
            type=memory_type,
            tenant=tenant,
            related=list(related or []),
            about=list(about or []),
            applies_to=list(applies_to or []),
            source=source,
            provenance=provenance,
            anchor=anchor,
            tags=tags or [],
            manually_marked=manually_marked,
            group_key=group_key,
            epistemic=epistemic,
            causal=causal,
            causal_record=causal_record,
            limitations=limitations or [],
            experience=experience,
            open_question=open_question,
            evidence=list(evidence or []),
            created_at=now,
            updated_at=now,
            recorded_at=now,
        )
        markdown = self.render(document)
        digest = self._write_version(identifier, markdown)
        self._atomic_write(path, markdown.encode("utf-8"))
        event_details = {"agent_id": agent_id, "signature": signature} if agent_id else {}
        self._append_event(
            EventKind.CREATE, identifier, actor=actor, after_hash=digest, details=event_details
        )
        return document

    def get(self: RepositoryHost, memory_id: str) -> MemoryDocument:
        self.assert_no_pending()
        path = self._current_path(memory_id)
        if not path.exists():
            raise NotFoundError(f"Memory not found: {memory_id}")
        return self.parse(path.read_bytes().decode("utf-8"), expected_id=memory_id)

    def list(self: RepositoryHost) -> list[MemoryDocument]:
        self.assert_no_pending()
        self._list_warnings: list[str] = []
        if not self.memories_dir.exists():
            return []
        documents: list[MemoryDocument] = []
        for path in sorted(self.memories_dir.glob("*.md")):
            try:
                document = self.parse(path.read_bytes().decode("utf-8"), expected_id=path.stem)
                documents.append(document)
                if document.ignored_keys:
                    self._list_warnings.append(
                        f"{path.name}: ignored unknown frontmatter keys: "
                        f"{', '.join(document.ignored_keys)} "
                        "(a typo? a rewrite by this version drops them)"
                    )
            except (IntegrityError, UnicodeDecodeError) as exc:
                self._list_warnings.append(f"{path.name}: invalid Markdown ({exc})")
        try:
            committed = self.replay()
            present = {doc.id for doc in documents}
            for memory_id, digest in committed.items():
                if digest is not None and memory_id not in present:
                    self._list_warnings.append(f"{memory_id}: committed file missing; run fsck")
        except IntegrityError as exc:
            self._list_warnings.append(f"event log integrity error: {exc}")
        return documents

    def list_warnings(self: RepositoryHost) -> builtins.list[str]:
        if not hasattr(self, "_list_warnings"):
            self.list()
        return list(self._list_warnings)

    @_serialized
    def update(
        self: RepositoryHost,
        /,
        memory_id: str,
        *,
        actor: str = "human",
        agent_id: str | None = None,
        signature: str | None = None,
        proposal_id: str | None = None,
        approved_by: str | None = None,
        proposal_operation: str | None = None,
        _human_decision: bool = False,
        **changes: Any,
    ) -> MemoryDocument:
        old = self.get(memory_id)
        if changes.get("tier") == Tier.COLD and old.tier != Tier.COLD:
            self._ensure_cooling_preserves_proposals(memory_id)
        current_bytes = self._current_path(memory_id).read_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        target_hash = self._payload_hash(memory_id, proposal_operation or "update", changes)
        approved = bool(
            proposal_id
            and approved_by == "human"
            and self._approved_proposal(
                actor=actor,
                proposal_id=proposal_id,
                target_hash=target_hash,
                before_hash=current_hash,
            )
        )
        if actor == "simulated_approver" and approved_by == "simulated_approver":
            approved = self._simulated_proposal_approved(memory_id, proposal_id)
        if not self._can_mutate(
            actor,
            layer=old.layer,
            anchor=old.anchor,
            signature=signature,
            agent_id=agent_id,
            approved=approved,
        ):
            raise IntegrityError("This actor lacks permission or a current human-approved proposal")
        old_markdown = self._current_path(memory_id).read_bytes().decode("utf-8")
        before_hash = sha256_text(old_markdown)
        allowed = {
            "title",
            "body",
            "layer",
            "tier",
            "type",
            "tenant",
            "related",
            "about",
            "applies_to",
            "source",
            "provenance",
            "anchor",
            "tags",
            "summary",
            "is_stub",
            "archive_ref",
            "archived_hash",
            "strength",
            "reference_count",
            "last_access_interval_days",
            "manually_marked",
            "group_key",
            "valid_from",
            "valid_to",
            "valid_until",
            "superseded_by",
            "superseded",
            # Verification state, set by a human approving a dream candidate.
            "status",
            "evidence",
            # Epistemic type and causal grade, gated by ``validate_epistemic_write``.
            "epistemic",
            "causal",
            "causal_record",
            "limitations",
            "experience",
            "open_question",
        }
        if (
            actor != "human"
            and (changes.get("layer") == Layer.ANCHOR or changes.get("anchor"))
            and not approved
        ):
            raise IntegrityError("Setting an anchor requires a human-approved proposal")
        if not changes or set(changes) - allowed:
            raise IntegrityError(f"Update accepts fields: {', '.join(sorted(allowed))}")
        anchor_change = ("anchor" in changes and changes["anchor"] != old.anchor) or (
            "layer" in changes
            and changes["layer"] != old.layer
            and (changes["layer"] == Layer.ANCHOR or old.layer == Layer.ANCHOR)
        )
        if anchor_change and actor != "human":
            raise IntegrityError("Only a human actor may set or remove anchor protection")
        # Fold aliases before they meet the stored document. A conflict is only
        # two names in this request, and it raises before any write.
        folded_changes = fold_temporal_aliases(dict(changes))
        new_target = folded_changes.get("superseded_by")
        if new_target is not None and new_target != old.superseded_by:
            _check_supersession_target(self, memory_id, str(new_target))
        values = old.model_dump()
        values.update(folded_changes)
        # One gate for every write path. A human approval of an agent proposal
        # counts as a human decision; a bare agent write can edit an
        # already-verified document but cannot turn one into verified.
        if set(folded_changes) & EPISTEMIC_FIELDS:
            validate_epistemic_write(
                values,
                actor=actor,
                human_decision=approved or (_human_decision and actor == "human"),
                previous_causal=old.causal,
            )
        if OPEN_QUESTION_TYPE in (values.get("type"), old.type):
            # Turning a memory into or out of an open question, and resolving
            # or reopening one, are human decisions: a human actor or an
            # approved proposal. A mechanism or a FULL-permission agent may
            # still edit an open question's other fields.
            if not (actor == "human" or approved):
                old_answer = old.open_question.resolved_by if old.open_question else None
                new_block = values.get("open_question")
                if isinstance(new_block, OpenQuestion):
                    new_answer = new_block.resolved_by
                elif isinstance(new_block, dict):
                    new_answer = new_block.get("resolved_by")
                else:
                    new_answer = None
                if (values.get("type") == OPEN_QUESTION_TYPE) != (old.type == OPEN_QUESTION_TYPE):
                    raise IntegrityError(
                        "Only a human or an approved proposal may change type to or from "
                        + OPEN_QUESTION_TYPE
                    )
                if new_answer != old_answer:
                    raise IntegrityError(
                        "Only a human or an approved proposal may resolve or reopen an open "
                        "question"
                    )
                if old.type == OPEN_QUESTION_TYPE and not _same_instant(
                    values.get("valid_to"), old.valid_to
                ):
                    raise IntegrityError(
                        "Only a human or an approved proposal may change valid_to of an open "
                        "question"
                    )
                if old.valid_to is not None and old.open_question is not None:
                    if isinstance(new_block, OpenQuestion):
                        new_conflicts = new_block.conflicts
                    elif isinstance(new_block, dict):
                        new_conflicts = OpenQuestion.model_validate(new_block).conflicts
                    else:
                        new_conflicts = []
                    if new_conflicts != old.open_question.conflicts:
                        raise IntegrityError(
                            "Only a human or an approved proposal may change the conflicts of "
                            "an open question that is no longer current"
                        )
            # Runs on every write to an open question, not only gated fields:
            # a tenant change must not leave conflicts in another tenant.
            validate_open_question_references(
                values,
                lookup=lambda ref: lookup_current(self, ref),
                previous=old.model_dump(),
                actor=actor,
            )
        config = self.read_config()
        is_anchored = values.get("layer") == Layer.ANCHOR or bool(values.get("anchor"))
        if config.get("anchor_always_hot", True) and old.is_stub and is_anchored and anchor_change:
            raise IntegrityError("Restore a cold stub before anchoring it")
        if config.get("anchor_always_hot", True) and is_anchored:
            values["tier"] = Tier.HOT
        values["version"] = old.version + 1
        values["updated_at"] = self._now()
        new_doc = MemoryDocument.model_validate(values)
        # ``values`` came from ``model_dump``, which materialises every default.
        # Carry the source file's optional-field spelling across the rebuild so a
        # write does not silently drop lines the file already had.
        new_doc._observed_default_fields = old._observed_default_fields
        if new_doc.is_stub:
            with_archived_body(self, new_doc)
        # A restored archive may have had its duplicate history object removed.
        # Preserve the exact current state before replacing it so rollback remains
        # available without keeping two copies of the active Markdown.
        self._write_version(memory_id, old_markdown)
        markdown = self.render(new_doc)
        after_hash = self._write_version(memory_id, markdown, base_hash=before_hash)
        self._atomic_write(self._current_path(memory_id), markdown.encode("utf-8"))
        if "layer" in changes and len(changes) == 1:
            kind = EventKind.LAYER_CHANGE
        elif "tier" in changes and len(changes) == 1:
            kind = EventKind.TIER_CHANGE
        elif "anchor" in changes and changes["anchor"] is not None and len(changes) == 1:
            kind = EventKind.ANCHOR_SET
        else:
            kind = EventKind.UPDATE
        event_details: dict[str, Any] = {}
        if agent_id:
            event_details.update({"agent_id": agent_id, "signature": signature})
        if proposal_id:
            event_details.update({"proposal_id": proposal_id, "approved_by": approved_by})
        self._append_event(
            kind,
            memory_id,
            actor=actor,
            before_hash=before_hash,
            after_hash=after_hash,
            details=event_details,
        )
        self._prune_history(memory_id)
        return new_doc

    @_serialized
    def supersede(
        self: RepositoryHost,
        /,
        memory_id: str,
        title: str,
        body: str,
        *,
        actor: str = "human",
        memory_id_new: str | None = None,
        tags: builtins.list[str] | None = None,
        source: str | None = None,
        provenance: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> MemoryDocument:
        """Create a replacement and mark the old version historical.

        The old Markdown is never deleted. Its valid_to/superseded_by fields are
        updated in place and a supersede event links the two records. Only a human
        actor may apply this operation.

        A replacement is a new version of the same memory, so it keeps the
        predecessor's scope and classification and starts fresh bookkeeping:

        ==========================  ==========================================
        Field                       Replacement value
        ==========================  ==========================================
        title, body                 the caller's (required arguments)
        tenant                      inherited, never overridable: a supersede
                                    cannot move a memory between tenants
                                    (``update`` is the explicit way to do that)
        layer, tier, type           inherited (``anchor_always_hot`` still
                                    forces an anchored replacement hot)
        related, about, applies_to  inherited
        anchor                      inherited
        group_key                   unset: budget consolidation groups by
                                    ``group_key`` and keeps the most salient or
                                    most recently updated member, which would be
                                    the just-superseded predecessor; the current
                                    replacement would then be planned for
                                    forgetting
        manually_marked             inherited (a human pin, not usage data)
        source, tags                inherited unless ``source``/``tags`` given
        provenance                  ``provenance`` or ``supersedes:<old id>``
        epistemic, causal,          inherited, through the same epistemic write
        causal_record, limitations, gate as ``add``
        experience, evidence
        open_question               inherited for an open question, with
                                    ``resolved_by`` cleared
        status                      reset to ``unconfirmed``: verification
                                    applied to the old body, not the new one
        id, version                 new id (or ``memory_id_new``), version 1
        created_at, updated_at,     fresh; ``created_at`` is the handoff instant
        recorded_at                 that also becomes the old ``valid_to``
        valid_from                  unset, so readers start validity at
                                    ``created_at`` (the handoff); the old
                                    ``valid_from`` describes the old version
        valid_to, superseded_by     unset (the replacement is current)
        summary                     unset: it is derived from the old body
        is_stub, archive_ref,       unset: they describe the old file's cold
        archived_hash               archive, not the replacement's body
        strength, reference_count,  defaults: access statistics belong to the
        last_access_interval_days   record that was accessed; copying them would
                                    invent a usage history for the new body
                                    (``manually_marked`` and ``anchor`` carry the
                                    human protections against forgetting)
        ==========================  ==========================================

        ``overrides`` may set any of ``layer``, ``tier``, ``type``, ``related``,
        ``about``, ``applies_to``, ``anchor``, ``group_key``, ``manually_marked``,
        ``epistemic``, ``causal``, ``causal_record``, ``limitations``,
        ``experience``, ``evidence`` and ``open_question``. ``tenant`` cannot be
        overridden.

        ``fsck`` reports ``supersede_scope_mismatch`` whenever a replacement's
        tenant differs from its predecessor's. That includes a deliberate move
        with ``update`` after the supersede: the finding stays reported, as
        there is no way to acknowledge it yet.
        """
        if actor != "human":
            raise IntegrityError("Supersede requires a human actor")
        old = self.get(memory_id)
        if old.valid_to is not None:
            raise IntegrityError(f"Memory is already superseded: {memory_id}")
        question_block: OpenQuestion | None = None
        dropped_conflicts: builtins.list[str] = []
        if old.type == OPEN_QUESTION_TYPE and old.open_question is not None:
            # An open question stays an open question, still unresolved
            # (resolving one is ``resolve_open_question``). A conflict deleted
            # (or moved to another tenant) since the question was asked cannot
            # carry over; it is dropped and named in the SUPERSEDE event instead
            # of blocking the rewrite.
            kept: builtins.list[str] = []
            for conflict in old.open_question.conflicts:
                target = lookup_current(self, conflict)
                if target is None or target.tenant != old.tenant:
                    dropped_conflicts.append(conflict)
                else:
                    kept.append(conflict)
            question_block = old.open_question.model_copy(
                update={"resolved_by": None, "conflicts": kept}
            )
        inherited: dict[str, Any] = {
            "layer": old.layer,
            "tier": old.tier,
            "type": old.type,
            "related": list(old.related),
            "about": list(old.about),
            "applies_to": list(old.applies_to),
            "anchor": old.anchor,
            # Not inherited: see the table above.
            "group_key": None,
            "manually_marked": old.manually_marked,
            # A replacement is still the same epistemic object: keep the
            # experience record (and the causal grade) instead of dropping it.
            "epistemic": old.epistemic,
            "causal": old.causal,
            "causal_record": old.causal_record,
            "limitations": list(old.limitations),
            "experience": old.experience,
            "evidence": list(old.evidence),
            "open_question": question_block,
        }
        if overrides:
            if "tenant" in overrides:
                raise IntegrityError(
                    "Supersede cannot override tenant: a replacement stays in its "
                    "predecessor's tenant"
                )
            unknown = sorted(set(overrides) - set(inherited))
            if unknown:
                raise IntegrityError(
                    f"Supersede override not accepted: {', '.join(unknown)}; "
                    f"accepted fields: {', '.join(sorted(inherited))}"
                )
            inherited.update(overrides)
        memory_type = inherited.pop("type")
        new = self.add(
            title,
            body,
            memory_id=memory_id_new,
            memory_type=memory_type,
            tenant=old.tenant,
            source=source or old.source,
            provenance=provenance or f"supersedes:{memory_id}",
            tags=tags if tags is not None else list(old.tags),
            actor="human",
            **inherited,
        )
        # One instant for both sides of the handoff. Taking a fresh ``_now()``
        # here would stamp ``valid_to`` a little after the replacement's own
        # ``created_at`` (the add above is stamped first, then finishes its
        # writes), and an ``as_of`` lookup inside that window would report both
        # records as current. The replacement takes effect exactly when the old
        # record becomes historical, so the two validity intervals stay disjoint.
        handoff = new.created_at
        self.update(
            memory_id,
            actor="human",
            valid_to=handoff,
            superseded_by=new.id,
        )
        self._append_event(
            EventKind.SUPERSEDE,
            memory_id,
            actor="human",
            details={
                "new_memory_id": new.id,
                "old_memory_id": memory_id,
                **({"dropped_conflicts": dropped_conflicts} if dropped_conflicts else {}),
            },
        )
        if dropped_conflicts:
            _LOG.warning(
                "supersede of open question %s dropped conflicts that no longer resolve: %s",
                memory_id,
                ", ".join(dropped_conflicts),
            )
        return new

    @_serialized
    def resolve_open_question(
        self: RepositoryHost,
        /,
        question_id: str,
        answer_id: str,
        *,
        actor: str = "human",
    ) -> MemoryDocument:
        """Close an open question with the memory that answered it.

        An ordinary update of the question: ``open_question.resolved_by`` names
        the answer and ``valid_to`` ends the question's validity. The question
        file is kept, and the unresolved version stays in the history. The
        answer must exist, share the question's tenant and be current. Only a
        human may resolve a question.
        """
        if actor != "human":
            raise IntegrityError("Resolving an open question requires a human actor")
        question = self.get(question_id)
        if question.type != OPEN_QUESTION_TYPE or question.open_question is None:
            raise IntegrityError(f"Not an open question: {question_id}")
        if question.open_question.resolved_by is not None:
            raise IntegrityError(
                f"Open question is already resolved by {question.open_question.resolved_by}: "
                + question_id
            )
        if question.valid_to is not None:
            raise IntegrityError(f"Open question is no longer current: {question_id}")
        block = question.open_question.model_copy(update={"resolved_by": answer_id})
        return self.update(
            question_id,
            actor="human",
            open_question=block.model_dump(mode="json"),
            valid_to=self._now(),
        )

    @_serialized
    def cold_archive(
        self: RepositoryHost,
        /,
        archive_id: str,
        *,
        actor: str = "human",
        memory_ids: set[str] | None = None,
    ) -> Path:
        from ..storage import create_cold_archive

        if actor not in {"human", "mechanism"}:
            raise IntegrityError(
                "Cold archive writes require a human or authorized mechanism actor"
            )
        documents = [doc for doc in self.list() if doc.tier == Tier.COLD and not doc.is_stub]
        for doc in documents:
            self._ensure_cooling_preserves_proposals(doc.id)
        if actor == "mechanism" or self.read_config().get("anchor_always_hot", True):
            documents = [
                doc for doc in documents if doc.layer != Layer.ANCHOR and doc.anchor is None
            ]
        if memory_ids is not None:
            documents = [doc for doc in documents if doc.id in memory_ids]
        selected_ids = {doc.id for doc in documents}
        archive = create_cold_archive(self, archive_id, memory_ids=selected_ids)
        for doc in documents:
            old_markdown = self._current_path(doc.id).read_bytes().decode("utf-8")
            before_hash = sha256_text(old_markdown)
            # Preserve a metadata-only rollback reference before replacing the
            # current Markdown. The archive is the body source for that version.
            self._write_version(doc.id, old_markdown)
            stub = doc.model_copy(
                update={
                    "summary": doc.summary or " ".join(doc.body.split())[:240],
                    "body": "",
                    "is_stub": True,
                    "archive_ref": archive.name.removesuffix(".tar.zst"),
                    "archived_hash": hashlib.sha256(
                        self._current_path(doc.id).read_bytes()
                    ).hexdigest(),
                    "version": doc.version + 1,
                    "updated_at": self._now(),
                }
            )
            stub_markdown = self.render(stub)
            after_hash = self._write_version(doc.id, stub_markdown)
            self._atomic_write(self._current_path(doc.id), stub_markdown.encode("utf-8"))
            self._append_event(
                EventKind.UPDATE,
                doc.id,
                actor=actor,
                before_hash=before_hash,
                after_hash=after_hash,
                details={"storage": "cold_stub"},
            )
            self._prune_archive_history(doc.id)
        # The disposable SQLite cache can contain full pre-archive bodies. Rebuild
        # from Markdown stubs when search is next requested.
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.index_path}{suffix}").unlink(missing_ok=True)
        return archive

    @_serialized
    def restore_cold(
        self: RepositoryHost, /, memory_id: str, *, actor: str = "human"
    ) -> MemoryDocument:
        """Restore archived Markdown bytes exactly; tier changes are a separate operation."""
        if actor not in {"human", "mechanism"}:
            raise IntegrityError("Cold restore requires a human or authorized mechanism actor")
        try:
            stub = self.get(memory_id)
        except NotFoundError as exc:
            if any(
                event.memory_id == memory_id and event.kind == EventKind.DELETE
                for event in self.events()
            ):
                raise DeletedContentUnavailable(f"Memory {memory_id} was hard deleted") from exc
            raise
        if not stub.is_stub:
            return stub
        import io
        import tarfile

        import zstandard

        archive_path = (
            self.archives_dir / f"{stub.archive_ref}.tar.zst" if stub.archive_ref else None
        )
        if archive_path is None or not archive_path.exists():
            raise DeletedContentUnavailable(f"No managed cold archive contains {memory_id}")
        try:
            tar_bytes = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
                member = tar.extractfile(f"memories/{memory_id}.md")
                if member is None:
                    raise DeletedContentUnavailable(f"No managed cold archive contains {memory_id}")
                raw = member.read()
            restored = self.parse(raw.decode("utf-8"), expected_id=memory_id)
        except (
            OSError,
            UnicodeDecodeError,
            zstandard.ZstdError,
            tarfile.TarError,
            KeyError,
        ) as exc:
            raise DeletedContentUnavailable(
                f"Unable to restore archived bytes for {memory_id}"
            ) from exc
        digest = hashlib.sha256(raw).hexdigest()
        if not stub.archived_hash or digest != stub.archived_hash:
            raise IntegrityError(f"Archived Markdown hash mismatch for {memory_id}")
        stub_bytes = self._current_path(memory_id).read_bytes()
        self._write_version(memory_id, raw.decode("utf-8"))
        self._atomic_write(self._current_path(memory_id), raw)
        self._append_event(
            EventKind.MEMORY_RESTORE,
            memory_id,
            actor=actor,
            before_hash=hashlib.sha256(stub_bytes).hexdigest(),
            after_hash=digest,
            details={"archive_ref": stub.archive_ref},
        )
        self._prune_history(memory_id)
        # The current Markdown is the authoritative active copy. Do not also retain
        # the byte-identical archive-time history object after removing the tar member.
        self._history_path(memory_id, digest).unlink(missing_ok=True)
        self._history_metadata_path(memory_id, digest).unlink(missing_ok=True)
        from ..storage import remove_memory_from_archives

        remove_memory_from_archives(self, memory_id)
        return restored

    def _prune_archive_history(self: RepositoryHost, memory_id: str) -> None:
        """Retain the configured number of pre-stub Markdown versions plus the stub."""
        config = self.read_config()
        keep = int(config.get("archive_keep_history", 1))
        events = [
            event.after_hash
            for event in self.events()
            if event.memory_id == memory_id and event.after_hash is not None
        ]
        current = hashlib.sha256(self._current_path(memory_id).read_bytes()).hexdigest()
        previous: list[str] = []
        for item in events:
            if item != current and (not previous or previous[-1] != item):
                previous.append(item)
        retained = set(previous[-keep:] if keep else []) | {current}
        retained |= {self._body_source_hash(memory_id, item) for item in tuple(retained)}
        # Archive history keeps metadata references only. Archived Markdown is
        # already the verified body object; duplicating it under .history is waste.
        history = self.history_dir / memory_id
        if history.exists():
            for candidate in history.iterdir():
                content_hash = candidate.name.removesuffix(".meta.json").removesuffix(".md")
                if content_hash not in retained:
                    candidate.unlink()

    @_serialized
    def autowarm(
        self: RepositoryHost, /, memory_id: str, *, actor: str = "mechanism"
    ) -> MemoryDocument:
        """Restore a cold stub while retaining its latest metadata, then warm it."""
        current = self.get(memory_id)
        if actor not in {"human", "mechanism"}:
            raise IntegrityError("Autowarm requires a human or authorized mechanism actor")
        if current.is_stub:
            if not current.archive_ref:
                raise DeletedContentUnavailable(f"Memory {memory_id} has no archive reference")
            archive_path = self.archives_dir / f"{current.archive_ref}.tar.zst"
            import io
            import tarfile

            import zstandard

            try:
                tar_bytes = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
                    member = tar.extractfile(f"memories/{memory_id}.md")
                    if member is None:
                        raise DeletedContentUnavailable(
                            f"No managed cold archive contains {memory_id}"
                        )
                    raw = member.read()
                archived = self.parse(raw.decode("utf-8"), expected_id=memory_id)
            except (
                OSError,
                UnicodeDecodeError,
                zstandard.ZstdError,
                tarfile.TarError,
                KeyError,
            ) as exc:
                raise DeletedContentUnavailable(
                    f"Unable to warm archived bytes for {memory_id}"
                ) from exc
            digest = hashlib.sha256(raw).hexdigest()
            if not current.archived_hash or digest != current.archived_hash:
                raise IntegrityError(f"Archived Markdown hash mismatch for {memory_id}")
            current_bytes = self._current_path(memory_id).read_bytes()
            restored = current.model_copy(
                update={
                    "body": archived.body,
                    "is_stub": False,
                    "archive_ref": None,
                    "archived_hash": None,
                    "version": current.version + 1,
                    "updated_at": self._now(),
                }
            )
            restored_markdown = self.render(restored)
            restored_hash = self._write_version(memory_id, restored_markdown)
            self._atomic_write(self._current_path(memory_id), restored_markdown.encode("utf-8"))
            self._append_event(
                EventKind.MEMORY_RESTORE,
                memory_id,
                actor=actor,
                before_hash=hashlib.sha256(current_bytes).hexdigest(),
                after_hash=restored_hash,
                details={"archive_ref": current.archive_ref, "metadata_preserved": True},
            )
            self._prune_history(memory_id)
            from ..storage import remove_memory_from_archives

            remove_memory_from_archives(self, memory_id)
            current = restored
        if current.tier != Tier.HOT:
            current_bytes = self._current_path(memory_id).read_bytes()
            warmed = current.model_copy(
                update={
                    "tier": Tier.HOT,
                    "version": current.version + 1,
                    "updated_at": self._now(),
                }
            )
            warmed_markdown = self.render(warmed)
            warmed_hash = self._write_version(memory_id, warmed_markdown)
            self._atomic_write(self._current_path(memory_id), warmed_markdown.encode("utf-8"))
            self._append_event(
                EventKind.TIER_CHANGE,
                memory_id,
                actor=actor,
                before_hash=hashlib.sha256(current_bytes).hexdigest(),
                after_hash=warmed_hash,
            )
            self._prune_history(memory_id)
            current = warmed
        return current

    @_serialized
    def delete(
        self: RepositoryHost,
        /,
        memory_id: str,
        *,
        actor: str = "human",
        agent_id: str | None = None,
        signature: str | None = None,
        proposal_id: str | None = None,
        approved_by: str | None = None,
        _preserve_proposal_ids: set[str] | None = None,
        _forget_details: dict[str, Any] | None = None,
    ) -> None:
        current = self.get(memory_id)
        current_path = self._current_path(memory_id)
        current_hash = (
            hashlib.sha256(current_path.read_bytes()).hexdigest() if current_path.exists() else None
        )
        if self.knowledge_source_counts().get(memory_id, 0) and not (
            actor == "human" and self._human_approved_source_delete(memory_id, proposal_id)
        ):
            raise IntegrityError(
                "Knowledge-card source deletion requires a current human-approved forget proposal"
            )
        target_hash = self._payload_hash(memory_id, "delete", {})
        approved = bool(
            proposal_id
            and approved_by == "human"
            and self._approved_proposal(
                actor=actor,
                proposal_id=proposal_id,
                target_hash=target_hash,
                before_hash=current_hash,
            )
        )
        if actor == "simulated_approver" and approved_by == "simulated_approver":
            approved = self._simulated_proposal_approved(memory_id, proposal_id)
        if not self._can_mutate(
            actor,
            layer=current.layer,
            anchor=current.anchor,
            signature=signature,
            agent_id=agent_id,
            approved=approved,
        ):
            raise IntegrityError("This actor lacks permission or a current human-approved proposal")
        current_path = self._current_path(memory_id)
        if current_path.exists():
            before_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
        else:
            previous = next(
                (
                    event.after_hash
                    for event in reversed(self.events())
                    if event.memory_id == memory_id and event.after_hash is not None
                ),
                None,
            )
            if previous is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            before_hash = previous
        # Commit the tombstone first. A crash leaves a recoverable pending marker and
        # recovery completes deletion rather than presenting a partial edit as external.
        delete_details: dict[str, Any] = {"operation": "delete"}
        if _forget_details:
            delete_details.update(_forget_details)
        if agent_id:
            delete_details.update({"agent_id": agent_id, "signature": signature})
        if proposal_id:
            delete_details.update({"proposal_id": proposal_id, "approved_by": approved_by})
        self._append_event(
            EventKind.DELETE,
            memory_id,
            actor=actor,
            before_hash=before_hash,
            details=delete_details,
        )
        self._preserve_shared_body_before_erasure(memory_id)
        # Remove every managed content copy, including cold archives. This is logical
        # erasure only; filesystem, snapshots, and storage media may retain blocks.
        self._current_path(memory_id).unlink(missing_ok=True)
        history = self.history_dir / memory_id
        if history.exists():
            shutil.rmtree(history)
        # The memory's history may have moved body text into shared body
        # objects; drop the ones no surviving history still references. The
        # DELETE is already committed, so a failure here must not abort the
        # rest of the erasure; ``fsck`` prunes again.
        try:
            cast(Any, self)._prune_body_objects()
        except Exception:
            _LOG.warning(
                "body-object prune after deleting %s failed; run fsck", memory_id, exc_info=True
            )
        for quarantined in (self.root / ".quarantine").glob(f"{memory_id}-*.md"):
            quarantined.unlink(missing_ok=True)
        if self.archives_dir.exists():
            from ..storage import remove_memory_from_archives

            remove_memory_from_archives(self, memory_id)
        self._erase_target_proposals(
            memory_id, actor=actor, preserve_ids=_preserve_proposal_ids or set()
        )
        _clear_superseded_by_refs(self, memory_id, actor=actor)
        if self.index_path.exists():
            # Drop the disposable database so deleted text is not left in FTS5 pages.
            self.index_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.index_path}{suffix}").unlink(missing_ok=True)
        self.validate_knowledge_cards()

    def _erase_target_proposals(
        self: RepositoryHost,
        memory_id: str,
        *,
        actor: str,
        preserve_ids: set[str] | None = None,
    ) -> None:
        """Remove erasable proposal Markdown that references a hard-deleted memory."""
        import json
        import re

        preserved = preserve_ids or set()
        if not self.proposals_dir.exists():
            return
        proposal_paths = sorted(self.proposals_dir.glob("*.md"))
        if not proposal_paths:
            return
        for path in proposal_paths:
            raw = path.read_bytes()
            match = re.fullmatch(
                rb"# Erasable agent proposal\n\n```json\n(.*)\n```\n", raw, re.DOTALL
            )
            if match is None:
                continue
            try:
                payload = json.loads(match.group(1))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            targets = payload.get("target_ids") if isinstance(payload, dict) else None
            if not isinstance(targets, list) or memory_id not in targets:
                continue
            proposal_id = payload.get("proposal_id")
            if not isinstance(proposal_id, str):
                continue
            if proposal_id in preserved:
                continue
            self._append_event(
                EventKind.PROPOSAL_ERASE,
                "proposal",
                actor=actor,
                before_hash=hashlib.sha256(raw).hexdigest(),
                details={"proposal_id": proposal_id},
            )
            path.unlink()

    @_serialized
    def rollback(self: RepositoryHost, /, event_id: str, *, actor: str = "human") -> MemoryDocument:
        event_list = self.events()
        event = next((item for item in event_list if item.event_id == event_id), None)
        if event is None:
            raise NotFoundError(f"Event not found: {event_id}")
        if event.kind == EventKind.DELETE:
            raise DeletedContentUnavailable(
                "A hard-deleted memory has no retained version to restore"
            )
        if event.before_hash is None:
            raise IntegrityError("Event has no prior state to roll back to")
        config = self.read_config()
        rollback_keep = max(
            1,
            int(config.get("history_keep", 2)),
            int(config.get("archive_keep_history", 1)),
        )
        retained_hashes = [
            item.after_hash
            for item in event_list
            if item.memory_id == event.memory_id and item.after_hash is not None
        ][-rollback_keep:]
        if event.before_hash not in retained_hashes:
            raise DeletedContentUnavailable(
                "Prior Markdown version is outside configured rollback retention"
            )
        if not self._has_version(event.memory_id, event.before_hash):
            raise DeletedContentUnavailable("Prior Markdown version has been erased")
        old_doc = self.parse(
            self._read_version(event.memory_id, event.before_hash).decode("utf-8"),
            expected_id=event.memory_id,
        )
        if old_doc.is_stub:
            try:
                # A historical stub is usable only while its referenced original
                # member is still present and matches archived_hash. Archive files
                # may remain for other notes after this note was restored.
                with_archived_body(self, old_doc)
            except (IntegrityError, OSError) as exc:
                raise DeletedContentUnavailable(
                    "Prior cold Markdown stub has no verified archived body"
                ) from exc
        # Restore every document field of the target version. Identity and the
        # server-stamped fields stay with the current record: ``update`` assigns
        # the next version and a fresh ``updated_at``; ``created_at`` and
        # ``recorded_at`` never change after ``add``.
        restored = old_doc.model_dump(
            exclude={"id", "created_at", "recorded_at", "version", "updated_at"}
        )
        # An absent block is left out of the dump; restore that absence too.
        restored.setdefault("open_question", None)
        stale_target = restored.get("superseded_by")
        if (
            stale_target is not None
            and stale_target != self.get(event.memory_id).superseded_by
            and not self._current_path(str(stale_target)).exists()
        ):
            raise IntegrityError(
                f"Rollback target version of {event.memory_id} has superseded_by "
                f"{stale_target}, which points to a deleted memory; clear superseded_by "
                f"on that version (update {event.memory_id} with superseded_by=None and "
                "the other fields you need) instead of rolling back to it"
            )
        # A human rollback is a human decision about the restored state, so the
        # epistemic gate treats it like a recorded HUMAN_DECISION.
        return self.update(
            event.memory_id,
            actor=actor,
            _human_decision=actor == "human",
            **restored,
        )

    @_serialized
    def fsck(self: RepositoryHost, /) -> builtins.list[dict[str, str | None]]:
        """Register valid external changes, quarantine invalid bytes, and report deletes."""
        expected = self.replay()
        issues: list[dict[str, str | None]] = []
        registered_deletions = False
        quarantine = self.root / ".quarantine"
        for memory_id, expected_hash in expected.items():
            path = self._current_path(memory_id)
            if expected_hash is None:
                if path.exists():
                    raw = path.read_bytes()
                    issues.append(
                        {
                            "memory_id": memory_id,
                            "kind": "tombstone_file_present",
                            "expected_hash": None,
                            "actual_hash": hashlib.sha256(raw).hexdigest(),
                        }
                    )
                continue
            if not path.exists():
                self._append_event(
                    EventKind.EXTERNAL_DELETE,
                    memory_id,
                    actor="mechanism",
                    before_hash=expected_hash,
                    details={"reason": "current_file_missing"},
                )
                registered_deletions = True
                issues.append(
                    {
                        "memory_id": memory_id,
                        "kind": "external_delete_registered",
                        "expected_hash": expected_hash,
                        "actual_hash": None,
                    }
                )
                continue
            raw = path.read_bytes()
            actual_hash = hashlib.sha256(raw).hexdigest()
            if actual_hash == expected_hash:
                continue
            try:
                self.parse(raw.decode("utf-8"), expected_id=memory_id)
            except (UnicodeDecodeError, IntegrityError) as exc:
                quarantine.mkdir(exist_ok=True)
                target = quarantine / f"{memory_id}-{actual_hash}.md"
                self._atomic_write(target, raw)
                path.unlink()
                self._append_event(
                    EventKind.EXTERNAL_EDIT,
                    memory_id,
                    actor="mechanism",
                    before_hash=expected_hash,
                    details={"actual_hash": actual_hash, "quarantined": True},
                )
                issues.append(
                    {
                        "memory_id": memory_id,
                        "kind": "invalid_file_quarantined",
                        "expected_hash": expected_hash,
                        "actual_hash": actual_hash,
                        "message": str(exc),
                    }
                )
                continue
            self._write_version(memory_id, raw.decode("utf-8"))
            self._append_event(
                EventKind.EXTERNAL_EDIT,
                memory_id,
                actor="mechanism",
                before_hash=expected_hash,
                after_hash=actual_hash,
            )
            self._prune_history(memory_id)
            issues.append(
                {
                    "memory_id": memory_id,
                    "kind": "external_edit_registered",
                    "expected_hash": expected_hash,
                    "actual_hash": actual_hash,
                }
            )
        committed = set(expected)
        for path in sorted(self.memories_dir.glob("*.md")):
            if path.stem in committed:
                continue
            try:
                self._check_id(path.stem)
                raw = path.read_bytes()
                markdown = raw.decode("utf-8")
                self.parse(markdown, expected_id=path.stem)
            except (IntegrityError, UnicodeDecodeError) as exc:
                raw = path.read_bytes()
                actual_hash = hashlib.sha256(raw).hexdigest()
                quarantine.mkdir(exist_ok=True)
                self._atomic_write(quarantine / f"{path.stem}-{actual_hash}.md", raw)
                path.unlink()
                self._append_event(
                    EventKind.EXTERNAL_EDIT,
                    path.stem,
                    actor="mechanism",
                    details={"actual_hash": actual_hash, "quarantined": True},
                )
                issues.append(
                    {
                        "memory_id": path.stem,
                        "kind": "invalid_file_quarantined",
                        "expected_hash": None,
                        "actual_hash": actual_hash,
                        "message": str(exc),
                    }
                )
                continue
            actual_hash = self._write_version(path.stem, markdown)
            self._append_event(
                EventKind.EXTERNAL_EDIT, path.stem, actor="mechanism", after_hash=actual_hash
            )
            issues.append(
                {
                    "memory_id": path.stem,
                    "kind": "untracked_file_registered",
                    "expected_hash": None,
                    "actual_hash": actual_hash,
                }
            )
        # Re-run the post-DELETE cleanup that ``delete`` performs after its
        # tombstone commits, in case it failed or was interrupted there.
        try:
            cast(Any, self)._prune_body_objects()
        except Exception as exc:
            issues.append(
                {
                    "memory_id": None,
                    "kind": "body_prune_failed",
                    "expected_hash": None,
                    "actual_hash": None,
                    "message": str(exc),
                }
            )
        deleted = {key for key, value in expected.items() if value is None}
        for document in _current_documents(self):
            stale = document.superseded_by
            if stale is None or stale not in deleted or self._current_path(stale).exists():
                continue
            cleared = _clear_superseded_by_refs(self, stale, actor="mechanism")
            for referrer in cleared:
                issues.append(
                    {
                        "memory_id": referrer,
                        "kind": "deleted_superseded_by_cleared",
                        "expected_hash": None,
                        "actual_hash": None,
                        "message": f"cleared superseded_by to deleted memory {stale}",
                    }
                )
        current_documents = _current_documents(self)
        issues.extend(_supersession_issues(current_documents))
        successors = {
            str(event.details["old_memory_id"]): str(event.details["new_memory_id"])
            for event in self.events()
            if event.kind == EventKind.SUPERSEDE
            and event.details.get("old_memory_id")
            and event.details.get("new_memory_id")
        }
        issues.extend(_open_question_issues(current_documents, successors=successors))
        if registered_deletions:
            self.validate_knowledge_cards()
        return issues


def _check_supersession_target(host: RepositoryHost, memory_id: str, target: str) -> None:
    """Reject a ``superseded_by`` that is missing, the memory itself, or a cycle."""
    if target == memory_id:
        raise IntegrityError(f"A memory cannot supersede itself: {memory_id}")
    if not host._current_path(target).exists():
        raise IntegrityError(f"superseded_by names a missing memory: {target}")
    seen: set[str] = set()
    current: str | None = target
    while current is not None and current not in seen:
        if current == memory_id:
            raise IntegrityError(
                f"superseded_by {target} would create a supersession cycle with {memory_id}"
            )
        seen.add(current)
        if not host._current_path(current).exists():
            return
        current = host.get(current).superseded_by


def _supersession_issues(
    documents: builtins.list[MemoryDocument],
) -> builtins.list[dict[str, str | None]]:
    """Report broken ``superseded_by`` links and replacements in another tenant.

    ``supersede_scope_mismatch`` is read-only: before the supersede fix a
    replacement was always written in the default tenant, whatever the
    predecessor's. fsck names such replacements so an operator can decide;
    it never rewrites them.
    """
    links = {doc.id: doc.superseded_by for doc in documents if doc.superseded_by is not None}
    by_id = {doc.id: doc for doc in documents}
    present = set(by_id)
    issues: builtins.list[dict[str, str | None]] = []
    for memory_id, target in sorted(links.items()):
        if target not in present:
            issues.append(
                {
                    "memory_id": memory_id,
                    "kind": "dangling_superseded_by",
                    "expected_hash": None,
                    "actual_hash": None,
                    "message": f"superseded_by names a missing memory: {target}",
                }
            )
            continue
        predecessor_tenant = by_id[memory_id].tenant
        replacement_tenant = by_id[target].tenant
        if predecessor_tenant != replacement_tenant:
            # Later replacements of a mis-scoped one were written in its tenant
            # too, so the fix must cover every id down the chain.
            chain = [target]
            following = links.get(target)
            while following is not None and following in present and following not in chain:
                chain.append(following)
                following = links.get(following)
            issues.append(
                {
                    "memory_id": target,
                    "kind": "supersede_scope_mismatch",
                    "expected_hash": None,
                    "actual_hash": None,
                    "message": (
                        f"replacement of {memory_id} has tenant {replacement_tenant!r}, "
                        f"its predecessor {predecessor_tenant!r}; not rewritten. "
                        f"Fix the whole chain: {', '.join(chain)}"
                    ),
                }
            )
        seen = [memory_id]
        current: str | None = target
        while current is not None and current not in seen:
            seen.append(current)
            current = links.get(current)
        if current == memory_id:
            issues.append(
                {
                    "memory_id": memory_id,
                    "kind": "cyclic_superseded_by",
                    "expected_hash": None,
                    "actual_hash": None,
                    "message": "supersession cycle: " + " -> ".join([*seen, memory_id]),
                }
            )
    return issues


def lookup_current(host: RepositoryHost, memory_id: str) -> MemoryDocument | None:
    """The current document for ``memory_id``, or ``None`` when there is none."""
    if not host._current_path(memory_id).exists():
        return None
    try:
        return host.get(memory_id)
    except NotFoundError:
        return None


def _open_question_issues(
    documents: builtins.list[MemoryDocument],
    *,
    successors: dict[str, str] | None = None,
) -> builtins.list[dict[str, str | None]]:
    """Lint ``type: open-question`` memories against the rest of the vault.

    * ``open_question_missing_block`` / ``open_question_wrong_type``: the type
      and the block come together or not at all;
    * ``open_question_self_conflict``: ``conflicts`` names the question;
    * ``open_question_dangling_conflict`` / ``open_question_cross_tenant_conflict``;
    * ``open_question_dangling_resolution`` (also when the answer's supersession
      chain ends at a deleted memory) / ``open_question_superseded_resolution``
      / ``open_question_cross_tenant_resolution``: ``resolved_by`` must name an
      existing, current memory in the same tenant;
    * ``open_question_resolved_but_current``: a resolved question has ``valid_to``;
    * ``open_question_multiple_unresolved``: one question (a supersession chain
      of open-question versions) has at most one unresolved version without
      ``valid_to``.
    """
    by_id = {doc.id: doc for doc in documents}
    issues: builtins.list[dict[str, str | None]] = []

    def report(memory_id: str, kind: str, message: str) -> None:
        issues.append(
            {
                "memory_id": memory_id,
                "kind": kind,
                "expected_hash": None,
                "actual_hash": None,
                "message": message,
            }
        )

    questions = [doc for doc in documents if doc.type == OPEN_QUESTION_TYPE]
    for doc in documents:
        if doc.type != OPEN_QUESTION_TYPE and (
            doc.open_question is not None or doc.stray_open_question == "block"
        ):
            report(
                doc.id,
                "open_question_wrong_type",
                "open_question requires type=open-question (the key was ignored)",
            )
        elif doc.type != OPEN_QUESTION_TYPE and "open_question" in doc.ignored_keys:
            report(
                doc.id,
                "open_question_invalid_block",
                "an unrelated open_question key was ignored (the file is unchanged)",
            )
    for doc in questions:
        block = doc.open_question
        if block is None:
            report(doc.id, "open_question_missing_block", "type=open-question has no open_question")
            continue
        for conflict in block.conflicts:
            target = by_id.get(conflict)
            if conflict == doc.id:
                report(doc.id, "open_question_self_conflict", "conflicts names the question itself")
            elif doc.valid_to is not None:
                # A historical version records what was in conflict back then;
                # later deletes and tenant moves are not its defects.
                continue
            elif target is None:
                report(
                    doc.id,
                    "open_question_dangling_conflict",
                    f"conflicts names a missing memory: {conflict}",
                )
            elif target.tenant != doc.tenant:
                report(
                    doc.id,
                    "open_question_cross_tenant_conflict",
                    f"conflicts names a memory in another tenant: {conflict}",
                )
        answer_id = block.resolved_by
        if answer_id is None:
            continue
        answer = by_id.get(answer_id)
        # The answer may itself be revised later: follow its supersession chain
        # and judge the version that is in force now.
        head = answer
        seen: set[str] = set()
        while (
            head is not None
            and head.superseded_by is not None
            and head.superseded_by in by_id
            and head.id not in seen
        ):
            seen.add(head.id)
            head = by_id[head.superseded_by]
        # A deleted successor breaks the chain. ``delete`` clears the pointer to
        # it, so the SUPERSEDE event log (``successors``) is what remembers it.
        successor = None
        if head is not None:
            successor = head.superseded_by
            if successor is None and head.valid_to is not None:
                # Only a tail that is still ended: a human may have restored it.
                successor = (successors or {}).get(head.id)
        broken_chain = successor is not None and successor not in by_id
        if answer is None or head is None or answer_id == doc.id or broken_chain:
            report(
                doc.id,
                "open_question_dangling_resolution",
                f"resolved_by names a missing memory: {answer_id}",
            )
        elif head.valid_to is not None or head.superseded_by is not None:
            report(
                doc.id,
                "open_question_superseded_resolution",
                f"resolved_by names a memory whose current version is not in force: {answer_id}",
            )
        elif head.tenant != doc.tenant:
            report(
                doc.id,
                "open_question_cross_tenant_resolution",
                f"resolved_by names a memory in another tenant: {answer_id}",
            )
        if doc.valid_to is None:
            report(
                doc.id,
                "open_question_resolved_but_current",
                "a resolved question must have valid_to",
            )
    # One question is one supersession chain of open-question versions.
    parent = {doc.id: doc.id for doc in questions}

    def root(memory_id: str) -> str:
        while parent[memory_id] != memory_id:
            parent[memory_id] = parent[parent[memory_id]]
            memory_id = parent[memory_id]
        return memory_id

    for doc in questions:
        if doc.superseded_by in parent:
            parent[root(doc.id)] = root(str(doc.superseded_by))
    open_versions: dict[str, builtins.list[str]] = {}
    for doc in questions:
        if (
            doc.open_question is not None
            and doc.open_question.resolved_by is None
            and doc.valid_to is None
        ):
            open_versions.setdefault(root(doc.id), []).append(doc.id)
    for ids in open_versions.values():
        if len(ids) > 1:
            for memory_id in sorted(ids):
                report(
                    memory_id,
                    "open_question_multiple_unresolved",
                    "one question has several unresolved current versions: "
                    + ", ".join(sorted(ids)),
                )
    return issues


def _clear_superseded_by_refs(
    host: RepositoryHost, memory_id: str, *, actor: str
) -> builtins.list[str]:
    """Unlink memories that named a hard-deleted memory as their replacement.

    ``valid_to`` stays: the record did stop being current at that instant.
    Each unlink is an ordinary update event. A pointer the cleanup may not
    touch (an anchor, when the deleting actor is not human) is left for
    ``fsck`` to report as dangling. This runs after the DELETE commits, so
    any failure is contained per reference; ``fsck`` re-runs the cleanup.
    Returns the ids whose pointer was cleared.
    """
    cleanup_actor = "human" if actor == "human" else "mechanism"
    cleared: builtins.list[str] = []
    try:
        documents = _current_documents(host, mentioning=memory_id)
    except Exception:
        _LOG.warning("superseded_by cleanup for %s failed; run fsck", memory_id, exc_info=True)
        return cleared
    for document in documents:
        if document.superseded_by != memory_id:
            continue
        try:
            host.update(document.id, actor=cleanup_actor, superseded_by=None)
        except IntegrityError:
            continue
        except Exception:
            _LOG.warning(
                "clearing superseded_by on %s after deleting %s failed; run fsck",
                document.id,
                memory_id,
                exc_info=True,
            )
            continue
        cleared.append(document.id)
    return cleared


def _current_documents(
    host: RepositoryHost, *, mentioning: str | None = None
) -> builtins.list[MemoryDocument]:
    """Parse the current Markdown files, skipping unreadable ones.

    Unlike ``list()`` this leaves the caller-visible list warnings alone.
    With ``mentioning``, files whose bytes do not contain both that memory id
    and ``superseded`` are skipped without a YAML parse. Memory ids match
    ``_ID_RE`` (no characters YAML would escape), so a file whose
    ``superseded_by`` (or ``superseded`` alias) names the id always contains
    it literally; the prefilter only drops files that cannot match.
    """
    documents: builtins.list[MemoryDocument] = []
    if not host.memories_dir.exists():
        return documents
    needle = mentioning.encode("utf-8") if mentioning is not None else None
    for path in sorted(host.memories_dir.glob("*.md")):
        try:
            raw = path.read_bytes()
            if needle is not None and (needle not in raw or b"superseded" not in raw):
                continue
            documents.append(host.parse(raw.decode("utf-8"), expected_id=path.stem))
        except (IntegrityError, UnicodeDecodeError):
            continue
    return documents
