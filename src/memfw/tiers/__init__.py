"""Hot/cold placement decisions driven by idle age, strength, and hot limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from memfw.budget import BudgetLimits, evaluate_budget
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind, Layer, Tier
from memfw.policies import PolicyCandidate


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("last_access must be timezone-aware")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TierDecision:
    memory_id: str
    before: Tier
    after: Tier
    reason: str
    idle_days: float
    strength: float


@dataclass(frozen=True, slots=True)
class TierPlan:
    decisions: tuple[TierDecision, ...]
    limits: BudgetLimits | None
    hot_usage_before: tuple[int, int]
    hot_usage_after: tuple[int, int]

    @property
    def cooled_ids(self) -> tuple[str, ...]:
        return tuple(row.memory_id for row in self.decisions if row.after == Tier.COLD)


class BudgetAwareTierPolicy:
    """Cool idle, weak memories first, then cool eligible memories under pressure."""

    def __init__(self, *, idle_days: float = 30.0, strength_threshold: float = 0.25) -> None:
        if idle_days < 0:
            raise ValueError("idle_days must be non-negative")
        if not 0.0 <= strength_threshold <= 1.0:
            raise ValueError("strength_threshold must be between 0 and 1")
        self.idle_days = idle_days
        self.strength_threshold = strength_threshold

    def choose_tier(
        self,
        *,
        last_access: datetime | str,
        token_cost: int,
        now: datetime,
        strength: float = 0.5,
        current: Tier = Tier.HOT,
        hot_budget_pressure: bool = False,
    ) -> Tier:
        if token_cost < 0:
            raise ValueError("token_cost must be non-negative")
        if not 0.0 <= strength <= 1.0:
            raise ValueError("strength must be between 0 and 1")
        if current == Tier.COLD:
            return Tier.COLD
        point = _utc(now)
        age = max(0.0, (point - _utc(last_access)).total_seconds() / 86400)
        if age >= self.idle_days or strength <= self.strength_threshold:
            return Tier.COLD
        if hot_budget_pressure:
            return Tier.COLD
        return Tier.HOT

    def plan(
        self,
        repository: Any,
        *,
        now: datetime | None = None,
        limits: BudgetLimits | None = None,
        strengths: Mapping[str, float] | None = None,
    ) -> TierPlan:
        point = _utc(now or repository._now())
        access: dict[str, tuple[datetime | None, int]] = {}
        for event in repository.events():
            if event.kind == EventKind.MEMORY_ACCESS:
                previous, count = access.get(event.memory_id, (None, 0))
                access[event.memory_id] = (event.timestamp, count + 1)

        docs = [doc for doc in repository.list() if not doc.is_stub]
        hot_docs = [doc for doc in docs if doc.tier == Tier.HOT]
        usage_before = (len(hot_docs), sum(len(doc.body.encode("utf-8")) for doc in hot_docs))
        eligible: list[tuple[Any, float, float, str]] = []
        for doc in hot_docs:
            if doc.layer == Layer.ANCHOR or doc.anchor:
                continue
            accessed, _count = access.get(doc.id, (None, 0))
            last_access = accessed or doc.updated_at
            age = max(0.0, (point - last_access).total_seconds() / 86400)
            # Access frequency is the fallback strength signal until a persisted
            # rehearsal model is available. Callers may supply measured strengths.
            candidate = PolicyCandidate.from_document(doc)
            strength = (strengths or {}).get(doc.id, candidate.strength)
            if age >= self.idle_days:
                reason = "idle_threshold"
            elif strength <= self.strength_threshold:
                reason = "low_strength"
            else:
                reason = ""
            if reason:
                eligible.append((doc, age, strength, reason))

        selected: dict[str, tuple[Any, float, float, str]] = {row[0].id: row for row in eligible}
        projected = [doc for doc in hot_docs if doc.id not in selected]
        if limits is not None and not evaluate_budget(hot_docs, limits).within_budget:
            choices: list[tuple[Any, float, float]] = []
            for doc in hot_docs:
                if doc.id in selected or doc.layer == Layer.ANCHOR or doc.anchor:
                    continue
                last_access = access.get(doc.id, (None, 0))[0] or doc.updated_at
                age = max(0.0, (point - last_access).total_seconds() / 86400)
                strength = (strengths or {}).get(
                    doc.id, PolicyCandidate.from_document(doc).strength
                )
                choices.append((doc, age, strength))
            choices.sort(key=lambda row: (row[2], -row[1], row[0].id))
            while choices and not evaluate_budget(projected, limits).within_budget:
                doc, age, strength = choices.pop(0)
                selected[doc.id] = (doc, age, strength, "hot_budget_pressure")
                projected = [item for item in projected if item.id != doc.id]

        decisions = tuple(
            TierDecision(doc.id, Tier.HOT, Tier.COLD, reason, age, strength)
            for doc, age, strength, reason in sorted(selected.values(), key=lambda row: row[0].id)
        )
        usage_after = (len(projected), sum(len(doc.body.encode("utf-8")) for doc in projected))
        return TierPlan(decisions, limits, usage_before, usage_after)

    def apply(self, repository: Any, plan: TierPlan, *, dry_run: bool = True) -> TierPlan:
        if dry_run:
            return plan
        with repository._locked():
            return self._apply_locked(repository, plan)

    def _apply_locked(self, repository: Any, plan: TierPlan) -> TierPlan:
        cooled: list[str] = []
        current = {doc.id: doc for doc in repository.list()}
        for decision in plan.decisions:
            doc = current.get(decision.memory_id)
            if doc is None or doc.tier != decision.before:
                continue
            if doc.layer == Layer.ANCHOR or doc.anchor:
                raise IntegrityError(f"Refusing to cool protected anchor: {doc.id}")
            repository.update(doc.id, actor="mechanism", tier=decision.after)
            repository.capture_event(
                EventKind.FORGET,
                actor="mechanism",
                details={
                    "memory_id": doc.id,
                    "operation": "cool",
                    "policy": "tier_policy",
                    "reason": decision.reason,
                },
            )
            cooled.append(doc.id)
        if cooled:
            tail = repository.events()[-1].event_hash if repository.events() else ""
            hashes = "\n".join(
                sha256(repository._current_path(memory_id).read_bytes()).hexdigest()
                for memory_id in sorted(cooled)
            )
            archive_id = (
                "tier-"
                + sha256(
                    ("\n".join(sorted(cooled)) + "\n" + hashes + "\n" + tail).encode("utf-8")
                ).hexdigest()[:16]
            )
            repository.cold_archive(archive_id, actor="mechanism", memory_ids=set(cooled))
        return plan


class DefaultTierPolicy(BudgetAwareTierPolicy):
    """Compatibility name for the original tier policy interface."""


TierPolicy = BudgetAwareTierPolicy

__all__ = ["BudgetAwareTierPolicy", "DefaultTierPolicy", "TierDecision", "TierPlan", "TierPolicy"]
