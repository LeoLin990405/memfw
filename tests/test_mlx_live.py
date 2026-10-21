from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from memfw.agents.mlx_live import (
    InferenceReceipt,
    LocalInference,
    LocalMLXJudge,
    LocalMLXQueryError,
)
from memfw.core.models import MemoryDocument


class FakeResult:
    elapsed_seconds: float = 0.25
    cold_load_seconds: float | None = None
    input_tokens: int = 31
    output_tokens: int = 8
    peak_memory_bytes: int = 4096
    cold_peak_memory_bytes: int = 2048
    total_seconds: float | None = 0.75
    input_truncated_bytes: int = 0
    raw_input_hash: str = ""
    submitted_input_hash: str = ""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeInference:
    def __init__(self, text: str, *, truncated_bytes: int = 0) -> None:
        self.text = text
        self.truncated_bytes = truncated_bytes
        self.calls: list[dict[str, Any]] = []

    def infer(
        self,
        *,
        model_name: str,
        quantization: str,
        prompt_version: str,
        prompt: str,
        input_json: str,
        temperature: float = 0.0,
        seed: int = 0,
    ) -> FakeResult:
        self.calls.append(
            {
                "model_name": model_name,
                "quantization": quantization,
                "prompt_version": prompt_version,
                "prompt": prompt,
                "input_json": input_json,
                "temperature": temperature,
                "seed": seed,
            }
        )
        result = FakeResult(self.text)
        result.input_truncated_bytes = self.truncated_bytes
        result.submitted_input_hash = "b" * 64 if self.truncated_bytes else ""
        return result


def memory(identifier: str = "one") -> MemoryDocument:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return MemoryDocument(
        id=identifier, title="A memory", created_at=now, updated_at=now, body="content"
    )


def make_judge(
    inference: LocalInference, telemetry: list[InferenceReceipt] | None = None
) -> LocalMLXJudge:
    return LocalMLXJudge(
        inference=inference,
        model_name="local/model",
        quantization="4bit",
        telemetry=telemetry.append if telemetry is not None else None,
    )


