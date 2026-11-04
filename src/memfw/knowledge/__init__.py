"""Persisted evidence-linked knowledge cards stored as YAML-frontmatter Markdown."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import EventKind
from memfw.storage.transactions import _serialized

if TYPE_CHECKING:
    from memfw.core.context import RepositoryHost


class KnowledgeKind(StrEnum):
    INSIGHT = "insight"
    PATTERN = "pattern"
    METHODOLOGY = "methodology"


@dataclass(frozen=True)
class KnowledgeCard:
    kind: KnowledgeKind
    statement: str
    source_refs: tuple[str, ...]
    card_id: str | None = None
    missing_source_refs: tuple[str, ...] = ()
    source_repoint_history: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.source_refs or any(not ref.strip() for ref in self.source_refs):
            raise ValueError("Every knowledge card must cite at least one source")
        if not self.statement.strip():
            raise ValueError("Knowledge-card statement must not be empty")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Knowledge-card source references must be unique")
        if not set(self.missing_source_refs).issubset(self.source_refs):
            raise ValueError("Missing source refs must be included in source_refs")
        if any(not old.strip() or not new.strip() for old, new in self.source_repoint_history):
            raise ValueError("Source repoint history requires non-empty source IDs")

    @property
    def status(self) -> str:
        return "missing_sources" if self.missing_source_refs else "complete"


@dataclass(frozen=True)
class SourceValidation:
    valid_refs: tuple[str, ...]
    missing_refs: tuple[str, ...]


def validate_source_refs(card: KnowledgeCard, available_source_ids: set[str]) -> SourceValidation:
    """Separate forgotten/unavailable refs so the persisted card can mark them."""
    valid = tuple(ref for ref in card.source_refs if ref in available_source_ids)
    missing = tuple(ref for ref in card.source_refs if ref not in available_source_ids)
    return SourceValidation(valid, missing)


class KnowledgeStoreMixin:
    """Repository mixin for atomic card writes and source-integrity events."""

    @staticmethod
    def _knowledge_card_path(self: RepositoryHost, card_id: str) -> Path:
        return self.root / "knowledge" / "cards" / f"{self._check_id(card_id)}.md"

    @staticmethod
    def _render_knowledge_card(card: KnowledgeCard) -> bytes:
        if card.card_id is None:
            raise ValueError("Persisted knowledge cards require a card_id")
        metadata = {
            "id": card.card_id,
            "kind": card.kind.value,
            "source_refs": list(card.source_refs),
            "missing_source_refs": list(card.missing_source_refs),
            "source_repoint_history": [
                {"from": old, "to": new} for old, new in card.source_repoint_history
            ],
            "status": card.status,
        }
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
        return f"---\n{frontmatter}\n---\n\n{card.statement.strip()}\n".encode()

    @staticmethod
    def _parse_knowledge_card(markdown: str, expected_id: str) -> KnowledgeCard:
        if not markdown.startswith("---\n"):
            raise IntegrityError("Knowledge card is missing YAML frontmatter")
        try:
            _, raw, statement = markdown.split("---\n", 2)
            metadata = yaml.safe_load(raw)
            if not isinstance(metadata, dict):
                raise ValueError("frontmatter must be a mapping")
            if metadata.get("id") != expected_id:
                raise ValueError("frontmatter id does not match filename")
            refs = metadata["source_refs"]
            missing = metadata.get("missing_source_refs", [])
            repoints = metadata.get("source_repoint_history", [])
            if not isinstance(refs, list) or not isinstance(missing, list):
                raise ValueError("source refs must be lists")
            if not isinstance(repoints, list) or any(
                not isinstance(item, dict) for item in repoints
            ):
                raise ValueError("source_repoint_history must be a list of mappings")
            card = KnowledgeCard(
                kind=KnowledgeKind(metadata["kind"]),
                statement=statement.strip(),
                source_refs=tuple(str(ref) for ref in refs),
                card_id=expected_id,
                missing_source_refs=tuple(str(ref) for ref in missing),
                source_repoint_history=tuple(
                    (str(item["from"]), str(item["to"])) for item in repoints
                ),
            )
            if metadata.get("status") != card.status:
                raise ValueError("status does not match missing_source_refs")
            return card
        except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise IntegrityError(f"Invalid knowledge card {expected_id}: {exc}") from exc

    @_serialized
    def create_knowledge_card(
        self: RepositoryHost,
        card_id: str,
        kind: KnowledgeKind | str,
        statement: str,
        source_refs: tuple[str, ...] | list[str],
    ) -> KnowledgeCard:
        """Create a card only when every cited memory currently exists."""
        self.init()
        identifier = self._check_id(card_id)
        path = KnowledgeStoreMixin._knowledge_card_path(self, identifier)
        if path.exists() or any(
            event.memory_id == identifier and event.kind == EventKind.KNOWLEDGE_CARD_CREATE
            for event in self.events()
        ):
            raise IntegrityError(f"Knowledge card id already used: {identifier}")
        try:
            card_kind = KnowledgeKind(kind)
        except ValueError as exc:
            raise ValueError(f"Unknown knowledge-card kind: {kind}") from exc
        card = KnowledgeCard(card_kind, statement, tuple(source_refs), card_id=identifier)
        for source_id in card.source_refs:
            try:
                self.get(source_id)
            except NotFoundError as exc:
                raise ValueError(f"Knowledge source does not exist: {source_id}") from exc
        raw = KnowledgeStoreMixin._render_knowledge_card(card)
        digest = hashlib.sha256(raw).hexdigest()
        self._atomic_write(path, raw)
        self._append_event(
            EventKind.KNOWLEDGE_CARD_CREATE,
            identifier,
            actor="human",
            after_hash=digest,
            details={"kind": card.kind.value, "source_refs": list(card.source_refs)},
        )
        return card

    def knowledge_source_counts(self: RepositoryHost) -> dict[str, int]:
        """Count valid, nonmissing memory references from persisted cards."""
        directory = self.root / "knowledge" / "cards"
        counts: dict[str, int] = {}
        if not directory.exists():
            return counts
        for path in sorted(directory.glob("*.md")):
            card = KnowledgeStoreMixin.get_knowledge_card(self, path.stem)
            missing = set(card.missing_source_refs)
            for source_id in card.source_refs:
                if source_id in missing:
                    continue
                try:
                    self.get(source_id)
                except NotFoundError:
                    continue
                counts[source_id] = counts.get(source_id, 0) + 1
        return counts

    @_serialized
    def repoint_knowledge_sources(
        self: RepositoryHost,
        old_source_id: str,
        new_source_id: str,
        *,
        actor: str = "mechanism",
    ) -> int:
        """Atomically relink cited sources before a merge removes the old ID.

        Existing refs are replaced in place, duplicates are removed while
        preserving order, and an audit trail remains in each card's frontmatter.
        """
        old_id = self._check_id(old_source_id)
        new_id = self._check_id(new_source_id)
        self.get(old_id)
        self.get(new_id)
        if old_id == new_id:
            return 0
        if not self._can_mutate(actor):
            raise IntegrityError("Actor is not authorized to repoint knowledge sources")
        directory = self.root / "knowledge" / "cards"
        if not directory.exists():
            return 0
        updated_count = 0
        for path in sorted(directory.glob("*.md")):
            card = KnowledgeStoreMixin.get_knowledge_card(self, path.stem)
            if old_id not in card.source_refs:
                continue
            new_refs: list[str] = []
            for source_id in card.source_refs:
                replacement = new_id if source_id == old_id else source_id
                if replacement not in new_refs:
                    new_refs.append(replacement)
            missing = set(card.missing_source_refs)
            missing.discard(old_id)
            missing.discard(new_id)
            updated = KnowledgeCard(
                kind=card.kind,
                statement=card.statement,
                source_refs=tuple(new_refs),
                card_id=card.card_id,
                missing_source_refs=tuple(ref for ref in new_refs if ref in missing),
                source_repoint_history=(
                    *card.source_repoint_history,
                    (old_id, new_id),
                ),
            )
            before_raw = path.read_bytes()
            after_raw = KnowledgeStoreMixin._render_knowledge_card(updated)
            before_hash = hashlib.sha256(before_raw).hexdigest()
            after_hash = hashlib.sha256(after_raw).hexdigest()
            self._atomic_write(path, after_raw)
            self._append_event(
                EventKind.KNOWLEDGE_SOURCE_REPOINT,
                str(updated.card_id),
                actor=actor,
                before_hash=before_hash,
                after_hash=after_hash,
                details={
                    "from_source_id": old_id,
                    "to_source_id": new_id,
                    "source_refs": list(updated.source_refs),
                },
            )
            updated_count += 1
        return updated_count

    def get_knowledge_card(self: RepositoryHost, card_id: str) -> KnowledgeCard:
        identifier = self._check_id(card_id)
        path = KnowledgeStoreMixin._knowledge_card_path(self, identifier)
        try:
            raw = path.read_bytes()
            expected_hash = next(
                (
                    event.after_hash
                    for event in reversed(self.events())
                    if event.memory_id == identifier
                    and event.kind
                    in {
                        EventKind.KNOWLEDGE_CARD_CREATE,
                        EventKind.KNOWLEDGE_SOURCE_MISSING,
                        EventKind.KNOWLEDGE_SOURCE_REPOINT,
                    }
                ),
                None,
            )
            if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
                raise IntegrityError(f"Knowledge card hash mismatch: {identifier}")
            return KnowledgeStoreMixin._parse_knowledge_card(raw.decode("utf-8"), identifier)
        except FileNotFoundError as exc:
            raise NotFoundError(f"Knowledge card not found: {identifier}") from exc
        except UnicodeDecodeError as exc:
            raise IntegrityError(f"Knowledge card is not UTF-8: {identifier}") from exc

    @_serialized
    def validate_knowledge_cards(self: RepositoryHost) -> list[KnowledgeCard]:
        """Mark cards incomplete when a source was deleted or is no longer readable."""
        directory = self.root / "knowledge" / "cards"
        cards: list[KnowledgeCard] = []
        if not directory.exists():
            return cards
        for path in sorted(directory.glob("*.md")):
            card = KnowledgeStoreMixin.get_knowledge_card(self, path.stem)
            valid: list[str] = []
            missing = set(card.missing_source_refs)
            for source_id in card.source_refs:
                if source_id in missing:
                    continue
                try:
                    self.get(source_id)
                    valid.append(source_id)
                except NotFoundError:
                    missing.add(source_id)
            updated = KnowledgeCard(
                card.kind,
                card.statement,
                card.source_refs,
                card.card_id,
                tuple(ref for ref in card.source_refs if ref in missing),
                card.source_repoint_history,
            )
            cards.append(updated)
            if updated.missing_source_refs != card.missing_source_refs:
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                raw = KnowledgeStoreMixin._render_knowledge_card(updated)
                after = hashlib.sha256(raw).hexdigest()
                self._atomic_write(path, raw)
                self._append_event(
                    EventKind.KNOWLEDGE_SOURCE_MISSING,
                    str(updated.card_id),
                    actor="mechanism",
                    before_hash=before,
                    after_hash=after,
                    details={
                        "missing_source_refs": list(updated.missing_source_refs),
                    },
                )
        return cards


__all__ = [
    "KnowledgeCard",
    "KnowledgeKind",
    "KnowledgeStoreMixin",
    "SourceValidation",
    "validate_source_refs",
]
