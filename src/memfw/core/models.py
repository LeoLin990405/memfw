"""Validated models shared by the vault, event log, and policy interfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class Layer(StrEnum):
    CURRENT = "current"
    CONSOLIDATED = "consolidated"
    ANCHOR = "anchor"


class Tier(StrEnum):
    HOT = "hot"
    COLD = "cold"


class CaptureState(StrEnum):
    DORMANT = "dormant"
    STANDBY = "standby"
    FULL = "full"


class EventKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SUPERSEDE = "supersede"
    LAYER_CHANGE = "layer_change"
    TIER_CHANGE = "tier_change"
    ANCHOR_SET = "anchor_set"
    AGENT_PROPOSAL = "agent_proposal"
    HUMAN_DECISION = "human_decision"
    SIMULATED_DECISION = "simulated_decision"
    EXTERNAL_EDIT = "external_edit"
    EXTERNAL_DELETE = "external_delete"
    MEMORY_RESTORE = "memory_restore"
    CAPTURE_WAKE = "capture_wake"
    CAPTURE_SLEEP = "capture_sleep"
    CAPTURE_STATE_CHANGE = "capture_state_change"
    CAPTURE_RECORD = "capture_record"
    FORGET = "forget"
    MEMORY_ACCESS = "memory_access"
    CONSOLIDATION_PROPOSAL = "consolidation_proposal"
    CONFIG_CHANGE = "config_change"
    BUDGET_UNSATISFIABLE = "budget_unsatisfiable"
    PROPOSAL_ERASE = "proposal_erase"
    KNOWLEDGE_CARD_CREATE = "knowledge_card_create"
    KNOWLEDGE_SOURCE_MISSING = "knowledge_source_missing"
    KNOWLEDGE_SOURCE_REPOINT = "knowledge_source_repoint"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


_TEMPORAL_ALIASES = (("valid_until", "valid_to"), ("superseded", "superseded_by"))


def _same_temporal_value(left: Any, right: Any) -> bool:
    if left == right:
        return True
    try:
        left_stamp = (
            left
            if isinstance(left, datetime)
            else datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        )
        right_stamp = (
            right
            if isinstance(right, datetime)
            else datetime.fromisoformat(str(right).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return False
    return left_stamp == right_stamp


def fold_temporal_aliases(data: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """Accept Conductor aliases and keep the stored canonical names.

    ``valid_until`` is an input alias of ``valid_to``. ``superseded`` is an
    input alias of ``superseded_by``. The stored document and every response
    use only the canonical names. A conflicting pair is rejected on input.
    A reader may pass ``strict=False`` and keep the canonical value.
    """
    folded = dict(data)
    for alias, canonical in _TEMPORAL_ALIASES:
        if alias not in folded:
            continue
        alias_value = folded.pop(alias)
        current = folded.get(canonical)
        if current in (None, ""):
            folded[canonical] = alias_value
            continue
        if alias_value in (None, "") or _same_temporal_value(current, alias_value):
            continue
        if strict:
            raise ValueError(f"{alias} conflicts with {canonical}")
    return folded


class MemoryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    layer: Layer = Layer.CURRENT
    tier: Tier = Tier.HOT
    type: str | None = None
    tenant: str = ""
    related: list[str] = Field(default_factory=list)
    source: str = "manual"
    provenance: str = "human-authored"
    anchor: str | None = None
    tags: list[str] = Field(default_factory=list)
    version: int = 1
    summary: str = ""
    is_stub: bool = False
    archive_ref: str | None = None
    archived_hash: str | None = None
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    reference_count: int = Field(default=0, ge=0)
    last_access_interval_days: float | None = Field(default=None, ge=0.0)
    manually_marked: bool = False
    group_key: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_by: str | None = None
    created_at: datetime
    updated_at: datetime
    body: str

    @model_validator(mode="before")
    @classmethod
    def accept_temporal_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return fold_temporal_aliases(data)
        return data

    @field_validator("created_at", "updated_at", "valid_from", "valid_to")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)


class Event(BaseModel):
    """Metadata-only event. Content is stored in erasable Markdown version objects."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    kind: EventKind
    memory_id: str
    timestamp: datetime
    actor: str
    before_hash: str | None = None
    after_hash: str | None = None
    version_ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    prev_event_hash: str = "0" * 64
    event_hash: str
    _compacted: bool = PrivateAttr(default=False)

    @property
    def is_compacted_summary(self) -> bool:
        return self._compacted

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class FsckIssue(BaseModel):
    memory_id: str
    kind: str
    expected_hash: str | None = None
    actual_hash: str | None = None
    message: str
