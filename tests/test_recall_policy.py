"""Kernel filter rules: every case where metadata is missing or odd fails closed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from memfw.recall.policy import (
    DEFAULT_CLEARANCE,
    DROP_REASONS,
    FilterPolicy,
    MemoryMeta,
    classify_privacy,
    evaluate,
    parse_clearance,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _meta(**changes: object) -> MemoryMeta:
    base = MemoryMeta(memory_id="m1", tenant="", source="manual")
    return replace(base, **changes)  # type: ignore[arg-type]


def _reason(meta: MemoryMeta | None, policy: FilterPolicy | None = None) -> str | None:
    return evaluate(meta, policy or FilterPolicy(), now=NOW).reason


def test_plain_memory_is_kept_by_default() -> None:
    verdict = evaluate(_meta(), FilterPolicy(), now=NOW)
    assert verdict.kept
    assert verdict.labels == ()
    assert verdict.weight == 1.0


def test_missing_metadata_fails_closed() -> None:
    assert _reason(None) == "missing_meta"


@pytest.mark.parametrize(
    ("doc_tenant", "policy_tenant", "kept"),
    [("", "", True), ("alpha", "alpha", True), ("alpha", "", False), ("", "alpha", False)],
)
def test_tenant_is_exact_including_empty(doc_tenant: str, policy_tenant: str, kept: bool) -> None:
    reason = _reason(_meta(tenant=doc_tenant), FilterPolicy(tenant=policy_tenant))
    assert (reason is None) is kept
    if not kept:
        assert reason == "tenant"


def test_tenant_prefix_or_case_does_not_match() -> None:
    assert _reason(_meta(tenant="Alpha"), FilterPolicy(tenant="alpha")) == "tenant"
    assert _reason(_meta(tenant="alpha-2"), FilterPolicy(tenant="alpha")) == "tenant"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "none"),
        ("", "none"),
        ("none", "none"),
        ("NULL", "none"),
        ("public", "none"),
        ("fleet", "none"),
        ("shared", "none"),
        ("local-private", "local-private"),
        ("Local-Private", "local-private"),
        ("secret", "unknown"),
        ("private", "unknown"),
    ],
)
def test_classify_privacy(raw: str | None, expected: str) -> None:
    assert classify_privacy(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {"none"}),
        ("", {"none"}),
        (" , ", {"none"}),
        ("none,local-private", {"none", "local-private"}),
        ("none, local-private ", {"none", "local-private"}),
        ("none,lcoal-private", {"none"}),  # typo never widens access
        ("everything", {"none"}),
        (["local-private"], {"local-private"}),
    ],
)
def test_parse_clearance_defaults_to_none(raw: object, expected: set[str]) -> None:
    assert parse_clearance(raw) == frozenset(expected)  # type: ignore[arg-type]


def test_default_clearance_drops_private_memories() -> None:
    assert FilterPolicy().privacy_clearance == DEFAULT_CLEARANCE
    assert _reason(_meta(sensitivity_private=True)) == "privacy"
    assert _reason(_meta(privacy="local-private")) == "privacy"


def test_explicit_clearance_keeps_private_and_labels_it() -> None:
    policy = FilterPolicy(privacy_clearance=parse_clearance("none,local-private"))
    verdict = evaluate(_meta(sensitivity_private=True), policy, now=NOW)
    assert verdict.kept
    assert "privacy:local-private" in verdict.labels


def test_unknown_privacy_is_always_dropped() -> None:
    wide = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    assert _reason(_meta(privacy="team-only"), wide) == "privacy"
    # Even a hand-built clearance that lists it cannot let it through.
    listed = FilterPolicy(privacy_clearance=frozenset({"none", "unknown"}))
    assert _reason(_meta(privacy="team-only"), listed) == "privacy"
    assert parse_clearance("none,unknown") == frozenset({"none"})
    assert parse_clearance("unknown") == frozenset({"none"})
    assert parse_clearance(["none", "local-private", "unknown"]) == frozenset({"none"})


def test_privacy_sources_disagreeing_take_the_stricter() -> None:
    meta = _meta(privacy="none", sensitivity_private=True)
    assert meta.privacy_class() == ("local-private", True)
    verdict = evaluate(meta, FilterPolicy(), now=NOW)
    assert verdict.reason == "privacy"
    assert verdict.privacy_mismatch
    # Declared private, tag absent: still private, still a mismatch.
    assert _meta(privacy="local-private").privacy_class() == ("local-private", True)
    # Agreement is not a mismatch.
    agreeing = _meta(privacy="local-private", sensitivity_private=True)
    assert agreeing.privacy_class() == ("local-private", False)


def test_source_filters() -> None:
    assert _reason(_meta(source="chat"), FilterPolicy(source="manual")) == "source"
    allowed = FilterPolicy(allowed_sources=("manual",))
    assert _reason(_meta(source="chat"), allowed) == "source"
    assert _reason(_meta(source="manual"), allowed) is None


@pytest.mark.parametrize("status", [None, "", "weird"])
def test_unknown_status_fails_closed(status: str | None) -> None:
    assert _reason(_meta(status=status)) == "status"


def test_excluded_status_and_disputed_downweight() -> None:
    excluded = FilterPolicy(excluded_statuses=frozenset({"disputed"}))
    assert _reason(_meta(status="disputed"), excluded) == "status"
    verdict = evaluate(_meta(status="disputed"), FilterPolicy(), now=NOW)
    assert verdict.kept
    assert "disputed" in verdict.labels
    assert verdict.weight == 0.5
    weighted = FilterPolicy(status_weights={"unconfirmed": 0.25})
    assert evaluate(_meta(), weighted, now=NOW).weight == 0.25


def test_retired_and_superseded_are_dropped_unless_included() -> None:
    assert _reason(_meta(lifecycle="retired")) == "retired"
    assert _reason(_meta(lifecycle="superseded")) == "superseded"
    assert _reason(_meta(superseded_by="m2")) == "superseded"
    keep = FilterPolicy(include_retired=True)
    retired = evaluate(_meta(lifecycle="retired"), keep, now=NOW)
    assert retired.kept and "retired" in retired.labels
    superseded = evaluate(_meta(superseded_by="m2"), keep, now=NOW)
    assert superseded.kept and "superseded" in superseded.labels


def test_include_retired_never_widens_privacy_or_tenant() -> None:
    keep = FilterPolicy(include_retired=True)
    assert _reason(_meta(lifecycle="retired", sensitivity_private=True), keep) == "privacy"
    assert _reason(_meta(lifecycle="retired", tenant="other"), keep) == "tenant"


def test_expired_by_either_valid_to_or_valid_until() -> None:
    past = NOW - timedelta(days=1)
    future = NOW + timedelta(days=1)
    assert _reason(_meta(valid_to=past)) == "expired"
    assert _reason(_meta(valid_until=past.isoformat())) == "expired"
    assert _reason(_meta(valid_to=future, valid_until=past)) == "expired"  # earlier wins
    assert _reason(_meta(valid_to=future)) is None
    assert _reason(_meta(valid_until=date(2020, 1, 1))) == "expired"


@pytest.mark.parametrize("field", ["valid_to", "valid_until", "valid_from"])
def test_unparsable_dates_fail_closed(field: str) -> None:
    assert _reason(_meta(**{field: "next tuesday"})) == "expiry_unparsable"


def test_unparsable_dates_fail_closed_even_with_history() -> None:
    assert _reason(_meta(valid_to="??"), FilterPolicy(include_history=True)) == "expiry_unparsable"


def test_as_of_window() -> None:
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    policy = FilterPolicy(as_of=as_of)
    assert _reason(_meta(valid_from=as_of + timedelta(days=1)), policy) == "not_yet_valid"
    assert _reason(_meta(valid_to=as_of), policy) == "expired"
    assert _reason(_meta(valid_to=as_of + timedelta(days=1)), policy) is None
    # Expired today but valid at as_of: kept for the evaluation view.
    assert _reason(_meta(valid_to="2026-06-01T00:00:00Z"), policy) is None


def test_as_of_requires_a_timezone() -> None:
    with pytest.raises(ValueError):
        FilterPolicy(as_of=datetime(2026, 1, 1))


def test_archived_and_stub_are_dropped() -> None:
    assert _reason(_meta(tier="cold")) == "archived"
    assert _reason(_meta(is_stub=True)) == "archived"
    assert _reason(_meta(tier="cold"), FilterPolicy(drop_archived=False)) is None


def test_anchor_is_labelled() -> None:
    assert "anchor" in evaluate(_meta(layer="anchor"), FilterPolicy(), now=NOW).labels


def test_compat_fts_applies_only_what_the_index_applies() -> None:
    policy = FilterPolicy.compat_fts(tenant="alpha")
    meta = _meta(tenant="alpha", sensitivity_private=True, status="weird", tier="cold")
    assert evaluate(meta, policy, now=NOW).kept
    # Any valid_to hides a memory without as_of, even in the future.
    assert _reason(_meta(tenant="alpha", valid_to=NOW + timedelta(days=9)), policy) == "expired"
    assert _reason(_meta(tenant="alpha", superseded_by="x"), policy) == "superseded"
    assert _reason(_meta(tenant="beta"), policy) == "tenant"
    # With as_of the index ignores superseded_by.
    as_of = FilterPolicy.compat_fts(tenant="alpha", as_of=NOW)
    assert _reason(_meta(tenant="alpha", superseded_by="x"), as_of) is None
    history = FilterPolicy.compat_fts(tenant="alpha", include_history=True)
    assert _reason(_meta(tenant="alpha", valid_to=NOW - timedelta(days=1)), history) is None


def test_every_reason_is_declared() -> None:
    reasons = {
        _reason(None),
        _reason(_meta(tenant="x")),
        _reason(_meta(sensitivity_private=True)),
        _reason(_meta(source="x"), FilterPolicy(source="y")),
        _reason(_meta(status=None)),
        _reason(_meta(lifecycle="retired")),
        _reason(_meta(superseded_by="x")),
        _reason(_meta(valid_to=NOW - timedelta(seconds=1))),
        _reason(_meta(valid_to="bad")),
        _reason(_meta(valid_from=NOW + timedelta(days=1)), FilterPolicy(as_of=NOW)),
        _reason(_meta(tier="cold")),
        _reason(_meta(lifecycle="archived")),
        _reason(_meta(lifecycle_join="unjoined")),
    }
    assert reasons <= set(DROP_REASONS)
    # The coordinator reports these two; evaluate() never does.
    assert set(DROP_REASONS) - reasons == {"unresolved", "id_mismatch"}


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        FilterPolicy(disputed_weight=2.0)
    with pytest.raises(ValueError):
        FilterPolicy(tenant=None)  # type: ignore[arg-type]
    assert FilterPolicy().with_tenant("a").tenant == "a"


@pytest.mark.parametrize("lifecycle", ["archived", "deleted", "draft", "pending", "x"])
def test_lifecycle_other_than_active_is_dropped(lifecycle: str) -> None:
    assert _reason(_meta(lifecycle=lifecycle)) == "lifecycle"


@pytest.mark.parametrize("lifecycle", [None, "", "active", "Active "])
def test_active_lifecycle_is_kept(lifecycle: str | None) -> None:
    assert _reason(_meta(lifecycle=lifecycle)) is None


def test_lifecycle_is_normalised() -> None:
    assert _reason(_meta(lifecycle=" RETIRED ")) == "retired"
    assert _reason(_meta(lifecycle="Superseded")) == "superseded"


def test_superseded_with_as_of_is_judged_by_validity() -> None:
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    policy = FilterPolicy(as_of=as_of)
    # Superseded today, but valid at as_of: kept and labelled.
    open_ended = evaluate(
        _meta(superseded_by="m2", valid_from=datetime(2025, 1, 1, tzinfo=UTC)), policy, now=NOW
    )
    assert open_ended.kept and "superseded" in open_ended.labels
    # Its validity had ended before as_of: expired.
    ended = _meta(superseded_by="m2", valid_to=datetime(2025, 6, 1, tzinfo=UTC))
    assert _reason(ended, policy) == "expired"
    # Retired stays dropped regardless of as_of.
    assert _reason(_meta(lifecycle="retired"), policy) == "retired"


def test_future_valid_from_without_as_of_is_not_yet_valid() -> None:
    assert _reason(_meta(valid_from=NOW + timedelta(days=1))) == "not_yet_valid"
    assert _reason(_meta(valid_from="2099-01-01")) == "not_yet_valid"
    assert _reason(_meta(valid_from=NOW - timedelta(days=1))) is None
    history = FilterPolicy(include_history=True)
    assert _reason(_meta(valid_from=NOW + timedelta(days=1)), history) is None


def test_compat_fts_tolerates_unparsable_start_like_the_index() -> None:
    policy = FilterPolicy.compat_fts(tenant="")
    assert _reason(_meta(valid_from="garbage", created_at="also bad"), policy) is None
    as_of = FilterPolicy.compat_fts(tenant="", as_of=NOW)
    assert _reason(_meta(valid_from="garbage"), as_of) is None
    # Any non-empty valid_to hides it without as_of, parsable or not.
    assert _reason(_meta(valid_to="garbage"), policy) == "expired"
    # valid_until is not part of the index clause.
    assert _reason(_meta(valid_until="2000-01-01"), policy) is None
    # Strict mode still fails closed on the same values.
    assert _reason(_meta(valid_from="garbage")) == "expiry_unparsable"
