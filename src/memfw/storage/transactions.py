"""Cross-process locking and recoverable write transactions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

from ..core.context import RepositoryHost
from ..core.errors import IntegrityError
from ..core.models import EventKind, Tier

P = ParamSpec("P")
R = TypeVar("R")
S = TypeVar("S", bound=RepositoryHost)


def _serialized(
    method: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], R]:
    @wraps(method)
    def wrapper(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        operation = getattr(method, "__name__", "")
        with self._locked():
            depth = getattr(self._mutation_local, "depth", 0)
            if depth:
                return method(self, *args, **kwargs)
            if operation == "recover_pending":
                return method(self, *args, **kwargs)
            if self.pending_path.exists() and not self._recovering:
                raise IntegrityError("A pending transaction needs explicit `memfw recover`")
            if (
                (self.root / ".event-append.pending.json").exists()
                and operation not in {"recover_pending", "fsck"}
                and not self._recovering
            ):
                raise IntegrityError("A pending event append needs explicit `memfw recover`")
            if operation == "capture_event":
                # Metadata-only event appends have their own append-intent WAL.
                # Keep Markdown mutations on the full transaction/fingerprint path.
                self._mutation_local.depth = 1
                self._mutation_local.fast_event_append = True
                try:
                    return method(self, *args, **kwargs)
                finally:
                    self._mutation_local.depth = 0
                    if hasattr(self._mutation_local, "fast_event_append"):
                        del self._mutation_local.fast_event_append
            baseline = self._tree_fingerprint()
            before_events = self.events_path.read_bytes() if self.events_path.exists() else b""
            targets = self._transaction_targets(operation, args, kwargs)
            before_ids = (
                sorted(p.stem for p in self.memories_dir.glob("*.md"))
                if self.memories_dir.exists()
                else []
            )
            manifest: dict[str, Any] = {
                "operation": operation,
                "targets": targets,
                "before_ids": before_ids,
                "before_log_hash": hashlib.sha256(before_events).hexdigest(),
                "archive_id": str(args[0]) if operation == "cold_archive" and args else None,
            }
            if operation == "configure":
                before_config = self.config_path.read_bytes() if self.config_path.exists() else b""
                manifest["before_config_b64"] = base64.b64encode(before_config).decode("ascii")
                manifest["before_config_hash"] = hashlib.sha256(before_config).hexdigest()
            self._atomic_write(
                self.pending_path,
                json.dumps(manifest, sort_keys=True).encode("utf-8"),
            )
            self._mutation_local.depth = 1
            try:
                self._mutation_local.event_cache = self.events()
                event_cache_start = len(self._mutation_local.event_cache)
                self._mutation_local.event_memory_ids = {
                    event.memory_id for event in self._mutation_local.event_cache
                }
                self._mutation_local.defer_events = operation == "add_many"
                result = method(self, *args, **kwargs)
                if self._mutation_local.defer_events:
                    appended = self._mutation_local.event_cache[event_cache_start:]
                    if appended:
                        existing = (
                            self.events_path.read_bytes() if self.events_path.exists() else b""
                        )
                        lines = b"".join(
                            (event.model_dump_json() + "\n").encode() for event in appended
                        )
                        self._atomic_write(self.events_path, existing + lines)
                        self._seed_event_cursor(
                            len(getattr(self._mutation_local, "event_cache", [])),
                            appended[-1].event_hash,
                        )
            except BaseException:
                if self._tree_fingerprint() == baseline:
                    self.pending_path.unlink(missing_ok=True)
                raise
            else:
                self.pending_path.unlink(missing_ok=True)
                return result
            finally:
                self._mutation_local.depth = 0
                if hasattr(self._mutation_local, "event_cache"):
                    del self._mutation_local.event_cache
                if hasattr(self._mutation_local, "event_memory_ids"):
                    del self._mutation_local.event_memory_ids
                if hasattr(self._mutation_local, "defer_events"):
                    del self._mutation_local.defer_events

    return cast(Callable[Concatenate[S, P], R], wrapper)


class TransactionMixin:
    @contextmanager
    def _locked(self: RepositoryHost) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self._file_lock:
            yield

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _tree_fingerprint(self: RepositoryHost) -> str:
        digest = hashlib.sha256()
        if not self.root.exists():
            return digest.hexdigest()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name in {".memfw.lock", ".pending.json"}:
                continue
            digest.update(path.relative_to(self.root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def _transaction_targets(
        self: RepositoryHost, operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> list[str]:
        memory_id = kwargs.get("memory_id")
        if (
            memory_id is None
            and args
            and operation in {"update", "delete", "restore_cold", "autowarm"}
        ):
            memory_id = args[0]
        if operation == "rollback" and args:
            target_event = next((e for e in self.events() if e.event_id == args[0]), None)
            memory_id = target_event.memory_id if target_event else None
        if operation in {"create_knowledge_card", "repoint_knowledge_sources"} and args:
            memory_id = str(args[0])
        if operation == "decide_proposal" and kwargs.get("payload"):
            memory_id = kwargs["payload"].get("memory_id")
        if operation == "create_knowledge_card":
            memory_ids = [str(args[0])] if args else []
        elif operation == "validate_knowledge_cards":
            cards_dir = self.root / "knowledge" / "cards"
            memory_ids = (
                sorted(path.stem for path in cards_dir.glob("*.md")) if cards_dir.exists() else []
            )
        elif operation == "repoint_knowledge_sources":
            old_source_id = str(args[0]) if args else ""
            cards_dir = self.root / "knowledge" / "cards"
            memory_ids = []
            if cards_dir.exists():
                for path in cards_dir.glob("*.md"):
                    if old_source_id in path.read_text(encoding="utf-8"):
                        memory_ids.append(path.stem)
        elif operation == "add_many":
            documents = args[0] if args else []
            memory_ids = [
                str(document["memory_id"]) for document in documents if document.get("memory_id")
            ]
        elif operation == "cold_archive":
            requested_ids = kwargs.get("memory_ids")
            memory_ids = [
                doc.id
                for doc in self.list()
                if doc.tier == Tier.COLD
                and not doc.is_stub
                and (requested_ids is None or doc.id in requested_ids)
            ]
        elif operation == "fsck":
            memory_ids = (
                sorted({p.stem for p in self.memories_dir.glob("*.md")})
                if self.memories_dir.exists()
                else []
            )
        else:
            memory_ids = [str(memory_id)] if memory_id else []
        return sorted(set(memory_ids))

    @_serialized
    def recover_pending(self: RepositoryHost) -> list[str]:
        """Explicitly reconcile current files to the last committed event projection."""
        cleaned: list[str] = []
        if self.recover_event_append():
            cleaned.append("events.jsonl")
        if not self.pending_path.exists():
            stage_path = self.root / ".staging" / "capture-candidates.jsonl"
            stage_intent = self.root / ".staging" / "capture-candidates.pending.json"
            if stage_intent.exists():
                from ..capture import _recover_stage_intent

                if _recover_stage_intent(self, stage_path):
                    cleaned.append(".staging/capture-candidates.jsonl")
            if self.recover_log_compaction():
                cleaned.append("events.jsonl")
            candidates = [
                self.root / ".pending.json.tmp",
                self.events_path.with_name("events.jsonl.tmp"),
                self.log_catalog_path.with_name("catalog.json.tmp"),
                self.root / "events.head.tmp",
            ]
            if self.memories_dir.exists():
                candidates.extend(self.memories_dir.glob("*.md.tmp"))
            if self.history_dir.exists():
                candidates.extend(self.history_dir.rglob("*.tmp"))
            if self.archives_dir.exists():
                candidates.extend(self.archives_dir.glob("*.tar.zst.tmp"))
            if self.log_segments_dir.exists():
                candidates.extend(self.log_segments_dir.glob("*.tar.zst.tmp"))
            if self.proposals_dir.exists():
                candidates.extend(self.proposals_dir.glob("*.md.tmp"))
            for candidate in candidates:
                if candidate.exists():
                    candidate.unlink()
                    cleaned.append(candidate.relative_to(self.root).as_posix())
            # Only remove orphaned files after the referenced archive chain and
            # active log both verify. The uncommitted files duplicate the still
            # authoritative active log because compaction commits its catalog
            # before truncating that log.
            self.events()
            _, records = self._log_segments()
            referenced = {str(record["name"]) for record in records}
            if self.log_segments_dir.exists():
                for pattern in (
                    "segment-*.tar.zst",
                    "summary-segment-*.tar.zst",
                    "summary-container-*.tar.zst",
                ):
                    for segment in self.log_segments_dir.glob(pattern):
                        if segment.name not in referenced:
                            segment.unlink()
                            cleaned.append(segment.relative_to(self.root).as_posix())
            self._reconcile_proposal_objects(self.events())
            return cleaned
        try:
            manifest = json.loads(self.pending_path.read_text(encoding="utf-8"))
            targets = [self._check_id(item) for item in manifest.get("targets", [])]
            if manifest.get("operation") in {"add", "add_many"} and self.memories_dir.exists():
                prior = set(manifest.get("before_ids", []))
                targets.extend(
                    self._check_id(p.stem)
                    for p in self.memories_dir.glob("*.md")
                    if p.stem not in prior
                )
            targets = sorted(set(targets))
        except (OSError, ValueError, TypeError) as exc:
            raise IntegrityError("Pending transaction manifest is invalid") from exc
        self._recovering = True
        try:
            state = self.replay()
            event_list = self.events()
            self._reconcile_proposal_objects(event_list)
            if event_list:
                self._seed_event_cursor(len(event_list), event_list[-1].event_hash)
            changed: list[str] = []
            for memory_id in targets:
                if manifest.get("operation") in {
                    "create_knowledge_card",
                    "validate_knowledge_cards",
                }:
                    card_path = self.root / "knowledge" / "cards" / f"{memory_id}.md"
                    knowledge_events = [
                        event
                        for event in event_list
                        if event.memory_id == memory_id
                        and event.kind
                        in {
                            EventKind.KNOWLEDGE_CARD_CREATE,
                            EventKind.KNOWLEDGE_SOURCE_MISSING,
                            EventKind.KNOWLEDGE_SOURCE_REPOINT,
                        }
                    ]
                    if not knowledge_events:
                        if card_path.exists():
                            card_path.unlink()
                            changed.append(memory_id)
                        continue
                    expected_card_hash = knowledge_events[-1].after_hash
                    if (
                        not card_path.exists()
                        or hashlib.sha256(card_path.read_bytes()).hexdigest() != expected_card_hash
                    ):
                        raise IntegrityError(
                            "Cannot recover knowledge card "
                            f"{memory_id}: committed card bytes are missing or differ"
                        )
                    continue
                path = self._current_path(memory_id)
                digest = state.get(memory_id)
                if digest is None:
                    if manifest.get("operation") == "delete" and path.exists():
                        self._preserve_shared_body_before_erasure(memory_id)
                    if path.exists():
                        path.unlink()
                        changed.append(memory_id)
                    if manifest.get("operation") == "add":
                        shutil.rmtree(self.history_dir / memory_id, ignore_errors=True)
                    if manifest.get("operation") == "delete":
                        shutil.rmtree(self.history_dir / memory_id, ignore_errors=True)
                        for quarantined in (self.root / ".quarantine").glob(f"{memory_id}-*.md"):
                            quarantined.unlink(missing_ok=True)
                        from ..storage import remove_memory_from_archives

                        remove_memory_from_archives(self, memory_id)
                        self.index_path.unlink(missing_ok=True)
                        Path(f"{self.index_path}-wal").unlink(missing_ok=True)
                        Path(f"{self.index_path}-shm").unlink(missing_ok=True)
                    continue
                if self._has_version(memory_id, digest):
                    raw = self._read_version(memory_id, digest)
                elif path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    raw = path.read_bytes()
                else:
                    raise IntegrityError(
                        f"Cannot recover {memory_id}: committed Markdown is missing"
                    )
                if not path.exists() or path.read_bytes() != raw:
                    self._atomic_write(path, raw)
                    changed.append(memory_id)
            if manifest.get("operation") == "cold_archive":
                archive_id = manifest.get("archive_id")
                if archive_id and not any(doc.archive_ref == archive_id for doc in self.list()):
                    (self.archives_dir / f"{archive_id}.tar.zst").unlink(missing_ok=True)
            if manifest.get("operation") in {"restore_cold", "autowarm"}:
                for memory_id in targets:
                    terminal_hash = state.get(memory_id)
                    if terminal_hash is None:
                        continue
                    if not self._has_version(memory_id, terminal_hash):
                        continue
                    try:
                        terminal = self.parse(
                            self._read_version(memory_id, terminal_hash).decode("utf-8"),
                            expected_id=memory_id,
                        )
                    except (UnicodeDecodeError, ValueError, IntegrityError):
                        continue
                    if terminal.is_stub:
                        continue
                    memory_events = [event for event in event_list if event.memory_id == memory_id]
                    stub_positions = [
                        index
                        for index, event in enumerate(memory_events)
                        if event.kind == EventKind.UPDATE
                        and event.details.get("storage") == "cold_stub"
                    ]
                    last_stub_position = stub_positions[-1] if stub_positions else -1
                    restored_after_stub = any(
                        event.kind == EventKind.MEMORY_RESTORE
                        for event in memory_events[last_stub_position + 1 :]
                    )
                    if restored_after_stub:
                        from ..storage import remove_memory_from_archives

                        remove_memory_from_archives(self, memory_id)
            if manifest.get("operation") == "configure":
                config_hash = (
                    hashlib.sha256(self.config_path.read_bytes()).hexdigest()
                    if self.config_path.exists()
                    else hashlib.sha256(b"").hexdigest()
                )
                committed_hashes = {
                    event.details.get("after_config_hash")
                    for event in event_list
                    if event.kind == EventKind.CONFIG_CHANGE
                }
                if config_hash not in committed_hashes:
                    before_config = base64.b64decode(
                        manifest.get("before_config_b64", ""), validate=True
                    )
                    if before_config:
                        self._atomic_write(self.config_path, before_config)
                    else:
                        self.config_path.unlink(missing_ok=True)
            temporary_candidates = [
                self.pending_path.with_name(self.pending_path.name + ".tmp"),
                self.events_path.with_name("events.jsonl.tmp"),
                self.root / "events.head.tmp",
            ]
            if self.memories_dir.exists():
                temporary_candidates.extend(self.memories_dir.glob("*.md.tmp"))
            if self.history_dir.exists():
                temporary_candidates.extend(self.history_dir.rglob("*.md.tmp"))
            if self.archives_dir.exists():
                temporary_candidates.extend(self.archives_dir.glob("*.tar.zst.tmp"))
            for candidate in temporary_candidates:
                candidate.unlink(missing_ok=True)
            self.pending_path.unlink(missing_ok=True)
            if self.recover_log_compaction():
                changed.append("events.jsonl")
            if manifest.get("operation") in {"delete", "fsck"}:
                self.validate_knowledge_cards()
            return changed
        finally:
            self._recovering = False

    def _reconcile_proposal_objects(self: RepositoryHost, events: list[Any]) -> None:
        """Remove uncommitted/erased proposal objects after an interrupted write."""
        if not self.proposals_dir.exists():
            return
        committed: dict[str, str] = {}
        erased: set[str] = set()
        for event in events:
            proposal_id = event.details.get("proposal_id")
            if not isinstance(proposal_id, str):
                continue
            if event.kind == EventKind.AGENT_PROPOSAL:
                source_hash = event.details.get("source_hash")
                if isinstance(source_hash, str):
                    committed[proposal_id] = source_hash
            elif event.kind == EventKind.PROPOSAL_ERASE:
                erased.add(proposal_id)
        for path in sorted(self.proposals_dir.glob("*.md")):
            proposal_id = path.stem
            if proposal_id in erased:
                path.unlink(missing_ok=True)
                continue
            expected = committed.get(proposal_id)
            if expected is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                path.unlink(missing_ok=True)
