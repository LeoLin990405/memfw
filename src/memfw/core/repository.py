"""Markdown-backed vault and metadata-only append-only event stream."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

import yaml
from filelock import FileLock

from ..budget import BudgetProfile
from ..knowledge import KnowledgeStoreMixin
from ..log.store import EventLogMixin
from ..permissions import PermissionMode, PermissionPolicy
from ..storage.markdown import MarkdownStorageMixin
from ..storage.operations import MemoryOperationsMixin
from ..storage.transactions import TransactionMixin, _serialized
from .common import sha256_text as sha256_text
from .errors import IntegrityError, NotFoundError
from .models import CaptureState, Event, EventKind, Layer, MemoryDocument, Tier, utc_now

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

if TYPE_CHECKING:
    from ..agents.models import Proposal, ProposalStatus


class Repository(
    MarkdownStorageMixin,
    TransactionMixin,
    EventLogMixin,
    MemoryOperationsMixin,
    KnowledgeStoreMixin,
):
    """A local vault. Markdown and events are authoritative; SQLite is disposable."""

    def __init__(
        self,
        root: Path | str,
        *,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.clock = clock
        self.id_factory = id_factory
        self.memories_dir = self.root / "memories"
        self.history_dir = self.root / ".history"
        self.events_path = self.root / "events.jsonl"
        self.event_state_path = self.root / "events.state.json"
        self.config_path = self.root / "vault.yaml"
        self.index_path = self.root / "index.sqlite3"
        self.archives_dir = self.root / ".archives"
        self.log_dir = self.root / ".log"
        self.log_segments_dir = self.log_dir / "segments"
        self.log_catalog_path = self.log_dir / "catalog.json"
        self.proposals_dir = self.root / ".proposals"
        self._thread_lock = RLock()
        self._file_lock = FileLock(str(self.root / ".memfw.lock"))
        self._mutation_local = threading.local()
        self._event_cursor_cache: tuple[tuple[int, int, int, int, int], dict[str, Any]] | None = (
            None
        )
        self.pending_path = self.root / ".pending.json"
        self._recovering = False

    @_serialized
    def init(self, /) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.memories_dir.mkdir(exist_ok=True)
        self.history_dir.mkdir(exist_ok=True)
        self.archives_dir.mkdir(exist_ok=True)
        self.proposals_dir.mkdir(exist_ok=True)
        self.log_segments_dir.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            self.events_path.write_bytes(b"")
        if not self.event_state_path.exists() and not self.events_path.stat().st_size:
            # One durable sidecar owns both cursor and committed head. Legacy files
            # are imported by _validated_event_cursor when opening an old vault.
            legacy_head = self.root / "events.head"
            if legacy_head.exists() and legacy_head.read_text(encoding="ascii").strip() != "0" * 64:
                raise IntegrityError("Legacy event head does not match the empty log")
            self._seed_event_cursor(0, "0" * 64)
        if not self.config_path.exists():
            self.config_path.write_text(
                "schema_version: 1\n"
                "content_format: markdown-frontmatter\n"
                "default_layer: current\n"
                "default_tier: hot\n"
                "permissions: propose_then_confirm\n"
                "history_keep: 2\n"
                "archive_keep_history: 1\n"
                "log_keep_recent: 100\n"
                "budget_level: 10MB\n"
                "budget_mode: body\n"
                "budget_scope: hot\n"
                "anchor_always_hot: true\n"
                "keyness_enabled: false\n"
                "keyness_window: 20\n"
                "keyness_top_k: 5\n"
                "knowledge_protection_boost: 1.0\n"
                "auto_enforce_on_write: false\n",
                encoding="utf-8",
            )

    def _permission_policy(self) -> PermissionPolicy:
        config = self.read_config()
        mode = self._configured_permission_mode(config)
        try:
            return PermissionPolicy(PermissionMode(mode))
        except ValueError as exc:
            raise IntegrityError("permissions must be none, propose_then_confirm, or full") from exc

    @staticmethod
    def _configured_permission_mode(config: dict[str, Any]) -> str:
        mode = config.get("permissions")
        if mode is not None:
            return str(mode)
        legacy = config.get("agent_writes")
        if isinstance(legacy, str):
            return legacy
        if legacy is True:
            return "full"
        return "propose_then_confirm"

    def _permission_mode(self) -> str:
        return self._permission_policy().mode.value

    def read_config(self) -> dict[str, Any]:
        """Read and validate YAML configuration without silently changing defaults."""
        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise IntegrityError(f"Invalid vault configuration: {exc}") from exc
        return self._validate_config_mapping(raw)

    @staticmethod
    def _validate_config_mapping(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise IntegrityError("Vault configuration must be a YAML mapping")
        try:
            if "agent_writes" in raw:
                agent_writes = raw["agent_writes"]
                if isinstance(agent_writes, bool):
                    pass
                elif isinstance(agent_writes, str):
                    PermissionMode(agent_writes)
                else:
                    raise ValueError("agent_writes must be a permission mode or boolean")
            PermissionMode(Repository._configured_permission_mode(raw))
            BudgetProfile(raw.get("budget_level", "10MB"))
        except ValueError as exc:
            raise IntegrityError(
                "Invalid permissions or budget_level in vault configuration"
            ) from exc
        for key in (
            "history_keep",
            "archive_keep_history",
            "log_keep_recent",
            "max_bytes",
            "max_count",
        ):
            if key in raw and (
                isinstance(raw[key], bool) or not isinstance(raw[key], int) or raw[key] < 0
            ):
                raise IntegrityError(f"{key} must be a non-negative integer")
        if "history_keep" in raw and raw["history_keep"] == 0:
            raise IntegrityError("history_keep must be greater than zero")
        if "log_keep_recent" in raw and raw["log_keep_recent"] == 0:
            raise IntegrityError("log_keep_recent must be greater than zero")
        if raw.get("budget_mode", "body") not in {"body", "total"}:
            raise IntegrityError("budget_mode must be body or total")
        if raw.get("budget_scope", "hot") not in {"hot", "all"}:
            raise IntegrityError("budget_scope must be hot or all")
        if not isinstance(raw.get("auto_enforce_on_write", False), bool):
            raise IntegrityError("auto_enforce_on_write must be boolean")
        if not isinstance(raw.get("anchor_always_hot", True), bool):
            raise IntegrityError("anchor_always_hot must be boolean")
        if not isinstance(raw.get("keyness_enabled", False), bool):
            raise IntegrityError("keyness_enabled must be boolean")
        window = raw.get("keyness_window", 20)
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise IntegrityError("keyness_window must be a positive integer")
        top_k = raw.get("keyness_top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise IntegrityError("keyness_top_k must be a positive integer")
        protection_boost = raw.get("knowledge_protection_boost", 1.0)
        if (
            isinstance(protection_boost, bool)
            or not isinstance(protection_boost, (int, float))
            or protection_boost < 0
        ):
            raise IntegrityError("knowledge_protection_boost must be a non-negative finite number")
        import math

        if not math.isfinite(protection_boost):
            raise IntegrityError("knowledge_protection_boost must be a non-negative finite number")
        reference_path = raw.get("keyness_reference_path")
        if reference_path is not None and not isinstance(reference_path, str):
            raise IntegrityError("keyness_reference_path must be a string or null")
        return raw

    @_serialized
    def configure(self, /, config: dict[str, Any]) -> None:
        """Atomically replace validated config and record only old/new config hashes."""
        if not isinstance(config, dict):
            raise IntegrityError("Vault configuration must be a YAML mapping")
        candidate = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
        try:
            parsed = yaml.safe_load(candidate.decode("utf-8"))
        except yaml.YAMLError as exc:
            raise IntegrityError(f"Invalid vault configuration: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IntegrityError("Vault configuration must be a YAML mapping")
        self._validate_config_mapping(parsed)
        before = self.config_path.read_bytes() if self.config_path.exists() else b""
        if before == candidate:
            return
        self._atomic_write(self.config_path, candidate)
        self._append_event(
            EventKind.CONFIG_CHANGE,
            "config",
            actor="human",
            details={
                "before_config_hash": hashlib.sha256(before).hexdigest(),
                "after_config_hash": hashlib.sha256(candidate).hexdigest(),
            },
        )

    @staticmethod
    def _payload_hash(memory_id: str, operation: str, changes: dict[str, Any]) -> str:
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            normalized[key] = value.value if hasattr(value, "value") else value
        payload = {"memory_id": memory_id, "operation": operation, "changes": normalized}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _approved_proposal(
        self, *, actor: str, proposal_id: str, target_hash: str, before_hash: str | None
    ) -> bool:
        events = self.events()
        proposal = next(
            (e for e in events if e.event_id == proposal_id and e.kind == EventKind.AGENT_PROPOSAL),
            None,
        )
        if (
            proposal is None
            or proposal.actor != actor
            or proposal.details.get("target_hash") != target_hash
        ):
            return False
        if proposal.before_hash != before_hash:
            return False
        decision = next(
            (
                e
                for e in events
                if e.kind == EventKind.HUMAN_DECISION
                and e.details.get("proposal_id") == proposal_id
                and e.details.get("target_hash") == target_hash
                and e.details.get("decision") == "approve"
            ),
            None,
        )
        if decision is None:
            return False
        return not any(
            e.details.get("proposal_id") == proposal_id
            and e.kind
            in {
                EventKind.UPDATE,
                EventKind.DELETE,
                EventKind.LAYER_CHANGE,
                EventKind.TIER_CHANGE,
                EventKind.ANCHOR_SET,
            }
            for e in events
        )

    def _human_approved_source_delete(self, memory_id: str, proposal_id: str | None) -> bool:
        if proposal_id is None:
            return False
        proposal = next(
            (
                item
                for item in self.list_proposals(status="approved")
                if item.proposal_id == proposal_id
                and item.kind == "forget"
                and memory_id in item.target_ids
                and item.output.get("action") == "hard"
            ),
            None,
        )
        if proposal is None:
            return False
        try:
            self.assert_proposal_current(proposal_id)
        except (IntegrityError, NotFoundError):
            return False
        return any(
            event.kind == EventKind.HUMAN_DECISION
            and event.details.get("proposal_id") == proposal_id
            and event.details.get("decision") == "approve"
            and event.details.get("target_hash") == proposal.payload_hash
            for event in self.events()
        )

    def _simulated_proposal_approved(self, memory_id: str, proposal_id: str | None) -> bool:
        if proposal_id is None:
            return False
        proposal = next(
            (
                item
                for item in self.list_proposals(status="approved")
                if item.proposal_id == proposal_id
                and item.kind in {"forget", "merge"}
                and memory_id in item.target_ids
            ),
            None,
        )
        if proposal is None or self.knowledge_source_counts().get(memory_id, 0):
            return False
        try:
            current = self.get(memory_id)
        except NotFoundError:
            return False
        from ..agents.models import canonical_payload_hash

        if proposal.target_hashes.get(memory_id) != canonical_payload_hash(
            current.model_dump(mode="json")
        ):
            return False
        return any(
            event.kind == EventKind.SIMULATED_DECISION
            and event.actor == "simulated_approver"
            and event.details.get("proposal_id") == proposal_id
            and event.details.get("decision") == "approve"
            and event.details.get("target_hash") == proposal.payload_hash
            for event in self.events()
        )

    @_serialized
    def propose_source_deletion(self, memory_id: str) -> Proposal:
        """Record a human-review proposal before deleting a knowledge source."""
        from ..agents.models import JsonValue, Proposal, canonical_payload_hash, mock_signature

        self._check_id(memory_id)
        if self.knowledge_source_counts().get(memory_id, 0) < 1:
            raise IntegrityError("Memory is not referenced by an active knowledge card")
        document_hash = self._source_protection_hash(memory_id)
        existing = [
            item
            for item in self.list_proposals(status=None)
            if item.kind == "forget"
            and memory_id in item.target_ids
            and item.output.get("source_protection") == "knowledge_reference"
        ]
        for reusable in existing:
            if reusable.status not in {"pending", "approved"}:
                continue
            try:
                self.assert_proposal_current(reusable.proposal_id)
            except IntegrityError:
                continue
            return reusable
        proposal_id = (
            f"source-delete-{hashlib.sha256((memory_id + document_hash).encode()).hexdigest()[:16]}"
            f"-{len(existing) + 1}"
        )
        output: dict[str, JsonValue] = {
            "action": "hard",
            "source_protection": "knowledge_reference",
        }
        payload_hash = canonical_payload_hash(output)
        timestamp = self._now()
        input_hash = hashlib.sha256(
            f"source-delete:{memory_id}:{document_hash}".encode()
        ).hexdigest()
        proposal = Proposal(
            proposal_id=proposal_id,
            kind="forget",
            target_ids=(memory_id,),
            target_hashes={memory_id: document_hash},
            agent_name="source-protection",
            model_name="deterministic-policy",
            input_hash=input_hash,
            output=output,
            reason="A knowledge card cites this source; deletion needs human review.",
            payload_hash=payload_hash,
            actor="agent:source-protection",
            signature=mock_signature(proposal_id, input_hash, payload_hash, timestamp),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._record_typed_proposal(proposal)
        return proposal

    @_serialized
    def simulate_proposal_decision(
        self, proposal_id: str, *, decision: str = "approve"
    ) -> Proposal:
        """Record an explicitly simulated approval for eligible non-protected proposals."""
        if decision != "approve":
            raise IntegrityError("Only simulated approve decisions are supported")
        proposal = next(
            (
                item
                for item in self.list_proposals(status="pending")
                if item.proposal_id == proposal_id
            ),
            None,
        )
        if proposal is None or proposal.kind not in {"merge", "forget"}:
            raise IntegrityError("Simulation supports pending merge and forget proposals only")
        for memory_id in proposal.target_ids:
            document = self.get(memory_id)
            if document.anchor or document.layer == Layer.ANCHOR:
                raise IntegrityError("Simulation cannot approve anchored targets")
            if self.knowledge_source_counts().get(memory_id, 0):
                raise IntegrityError("Simulation cannot approve knowledge-card source deletion")
        self.assert_proposal_current(proposal_id)
        self.capture_event(
            EventKind.SIMULATED_DECISION,
            actor="simulated_approver",
            details={
                "proposal_id": proposal_id,
                "decision": "approve",
                "target_hash": proposal.payload_hash,
                "simulation_mode": True,
            },
        )
        return proposal.model_copy(update={"status": "approved"})

    def _can_mutate(
        self,
        actor: str,
        *,
        layer: Layer | None = None,
        anchor: str | None = None,
        signature: str | None = None,
        agent_id: str | None = None,
        approved: bool = False,
    ) -> bool:
        return self._permission_policy().can_mutate(
            actor,
            signature=signature,
            agent_id=agent_id,
            approved=approved,
            layer=layer,
            anchor=anchor,
        )

    def assert_no_pending(self) -> None:
        if (
            self.pending_path.exists()
            and not getattr(self._mutation_local, "depth", 0)
            and not self._recovering
        ):
            raise IntegrityError("A pending transaction needs explicit `memfw recover`")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise IntegrityError("Clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _check_id(self, memory_id: str) -> str:
        if not _ID_RE.fullmatch(memory_id):
            raise IntegrityError(f"Invalid memory id: {memory_id!r}")
        path = (self.memories_dir / f"{memory_id}.md").resolve()
        if path.parent != self.memories_dir.resolve():
            raise IntegrityError("Memory path escapes the vault")
        return memory_id

    @_serialized
    def capture_event(
        self,
        /,
        kind: EventKind,
        *,
        actor: str,
        details: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> Event:
        if kind not in {
            EventKind.EXTERNAL_DELETE,
            EventKind.MEMORY_RESTORE,
            EventKind.CAPTURE_WAKE,
            EventKind.CAPTURE_SLEEP,
            EventKind.CAPTURE_STATE_CHANGE,
            EventKind.CAPTURE_RECORD,
            EventKind.AGENT_PROPOSAL,
            EventKind.CONSOLIDATION_PROPOSAL,
            EventKind.HUMAN_DECISION,
            EventKind.SIMULATED_DECISION,
            EventKind.FORGET,
            EventKind.MEMORY_ACCESS,
            EventKind.BUDGET_UNSATISFIABLE,
            EventKind.PROPOSAL_ERASE,
        }:
            raise IntegrityError(f"Unsupported metadata event: {kind.value}")
        allowed = {
            "memory_id",
            "operation",
            "decision",
            "simulation_mode",
            "state",
            "source",
            "proposal_hash",
            "agent_id",
            "signature",
            "reason",
            "policy",
            "target_hash",
            "target_snapshot_hash",
            "proposal_id",
            "source_hash",
            "archive_ref",
            "byte_count",
            "representative_id",
            "duplicate_id",
            "group_hash",
            "action",
            "scope",
            "access_count",
            "score",
            "intensity",
            "candidate_id",
            "session_id",
            "age_days",
            "elapsed_seconds",
            "last_accessed_at",
            "strength",
            "reference_count",
            "match_basis",
            "budget_mode",
            "budget_bytes",
            "physical_bytes",
            "max_count",
            "anchor_ids",
            "state_hash",
            "physical_breakdown",
            "dominant_current_component",
            "dominant_component_bytes",
            "physical_overage_bytes",
            "overage_explanation",
            "reason_code",
            "remaining_anchor_count",
            "remaining_non_anchor_count",
            "rebuildable_index_bytes",
            "floor_explanation",
            "kind",
            "input_hash",
        }
        if set(details) - allowed:
            raise IntegrityError("Event details accept metadata keys only")
        if any(
            not isinstance(value, (str, int, float, bool, type(None))) for value in details.values()
        ):
            raise IntegrityError("Event details must be scalar metadata")
        if "state" in details:
            try:
                CaptureState(details["state"])
            except (TypeError, ValueError) as exc:
                raise IntegrityError("Capture state must be dormant, standby, or full") from exc
        operation = details.get("operation")
        if operation is not None and (
            not isinstance(operation, str) or not re.fullmatch(r"[a-z_]{1,48}", operation)
        ):
            raise IntegrityError("Operation must be a short metadata identifier")
        if kind == EventKind.AGENT_PROPOSAL:
            if (
                not details.get("agent_id")
                or not details.get("signature")
                or not details.get("target_hash")
            ):
                raise IntegrityError(
                    "Agent proposals require identity, signature, and bound target metadata"
                )
            if actor != f"agent:{details['agent_id']}":
                raise IntegrityError("Proposal actor must match the declared agent id")
            if len(str(details["signature"])) > 256:
                raise IntegrityError("Signature metadata exceeds 256 characters")
        if kind == EventKind.HUMAN_DECISION and actor != "human":
            raise IntegrityError("Only a human actor may record a decision")
        if kind == EventKind.SIMULATED_DECISION and actor != "simulated_approver":
            raise IntegrityError("Simulated decisions must use the simulated_approver actor")
        if kind == EventKind.CONSOLIDATION_PROPOSAL:
            required = {"representative_id", "duplicate_id", "match_basis"}
            if not required.issubset(details) or actor not in {"human", "mechanism"}:
                raise IntegrityError(
                    "Consolidation proposals need metadata-only links and a trusted workflow actor"
                )
            self._check_id(str(details["representative_id"]))
            self._check_id(str(details["duplicate_id"]))
        if (
            actor != "human"
            and actor != "mechanism"
            and actor != "simulated_approver"
            and not actor.startswith("agent:")
        ):
            raise IntegrityError(
                "Actor must be human, mechanism, simulated_approver, or agent:<name>"
            )
        memory_id = str(details.get("memory_id", "capture"))
        if memory_id != "capture":
            self._check_id(memory_id)
        return self._append_event(
            kind, memory_id, actor=actor, details=details, timestamp=timestamp
        )

    @_serialized
    def record_proposal(
        self,
        /,
        proposal: Proposal | None = None,
        *,
        memory_id: str | None = None,
        operation: str | None = None,
        changes: dict[str, Any] | None = None,
        rationale: str | None = None,
        agent_id: str | None = None,
        signature: str | None = None,
    ) -> Event:
        if proposal is not None:
            return self._record_typed_proposal(proposal)
        if None in (memory_id, operation, rationale, agent_id, signature):
            raise IntegrityError(
                "Legacy proposal requires memory_id, operation, rationale, agent_id, and signature"
            )
        assert memory_id is not None and operation is not None
        assert rationale is not None and agent_id is not None and signature is not None
        changes = changes or {}
        if not agent_id or not signature:
            raise IntegrityError("Agent proposals require an agent id and signature metadata")
        self._check_id(memory_id)
        if operation not in {"update", "delete", "layer_change", "tier_change"}:
            raise IntegrityError("Unsupported proposal operation")
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
            "strength",
            "reference_count",
            "last_access_interval_days",
            "manually_marked",
            "group_key",
        }
        if operation != "delete" and (not changes or set(changes) - allowed):
            raise IntegrityError("Proposal changes contain unsupported fields")
        normalized = dict(changes)
        for key in ("layer", "tier"):
            if key in normalized:
                normalized[key] = (
                    normalized[key].value
                    if hasattr(normalized[key], "value")
                    else str(normalized[key])
                )
        payload_hash = self._payload_hash(memory_id, operation, normalized)
        try:
            before_hash = hashlib.sha256(self._current_path(memory_id).read_bytes()).hexdigest()
        except FileNotFoundError:
            before_hash = None
        return self._append_event(
            EventKind.AGENT_PROPOSAL,
            memory_id,
            actor=f"agent:{agent_id}",
            before_hash=before_hash,
            details={
                "memory_id": memory_id,
                "operation": operation,
                "agent_id": agent_id,
                "signature": signature,
                "proposal_hash": hashlib.sha256(rationale.encode()).hexdigest(),
                "target_hash": payload_hash,
            },
        )

    def _proposal_path(self, proposal_id: str) -> Path:
        if not _ID_RE.fullmatch(proposal_id):
            raise IntegrityError("Invalid proposal id")
        path = (self.proposals_dir / f"{proposal_id}.md").resolve()
        if path.parent != self.proposals_dir.resolve():
            raise IntegrityError("Proposal path escapes the vault")
        return path

    def _record_typed_proposal(self, proposal: Proposal) -> Event:
        from ..agents.models import Proposal, canonical_json, canonical_payload_hash

        if not isinstance(proposal, Proposal):
            raise IntegrityError("record_proposal expects a validated Proposal")
        self._proposal_path(proposal.proposal_id)
        is_agent = proposal.actor == f"agent:{proposal.agent_name}"
        is_human_edit = proposal.actor == "human" and proposal.agent_name == "human-review"
        if not (is_agent or is_human_edit):
            raise IntegrityError("Proposal actor must match agent_name")
        if not proposal.signature or not proposal.target_ids:
            raise IntegrityError("Proposal needs a signature and at least one target")
        if len(set(proposal.target_ids)) != len(proposal.target_ids):
            raise IntegrityError("Proposal target_ids must be unique")
        if canonical_payload_hash(proposal.output) != proposal.payload_hash:
            raise IntegrityError("Proposal payload hash does not match output")
        for target_id in proposal.target_ids:
            self._check_id(target_id)
            target = self.get(target_id)
            if proposal.target_hashes:
                expected = proposal.target_hashes.get(target_id)
                actual = (
                    self._source_protection_hash(target_id)
                    if proposal.output.get("source_protection") == "knowledge_reference"
                    else canonical_payload_hash(target.model_dump(mode="json"))
                )
                if expected != actual:
                    raise IntegrityError(f"Proposal target snapshot is stale: {target_id}")
        path = self._proposal_path(proposal.proposal_id)
        markdown = (
            "# Erasable agent proposal\n\n"
            "```json\n"
            f"{canonical_json(proposal.model_dump(mode='json'))}\n"
            "```\n"
        ).encode()
        digest = hashlib.sha256(markdown).hexdigest()
        if path.exists():
            if path.read_bytes() != markdown:
                raise IntegrityError("Proposal id already names different content")
            existing = next(
                (
                    event
                    for event in self.events()
                    if event.kind == EventKind.AGENT_PROPOSAL
                    and event.details.get("proposal_id") == proposal.proposal_id
                ),
                None,
            )
            if existing is not None and existing.details.get("source_hash") == digest:
                return existing
            raise IntegrityError("Proposal object exists without its matching event")
        self._atomic_write(path, markdown)
        return self._append_event(
            EventKind.AGENT_PROPOSAL,
            "proposal",
            actor=proposal.actor,
            details={
                "proposal_id": proposal.proposal_id,
                "agent_id": proposal.agent_name,
                "signature": proposal.signature,
                "operation": proposal.kind,
                "target_hash": proposal.payload_hash,
                "proposal_hash": proposal.payload_hash,
                "source_hash": digest,
                "target_snapshot_hash": self._proposal_targets_hash(
                    proposal.target_ids,
                    source_protection=proposal.output.get("source_protection")
                    == "knowledge_reference",
                ),
                **(
                    {"target_snapshot_hash_mode": "source_semantic_v1"}
                    if proposal.output.get("source_protection") == "knowledge_reference"
                    else {}
                ),
            },
            timestamp=proposal.created_at,
        )

    def list_proposals(self, /, status: ProposalStatus | None = "pending") -> list[Proposal]:
        """Load validated Markdown proposals and project status from decision events."""
        import re

        from ..agents.models import Proposal

        records: dict[str, tuple[Event, str]] = {}
        status_by_id: dict[str, ProposalStatus] = {}
        erased: set[str] = set()
        for event in self.events():
            proposal_id = event.details.get("proposal_id")
            if not isinstance(proposal_id, str):
                continue
            if event.kind == EventKind.AGENT_PROPOSAL:
                source_hash = event.details.get("source_hash")
                if isinstance(source_hash, str):
                    records[proposal_id] = (event, source_hash)
            elif event.kind == EventKind.PROPOSAL_ERASE:
                erased.add(proposal_id)
            elif event.kind in {EventKind.HUMAN_DECISION, EventKind.SIMULATED_DECISION}:
                decision = event.details.get("decision")
                if decision == "approve":
                    status_by_id[proposal_id] = "approved"
                elif decision == "reject":
                    status_by_id[proposal_id] = "rejected"
                elif decision == "applied":
                    status_by_id[proposal_id] = "applied"
        proposals: list[Proposal] = []
        for proposal_id, (_event, expected_hash) in records.items():
            if proposal_id in erased:
                continue
            path = self._proposal_path(proposal_id)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise IntegrityError(f"Proposal Markdown hash mismatch: {proposal_id}")
            match = re.fullmatch(
                rb"# Erasable agent proposal\n\n```json\n(.*)\n```\n", raw, re.DOTALL
            )
            if match is None:
                raise IntegrityError(f"Malformed proposal Markdown: {proposal_id}")
            try:
                proposal = Proposal.model_validate_json(match.group(1))
            except (ValueError, TypeError) as exc:
                raise IntegrityError(f"Invalid proposal object: {proposal_id}") from exc
            if proposal.proposal_id != proposal_id:
                raise IntegrityError("Proposal id does not match its Markdown path")
            projected_status: ProposalStatus = status_by_id.get(proposal_id, "pending")
            projected = proposal.model_copy(update={"status": projected_status})
            if status is None or projected.status == status:
                proposals.append(projected)
        return sorted(proposals, key=lambda item: (item.created_at, item.proposal_id))

    def _source_protection_hash(self, memory_id: str) -> str:
        """Hash source meaning while ignoring cold-storage-only metadata."""
        from ..agents.models import canonical_payload_hash

        current = self.get(memory_id)
        if current.is_stub:
            if not current.archive_ref or not current.archived_hash:
                raise IntegrityError(f"Cold source {memory_id} has no verified archive")
            archive_path = self.archives_dir / f"{current.archive_ref}.tar.zst"
            try:
                import io
                import tarfile

                import zstandard

                raw_tar = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
                with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
                    member = archive.extractfile(f"memories/{memory_id}.md")
                    if member is None:
                        raise IntegrityError(f"Cold archive is missing source {memory_id}")
                    raw = member.read()
                if hashlib.sha256(raw).hexdigest() != current.archived_hash:
                    raise IntegrityError(f"Cold archive hash mismatch for {memory_id}")
                original = self.parse(raw.decode("utf-8"), expected_id=memory_id)
            except IntegrityError:
                raise
            except Exception as exc:
                raise IntegrityError(f"Unable to verify cold source {memory_id}: {exc}") from exc
            generated_summary = original.summary or " ".join(original.body.split())[:240]
            current = current.model_copy(
                update={
                    "body": original.body,
                    "summary": original.summary
                    if current.summary == generated_summary
                    else current.summary,
                }
            )
        values = current.model_dump(
            mode="json",
            exclude={"tier", "is_stub", "archive_ref", "archived_hash", "version", "updated_at"},
        )
        return canonical_payload_hash(values)

    def _proposal_targets_hash(
        self,
        target_ids: tuple[str, ...],
        *,
        source_protection: bool = False,
        raw_source_protection: bool = False,
    ) -> str:
        snapshots: list[tuple[str, str]] = []
        for memory_id in sorted(target_ids):
            self._check_id(memory_id)
            if source_protection:
                digest = self._source_protection_hash(memory_id)
            elif raw_source_protection:
                digest = self._raw_target_markdown_hash(memory_id)
            else:
                raw = self._current_path(memory_id).read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
            snapshots.append((memory_id, digest))
        encoded = json.dumps(snapshots, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _raw_target_markdown_hash(self, memory_id: str) -> str:
        """Return the original Markdown hash, including for a verified cold stub."""
        current = self.get(memory_id)
        if not current.is_stub:
            return hashlib.sha256(self._current_path(memory_id).read_bytes()).hexdigest()
        if not current.archive_ref or not current.archived_hash:
            raise IntegrityError(f"Cold source {memory_id} has no verified archive")
        archive_path = self.archives_dir / f"{current.archive_ref}.tar.zst"
        try:
            import io
            import tarfile

            import zstandard

            raw_tar = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
            with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
                member = archive.extractfile(f"memories/{memory_id}.md")
                if member is None:
                    raise IntegrityError(f"Cold archive is missing source {memory_id}")
                raw = member.read()
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(f"Unable to verify cold source {memory_id}: {exc}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != current.archived_hash:
            raise IntegrityError(f"Cold archive hash mismatch for {memory_id}")
        return digest

    def _ensure_cooling_preserves_proposals(self, memory_id: str) -> None:
        """Keep active legacy raw-bound source proposals valid across tier changes."""
        active = [
            proposal
            for proposal in self.list_proposals(status=None)
            if proposal.status in {"pending", "approved"}
            and proposal.output.get("source_protection") == "knowledge_reference"
            and memory_id in proposal.target_ids
        ]
        for proposal in active:
            record = next(
                (
                    event
                    for event in self.events()
                    if event.kind == EventKind.AGENT_PROPOSAL
                    and event.details.get("proposal_id") == proposal.proposal_id
                ),
                None,
            )
            if record is None:
                continue
            if record.details.get("target_snapshot_hash_mode") == "source_semantic_v1":
                continue
            # Verify the old comparison still holds before deciding that the
            # upcoming metadata rewrite would invalidate this legacy proposal.
            try:
                self.assert_proposal_current(proposal.proposal_id)
            except IntegrityError:
                continue
            raise IntegrityError(
                "Cannot cool a source with an active legacy raw-hash deletion proposal; "
                "resolve the proposal first"
            )

    def assert_proposal_current(self, proposal_id: str) -> None:
        """Reject stale proposals before a review workflow begins any mutation."""
        proposal = next(
            (item for item in self.list_proposals(status=None) if item.proposal_id == proposal_id),
            None,
        )
        if proposal is None:
            raise NotFoundError("Agent proposal not found")
        record = next(
            (
                event
                for event in self.events()
                if event.kind == EventKind.AGENT_PROPOSAL
                and event.details.get("proposal_id") == proposal_id
            ),
            None,
        )
        source_protection = proposal.output.get("source_protection") == "knowledge_reference"
        semantic_source_hash = bool(
            record is not None
            and record.details.get("target_snapshot_hash_mode") == "source_semantic_v1"
        )
        if record is None or record.details.get(
            "target_snapshot_hash"
        ) != self._proposal_targets_hash(
            proposal.target_ids,
            source_protection=source_protection and semantic_source_hash,
            raw_source_protection=source_protection and not semantic_source_hash,
        ):
            raise IntegrityError("Proposal is stale; one or more target Markdown files changed")

    @_serialized
    def decide_proposal(
        self,
        /,
        proposal_id: str,
        *,
        payload: dict[str, Any] | None = None,
        decision: str = "approve",
        payload_hash: str | None = None,
        edited_proposal: Proposal | None = None,
    ) -> MemoryDocument | Proposal | None:
        if payload is None:
            return self._decide_typed_proposal(
                proposal_id,
                decision=decision,
                payload_hash=payload_hash,
                edited_proposal=edited_proposal,
            )
        if decision != "approve":
            raise IntegrityError("Only explicit human approve decisions are currently supported")
        events = self.events()
        proposal = next((event for event in events if event.event_id == proposal_id), None)
        if proposal is None or proposal.kind != EventKind.AGENT_PROPOSAL:
            raise NotFoundError("Agent proposal not found")
        if any(
            event.kind == EventKind.HUMAN_DECISION
            and event.details.get("proposal_id") == proposal_id
            for event in events
        ):
            raise IntegrityError("Proposal already has a human decision")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if digest != proposal.details.get("target_hash"):
            raise IntegrityError("Approval payload does not match the signed proposal target hash")
        memory_id = str(payload.get("memory_id", ""))
        operation = str(payload.get("operation", ""))
        changes = payload.get("changes", {})
        if not isinstance(changes, dict):
            raise IntegrityError("Proposal changes must be an object")
        current_path = self._current_path(memory_id)
        current_hash = (
            hashlib.sha256(current_path.read_bytes()).hexdigest() if current_path.exists() else None
        )
        if current_hash != proposal.before_hash:
            raise IntegrityError(
                "Proposal is stale; current memory hash differs from proposal before_hash"
            )
        self.capture_event(
            EventKind.HUMAN_DECISION,
            actor="human",
            details={
                "memory_id": memory_id,
                "proposal_id": proposal_id,
                "decision": "approve",
                "target_hash": digest,
            },
        )
        changes_for_review = payload.get("changes", {})
        if self._permission_mode() == PermissionMode.NONE.value or (
            isinstance(changes_for_review, dict)
            and (
                "anchor" in changes_for_review
                or changes_for_review.get("layer") in {Layer.ANCHOR, Layer.ANCHOR.value}
            )
        ):
            # Approval records authorization intent. In none mode and for anchor
            # changes, a human must perform the write as a separate action.
            return None
        agent_id = str(proposal.details["agent_id"])
        signature = str(proposal.details["signature"])
        result: MemoryDocument | None
        if operation == "delete":
            self.delete(
                memory_id,
                actor=proposal.actor,
                agent_id=agent_id,
                signature=signature,
                proposal_id=proposal_id,
                approved_by="human",
            )
            result = None
        elif operation in {"update", "layer_change", "tier_change"}:
            normalized = dict(changes)
            if "layer" in normalized:
                normalized["layer"] = Layer(normalized["layer"])
            if "tier" in normalized:
                normalized["tier"] = Tier(normalized["tier"])
            result = self.update(
                memory_id,
                actor=proposal.actor,
                agent_id=agent_id,
                signature=signature,
                proposal_id=proposal_id,
                approved_by="human",
                proposal_operation=operation,
                **normalized,
            )
        else:
            raise IntegrityError("Unsupported proposal operation")
        return result

    def _decide_typed_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        payload_hash: str | None,
        edited_proposal: Proposal | None,
    ) -> Proposal:
        from ..agents.models import Proposal

        if decision not in {"approve", "reject", "edit"}:
            raise IntegrityError("Proposal decision must be approve, reject, or edit")
        proposal = next(
            (item for item in self.list_proposals(status=None) if item.proposal_id == proposal_id),
            None,
        )
        if proposal is None:
            if any(
                event.kind == EventKind.HUMAN_DECISION
                and event.details.get("proposal_id") == proposal_id
                for event in self.events()
            ):
                raise IntegrityError("Proposal already has a human decision")
            raise NotFoundError("Agent proposal not found")
        if proposal.status != "pending":
            raise IntegrityError("Proposal already has a human decision")
        path = self._proposal_path(proposal_id)
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        events = self.events()
        record = next(
            (
                event
                for event in events
                if event.kind == EventKind.AGENT_PROPOSAL
                and event.details.get("proposal_id") == proposal_id
            ),
            None,
        )
        if record is None or record.details.get("source_hash") != source_hash:
            raise IntegrityError("Proposal file is not bound to its committed event")
        if payload_hash != proposal.payload_hash:
            raise IntegrityError("Decision payload hash does not match the proposal")
        self.assert_proposal_current(proposal_id)
        if decision == "edit":
            if not isinstance(edited_proposal, Proposal):
                raise IntegrityError("Edit decision requires a validated replacement Proposal")
            if edited_proposal.proposal_id == proposal_id:
                raise IntegrityError("Edited proposal must have a new proposal id")
            self._record_typed_proposal(edited_proposal)
            decision_value = "reject"
        else:
            decision_value = decision
        self.capture_event(
            EventKind.HUMAN_DECISION,
            actor="human",
            details={
                "proposal_id": proposal_id,
                "decision": decision_value,
                "target_hash": proposal.payload_hash,
                "source_hash": source_hash,
            },
        )
        return proposal.model_copy(
            update={"status": "approved" if decision == "approve" else "rejected"}
        )

    @_serialized
    def mark_proposal_applied(
        self,
        /,
        proposal_id: str,
        *,
        payload_hash: str,
        actor: str = "human",
    ) -> Event:
        """Record one-time human or simulated completion of a reviewed proposal."""
        if actor not in {"human", "simulated_approver"}:
            raise IntegrityError("Only a human or simulated approver may apply a proposal")
        decision_kind = (
            EventKind.HUMAN_DECISION if actor == "human" else EventKind.SIMULATED_DECISION
        )
        events = self.events()
        proposal_event = next(
            (
                event
                for event in events
                if event.kind == EventKind.AGENT_PROPOSAL
                and event.details.get("proposal_id") == proposal_id
            ),
            None,
        )
        if proposal_event is None:
            raise NotFoundError("Proposal event not found")
        if proposal_event.details.get("target_hash") != payload_hash:
            raise IntegrityError("Applied payload hash does not match the approved proposal")
        if not any(
            event.kind == decision_kind
            and event.details.get("proposal_id") == proposal_id
            and event.details.get("decision") == "approve"
            and event.details.get("target_hash") == payload_hash
            for event in events
        ):
            raise IntegrityError("Proposal needs a matching human approval before apply")
        if any(
            event.kind == decision_kind
            and event.details.get("proposal_id") == proposal_id
            and event.details.get("decision") == "applied"
            for event in events
        ):
            raise IntegrityError("Proposal has already been applied")
        source_hash = proposal_event.details.get("source_hash")
        if not isinstance(source_hash, str):
            raise IntegrityError("Proposal source hash is missing")
        applied = self.capture_event(
            decision_kind,
            actor=actor,
            details={
                "proposal_id": proposal_id,
                "decision": "applied",
                "target_hash": payload_hash,
                "source_hash": source_hash,
            },
        )
        proposal_path = self._proposal_path(proposal_id)
        if proposal_path.exists():
            self._append_event(
                EventKind.PROPOSAL_ERASE,
                "proposal",
                actor=actor,
                before_hash=hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
                details={"proposal_id": proposal_id},
            )
            proposal_path.unlink()
        return applied

    @_serialized
    def apply_proposal(
        self, /, proposal_id: str, *, payload: dict[str, Any]
    ) -> MemoryDocument | None:
        """Apply one approved proposal as a human-authored repository operation."""
        events = self.events()
        proposal = next(
            (
                event
                for event in events
                if event.event_id == proposal_id and event.kind == EventKind.AGENT_PROPOSAL
            ),
            None,
        )
        if proposal is None:
            raise NotFoundError("Agent proposal not found")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        decision = next(
            (
                event
                for event in events
                if event.kind == EventKind.HUMAN_DECISION
                and event.details.get("proposal_id") == proposal_id
                and event.details.get("decision") == "approve"
                and event.details.get("target_hash") == payload_hash
            ),
            None,
        )
        if decision is None or payload_hash != proposal.details.get("target_hash"):
            raise IntegrityError("Proposal must have a matching human approval before apply")
        if any(
            event.details.get("proposal_id") == proposal_id
            and event.kind
            in {
                EventKind.UPDATE,
                EventKind.DELETE,
                EventKind.LAYER_CHANGE,
                EventKind.TIER_CHANGE,
                EventKind.ANCHOR_SET,
            }
            for event in events
        ):
            raise IntegrityError("Proposal has already been applied")
        memory_id = str(payload.get("memory_id", ""))
        operation = str(payload.get("operation", ""))
        changes = payload.get("changes", {})
        if not isinstance(changes, dict):
            raise IntegrityError("Proposal changes must be an object")
        current_path = self._current_path(memory_id)
        current_hash = (
            hashlib.sha256(current_path.read_bytes()).hexdigest() if current_path.exists() else None
        )
        if current_hash != proposal.before_hash:
            raise IntegrityError(
                "Proposal is stale; current memory hash differs from proposal before_hash"
            )
        if operation == "delete":
            self.delete(memory_id, actor="human")
            return None
        if operation not in {"update", "layer_change", "tier_change"}:
            raise IntegrityError("Unsupported proposal operation")
        normalized = dict(changes)
        if "layer" in normalized:
            normalized["layer"] = Layer(normalized["layer"])
        if "tier" in normalized:
            normalized["tier"] = Tier(normalized["tier"])
        return self.update(memory_id, actor="human", proposal_id=proposal_id, **normalized)
