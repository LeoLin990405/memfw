from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from memfw.agents import ClaudeJudge, DeepSeekJudge, MLXJudge, OpenAIJudge
from memfw.agents.models import JsonValue
from memfw.core.models import MemoryDocument


def memory(memory_id: str, text: str) -> MemoryDocument:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return MemoryDocument(
        id=memory_id,
        title=f"{text} title",
        created_at=timestamp,
        updated_at=timestamp,
        body=text,
    )


class StaticTransport:
    def __init__(self, response: Mapping[str, JsonValue]) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []

    def __call__(self, provider: str, request: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        self.calls.append((provider, request))
        return self.response


def test_all_mock_judges_are_stable_and_make_no_socket_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("mock judges must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    items = [
        memory("one", "recall the planning meeting"),
        memory("two", "recall the planning meeting"),
    ]
    for judge_type in (ClaudeJudge, OpenAIJudge, DeepSeekJudge, MLXJudge):
        first = judge_type()
        second = judge_type()
        assert first.score_salience(items) == second.score_salience(items)
        assert first.find_near_duplicates(items) == second.find_near_duplicates(items)
        assert first.suggest_layer(items[0]) == second.suggest_layer(items[0])
        assert first.wake_intensity("speech") == second.wake_intensity("speech")
        proposal = first.score_salience(items)[0]
        assert proposal.created_at == datetime(2000, 1, 1, tzinfo=UTC)
        assert proposal.signature.startswith("mock:")
        assert proposal.input_hash
        assert proposal.output
        assert proposal.target_hashes == {
            items[0].id: hashlib.sha256(
                json.dumps(
                    items[0].model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        }
        merge = first.find_near_duplicates(items)[0]
        assert set(merge.proposal.target_hashes) == {"one", "two"}


@pytest.mark.parametrize("threshold", [0.0, 0.2, 0.8, 1.0])
def test_bitset_judge_matches_set_jaccard_exactly(threshold: float) -> None:
    items = [
        memory("a", "短文"),
        memory("b", "短文"),
        memory("c", "Same long bilingual content 中文 with punctuation!"),
        memory("d", "Same long bilingual content 中文 with punctuation?"),
        memory("e", "completely unrelated maintenance action"),
    ]
    judge = ClaudeJudge(ngram_size=3, duplicate_threshold=threshold)
    ordered = sorted(items, key=lambda item: item.id)
    grams = {item.id: judge._ngrams(item, judge.ngram_size) for item in ordered}
    expected: list[tuple[str, str, float]] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            union = grams[left.id] | grams[right.id]
            score = len(grams[left.id] & grams[right.id]) / len(union) if union else 0.0
            if score >= threshold:
                expected.append((left.id, right.id, round(score, 8)))

    actual = [
        (item.representative_id, item.duplicate_ids[0], item.confidence)
        for item in judge.find_near_duplicates(items)
    ]
    assert actual == expected


def test_mock_mode_ignores_injected_transport_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present-for-gate-only")
    transport = StaticTransport({"score": 0.9})
    judge = OpenAIJudge(mode="mock", transport=transport, env_key="OPENAI_API_KEY")
    assert judge.score_salience([memory("one", "content")])[0].output["score"] != 0.9
    assert transport.calls == []


def test_live_mode_requires_key_and_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    transport = StaticTransport({"score": 0.9})
    with pytest.raises(ValueError, match="environment key"):
        OpenAIJudge(mode="live", transport=transport)
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    with pytest.raises(ValueError, match="injected transport"):
        OpenAIJudge(mode="live")


def test_live_mlx_mode_needs_transport_but_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLX_API_KEY", raising=False)
    transport = StaticTransport({"intensity": 0.5})
    judge = MLXJudge(mode="live", transport=transport)
    assert judge.wake_intensity("local inference") == 0.5
    assert transport.calls[0][0] == "mlx"


def test_live_transport_maps_typed_response_and_validates_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    transport = StaticTransport({"score": 0.9})
    judge = OpenAIJudge(mode="live", transport=transport)
    proposal = judge.score_salience([memory("one", "content")])[0]
    assert proposal.output["score"] == 0.9
    assert transport.calls[0][1]["input"]
    assert proposal.input_hash == transport.calls[0][1]["input_hash"]
    assert "mock" not in proposal.reason

    layer_transport = StaticTransport({"layer": "anchor"})
    layer_proposal = OpenAIJudge(mode="live", transport=layer_transport).suggest_layer(
        memory("one", "content")
    )
    assert layer_proposal.output["layer"] == "anchor"
    assert "human review" in layer_proposal.reason

    bad_merge = StaticTransport(
        {
            "merge_pairs": [
                {
                    "representative_id": "one",
                    "duplicate_ids": ["not-in-input"],
                    "match_basis": "provider",
                    "disposition": "retain",
                    "confidence": 0.9,
                }
            ]
        }
    )
    judge_with_bad_merge = OpenAIJudge(mode="live", transport=bad_merge)
    with pytest.raises(ValueError, match="invalid targets"):
        judge_with_bad_merge.find_near_duplicates([memory("one", "content")])