def test_local_adapter_maps_score_proposal_and_metadata_only_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("adapter tests must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    inference = FakeInference('{"score":0.75,"reason":"Useful recurring preference."}')
    telemetry: list[InferenceReceipt] = []
    proposal = make_judge(inference, telemetry).score_salience([memory()])[0]

    assert proposal.output["score"] == 0.75
    assert proposal.reason == "Useful recurring preference."
    assert proposal.target_hashes["one"]
    assert proposal.signature.startswith("mock:")
    assert inference.calls[0]["temperature"] == 0.0
    assert inference.calls[0]["seed"] == 0
    assert len(telemetry) == 1
    receipt = telemetry[0]
    assert receipt.status == "completed"
    assert receipt.elapsed_seconds == 0.25
    assert receipt.cold_load_seconds is None
    assert receipt.cold_peak_memory_bytes == 2048
    assert receipt.total_seconds == 0.75
    assert receipt.input_truncated_bytes == 0
    assert receipt.raw_input_hash
    assert receipt.submitted_input_hash == receipt.raw_input_hash
    assert not hasattr(receipt, "prompt")
    assert not hasattr(receipt, "text")
    assert not hasattr(receipt, "input_json")


def test_malformed_json_fails_closed_and_emits_metadata_receipt() -> None:
    inference = FakeInference("not JSON")
    telemetry: list[InferenceReceipt] = []
    with pytest.raises(ValueError, match="response validation failed"):
        make_judge(inference, telemetry).score_salience([memory()])
    assert len(telemetry) == 1
    assert telemetry[0].status == "failed"


def test_truncation_receipt_contains_counts_and_hashes_but_no_input_text() -> None:
    inference = FakeInference('{"score":0.75,"reason":"ok"}', truncated_bytes=123)
    telemetry: list[InferenceReceipt] = []
    make_judge(inference, telemetry).score_salience([memory()])

    receipt = telemetry[0]
    assert receipt.input_truncated_bytes == 123
    assert receipt.raw_input_hash
    assert receipt.submitted_input_hash == "b" * 64
    assert not hasattr(receipt, "body")
    assert not hasattr(receipt, "input_json")
    assert not hasattr(receipt, "prompt")


def test_invalid_merge_target_rejected_without_proposals() -> None:
    inference = FakeInference(
        '{"merge_pairs":[{"representative_id":"one","duplicate_ids":["missing"],'
        '"match_basis":"semantic","disposition":"retain","confidence":0.9,'
        '"reason":"looks similar"}]}'
    )
    telemetry: list[InferenceReceipt] = []
    with pytest.raises(ValueError, match="response validation failed"):
        make_judge(inference, telemetry).find_near_duplicates([memory()])
    assert len(telemetry) == 1
    assert telemetry[0].status == "failed"


@pytest.mark.parametrize(
    "response",
    ['{"score":1.5,"reason":"bad score"}', '{"score":0.5}'],
)
def test_invalid_score_or_missing_reason_is_a_failed_audit(response: str) -> None:
    telemetry: list[InferenceReceipt] = []
    with pytest.raises(ValueError, match="response validation failed"):
        make_judge(FakeInference(response), telemetry).score_salience([memory()])
    assert len(telemetry) == 1
    assert telemetry[0].status == "failed"


def test_transport_exception_is_sanitized_and_audited() -> None:
    class RaisingInference:
        def infer(
            self,
            *,
            model_name: str,
            quantization: str,
            prompt_version: str,
            prompt: str,
            input_json: str,
            temperature: float = 0.0,
            seed: int = 0,
        ) -> FakeResult:
            raise RuntimeError(f"private prompt {prompt}")

    telemetry: list[InferenceReceipt] = []
    with pytest.raises(ValueError, match="response validation failed") as error:
        make_judge(RaisingInference(), telemetry).score_salience([memory()])
    assert "private prompt" not in str(error.value)
    assert len(telemetry) == 1
    assert telemetry[0].status == "failed"


def test_merge_proposals_bind_each_input_hash() -> None:
    inference = FakeInference(
        '{"merge_pairs":[{"representative_id":"one","duplicate_ids":["two"],'
        '"match_basis":"semantic","disposition":"retain","confidence":0.9,'
        '"reason":"same fact"}]}'
    )
    result = make_judge(inference).find_near_duplicates([memory("one"), memory("two")])
    assert len(result) == 1
    assert result[0].proposal.target_hashes.keys() == {"one", "two"}


def test_query_generation_binds_single_target_and_rejects_empty_or_malformed_output() -> None:
    inference = FakeInference(
        '{"queries":[{"target_id":"one","query":"What was the preference?"},'
        '{"target_id":"outside","query":"leak"}]}'
    )
    judge = make_judge(inference)
    assert judge.suggest_queries([memory()], count=30) == [
        {"target_id": "one", "query": "What was the preference?"},
        {"target_id": "one", "query": "leak"},
    ]

    valid = make_judge(
        FakeInference('{"queries":[{"target_id":"one","query":"What was the preference?"}]}')
    )
    assert valid.suggest_queries([memory()], count=30) == [
        {"target_id": "one", "query": "What was the preference?"}
    ]

    malformed = make_judge(FakeInference("unparseable output"))
    with pytest.raises(LocalMLXQueryError) as malformed_error:
        malformed.suggest_queries([memory()], count=30)
    assert malformed_error.value.code == "invalid_json"

    empty = make_judge(FakeInference('{"queries":[]}'))
    with pytest.raises(LocalMLXQueryError) as empty_error:
        empty.suggest_queries([memory()], count=30)
    assert empty_error.value.code == "empty_output"


def test_query_generation_caps_valid_overproduction_to_requested_count() -> None:
    inference = FakeInference(
        '{"queries":[{"target_id":"one","query":"First retrieval question?"},'
        '{"target_id":"one","query":"Second retrieval question?"}]}'
    )
    assert make_judge(inference).suggest_queries([memory("one")], count=1) == [
        {"target_id": "one", "query": "First retrieval question?"}
    ]


@pytest.mark.parametrize(
    "output",
    [
        '```json\n{"queries":[{"query":"Which preference?"}]}\n```',
        'Here are results: [{"target_id":"wrong","query":"Which preference?"}] Thanks.',
        '{"query":"Which preference?"}',
        '["Which preference?"]',
    ],
)
def test_query_parser_recovers_wrapped_outputs_and_binds_single_target(output: str) -> None:
    assert make_judge(FakeInference(output)).suggest_queries([memory("bound")], 5) == [
        {"target_id": "bound", "query": "Which preference?"}
    ]


def test_query_shape_callback_reports_only_types_counts_and_blankness() -> None:
    raw = (
        '{"queries":[{"target_id":"PRIVATE_ID_CANARY",'
        '"query":"PRIVATE_QUERY_CANARY","extra":"PRIVATE_KEY_CANARY"}]}'
    )
    observed: list[dict[str, object]] = []
    judge = LocalMLXJudge(
        inference=FakeInference(raw),
        model_name="local/model",
        quantization="4bit",
        query_shape_callback=lambda shape: observed.append(dict(shape)),
    )
    assert judge.suggest_queries([memory("bound")], 1)[0]["target_id"] == "bound"
    assert observed == [
        {
            "root_type": "object",
            "queries_present": True,
            "queries_type": "list",
            "query_count": 1,
            "row_types": {"dict": 1},
            "known_field_types": {
                "target_id": {"str": 1},
                "query": {"str": 1},
            },
            "blank_query_count": 0,
        }
    ]
    assert "PRIVATE_" not in repr(observed)


def test_query_shape_callback_distinguishes_missing_and_blank_query_fields() -> None:
    raw = '{"queries":[{"target_id":"id"},{"query":"  "}]}'
    observed: list[dict[str, object]] = []
    with pytest.raises(LocalMLXQueryError) as error:
        LocalMLXJudge(
            inference=FakeInference(raw),
            model_name="local/model",
            quantization="4bit",
            query_shape_callback=lambda shape: observed.append(dict(shape)),
        ).suggest_queries([memory("bound")], 2)
    assert error.value.code == "invalid_schema"
    assert observed[0]["blank_query_count"] == 1
    assert observed[0]["known_field_types"] == {
        "target_id": {"str": 1, "missing": 1},
        "query": {"missing": 1, "str": 1},
    }


def test_multi_memory_requires_each_target_id_and_diagnostic_callback_is_opt_in() -> None:
    raw = '{"queries":[{"target_id":"wrong","query":"Which preference?"}]}'
    observed: list[tuple[str | None, BaseException | None]] = []
    with pytest.raises(LocalMLXQueryError) as error:
        make_judge(FakeInference(raw)).suggest_queries([memory("one"), memory("two")], 5)
    assert error.value.code == "invalid_schema"

    judge = LocalMLXJudge(
        inference=FakeInference(raw),
        model_name="local/model",
        quantization="4bit",
        diagnostic_callback=lambda response, exc: observed.append((response, exc)),
    )
    with pytest.raises(LocalMLXQueryError):
        judge.suggest_queries([memory("one"), memory("two")], 5)
    assert len(observed) == 1
    assert observed[0][0] == raw
    assert observed[0][1] is not None


def test_query_diagnostic_callback_receives_success_without_persisting_raw_output() -> None:
    raw = '```json\n{"queries":[{"query":"What changed?"}]}\n```'
    observed: list[tuple[str | None, BaseException | None]] = []
    telemetry: list[InferenceReceipt] = []
    judge = LocalMLXJudge(
        inference=FakeInference(raw),
        model_name="local/model",
        quantization="4bit",
        telemetry=telemetry.append,
        diagnostic_callback=lambda response, exc: observed.append((response, exc)),
    )
    assert judge.suggest_queries([memory("bound")], 5)[0]["target_id"] == "bound"
    assert observed == [(raw, None)]
    assert raw not in repr(telemetry)


def test_query_diagnostic_callback_failure_is_visible_and_sanitized() -> None:
    def broken_sink(response: str | None, error: BaseException | None) -> None:
        raise RuntimeError("private-canary diagnostic write failed")

    judge = LocalMLXJudge(
        inference=FakeInference('{"queries":[{"query":"What changed?"}]}'),
        model_name="local/model",
        quantization="4bit",
        diagnostic_callback=broken_sink,
    )
    with pytest.raises(LocalMLXQueryError) as error:
        judge.suggest_queries([memory("bound")], 5)
    assert error.value.code == "diagnostic_logging_failure"
    assert "private-canary" not in str(error.value)


def test_query_parser_rejects_ambiguous_json_candidates() -> None:
    output = (
        'First: {"queries":[{"query":"What changed?"}]} '
        'Second: {"queries":[{"query":"What stayed?"}]}'
    )
    with pytest.raises(LocalMLXQueryError) as error:
        make_judge(FakeInference(output)).suggest_queries([memory("bound")], 5)
    assert error.value.code == "ambiguous_json"
