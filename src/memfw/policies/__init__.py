"""Pure, deterministic byte and count budget selectors."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from memfw.budget import BudgetLimits, BudgetUsage, evaluate_budget


class PolicyName(StrEnum):
    FIFO = "fifo"
    LRU = "lru"
    DECAY = "decay"
    SALIENCE = "salience"
    CONSOLIDATE_THEN_FORGET = "consolidate_then_forget"


class DecisionAction(StrEnum):
    RETAIN = "retain"
    FORGET = "forget"


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """Immutable policy view of a memory document and optional usage metadata."""

    id: str
    body: str
    created_at: datetime
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int = 0
    reference_count: int = 0
    strength: float = 0.5
    keyness: float = 0.0
    manually_marked: bool = False
    last_access_interval_days: float | None = None
    group_key: str | None = None
    layer: str = "current"
    anchor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("candidate id must not be empty")
        if self.access_count < 0 or self.reference_count < 0:
            raise ValueError("access and reference counts must be non-negative")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength must be between 0 and 1")
        if not 0.0 <= self.keyness <= 1.0:
            raise ValueError("keyness must be between 0 and 1")
        if self.last_access_interval_days is not None and self.last_access_interval_days < 0:
            raise ValueError("last_access_interval_days must be non-negative")
        _utc(self.created_at)
        if self.updated_at is not None:
            _utc(self.updated_at)
        if self.last_accessed_at is not None:
            _utc(self.last_accessed_at)

    @property
    def is_anchor(self) -> bool:
        # A non-empty legacy anchor is a user pin, even if layer was not changed.
        return self.layer == "anchor" or bool(self.anchor)

    @classmethod
    def from_document(cls, document: Any) -> PolicyCandidate:
        """Adapt a MemoryDocument or mapping without mutating it."""

        values = document if isinstance(document, dict) else None

        def read(name: str, default: Any = None) -> Any:
            return (
                values.get(name, default)
                if values is not None
                else getattr(document, name, default)
            )

        metadata = read("metadata", {}) or {}
        if not isinstance(metadata, dict):
            dump = getattr(metadata, "model_dump", None)
            metadata = dump() if dump else {}

        def meta(name: str, default: Any = None) -> Any:
            direct = read(name, None)
            return direct if direct is not None else metadata.get(name, default)

        layer = read("layer", "current")
        return cls(
            id=str(read("id")),
            body=str(read("body", "")),
            created_at=_datetime(read("created_at")),
            updated_at=_optional_datetime(read("updated_at")),
            last_accessed_at=_optional_datetime(meta("last_accessed_at")),
            access_count=int(meta("access_count", 0)),
            reference_count=int(meta("reference_count", 0)),
            strength=float(meta("strength", 0.5)),
            keyness=float(meta("keyness", 0.0)),
            manually_marked=bool(meta("manually_marked", meta("manual_mark", False))),
            last_access_interval_days=(
                None
                if meta("last_access_interval_days") is None
                else float(meta("last_access_interval_days"))
            ),
            group_key=meta("group_key", meta("consolidation_group")),
            layer=str(getattr(layer, "value", layer)),
            anchor=read("anchor"),
            metadata=dict(metadata),
        )

    @property
    def byte_size(self) -> int:
        return len(self.body.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class Decision:
    memory_id: str
    action: DecisionAction
    reason: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class ConsolidationProposal:
    representative_id: str
    duplicate_ids: tuple[str, ...]
    match_basis: str
    reason: str


@dataclass(frozen=True, slots=True)
class PolicyPlan:
    policy: PolicyName
    decisions: tuple[Decision, ...]
    consolidation_proposals: tuple[ConsolidationProposal, ...]
    retained_ids: tuple[str, ...]
    forgotten_ids: tuple[str, ...]
    usage: BudgetUsage
    limits: BudgetLimits
    satisfied: bool


class Policy(Protocol):
    name: PolicyName

    def plan(
        self,
        candidates: Iterable[PolicyCandidate | Any],
        budget: BudgetLimits,
        *,
        now: datetime,
    ) -> PolicyPlan: ...


RankedItem = tuple[PolicyCandidate, str, float | None]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError("created_at must be a timezone-aware datetime")


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _datetime(value)


def _normalize(candidates: Iterable[PolicyCandidate | Any]) -> tuple[PolicyCandidate, ...]:
    items = tuple(
        item if isinstance(item, PolicyCandidate) else PolicyCandidate.from_document(item)
        for item in candidates
    )
    if len({item.id for item in items}) != len(items):
        raise ValueError("candidate ids must be unique")
    return tuple(sorted(items, key=lambda item: item.id))


def _salience(candidate: PolicyCandidate) -> float:
    return (
        math.log1p(candidate.access_count)
        + 1.5 * math.log1p(candidate.reference_count)
        + 2.0 * candidate.keyness
        + (3.0 if candidate.manually_marked else 0.0)
    )


def _knowledge_protection(candidate: PolicyCandidate) -> int:
    """Whether a configured knowledge-card citation protects this candidate."""
    return int(
        bool(candidate.metadata.get("knowledge_reference_count", 0))
        and candidate.metadata.get("knowledge_protection_boost", 0) > 0
    )


def _fits(items: Iterable[PolicyCandidate], budget: BudgetLimits) -> bool:
    return evaluate_budget(items, budget).within_budget


class _BasePolicy:
    name: PolicyName

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        raise NotImplementedError

    def _proposals(
        self,
        candidates: tuple[PolicyCandidate, ...],
        forgotten_ids: set[str],
    ) -> tuple[ConsolidationProposal, ...]:
        return ()

    def plan(
        self,
        candidates: Iterable[PolicyCandidate | Any],
        budget: BudgetLimits,
        *,
        now: datetime,
    ) -> PolicyPlan:
        current = _utc(now)
        items = _normalize(candidates)
        forgotten: list[PolicyCandidate] = []
        ranked = self._rank(items, current) if not _fits(items, budget) else []
        # Knowledge citations add an opt-in, strategy-independent protection
        # tier. Preserve each policy's ordering within both tiers. Anchors
        # remain absolute via the existing skip below.
        if ranked:
            prioritized = sorted(
                enumerate(ranked),
                key=lambda indexed: (
                    _knowledge_protection(indexed[1][0]),
                    indexed[0],
                ),
            )
            ranked = [entry for _, entry in prioritized]

        # An already-fitting set creates no churn and no speculative proposals.
        if not _fits(items, budget):
            retained = list(items)
            for candidate, _, _ in ranked:
                if _fits(retained, budget):
                    break
                if candidate.is_anchor:
                    continue
                retained.remove(candidate)
                forgotten.append(candidate)

        forgotten_ids = {item.id for item in forgotten}
        retained_ids = tuple(item.id for item in items if item.id not in forgotten_ids)
        kept = tuple(item for item in items if item.id in retained_ids)
        evaluation = evaluate_budget(kept, budget)
        reason_map = {
            item.id: (reason, score) for item, reason, score in ranked if item.id in forgotten_ids
        }
        decisions = tuple(
            Decision(
                memory_id=item.id,
                action=(
                    DecisionAction.FORGET if item.id in forgotten_ids else DecisionAction.RETAIN
                ),
                reason=(
                    reason_map[item.id][0]
                    if item.id in forgotten_ids
                    else "protected_anchor"
                    if item.is_anchor
                    else "budget_fits_or_candidate_not_selected"
                ),
                score=reason_map[item.id][1] if item.id in forgotten_ids else None,
            )
            for item in items
        )
        return PolicyPlan(
            policy=self.name,
            decisions=decisions,
            consolidation_proposals=self._proposals(items, forgotten_ids),
            retained_ids=retained_ids,
            forgotten_ids=tuple(item.id for item in forgotten),
            usage=evaluation.usage,
            limits=budget,
            satisfied=evaluation.within_budget,
        )


class FIFOPolicy(_BasePolicy):
    name = PolicyName.FIFO

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        ordered = sorted(candidates, key=lambda item: (_utc(item.created_at), item.id))
        return [
            (
                item,
                f"budget_pressure; fifo_oldest_created_at={_utc(item.created_at).isoformat()}",
                None,
            )
            for item in ordered
        ]


class LRUPolicy(_BasePolicy):
    name = PolicyName.LRU

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        ordered = sorted(
            candidates,
            key=lambda item: (_utc(item.last_accessed_at or item.created_at), item.id),
        )
        ranked: list[RankedItem] = []
        for item in ordered:
            accessed = item.last_accessed_at or item.created_at
            basis = "last_accessed_at" if item.last_accessed_at else "created_at_fallback"
            ranked.append(
                (item, f"budget_pressure; lru_oldest_{basis}={_utc(accessed).isoformat()}", None)
            )
        return ranked


class DecayPolicy(_BasePolicy):
    """Exponential recency and rehearsal score with transparent threshold fallback."""

    name = PolicyName.DECAY

    def __init__(
        self,
        *,
        half_life_days: float = 30.0,
        score_threshold: float = 0.20,
        allow_budget_pressure_fallback: bool = True,
    ) -> None:
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if score_threshold < 0:
            raise ValueError("score_threshold must be non-negative")
        self.half_life_days = half_life_days
        self.score_threshold = score_threshold
        self.allow_budget_pressure_fallback = allow_budget_pressure_fallback

    def score(self, candidate: PolicyCandidate, now: datetime) -> float:
        reference = candidate.last_accessed_at or candidate.created_at
        age_days = max(0.0, (now - _utc(reference)).total_seconds() / 86400.0)
        interval_days = candidate.last_access_interval_days or 0.0
        rehearsal = math.log2(2 + candidate.access_count + candidate.reference_count)
        spacing = 1.0 + math.log1p(interval_days) / 5.0
        return (
            candidate.strength
            * math.exp(-math.log(2) * age_days / self.half_life_days)
            * rehearsal
            * spacing
            * (1.0 + 0.5 * candidate.keyness)
        )

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        scored = [(item, self.score(item, now)) for item in candidates if not item.is_anchor]
        scored.sort(key=lambda pair: (pair[1], pair[0].id))
        low = [pair for pair in scored if pair[1] <= self.score_threshold]
        above = [pair for pair in scored if pair[1] > self.score_threshold]
        ordered = low + (above if self.allow_budget_pressure_fallback else [])
        result: list[RankedItem] = []
        for item, score in ordered:
            if score <= self.score_threshold:
                rule = f"score<={self.score_threshold:g}"
            else:
                rule = "fallback=budget_pressure_after_threshold_candidates"
            result.append((item, f"budget_pressure; decay_score={score:.12g}; {rule}", score))
        return result


class SaliencePolicy(_BasePolicy):
    name = PolicyName.SALIENCE

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        ordered = sorted(
            candidates, key=lambda item: (_salience(item), _utc(item.created_at), item.id)
        )
        return [
            (
                item,
                _salience_reason(item),
                _salience(item),
            )
            for item in ordered
        ]


def _salience_reason(candidate: PolicyCandidate) -> str:
    formula = "score=log1p(access_count)+1.5*log1p(reference_count)"
    if candidate.keyness:
        formula += "+2*keyness"
    formula += f"+3*manual_mark={_salience(candidate):.12g}"
    return f"budget_pressure; {formula}"


def _components(
    candidates: tuple[PolicyCandidate, ...],
) -> tuple[tuple[tuple[PolicyCandidate, ...], str], ...]:
    """Connected components linked only by exact body hash or explicit group key."""

    parent = {item.id: item.id for item in candidates}

    def find(identifier: str) -> str:
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            low, high = sorted((left_root, right_root))
            parent[high] = low

    basis_for_pair: dict[frozenset[str], set[str]] = {}
    buckets: dict[tuple[str, str], list[str]] = {}
    for item in candidates:
        body_hash = hashlib.sha256(item.body.encode("utf-8")).hexdigest()
        buckets.setdefault(("body_hash", body_hash), []).append(item.id)
        if item.group_key:
            buckets.setdefault(("group_key", item.group_key), []).append(item.id)
    for (basis, _), identifiers in sorted(buckets.items()):
        for index, left in enumerate(identifiers):
            for right in identifiers[index + 1 :]:
                union(left, right)
                basis_for_pair.setdefault(frozenset((left, right)), set()).add(basis)

    groups: dict[str, list[PolicyCandidate]] = {}
    for item in candidates:
        groups.setdefault(find(item.id), []).append(item)
    result: list[tuple[tuple[PolicyCandidate, ...], str]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = {item.id for item in members}
        bases = {
            basis
            for pair, pair_bases in basis_for_pair.items()
            if pair.issubset(ids)
            for basis in pair_bases
        }
        result.append((tuple(sorted(members, key=lambda item: item.id)), "+".join(sorted(bases))))
    return tuple(sorted(result, key=lambda entry: entry[0][0].id))


class ConsolidateThenForgetPolicy(_BasePolicy):
    name = PolicyName.CONSOLIDATE_THEN_FORGET

    def _rank(self, candidates: tuple[PolicyCandidate, ...], now: datetime) -> list[RankedItem]:
        ranked: list[RankedItem] = []
        for members, component_basis in _components(candidates):
            representative = min(
                members,
                key=lambda item: (
                    -int(item.is_anchor),
                    -_knowledge_protection(item),
                    -_salience(item),
                    -_utc(item.updated_at or item.created_at).timestamp(),
                    item.id,
                ),
            )
            for item in members:
                if item.id == representative.id or item.is_anchor:
                    continue
                reason = (
                    f"budget_pressure; exact_redundancy={component_basis}; "
                    f"representative={representative.id}"
                )
                ranked.append((item, reason, None))
        return sorted(ranked, key=lambda entry: (entry[0].id, entry[1]))

    def _proposals(
        self,
        candidates: tuple[PolicyCandidate, ...],
        forgotten_ids: set[str],
    ) -> tuple[ConsolidationProposal, ...]:
        proposals: list[ConsolidationProposal] = []
        for members, component_basis in _components(candidates):
            representative = min(
                members,
                key=lambda item: (
                    -int(item.is_anchor),
                    -_knowledge_protection(item),
                    -_salience(item),
                    -_utc(item.updated_at or item.created_at).timestamp(),
                    item.id,
                ),
            )
            duplicates = tuple(sorted(item.id for item in members if item.id in forgotten_ids))
            if duplicates:
                proposals.append(
                    ConsolidationProposal(
                        representative_id=representative.id,
                        duplicate_ids=duplicates,
                        match_basis=component_basis,
                        reason="exact-body or explicit-group match; human review required",
                    )
                )
        return tuple(sorted(proposals, key=lambda item: (item.representative_id, item.match_basis)))


def make_policy(name: PolicyName | str) -> Policy:
    selected = PolicyName(name)
    policies: dict[PolicyName, Policy] = {
        PolicyName.FIFO: FIFOPolicy(),
        PolicyName.LRU: LRUPolicy(),
        PolicyName.DECAY: DecayPolicy(),
        PolicyName.SALIENCE: SaliencePolicy(),
        PolicyName.CONSOLIDATE_THEN_FORGET: ConsolidateThenForgetPolicy(),
    }
    return policies[selected]
