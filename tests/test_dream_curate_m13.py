"""M1-3 curate follow-ups: clause negation, value signatures, identity keys, synonyms."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memfw.core.models import MemoryDocument
from memfw.dream.curate import curate

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _pair(first: str, second: str) -> list[tuple[str, str]]:
    documents = [
        MemoryDocument(
            id=f"m{index}",
            title="note",
            body=body,
            about=["concept:fixture:pair"],
            reference_count=1,
            created_at=NOW - timedelta(days=10 - index),
            updated_at=NOW - timedelta(days=10 - index),
        )
        for index, body in enumerate((first, second))
    ]
    return [
        (item.action, item.reason)
        for item in curate(documents, now=NOW)
        if len(item.memory_ids) == 2
    ]


def _action(first: str, second: str) -> str:
    actions = {action for action, _reason in _pair(first, second)}
    return "merge" if "merge" in actions else "supersede" if "supersede" in actions else "none"


CASES = [
    # clause-level negation
    (
        "neg-contraction",
        "supersede",
        "Telemetry is enabled for the api.",
        "Telemetry isn't enabled for the api.",
    ),
    (
        "neg-curly-contraction",
        "supersede",
        "Telemetry is enabled for the api.",
        "Telemetry isn’t enabled for the api.",
    ),
    ("neg-not", "supersede", "The cache is enabled.", "The cache is not enabled."),
    ("neg-kv-line", "supersede", "- cache: enabled", "- cache: not enabled"),
    (
        "neg-unrelated-clause",
        "none",
        "The backup runs nightly. Do not run it twice.",
        "The backup runs nightly.",
    ),
    ("neg-equivalent", "merge", "The cache is not enabled.", "The cache is disabled."),
    ("neg-other-value", "none", "The owner is not alice.", "The owner is bob."),
    ("neg-both-clauses", "merge", "Do not deploy on Fridays.", "Never deploy on a Friday."),
    ("neg-clause-prose", "supersede", "Deploy on Fridays.", "Never deploy on Fridays."),
    # value signatures
    (
        "value-port-low-context",
        "supersede",
        "The api runs on port 8000.",
        "The api runs on port 9000 behind the new proxy layer after the move to the rack.",
    ),
    (
        "value-timeout-unit",
        "supersede",
        "The job timeout 30s for uploads.",
        "Uploads use a job timeout of 45s.",
    ),
    ("value-unrelated-nouns", "none", "The team has 3 members.", "Retention is 30 days."),
    ("value-same", "merge", "The api runs on port 8000.", "The api runs on port 8000 now."),
    # identity keys
    (
        "identity-agreeing-owner",
        "none",
        "service: api\nowner: alice",
        "service: worker\nowner: alice",
    ),
    (
        "identity-same-service-new-owner",
        "supersede",
        "service: api\nowner: alice",
        "service: api\nowner: bob",
    ),
    (
        "identity-env-synonym",
        "supersede",
        "env: prod\nreplicas: 3",
        "environment: production\nreplicas: 5",
    ),
    # an identity attribute vs a different identity named in prose (review fix 1)
    (
        "identity-prose-env",
        "none",
        "- env: prod\n- replicas: 3",
        "The staging environment has 3 replicas.",
    ),
    (
        "identity-prose-service",
        "none",
        "- service: api\n- replicas: 3",
        "The worker service has 3 replicas.",
    ),
    (
        "identity-prose-cluster",
        "none",
        "- cluster: eu1\n- nodes: 4",
        "The us2 cluster has 4 nodes.",
    ),
    (
        "identity-prose-same-env",
        "merge",
        "- env: prod\n- replicas: 3",
        "The production environment has 3 replicas.",
    ),
    (
        "identity-prose-same-service",
        "merge",
        "- service: api\n- replicas: 3",
        "The api service has 3 replicas.",
    ),
    # trailing time adverbs are not part of a value (review fix 4)
    (
        "adverb-trailing-now",
        "merge",
        "Notifications are enabled.",
        "Notifications are enabled now.",
    ),
    ("adverb-trailing-still", "merge", "The gateway is healthy.", "The gateway is healthy still."),
    (
        "adverb-real-change",
        "supersede",
        "Notifications are enabled.",
        "Notifications are disabled now.",
    ),
    # synonyms
    ("syn-deploys", "merge", "Deploys happen on Fridays.", "Deployments are scheduled on Friday."),
    (
        "syn-db-prod",
        "merge",
        "The prod database is backed up nightly.",
        "The production db is backed up nightly.",
    ),
    (
        "syn-not-across-env",
        "none",
        "The staging database is backed up nightly.",
        "The production db is backed up nightly.",
    ),
    ("syn-opposites-not-folded", "supersede", "The cache is enabled.", "The cache is disabled."),
]


@pytest.mark.parametrize(
    ("name", "expected", "first", "second"), CASES, ids=[case[0] for case in CASES]
)
def test_m13_pairs(name: str, expected: str, first: str, second: str) -> None:
    assert _action(first, second) == expected


def test_negation_reason_names_the_attribute() -> None:
    reasons = [
        reason
        for action, reason in _pair(
            "Telemetry is enabled for the api.", "Telemetry isn't enabled for the api."
        )
        if action == "supersede"
    ]
    assert any("attribute 'telemetry' is negated in one memory" in item for item in reasons)


def test_value_signature_reason_names_the_key() -> None:
    reasons = [
        reason
        for action, reason in _pair(
            "The job timeout 30s for uploads.", "Uploads use a job timeout of 45s."
        )
        if action == "supersede"
    ]
    assert any("value of 'timeout' differs" in item for item in reasons)


def test_identity_key_differs_even_with_many_agreeing_attributes() -> None:
    first = "service: api\nowner: alice\nregion: north\ntier: gold"
    second = "service: worker\nowner: alice\nregion: north\ntier: gold"
    assert _pair(first, second) == []
