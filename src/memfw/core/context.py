"""Structural host contract for repository mixins."""

from __future__ import annotations

import builtins
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock

from .models import Event, EventKind, Layer, MemoryDocument, Tier


class RepositoryHost(Protocol):
    root: Path
    memories_dir: Path
    history_dir: Path
    events_path: Path
    event_state_path: Path
    config_path: Path
    index_path: Path
    archives_dir: Path
    log_dir: Path
    log_segments_dir: Path
    log_catalog_path: Path
    proposals_dir: Path
    pending_path: Path
    id_factory: Callable[[], str]
    _thread_lock: threading.RLock
    _file_lock: FileLock
    _mutation_local: threading.local
    _event_cursor_cache: tuple[tuple[int, int, int, int, int], dict[str, Any]] | None
    _recovering: bool
    _list_warnings: list[str]

    def _check_id(self, memory_id: str) -> str: ...

    def _locked(self) -> AbstractContextManager[None]: ...

    def _tree_fingerprint(self) -> str: ...

    def _transaction_targets(
        self, operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> builtins.list[str]: ...

    def _now(self) -> datetime: ...

    def _payload_hash(self, memory_id: str, operation: str, changes: dict[str, Any]) -> str: ...

    def _approved_proposal(
        self, *, actor: str, proposal_id: str, target_hash: str, before_hash: str | None
    ) -> bool: ...

    def _human_approved_source_delete(self, memory_id: str, proposal_id: str | None) -> bool: ...

    def _ensure_cooling_preserves_proposals(self, memory_id: str) -> None: ...

    def _simulated_proposal_approved(self, memory_id: str, proposal_id: str | None) -> bool: ...

    def _can_mutate(
        self,
        actor: str,
        *,
        layer: Layer | None = None,
        anchor: str | None = None,
        signature: str | None = None,
        agent_id: str | None = None,
        approved: bool = False,
    ) -> bool: ...

    def assert_no_pending(self) -> None: ...

    def init(self) -> None: ...

    def _current_path(self, memory_id: str) -> Path: ...

    def _history_path(self, memory_id: str, content_hash: str) -> Path: ...

    def _history_metadata_path(self, memory_id: str, content_hash: str) -> Path: ...

    def _has_version(self, memory_id: str, content_hash: str) -> bool: ...

    def _read_version(self, memory_id: str, content_hash: str) -> bytes: ...

    def _body_source_hash(self, memory_id: str, content_hash: str) -> str: ...

    def _preserve_shared_body_before_erasure(self, memory_id: str) -> None: ...

    def render(self, document: MemoryDocument) -> str: ...

    def parse(self, markdown: str, *, expected_id: str | None = None) -> MemoryDocument: ...

    def _write_version(
        self, memory_id: str, markdown: str, *, base_hash: str | None = None
    ) -> str: ...

    def _prune_history(self, memory_id: str) -> None: ...

    def _prune_archive_history(self, memory_id: str) -> None: ...

    def _reconcile_proposal_objects(self, events: builtins.list[Any]) -> None: ...

    def read_config(self) -> dict[str, Any]: ...

    def _erase_target_proposals(
        self, memory_id: str, *, actor: str, preserve_ids: set[str] | None = None
    ) -> None: ...

    def _atomic_write(self, path: Path, data: bytes) -> None: ...

    def _append_event(
        self,
        kind: EventKind,
        memory_id: str,
        *,
        actor: str,
        before_hash: str | None = None,
        after_hash: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> Event: ...

    def capture_event(
        self,
        kind: EventKind,
        *,
        actor: str,
        details: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> Event: ...

    def events(self) -> builtins.list[Event]: ...

    def compact_log(self, keep_recent: int | None = None) -> dict[str, int | str]: ...

    def _log_segments(self) -> tuple[builtins.list[Event], builtins.list[dict[str, Any]]]: ...

    def _live_events(self, archived: builtins.list[Event]) -> builtins.list[Event]: ...

    def recover_log_compaction(self) -> bool: ...

    def recover_event_append(self) -> bool: ...

    def _validated_event_cursor(self) -> dict[str, Any]: ...

    def _seed_event_cursor(self, event_count: int, last_hash: str) -> dict[str, Any]: ...

    def _append_event_with_intent(
        self, event: Event, line: bytes, previous: str, event_count: int
    ) -> None: ...

    def _read_head_hash(self) -> str: ...

    def replay(self) -> dict[str, str | None]: ...

    def list(self) -> builtins.list[MemoryDocument]: ...

    def add(
        self,
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
        tags: builtins.list[str] | None = None,
        actor: str = "human",
        agent_id: str | None = None,
        signature: str | None = None,
    ) -> MemoryDocument: ...

    def add_many(
        self, documents: builtins.list[dict[str, Any]]
    ) -> builtins.list[MemoryDocument]: ...

    def get(self, memory_id: str) -> MemoryDocument: ...

    def update(self, memory_id: str, **changes: Any) -> MemoryDocument: ...

    def delete(
        self,
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
    ) -> None: ...

    def create_knowledge_card(
        self,
        card_id: str,
        kind: str,
        statement: str,
        source_refs: tuple[str, ...] | builtins.list[str],
    ) -> Any: ...

    def validate_knowledge_cards(self) -> builtins.list[Any]: ...

    def knowledge_source_counts(self) -> dict[str, int]: ...

    def repoint_knowledge_sources(
        self, old_source_id: str, new_source_id: str, *, actor: str = "mechanism"
    ) -> int: ...
