"""Validated models shared by the vault, event log, and policy interfaces."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import core_schema

from .epistemic import CausalLevel, CausalRecord, EpistemicType, ExperiencePoint, OpenQuestion


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
    # Attachments (MM1). Their records live in ``attachments/``, never in
    # ``memories/``; only ``ATTACH`` is written in MM1a, the rest are reserved
    # for MM1b (detach, purge, missing, relink) and MM2 (proxies).
    ATTACH = "attach"
    ATTACH_UPDATE = "attach_update"
    DETACH = "detach"
    ATTACH_PURGE = "attach_purge"
    ATTACH_MISSING = "attach_missing"
    ATTACH_RELINK = "attach_relink"
    PROXY_ADD = "proxy_add"
    PROXY_SUPERSEDE = "proxy_supersede"


#: Event kinds that change a memory's committed Markdown (the ``replay`` projection).
MEMORY_EVENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.CREATE,
        EventKind.UPDATE,
        EventKind.DELETE,
        EventKind.SUPERSEDE,
        EventKind.LAYER_CHANGE,
        EventKind.TIER_CHANGE,
        EventKind.ANCHOR_SET,
        EventKind.EXTERNAL_EDIT,
        EventKind.EXTERNAL_DELETE,
        EventKind.MEMORY_RESTORE,
    }
)

#: Event kinds that ``replay`` and the log-segment snapshot skip: they are not
#: about a memory file (config, proposals, capture, access, knowledge cards,
#: attachments and proxies). Every :class:`EventKind` is in exactly one of
#: this set and :data:`MEMORY_EVENT_KINDS` (``tests/test_attachments.py``), so
#: a new kind must be classified on purpose. A memory-projected attachment
#: event would make ``fsck`` register an ``external_delete`` for it.
NON_MEMORY_EVENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.CONFIG_CHANGE,
        EventKind.AGENT_PROPOSAL,
        EventKind.HUMAN_DECISION,
        EventKind.SIMULATED_DECISION,
        EventKind.CAPTURE_WAKE,
        EventKind.CAPTURE_SLEEP,
        EventKind.CAPTURE_STATE_CHANGE,
        EventKind.CAPTURE_RECORD,
        EventKind.FORGET,
        EventKind.MEMORY_ACCESS,
        EventKind.CONSOLIDATION_PROPOSAL,
        EventKind.BUDGET_UNSATISFIABLE,
        EventKind.PROPOSAL_ERASE,
        EventKind.KNOWLEDGE_CARD_CREATE,
        EventKind.KNOWLEDGE_SOURCE_MISSING,
        EventKind.KNOWLEDGE_SOURCE_REPOINT,
        EventKind.ATTACH,
        EventKind.ATTACH_UPDATE,
        EventKind.DETACH,
        EventKind.ATTACH_PURGE,
        EventKind.ATTACH_MISSING,
        EventKind.ATTACH_RELINK,
        EventKind.PROXY_ADD,
        EventKind.PROXY_SUPERSEDE,
    }
)

#: Event kinds about one attachment record (``memory_id`` is the attachment id).
ATTACHMENT_EVENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.ATTACH,
        EventKind.ATTACH_UPDATE,
        EventKind.DETACH,
        EventKind.ATTACH_PURGE,
        EventKind.ATTACH_MISSING,
        EventKind.ATTACH_RELINK,
    }
)


#: Forward compatibility (v0.4.x). An event whose kind this version does not
#: know is accepted only when it carries this marker, ``details.projection ==
#: "none"``: a promise by the newer writer that the event never changes a
#: memory's committed state. Such an event is kept byte-for-byte (also through
#: log compaction), skipped by ``replay``, listed by ``log verify`` and
#: ``fsck``, and never acted on by ``rollback``. An unknown kind without the
#: marker is refused exactly as before (fail closed). Known kinds are classified
#: by the tables above alone; the marker on them changes nothing. See
#: ``docs/attachments.md`` ("Unknown event kinds").
NON_MEMORY_MARKER_KEY = "projection"
NON_MEMORY_MARKER_VALUE = "none"
_FOREIGN_KIND_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class ForeignEventKind(str):
    """An event kind written by a newer memfw and marked as never affecting memories.

    It is a ``str`` with ``.value`` (like :class:`EventKind`) so code that
    prints a kind works unchanged; it never equals an :class:`EventKind`.
    Only :class:`Event` validation creates one, from a marked event.
    """

    __slots__ = ()

    @property
    def value(self) -> str:
        return str(self)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.is_instance_schema(cls)


_EVENT_KIND_ADAPTER: TypeAdapter[EventKind] = TypeAdapter(EventKind)


def _validate_event_kind(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    if isinstance(value, ForeignEventKind):
        return value
    # Same validation and error as a plain ``EventKind`` field, so an unmarked
    # unknown kind fails exactly as in 0.3.0 (``input_value='<kind>'``).
    return _EVENT_KIND_ADAPTER.validate_python(value)


def foreign_event_kind_counts(events: Iterable[Event]) -> dict[str, int]:
    """Count marked events of kinds this version does not know, by kind name."""
    counts = Counter(str(event.kind) for event in events if event.is_foreign_kind)
    return dict(sorted(counts.items()))


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _same_instant(left: Any, right: Any) -> bool:
    """True when both values are the same moment. Empty values are not moments."""
    if left == right:
        return True
    if left in (None, "") or right in (None, ""):
        return False
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
    use only the canonical names. Instants compare as moments. Supersession
    ids compare as exact strings, so ``2026-11-06`` and ``20261106`` conflict.
    A conflict is two names in this same mapping. A reader may pass
    ``strict=False`` and keep the canonical value.
    """
    folded = dict(data)
    specs = (
        ("valid_until", "valid_to", "instant"),
        ("superseded", "superseded_by", "exact"),
    )
    for alias, canonical, kind in specs:
        if alias not in folded:
            continue
        alias_value = folded.pop(alias)
        if canonical not in folded:
            folded[canonical] = alias_value
            continue
        current = folded[canonical]
        same = _same_instant(current, alias_value) if kind == "instant" else current == alias_value
        if same:
            continue
        if strict:
            raise ValueError(f"{alias} conflicts with {canonical}")
    return folded


