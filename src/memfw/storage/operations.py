"""Memory lifecycle, archive, rollback, and fsck operations."""

from __future__ import annotations

import builtins
import hashlib
import shutil
from pathlib import Path
from typing import Any

from ..core.common import sha256_text
from ..core.context import RepositoryHost
from ..core.errors import DeletedContentUnavailable, IntegrityError, NotFoundError
from ..core.models import EventKind, Layer, MemoryDocument, Tier
from ..storage.transactions import _serialized


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
        source: str = "manual",
        provenance: str = "human-authored",
        anchor: str | None = None,
        tags: list[str] | None = None,
        actor: str = "human",
        agent_id: str | None = None,
        signature: str | None = None,
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
        self.init()
        identifier = self._check_id(memory_id or self.id_factory())
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
            source=source,
            provenance=provenance,
            anchor=anchor,
            tags=tags or [],
            created_at=now,
            updated_at=now,
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
                documents.append(
                    self.parse(path.read_bytes().decode("utf-8"), expected_id=path.stem)
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
        values = old.model_dump()
        values.update(changes)
        config = self.read_config()
        is_anchored = values.get("layer") == Layer.ANCHOR or bool(values.get("anchor"))
        if config.get("anchor_always_hot", True) and old.is_stub and is_anchored and anchor_change:
            raise IntegrityError("Restore a cold stub before anchoring it")
        if config.get("anchor_always_hot", True) and is_anchored:
            values["tier"] = Tier.HOT
        values["version"] = old.version + 1
        values["updated_at"] = self._now()
        new_doc = MemoryDocument.model_validate(values)
        if new_doc.is_stub:
            from ..curation.engine import _with_archived_body

            _with_archived_body(self, new_doc)
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
        for quarantined in (self.root / ".quarantine").glob(f"{memory_id}-*.md"):
            quarantined.unlink(missing_ok=True)
        if self.archives_dir.exists():
            from ..storage import remove_memory_from_archives

            remove_memory_from_archives(self, memory_id)
        self._erase_target_proposals(
            memory_id, actor=actor, preserve_ids=_preserve_proposal_ids or set()
        )
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
            from ..curation.engine import _with_archived_body

            try:
                # A historical stub is usable only while its referenced original
                # member is still present and matches archived_hash. Archive files
                # may remain for other notes after this note was restored.
                _with_archived_body(self, old_doc)
            except (IntegrityError, OSError) as exc:
                raise DeletedContentUnavailable(
                    "Prior cold Markdown stub has no verified archived body"
                ) from exc
        return self.update(
            event.memory_id,
            actor=actor,
            **old_doc.model_dump(
                include={
                    "title",
                    "body",
                    "layer",
                    "tier",
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
                }
            ),
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
        if registered_deletions:
            self.validate_knowledge_cards()
        return issues
