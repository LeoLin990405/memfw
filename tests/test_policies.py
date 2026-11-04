from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from memfw.budget import BudgetLimits, evaluate_budget
from memfw.policies import (
    ConsolidateThenForgetPolicy,
    DecayPolicy,
    DecisionAction,
    PolicyCandidate,
    PolicyName,
    PolicyPlan,
    make_policy,
)

NOW = datetime(2026, 2, 1, tzinfo=UTC)
STRATEGIES = tuple(PolicyName)


def candidate(
    identifier: str,
    *,
    body: str = "x",
    created_days_ago: int = 0,
    accessed_days_ago: int | None = None,
    strength: float = 0.5,
    access_count: int = 0,
    reference_count: int = 0,
    keyness: float = 0.0,
    manually_marked: bool = False,
    interval_days: float | None = None,
    layer: str = "current",
    anchor: str | None = None,
    group_key: str | None = None,
) -> PolicyCandidate:
    created = NOW - timedelta(days=created_days_ago)
    return PolicyCandidate(
        id=identifier,
        body=body,
        created_at=created,
        updated_at=created,
        last_accessed_at=(
            None if accessed_days_ago is None else NOW - timedelta(days=accessed_days_ago)
        ),
        strength=strength,
        access_count=access_count,
        reference_count=reference_count,
        keyness=keyness,
        manually_marked=manually_marked,
        last_access_interval_days=interval_days,
        layer=layer,
        anchor=anchor,
        group_key=group_key,
    )


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_each_policy_keeps_all_items_when_budget_fits(strategy: PolicyName) -> None:
    items = [candidate("a", body="雪"), candidate("b", body="雪")]
    plan = make_policy(strategy).plan(items, BudgetLimits(max_bytes=6, max_count=2), now=NOW)

    assert plan.satisfied
    assert plan.forgotten_ids == ()
    assert plan.retained_ids == ("a", "b")
    assert plan.consolidation_proposals == ()
    assert all(item.action == DecisionAction.RETAIN for item in plan.decisions)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_knowledge_citation_priority_applies_to_every_policy(strategy: PolicyName) -> None:
    cited = candidate("cited", body="same", created_days_ago=30)
    ordinary = candidate("ordinary", body="same", created_days_ago=1)
    cited = replace(
        cited,
        metadata={
            "knowledge_reference_count": 1,
            "knowledge_protection_boost": 1.0,
        },
    )
    plan = make_policy(strategy).plan([cited, ordinary], BudgetLimits(max_count=1), now=NOW)

    assert plan.forgotten_ids == ("ordinary",)
    assert plan.retained_ids == ("cited",)


@pytest.mark.parametrize(
    ("strategy", "items", "expected_forgotten", "expected_reason"),
    [
        (
            PolicyName.FIFO,
            [candidate("a", created_days_ago=9), candidate("b", created_days_ago=2)],
            ("a",),
            "budget_pressure; fifo_oldest_created_at=2026-01-23T00:00:00+00:00",
        ),
        (
            PolicyName.LRU,
            [
                candidate("a", accessed_days_ago=10),
                candidate("b", accessed_days_ago=2),
            ],
            ("a",),
            "budget_pressure; lru_oldest_last_accessed_at=2026-01-22T00:00:00+00:00",
        ),
        (
            PolicyName.DECAY,
            [
                candidate("a", created_days_ago=90, strength=0.2),
                candidate("b", created_days_ago=1, strength=0.9),
            ],
            ("a",),
            "budget_pressure; decay_score=0.025; score<=0.2",
        ),
        (
            PolicyName.SALIENCE,
            [candidate("a"), candidate("b", access_count=2, reference_count=2)],
            ("a",),
            "budget_pressure; score=log1p(access_count)+1.5*log1p(reference_count)+3*manual_mark=0",
        ),
        (
            PolicyName.CONSOLIDATE_THEN_FORGET,
            [candidate("a", body="same"), candidate("b", body="same", created_days_ago=1)],
            ("b",),
            "budget_pressure; exact_redundancy=body_hash; representative=a",
        ),
    ],
)
def test_each_policy_forgets_in_expected_order_with_reason(
    strategy: PolicyName,
    items: list[PolicyCandidate],
    expected_forgotten: tuple[str, ...],
    expected_reason: str,
) -> None:
    plan = make_policy(strategy).plan(items, BudgetLimits(max_count=1), now=NOW)

    assert plan.satisfied
    assert plan.forgotten_ids == expected_forgotten
    decision = next(row for row in plan.decisions if row.memory_id == expected_forgotten[0])
    assert decision.action == DecisionAction.FORGET
    assert decision.reason == expected_reason


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_anchor_is_never_forgotten_even_when_it_exceeds_budget(strategy: PolicyName) -> None:
    items = [
        candidate("large-anchor", body="oversized", layer="anchor"),
        candidate("old", created_days_ago=30),
    ]
    plan = make_policy(strategy).plan(items, BudgetLimits(max_bytes=1, max_count=0), now=NOW)

    assert "large-anchor" in plan.retained_ids
    assert "large-anchor" not in plan.forgotten_ids
    assert (
        next(row for row in plan.decisions if row.memory_id == "large-anchor").reason
        == "protected_anchor"
    )
    assert not plan.satisfied


