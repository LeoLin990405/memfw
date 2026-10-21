from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from pilot_diagnostics import (
    aggregate_real_receipts,
    rank_disagreement,
    repeatability_probe,
)

from memfw.agents import LocalMLXJudge
from memfw.agents.mlx_live import InferenceReceipt, LocalInferenceResult, Telemetry
from memfw.core.models import MemoryDocument


def memory() -> MemoryDocument:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return MemoryDocument(
        id="diag-memory",
        title="Diagnostic fixture",
        created_at=now,
        updated_at=now,
        body="Private fixture body.",
    )


def test_rank_diagnostic_is_deterministic_tie_aware_and_reports_coverage() -> None:
    keyness = {f"id-{index:02d}": float(index // 2) for index in range(55)}
    mlx = {f"id-{index:02d}": float((54 - index) // 2) for index in range(53)}
    forward = rank_disagreement(keyness, mlx)
    reverse = rank_disagreement(
        dict(reversed(list(keyness.items()))), dict(reversed(list(mlx.items())))
    )
    assert forward == reverse
    assert forward["sample_count"] == 50
    assert forward["common_id_count"] == 53
    assert forward["coverage_fraction"] == 1.0
    assert len(cast(list[dict[str, object]], forward["largest_disagreements"])) == 10
    sample_scores = cast(list[dict[str, object]], forward["sample_scores"])
    assert all(
        set(row) == {"id", "keyness_score", "mlx_score", "absolute_difference"}
        for row in sample_scores
    )

    tied = rank_disagreement(
        {"a": 1, "b": 1, "c": 2}, {"a": 1, "b": 2, "c": 2}, sample_size=3, top_n=3
    )
    assert tied["spearman"] == pytest.approx(0.5)
    assert tied["sample_count"] == 3


@dataclass
class FakeResult:
    text: str
    elapsed_seconds: float = 0.25
    input_tokens: int = 7
    output_tokens: int = 4
    peak_memory_bytes: int = 1024
    cold_peak_memory_bytes: int = 512
    cold_load_seconds: float | None = 0.5
    total_seconds: float | None = 0.75
    input_truncated_bytes: int = 0
    raw_input_hash: str = ""
    submitted_input_hash: str = ""


class SequenceInference:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls = 0

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
    ) -> LocalInferenceResult:
        del model_name, quantization, prompt_version, prompt, input_json, temperature, seed
        self.calls += 1
        return FakeResult(next(self.responses))


def test_repeatability_probe_compares_raw_hash_and_payload_without_timestamps() -> None:
    inference = SequenceInference(
        [
            '{"score":0.7,"reason":"stable reason"}',
            '{ "reason": "stable reason", "score": 0.7 }',
            '{"score":0.6,"reason":"changed reason"}',
        ]
    )
    receipts: list[InferenceReceipt] = []
    call_number = 0

    def factory(callback: Telemetry) -> LocalMLXJudge:
        nonlocal call_number
        call_number += 1
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=call_number)

        def capture(receipt: InferenceReceipt) -> None:
            receipts.append(receipt)
            callback(receipt)

        return LocalMLXJudge(
            inference=inference,
            model_name="fake-local",
            quantization="4bit",
            clock=lambda: timestamp,
            telemetry=capture,
        )

    result = repeatability_probe(factory, memory())
    assert result["status"] == "completed"
    assert result["valid_repeat_count"] == 3
    assert result["telemetry_receipt_count"] == 3
    assert result["raw_output_agreement_count"] == 1
    assert result["payload_agreement_count"] == 2
    runs = cast(list[dict[str, object]], result["runs"])
    assert len({run["reason_hash"] for run in runs}) == 2
    assert len({run["raw_output_hash"] for run in runs}) == 3
    assert len(receipts) == 3
    assert all(len(receipt.output_hash) == 64 for receipt in receipts)


def test_repeatability_probe_returns_unknown_agreement_when_no_valid_runs() -> None:
    inference = SequenceInference(["bad JSON"] * 3)

    def factory(callback: Telemetry) -> LocalMLXJudge:
        return LocalMLXJudge(
            inference=inference,
            model_name="fake-local",
            quantization="4bit",
            telemetry=callback,
        )

    result = repeatability_probe(factory, memory())
    assert result["status"] == "partial"
    assert result["valid_repeat_count"] == 0
    assert result["telemetry_receipt_count"] == 3
    assert result["raw_output_agreement_count"] is None
    assert result["payload_agreement_count"] is None


@dataclass
class Receipt:
    status: str
    elapsed_seconds: float | None
    cold_load_seconds: float | None
    peak_memory_bytes: int | None
    cold_peak_memory_bytes: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_seconds: float | None = None


def test_real_receipt_aggregate_requires_explicit_live_evidence_and_uses_formula() -> None:
    assert aggregate_real_receipts(None, measured_live=True) is None
    assert aggregate_real_receipts([Receipt("completed", 1.0, 0.5, 1024)]) is None
    receipts = [
        Receipt("failed", 99.0, 99.0, 99),
        Receipt("completed", 0.2, 0.5, 1000, 500, 10, 5, 0.7),
        Receipt("completed", 0.4, 0.6, 2000, 700, 20, 15, 0.9),
    ]
    result = aggregate_real_receipts(receipts, measured_live=True)
    assert result is not None
    assert result["sample_count"] == 2
    assert result["mean_inference_seconds"] == pytest.approx(0.3)
    assert result["cold_load_seconds"] == 0.5
    assert result["peak_memory_bytes_max"] == 2000
    assert result["cold_peak_memory_bytes_max"] == 700
    assert result["input_tokens_total"] == 30
    assert result["output_tokens_total"] == 20
    assert result["estimated_1000_input_tokens"] == 15000
    assert result["estimated_1000_output_tokens"] == 10000
    assert result["total_seconds_samples"] == [0.7, 0.9]
    assert result["estimated_1000_calls_seconds"] == pytest.approx(300.5)