VerificationStatus = Literal["confirmed", "unconfirmed", "disputed"]
"""Whether a memory has been checked against transcripts (see ``memfw dream``)."""


class MemoryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    layer: Layer = Layer.CURRENT
    tier: Tier = Tier.HOT
    type: str | None = None
    tenant: str = ""
    related: list[str] = Field(default_factory=list)
    about: list[str] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)
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
    # Verification state (dreams v0.2). Absent in older files: they parse as
    # unconfirmed with no evidence, and render without the keys.
    status: VerificationStatus = "unconfirmed"
    evidence: list[str] = Field(default_factory=list)
    # Epistemic type and causal grade. Absent in older files: they parse with no
    # epistemic type and ``causal=none``, and render without the keys. See
    # ``core.epistemic`` for the write gate and the meaning of each level.
    epistemic: EpistemicType | None = None
    causal: CausalLevel = "none"
    causal_record: CausalRecord | None = None
    limitations: list[str] = Field(default_factory=list)
    # Only for ``type: experience-point``: what a human accepted, in scope.
    experience: ExperiencePoint | None = None
    # Only for ``type: open-question``: what is unknown, which memories
    # conflict on it, and what answered it. Absent in older files; rendered
    # only when present, so their bytes do not change.
    open_question: OpenQuestion | None = None
    # Privacy level (MM0): ``public``, ``shared`` or ``local-private``, read
    # case-insensitively by ``memfw.ports.privacy``. Absent in older files: they
    # parse as ``None`` (exits read it as ``legacy_privacy_default``) and render
    # without the key, so their bytes do not change. An unrecognised value is
    # kept as written and read as ``unknown``, which no clearance admits.
    privacy: str | None = None
    created_at: datetime
    updated_at: datetime
    # Transaction time: when memfw itself wrote this memory, from the server
    # clock (bitemporal with ``valid_from``/``valid_to``). Callers never set it,
    # and updates keep it. Files written before it existed have none; readers
    # then report ``created_at``, which ``add`` always stamped the same way.
    recorded_at: datetime | None = None
    body: str
    _ignored_keys: tuple[str, ...] = PrivateAttr(default=())
    #: Frontmatter keys the source file carried at their default value. A count
    #: of zero never means "the file had none": it means the document did not
    #: come from ``parse`` (a fresh document), so ``render`` applies its normal
    #: omission rule. Parsed documents always record the full observed set.
    _observed_default_fields: tuple[str, ...] = PrivateAttr(default=())
    #: ``"block"`` or ``"invalid"`` when the source file carried an
    #: ``open_question`` key on a memory that is not an open question (ignored).
    _stray_open_question: str | None = PrivateAttr(default=None)
    #: ``(value,)`` of a hand-added top-level ``sensitivity`` key, ``()`` when
    #: the file had none. Not a field: every exit reads it like
    #: ``memfw.ports.privacy`` and ``render`` writes it back, so a rewrite
    #: never drops it (only ``update(sensitivity=...)`` changes it).
    _sensitivity_key: tuple[Any, ...] = PrivateAttr(default=())

    @property
    def ignored_keys(self) -> tuple[str, ...]:
        """Frontmatter keys this version did not know and ignored when parsing."""

        return self._ignored_keys

    @property
    def top_level_sensitivity(self) -> Any:
        """The source file's top-level ``sensitivity`` value, ``None`` when it had none."""

        return self._sensitivity_key[0] if self._sensitivity_key else None

    @property
    def has_top_level_sensitivity(self) -> bool:
        """Whether the source file carried a top-level ``sensitivity`` key."""

        return bool(self._sensitivity_key)

    @property
    def stray_open_question(self) -> str | None:
        """Whether an ignored ``open_question`` key was a well-formed block."""

        return self._stray_open_question

    @property
    def observed_default_fields(self) -> tuple[str, ...]:
        """Optional fields the source file carried at their default value."""

        return self._observed_default_fields

    @model_validator(mode="before")
    @classmethod
    def accept_temporal_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return fold_temporal_aliases(data)
        return data

    @field_validator("evidence", mode="before")
    @classmethod
    def evidence_is_a_list(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("about", "applies_to", "limitations", mode="before")
    @classmethod
    def alias_targets_are_lists(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("privacy", mode="before")
    @classmethod
    def privacy_is_text(cls, value: Any) -> Any:
        # A non-string value (a list, a number) must not make the whole memory
        # unreadable; as text it is still an unknown level (fail closed).
        if value is None or isinstance(value, str):
            return value
        return str(value)

    @field_validator("created_at", "updated_at", "recorded_at", "valid_from", "valid_to")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_serializer(mode="wrap")
    def _omit_absent_open_question(self, handler: SerializerFunctionWrapHandler) -> Any:
        """Leave ``open_question`` out of a dump while it is absent.

        The key arrived with U1. Proposal and agent snapshot hashes, and other
        consumers, take ``model_dump`` of a memory; omitting the empty key keeps
        every memory that is not an open question dumping (and hashing) exactly
        as before.
        """

        data = handler(self)
        if isinstance(data, dict) and data.get("open_question", ...) is None:
            data.pop("open_question")
        # Same for ``privacy`` (MM0): snapshot hashes of memories without the
        # field stay what they were.
        if isinstance(data, dict) and data.get("privacy", ...) is None:
            data.pop("privacy")
        return data


class Event(BaseModel):
    """Metadata-only event. Content is stored in erasable Markdown version objects."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    kind: Annotated[EventKind | ForeignEventKind, WrapValidator(_validate_event_kind)]
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

    @property
    def is_foreign_kind(self) -> bool:
        """Whether this is a marked event of a kind this version does not know."""
        return isinstance(self.kind, ForeignEventKind)

    @model_validator(mode="before")
    @classmethod
    def accept_marked_foreign_kind(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        kind = data.get("kind")
        details = data.get("details")
        if (
            type(kind) is str
            and kind not in EventKind._value2member_map_
            and _FOREIGN_KIND_NAME.fullmatch(kind)
            and isinstance(details, dict)
            and details.get(NON_MEMORY_MARKER_KEY) == NON_MEMORY_MARKER_VALUE
        ):
            return {**data, "kind": ForeignEventKind(kind)}
        return data

    @field_serializer("kind")
    def serialize_kind(
        self, kind: EventKind | ForeignEventKind, info: FieldSerializationInfo
    ) -> EventKind | ForeignEventKind | str:
        return str(kind.value) if info.mode_is_json() else kind

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