def test_anchor_field_also_pins_without_anchor_layer() -> None:
    pinned = candidate("pinned", anchor="project-alpha")
    plan = make_policy(PolicyName.FIFO).plan(
        [pinned, candidate("ordinary")], BudgetLimits(max_count=0), now=NOW
    )
    assert "pinned" in plan.retained_ids
    assert "pinned" not in plan.forgotten_ids


def test_decay_uses_calculated_score_threshold_and_reports_pressure_fallback() -> None:
    low = candidate("low", created_days_ago=180, strength=0.2)
    high = candidate("high", created_days_ago=0, strength=0.9)
    strict = DecayPolicy(score_threshold=0.2, allow_budget_pressure_fallback=False)
    strict_plan = strict.plan([low, high], BudgetLimits(max_count=0), now=NOW)
    assert strict_plan.forgotten_ids == ("low",)
    assert not strict_plan.satisfied

    fallback_plan = DecayPolicy(score_threshold=0.0).plan(
        [low, high], BudgetLimits(max_count=1), now=NOW
    )
    assert fallback_plan.forgotten_ids == ("low",)
    low_decision = next(row for row in fallback_plan.decisions if row.memory_id == "low")
    assert "fallback=budget_pressure_after_threshold_candidates" in low_decision.reason


def test_consolidation_protects_one_component_representative_and_only_uses_exact_links() -> None:
    items = [
        candidate("anchor", body="same", layer="anchor"),
        candidate("copy", body="same"),
        candidate("grouped", body="different", group_key="g1"),
        candidate("group-copy", body="also-different", group_key="g1"),
        candidate("near", body="same "),
    ]
    plan = ConsolidateThenForgetPolicy().plan(items, BudgetLimits(max_count=3), now=NOW)

    assert plan.retained_ids == ("anchor", "group-copy", "near")
    assert "anchor" in plan.retained_ids
    proposal_pairs = {
        (proposal.representative_id, proposal.duplicate_ids)
        for proposal in plan.consolidation_proposals
    }
    assert ("anchor", ("copy",)) in proposal_pairs
    assert ("group-copy", ("grouped",)) in proposal_pairs
    assert len(plan.forgotten_ids) == 2
    assert plan.satisfied


def test_consolidation_reports_unsatisfied_without_forgetting_representatives() -> None:
    items = [
        candidate("a", body="pair"),
        candidate("b", body="pair", created_days_ago=1),
        candidate("solo", body="unique"),
    ]
    plan = ConsolidateThenForgetPolicy().plan(items, BudgetLimits(max_count=1), now=NOW)

    assert plan.forgotten_ids == ("b",)
    assert "a" in plan.retained_ids
    assert "solo" in plan.retained_ids
    assert not plan.satisfied


def test_same_batch_and_policy_produce_identical_plan_and_byte_usage() -> None:
    items = [candidate("a", body="雪" * 7), candidate("b", body="x" * 3)]
    policy = make_policy(PolicyName.FIFO)
    limits = BudgetLimits(max_bytes=10, max_count=1)
    first = policy.plan(items, limits, now=NOW)
    policy.plan([candidate("other", body="z")], BudgetLimits(max_count=0), now=NOW)
    second = policy.plan(items, limits, now=NOW)

    assert first == second
    assert evaluate_budget([items[0]], limits).usage.bytes == 21
    assert first.usage.bytes == len(items[1].body.encode("utf-8"))


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_each_policy_serializes_identically_for_repeated_same_input(strategy: PolicyName) -> None:
    items = [
        candidate("a", body="same", created_days_ago=10, accessed_days_ago=9),
        candidate(
            "b",
            body="same",
            created_days_ago=4,
            accessed_days_ago=1,
            access_count=3,
            reference_count=2,
            strength=0.3,
            group_key="pair",
        ),
        candidate("c", body="unique", created_days_ago=2, strength=0.8),
    ]
    policy = make_policy(strategy)
    limits = BudgetLimits(max_bytes=5, max_count=2)
    first = policy.plan(items, limits, now=NOW)
    policy.plan([candidate("other")], BudgetLimits(max_count=0), now=NOW)
    second = policy.plan(items, limits, now=NOW)

    def encoded(plan: PolicyPlan) -> bytes:
        return json.dumps(
            asdict(plan),
            default=lambda value: value.isoformat(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    assert encoded(first) == encoded(second)


def test_decay_score_increases_with_access_reference_and_spacing() -> None:
    base = candidate("base", created_days_ago=20, strength=0.5)
    accessed = candidate("accessed", created_days_ago=20, strength=0.5, access_count=4)
    referenced = candidate("referenced", created_days_ago=20, strength=0.5, reference_count=3)
    spaced = candidate("spaced", created_days_ago=20, strength=0.5, interval_days=12)
    policy = DecayPolicy()

    assert policy.score(accessed, NOW) > policy.score(base, NOW)
    assert policy.score(referenced, NOW) > policy.score(base, NOW)
    assert policy.score(spaced, NOW) > policy.score(base, NOW)


def test_salience_orders_access_references_and_manual_mark() -> None:
    items = [
        candidate("base"),
        candidate("access", access_count=1),
        candidate("reference", reference_count=1),
        candidate("manual", manually_marked=True),
    ]
    plan = make_policy(PolicyName.SALIENCE).plan(items, BudgetLimits(max_count=2), now=NOW)

    assert plan.forgotten_ids == ("base", "access")
