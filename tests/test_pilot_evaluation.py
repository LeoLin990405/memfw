"""Offline tests for the read-only Day 8 evaluation helper."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.agents.models import JsonValue, canonical_payload_hash
from memfw.core.models import MemoryDocument

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from pilot_evaluation import ApprovedQuery, EvaluationResult, corpus_fingerprint, evaluate_queries

_NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _documents() -> list[MemoryDocument]:
    contents = [
        ("fake-a", "first", "alpha words go here longer"),
        ("fake-b", "second", "bravo words go here"),
        ("fake-c", "third", "charlie words go here"),
        ("fake-d", "fourth", "delta words go here"),
        ("fake-e", "target", "zulu zulu"),
    ]
    return [
        MemoryDocument(
            id=identifier,
            title=title,
            body=body,
            created_at=datetime(2026, 1, 1 if identifier == "fake-e" else index + 2, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1 if identifier == "fake-e" else index + 2, tzinfo=UTC),
        )
        for index, (identifier, title, body) in enumerate(contents)
    ]


def _evaluate(documents: list[MemoryDocument]) -> EvaluationResult:
    ids = {document.id for document in documents}
    fingerprint = corpus_fingerprint(documents)
    scores = {identifier: float(identifier == "fake-e") for identifier in ids}
    payloads: dict[str, dict[str, JsonValue]] = {
        identifier: {"score": score} for identifier, score in scores.items()
    }
    return evaluate_queries(
        documents,
        [ApprovedQuery(target_id="fake-e", query="target zulu")],
        now=_NOW,
        approved_mlx_scores=scores,
        approved_mlx_proposal_ids={identifier: f"proposal-{identifier}" for identifier in ids},
        approved_mlx_payload_hashes={
            identifier: canonical_payload_hash(payload) for identifier, payload in payloads.items()
        },
        approved_mlx_payloads=payloads,
        approved_mlx_corpus_fingerprint=fingerprint,
        approved_query_binding_hash="query-binding-v1",
        current_query_binding_hash="query-binding-v1",
        approved_query_target_ids=["fake-e"],
    )


def test_five_fake_records_run_all_arms_and_mlx_signal_changes_retention() -> None:
    documents = _documents()

    result = _evaluate(documents)

    assert len(result.metrics) == 12
    fifo = next(item for item in result.metrics if item.arm == "fifo" and item.budget_percent == 10)
    mlx = next(
        item
        for item in result.metrics
        if item.arm == "approved_mlx_salience" and item.budget_percent == 10
    )
    keyness = next(
        item
        for item in result.metrics
        if item.arm == "keyness_salience" and item.budget_percent == 10
    )
    no_forget = next(
        item
        for item in result.metrics
        if item.arm == "no_forget_full_corpus_reference" and item.budget_percent == 10
    )
    assert fifo.retained_count == 0
    assert mlx.retained_count == 1
    assert mlx.score_signal == "approved_mlx_score_via_policy_keyness_slot_2x"
    assert keyness.score_signal == "measured_keyness_via_policy_keyness_slot_2x"
    assert no_forget.retained_count == 5
    assert mlx.hit_at_5 == 1.0
    assert "target zulu" not in repr(result)


def test_stale_score_or_query_approval_fails_closed() -> None:
    documents = _documents()
    ids = {document.id for document in documents}
    fingerprint = corpus_fingerprint(documents)
    scores = {identifier: 0.5 for identifier in ids}
    payloads: dict[str, dict[str, JsonValue]] = {
        identifier: {"score": score} for identifier, score in scores.items()
    }
    args: dict[str, Any] = dict(
        now=_NOW,
        approved_mlx_scores=scores,
        approved_mlx_proposal_ids={identifier: f"proposal-{identifier}" for identifier in ids},
        approved_mlx_payload_hashes={
            identifier: canonical_payload_hash(payload) for identifier, payload in payloads.items()
        },
        approved_mlx_payloads=payloads,
        approved_mlx_corpus_fingerprint=fingerprint,
        approved_query_binding_hash="query-binding-v1",
        current_query_binding_hash="query-binding-v1",
        approved_query_target_ids=["fake-e"],
    )
    changed = documents[0].model_copy(update={"body": "changed corpus"})
    with pytest.raises(ValueError, match="stale"):
        evaluate_queries([changed, *documents[1:]], [ApprovedQuery("fake-e", "target")], **args)
    with pytest.raises(ValueError, match="stale"):
        evaluate_queries(
            documents,
            [ApprovedQuery("fake-e", "target")],
            **(args | {"current_query_binding_hash": "changed-query-binding"}),
        )


def test_missing_or_invalid_approved_mlx_score_is_rejected() -> None:
    documents = _documents()
    ids = {document.id for document in documents}
    fingerprint = corpus_fingerprint(documents)
    score = 0.8
    payloads: dict[str, dict[str, JsonValue]] = {identifier: {"score": score} for identifier in ids}
    args: dict[str, Any] = dict(
        now=_NOW,
        approved_mlx_scores={"fake-e": score},
        approved_mlx_proposal_ids={identifier: f"proposal-{identifier}" for identifier in ids},
        approved_mlx_payload_hashes={
            identifier: canonical_payload_hash(payload) for identifier, payload in payloads.items()
        },
        approved_mlx_payloads=payloads,
        approved_mlx_corpus_fingerprint=fingerprint,
        approved_query_binding_hash="query-binding-v1",
        current_query_binding_hash="query-binding-v1",
        approved_query_target_ids=["fake-e"],
    )
    with pytest.raises(ValueError, match="cover every candidate"):
        evaluate_queries(documents, [ApprovedQuery("fake-e", "target")], **args)


def test_approved_payload_hash_and_score_must_match_exact_binding() -> None:
    documents = _documents()
    ids = {document.id for document in documents}
    fingerprint = corpus_fingerprint(documents)
    scores = {identifier: 0.5 for identifier in ids}
    payloads: dict[str, dict[str, JsonValue]] = {
        identifier: {"score": score} for identifier, score in scores.items()
    }
    payload_hashes = {
        identifier: canonical_payload_hash(payload) for identifier, payload in payloads.items()
    }
    args: dict[str, Any] = dict(
        now=_NOW,
        approved_mlx_scores=scores,
        approved_mlx_proposal_ids={identifier: f"proposal-{identifier}" for identifier in ids},
        approved_mlx_payload_hashes=payload_hashes,
        approved_mlx_payloads=payloads,
        approved_mlx_corpus_fingerprint=fingerprint,
        approved_query_binding_hash="query-binding-v1",
        current_query_binding_hash="query-binding-v1",
        approved_query_target_ids=["fake-e"],
    )
    query = [ApprovedQuery("fake-e", "target")]

    wrong_hashes = dict(payload_hashes) | {"fake-e": "not-the-payload-hash"}
    with pytest.raises(ValueError, match="bind to an approved proposal payload"):
        evaluate_queries(
            documents,
            query,
            **(args | {"approved_mlx_payload_hashes": wrong_hashes}),
        )

    changed_payloads = dict(payloads) | {"fake-e": {"score": 0.9}}
    with pytest.raises(ValueError, match="bind to an approved proposal payload"):
        evaluate_queries(
            documents,
            query,
            **(args | {"approved_mlx_payloads": changed_payloads}),
        )

    with pytest.raises(ValueError, match="must match"):
        evaluate_queries(
            documents,
            query,
            **(args | {"approved_mlx_scores": dict(scores) | {"fake-e": 0.9}}),
        )


def test_percent_budgets_use_total_canonical_body_bytes() -> None:
    documents = [document.model_copy(update={"body": "x" * 200}) for document in _documents()]
    result = _evaluate(documents)

    observed = {(item.budget_percent, item.budget_bytes) for item in result.metrics}
    assert observed == {(10, 100), (25, 250), (50, 500)}


def test_query_target_binding_uses_unique_targets_not_query_count() -> None:
    documents = _documents()
    ids = {document.id for document in documents}
    fingerprint = corpus_fingerprint(documents)
    scores = {identifier: float(identifier == "fake-e") for identifier in ids}
    payloads: dict[str, dict[str, JsonValue]] = {
        identifier: {"score": score} for identifier, score in scores.items()
    }
    query = ApprovedQuery(target_id="fake-e", query="target zulu")
    result = evaluate_queries(
        documents,
        [query, ApprovedQuery(target_id="fake-e", query="zulu target")],
        now=_NOW,
        approved_mlx_scores=scores,
        approved_mlx_proposal_ids={identifier: f"proposal-{identifier}" for identifier in ids},
        approved_mlx_payload_hashes={
            identifier: canonical_payload_hash(payload) for identifier, payload in payloads.items()
        },
        approved_mlx_payloads=payloads,
        approved_mlx_corpus_fingerprint=fingerprint,
        approved_query_binding_hash="query-binding-v1",
        current_query_binding_hash="query-binding-v1",
        approved_query_target_ids=["fake-e"],
    )
    assert all(metric.query_count == 2 for metric in result.metrics)
