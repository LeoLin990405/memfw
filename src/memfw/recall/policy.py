"""Kernel filter rules (R6 §5.4). Missing or unreadable metadata fails closed.

``evaluate`` decides one memory at a time and returns a drop reason or the
labels to attach. Reasons are fixed codes, counted in metrics; they never
carry query text or memory content.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Literal

#: Privacy classes, from least to most restricted.
PrivacyClass = Literal["none", "local-private", "unknown"]
_PRIVACY_ORDER: dict[str, int] = {"none": 0, "local-private": 1, "unknown": 2}
_PRIVACY_NONE = frozenset({"", "none", "null", "public", "fleet", "shared"})
KNOWN_PRIVACY: frozenset[str] = frozenset(_PRIVACY_ORDER)
#: Classes a caller can be cleared for. ``unknown`` is never grantable: an
#: unrecognised privacy value is always dropped (design decision, R6 §3.8).
GRANTABLE_PRIVACY: frozenset[str] = frozenset({"none", "local-private"})
#: Lifecycle values that mean "in force". Anything else that is not
#: ``retired``/``superseded`` (e.g. ``archived``, ``deleted``) is dropped.
ACTIVE_LIFECYCLES: frozenset[str] = frozenset({"", "active"})

KNOWN_STATUSES: frozenset[str] = frozenset({"confirmed", "unconfirmed", "disputed"})

#: Every drop reason the filter can report.
DROP_REASONS: tuple[str, ...] = (
    "unresolved",
    "missing_meta",
    "tenant",
    "privacy",
    "source",
    "status",
    "retired",
    "superseded",
    "lifecycle",
    "id_mismatch",
    "expired",
    "expiry_unparsable",
    "not_yet_valid",
    "archived",
    "unjoined",
)

DEFAULT_CLEARANCE: frozenset[str] = frozenset({"none"})

TemporalValue = datetime | date | str | None


def parse_clearance(value: str | Iterable[str] | None) -> frozenset[str]:
    """Caller privacy clearance. Unset, empty or malformed → ``{"none"}``.

    A string is comma separated. Any token other than ``none`` and
    ``local-private`` (including ``unknown``) makes the whole value
    malformed, so a typo can never widen access.
    """

    if value is None:
        return DEFAULT_CLEARANCE
    tokens = value.split(",") if isinstance(value, str) else list(value)
    cleaned = {str(token).strip() for token in tokens if str(token).strip()}
    if not cleaned or not cleaned <= GRANTABLE_PRIVACY:
        return DEFAULT_CLEARANCE
    return frozenset(cleaned)


def classify_privacy(raw: str | None) -> PrivacyClass:
    """Map a raw privacy value to a class (§3.8). Anything unrecognised is ``unknown``."""

    if raw is None:
        return "none"
    value = str(raw).strip().lower()
    if value in _PRIVACY_NONE:
        return "none"
    if value == "local-private":
        return "local-private"
    return "unknown"


@dataclass(frozen=True, slots=True)
class MemoryMeta:
    """The metadata the filter reads for one memory.

    A resolver fills this from the vault, plus the meta-index's lifecycle fields
    when the document joins one (``memfw.recall.builtin.VaultIdResolver``).
    Temporal fields keep their raw form so an unparsable value can be told
    apart from an absent one.
    """

    memory_id: str
    tenant: str
    source: str = ""
    privacy: str | None = None
    sensitivity_private: bool = False
    #: Class derived from ``sensitivity:*`` tags (``none``/``local-private``/
    #: ``unknown``). ``None`` means "not computed": fall back to
    #: ``sensitivity_private``, so callers that only set the bool keep working.
    sensitivity_class: PrivacyClass | None = None
    status: str | None = "unconfirmed"
    lifecycle: str | None = None
    superseded_by: str | None = None
    valid_from: TemporalValue = None
    valid_to: TemporalValue = None
    valid_until: TemporalValue = None
    created_at: TemporalValue = None
    tier: str = "hot"
    is_stub: bool = False
    layer: str = "current"
    version: int | None = None
    #: The meta-index join (§3.8) for a document that carries a ``fleet_path``
    #: tag: ``""`` no join was attempted (no tag, or no meta-index), ``"ok"``
    #: joined (its lifecycle fields are merged above), ``"unjoined"`` the join
    #: failed or the meta-index could not be read. Only the strict policy
    #: reads it: ``unjoined`` is dropped there, whatever ``include_retired``.
    lifecycle_join: Literal["", "ok", "unjoined"] = ""
    #: ``valid_to`` from the meta-index item (the vault's is ``valid_to``).
    #: Strict only, like ``valid_until``: the index clause never sees it.
    meta_valid_to: TemporalValue = None

    def privacy_class(self) -> tuple[PrivacyClass, bool]:
        """Strictest class of the two sources, and whether they disagreed."""

        declared = classify_privacy(self.privacy)
        tagged: PrivacyClass
        if self.sensitivity_class is not None:
            tagged = self.sensitivity_class
            floor = _PRIVACY_ORDER["local-private"]
            if self.sensitivity_private and _PRIVACY_ORDER[tagged] < floor:
                tagged = "local-private"
        else:
            tagged = "local-private" if self.sensitivity_private else "none"
        if self.privacy is None:
            return tagged, False
        strict = declared if _PRIVACY_ORDER[declared] >= _PRIVACY_ORDER[tagged] else tagged
        return strict, declared != tagged


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    """Who is asking and what they may see.

    Defaults are the strict two-stage rules. ``compat_fts`` builds the policy
    that applies exactly the filters today's ``mode=fts`` path applies.
    """

    tenant: str = ""
    privacy_clearance: frozenset[str] = DEFAULT_CLEARANCE
    allowed_sources: tuple[str, ...] = ()
    source: str = ""
    as_of: datetime | None = None
    include_history: bool = False
    include_retired: bool = False
    excluded_statuses: frozenset[str] = frozenset()
    drop_archived: bool = True
    check_privacy: bool = True
    check_status: bool = True
    temporal_mode: Literal["strict", "index"] = "strict"
    disputed_weight: float = 0.5
    status_weights: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant, str):
            raise ValueError("tenant must be a string")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        if not 0.0 <= self.disputed_weight <= 1.0:
            raise ValueError("disputed_weight must be within [0, 1]")
        object.__setattr__(self, "privacy_clearance", frozenset(self.privacy_clearance))
        object.__setattr__(self, "status_weights", dict(self.status_weights))

    @classmethod
    def compat_fts(
        cls,
        *,
        tenant: str,
        allowed_sources: tuple[str, ...] = (),
        source: str = "",
        as_of: datetime | None = None,
        include_history: bool = False,
    ) -> FilterPolicy:
        """Only the filters the current FTS path applies: tenant, source, temporal.

        For the "default byte-identical" wiring: no privacy, status, lifecycle
        or archive rule is added, and the temporal rule is the index's own
        clause (``temporal_mode="index"``).
        """

        return cls(
            tenant=tenant,
            privacy_clearance=GRANTABLE_PRIVACY,
            allowed_sources=allowed_sources,
            source=source,
            as_of=as_of,
            include_history=include_history,
            drop_archived=False,
            check_privacy=False,
            check_status=False,
            temporal_mode="index",
            disputed_weight=1.0,
        )

    def with_tenant(self, tenant: str) -> FilterPolicy:
        return replace(self, tenant=tenant)


@dataclass(frozen=True, slots=True)
class Verdict:
    """``reason`` is set when dropped; ``labels``/``weight`` apply when kept."""

    reason: str | None
    labels: tuple[str, ...] = ()
    weight: float = 1.0
    privacy_mismatch: bool = False

    @property
    def kept(self) -> bool:
        return self.reason is None


class _Unparsable(ValueError):
    pass


def _as_datetime(value: TemporalValue) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise _Unparsable(value) from exc
    else:
        raise _Unparsable(repr(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def evaluate(meta: MemoryMeta | None, policy: FilterPolicy, *, now: datetime) -> Verdict:
    """Apply §5.4 to one memory. ``None`` metadata always fails closed."""

    if meta is None:
        return Verdict("missing_meta")
    if not isinstance(meta.tenant, str) or meta.tenant != policy.tenant:
        return Verdict("tenant")
    mismatch = False
    labels: list[str] = []
    if policy.check_privacy:
        privacy, mismatch = meta.privacy_class()
        if privacy == "unknown" or privacy not in policy.privacy_clearance:
            return Verdict("privacy", privacy_mismatch=mismatch)
        if privacy != "none":
            labels.append(f"privacy:{privacy}")
    if policy.source and meta.source != policy.source:
        return Verdict("source", privacy_mismatch=mismatch)
    if policy.allowed_sources and meta.source not in policy.allowed_sources:
        return Verdict("source", privacy_mismatch=mismatch)
    weight = 1.0
    if policy.check_status or policy.excluded_statuses:
        status = meta.status
        if status is None or status not in KNOWN_STATUSES:
            return Verdict("status", privacy_mismatch=mismatch)
        if status in policy.excluded_statuses:
            return Verdict("status", privacy_mismatch=mismatch)
    if meta.status == "disputed":
        labels.append("disputed")
        weight *= policy.disputed_weight
    if meta.status is not None and meta.status in policy.status_weights:
        weight *= max(0.0, min(1.0, float(policy.status_weights[meta.status])))
    if policy.temporal_mode == "index":
        reason = _index_temporal(meta, policy)
    else:
        reason = _strict_lifecycle(meta, policy, now, labels)
    if reason is not None:
        return Verdict(reason, privacy_mismatch=mismatch)
    if policy.drop_archived and (meta.is_stub or meta.tier == "cold"):
        return Verdict("archived", privacy_mismatch=mismatch)
    if meta.layer == "anchor":
        labels.append("anchor")
    return Verdict(None, tuple(labels), weight, mismatch)


def _lenient(value: TemporalValue) -> datetime | None:
    try:
        return _as_datetime(value)
    except _Unparsable:
        return None


def _index_temporal(meta: MemoryMeta, policy: FilterPolicy) -> str | None:
    """The index's temporal clause, as ``search_vault`` applies it.

    The index never fails on an odd ``valid_from``/``created_at`` (it compares
    strings), so neither does this: an unparsable start is treated as absent.
    Without ``as_of`` any non-empty ``valid_to`` hides the memory, parsable
    or not. ``valid_until`` is not part of the index clause.
    """

    if policy.include_history:
        return None
    if policy.as_of is None:
        if meta.superseded_by:
            return "superseded"
        if meta.valid_to not in (None, ""):
            return "expired"
        return None
    start = _lenient(meta.valid_from) or _lenient(meta.created_at)
    if start is not None and start > policy.as_of:
        return "not_yet_valid"
    try:
        valid_to = _as_datetime(meta.valid_to)
    except _Unparsable:
        return "expiry_unparsable"
    if valid_to is not None and valid_to <= policy.as_of:
        return "expired"
    return None


def _strict_lifecycle(
    meta: MemoryMeta, policy: FilterPolicy, now: datetime, labels: list[str]
) -> str | None:
    """§5.4: lifecycle, supersession and validity. Unparsable dates fail closed.

    A failed meta-index join (``lifecycle_join == "unjoined"``) is dropped
    first: its lifecycle is unknown.

    ``include_retired`` keeps retired/superseded/expired memories, labelled.
    With ``as_of`` a superseded memory is judged by its validity window at
    that instant instead of being dropped for being superseded today.
    """

    if meta.lifecycle_join == "unjoined":
        return "unjoined"
    try:
        valid_from = _as_datetime(meta.valid_from)
        created_at = _as_datetime(meta.created_at)
        valid_to = _as_datetime(meta.valid_to)
        valid_until = _as_datetime(meta.valid_until)
        meta_valid_to = _as_datetime(meta.meta_valid_to)
    except _Unparsable:
        return "expiry_unparsable"
    keep_old = policy.include_retired or policy.include_history
    lifecycle = str(meta.lifecycle or "").strip().lower()
    if lifecycle == "retired":
        if not keep_old:
            return "retired"
        labels.append("retired")
    elif lifecycle == "superseded" or meta.superseded_by:
        if policy.as_of is None and not keep_old:
            return "superseded"
        labels.append("superseded")
    elif lifecycle not in ACTIVE_LIFECYCLES:
        return "lifecycle"
    if policy.include_history:
        return None
    ends = [item for item in (valid_to, valid_until, meta_valid_to) if item is not None]
    end = min(ends) if ends else None
    if policy.as_of is not None:
        start = valid_from or created_at
        if start is not None and start > policy.as_of:
            return "not_yet_valid"
        if end is not None and end <= policy.as_of:
            return "expired"
        return None
    if valid_from is not None and valid_from > now:
        return "not_yet_valid"
    if end is not None and end <= now:
        if not policy.include_retired:
            return "expired"
        labels.append("expired")
    return None


#: MM0 privacy levels (``memfw.ports.privacy.PRIVACY_LEVELS``) → recall classes.
#: Anything else is ``unknown`` and never admitted.
_SEGMENT_PRIVACY: dict[str, PrivacyClass] = {
    "public": "none",
    "shared": "none",
    "local-private": "local-private",
}


def segment_verdict(*, source: str, tenant: str, privacy: str, policy: FilterPolicy) -> Verdict:
    """The filter for a non-memory segment (a note section, later an attachment).

    A segment has no status, lifecycle or validity; its resolver has already
    applied its own clearance. Here, always (also under ``compat_fts``, whose
    clearance admits every grantable class): the tenant must match, the MM0
    ``privacy`` level must map to a class in ``privacy_clearance`` (an
    unrecognised level never does), and a policy restricted to memory sources
    (``source`` or ``allowed_sources``) keeps a segment only when it names the
    segment's own ``source``.
    """

    if not isinstance(tenant, str) or tenant != policy.tenant:
        return Verdict("tenant")
    privacy_class = _SEGMENT_PRIVACY.get(privacy, "unknown")
    if privacy_class == "unknown" or privacy_class not in policy.privacy_clearance:
        return Verdict("privacy")
    if policy.source and source != policy.source:
        return Verdict("source")
    if policy.allowed_sources and source not in policy.allowed_sources:
        return Verdict("source")
    labels = (f"privacy:{privacy_class}",) if privacy_class != "none" else ()
    return Verdict(None, labels)


__all__ = [
    "DEFAULT_CLEARANCE",
    "ACTIVE_LIFECYCLES",
    "DROP_REASONS",
    "GRANTABLE_PRIVACY",
    "KNOWN_PRIVACY",
    "KNOWN_STATUSES",
    "FilterPolicy",
    "MemoryMeta",
    "PrivacyClass",
    "Verdict",
    "classify_privacy",
    "evaluate",
    "parse_clearance",
    "segment_verdict",
]
