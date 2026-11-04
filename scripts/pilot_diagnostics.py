"""Pure, injected diagnostics for the local memory pilot."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

from memfw.agents import JudgeAgent
from memfw.agents.mlx_live import InferenceReceipt, Telemetry
from memfw.agents.models import Proposal
from memfw.core.models import MemoryDocument


class ReceiptMetrics(Protocol):
    status: str
    elapsed_seconds: float | None
    cold_load_seconds: float | None
    peak_memory_bytes: int | None
    cold_peak_memory_bytes: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_seconds: float | None


JudgeFactory = Callable[[Telemetry], JudgeAgent]


def _finite_score_map(values: Mapping[str, float]) -> dict[str, float]:
    return {
        identifier: float(value)
        for identifier, value in values.items()
        if isinstance(identifier, str)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    }


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = (start + 1 + end) / 2
        for position, _ in ordered[start:end]:
            ranks[position] = average_rank
        start = end
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    left_delta = [value - left_mean for value in left_ranks]
    right_delta = [value - right_mean for value in right_ranks]
    denominator = math.sqrt(
        sum(value * value for value in left_delta) * sum(value * value for value in right_delta)
    )
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(left_delta, right_delta, strict=True)) / denominator


def rank_disagreement(
    keyness_scores: Mapping[str, float],
    approved_mlx_scores: Mapping[str, float],
    *,
    sample_size: int = 50,
    top_n: int = 10,
) -> dict[str, object]:
    """Compare tie-aware ranks and score differences over a stable ID sample."""
    if sample_size < 1 or top_n < 1:
        raise ValueError("sample_size and top_n must be positive")
    keyness = _finite_score_map(keyness_scores)
    mlx = _finite_score_map(approved_mlx_scores)
    common = set(keyness) & set(mlx)
    selected = sorted(
        common, key=lambda identifier: (hashlib.sha256(identifier.encode()).hexdigest(), identifier)
    )[:sample_size]
    rows = [
        {
            "id": identifier,
            "keyness_score": keyness[identifier],
            "mlx_score": mlx[identifier],
            "absolute_difference": abs(mlx[identifier] - keyness[identifier]),
        }
        for identifier in selected
    ]
    largest = sorted(
        rows,
        key=lambda row: (
            -abs(mlx[str(row["id"])] - keyness[str(row["id"])]),
            str(row["id"]),
        ),
    )[:top_n]
    coverage = len(selected) / sample_size
    return {
        "spearman": _spearman(
            [keyness[identifier] for identifier in selected],
            [mlx[identifier] for identifier in selected],
        ),
        "requested_sample_size": sample_size,
        "sample_count": len(selected),
        "common_id_count": len(common),
        "coverage_fraction": coverage,
        "sample_ids": selected,
        "sample_scores": rows,
        "largest_disagreements": largest,
    }


def _valid_receipt_hash(receipt: InferenceReceipt) -> str | None:
    value = receipt.output_hash
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def _payload_digest(score: float, reason: str) -> str:
    payload = json.dumps(
        {"reason": reason, "score": score},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_nonnegative_float(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def repeatability_probe(
    judge_factory: JudgeFactory,
    memory: MemoryDocument,
    *,
    repeats: int = 3,
) -> dict[str, object]:
    """Call an injected judge three times; capture hashes and never apply proposals."""
    if repeats != 3:
        raise ValueError("the pilot repeatability probe uses exactly three repeats")
    runs: list[dict[str, object]] = []
    receipts: list[InferenceReceipt] = []
    for index in range(repeats):
        current: list[InferenceReceipt] = []

        def capture(
            receipt: InferenceReceipt, current_run: list[InferenceReceipt] = current
        ) -> None:
            current_run.append(receipt)

        state: dict[str, object] = {"repeat": index + 1, "status": "failed"}
        try:
            judge = judge_factory(capture)
            proposals = judge.score_salience([memory])
            receipts.extend(current)
            if len(current) != 1 or len(proposals) != 1:
                raise ValueError("expected one call and one proposal")
            receipt = current[0]
            proposal: Proposal = proposals[0]
            score_value = proposal.output.get("score")
            if (
                receipt.status != "completed"
                or _valid_receipt_hash(receipt) is None
                or isinstance(score_value, bool)
                or not isinstance(score_value, (int, float))
                or not math.isfinite(score_value)
                or not 0.0 <= score_value <= 1.0
                or not proposal.reason.strip()
            ):
                raise ValueError("invalid repeat result")
            score = float(score_value)
            state.update(
                {
                    "status": "completed",
                    "raw_output_hash": receipt.output_hash,
                    "payload_hash": _payload_digest(score, proposal.reason),
                    "reason_hash": hashlib.sha256(proposal.reason.encode("utf-8")).hexdigest(),
                    "score": score,
                }
            )
        except Exception:
            receipts.extend(item for item in current if item not in receipts)
        state["telemetry_receipt_count"] = len(current)
        runs.append(state)

    valid_runs = [run for run in runs if run["status"] == "completed"]

    def matches_first(key: str) -> int | None:
        if not valid_runs:
            return None
        first = valid_runs[0][key]
        return sum(run[key] == first for run in valid_runs)

    return {
        "status": "completed" if len(valid_runs) == repeats else "partial",
        "repeat_count": repeats,
        "valid_repeat_count": len(valid_runs),
        "telemetry_receipt_count": len(receipts),
        "raw_output_agreement_count": matches_first("raw_output_hash"),
        "payload_agreement_count": matches_first("payload_hash"),
        "runs": runs,
    }


def aggregate_real_receipts(
    receipts: Iterable[ReceiptMetrics] | None,
    *,
    measured_live: bool = False,
) -> dict[str, object] | None:
    """Summarize explicitly designated real receipts; never extrapolate fake runs."""
    if not measured_live or receipts is None:
        return None
    measured = []
    for receipt in receipts:
        elapsed = receipt.elapsed_seconds
        if (
            receipt.status == "completed"
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(elapsed)
            and elapsed >= 0
        ):
            measured.append(receipt)
    if not measured:
        return None
    inference_times = [
        float(item.elapsed_seconds) for item in measured if item.elapsed_seconds is not None
    ]
    cold_loads = [
        float(item.cold_load_seconds)
        for item in measured
        if item.cold_load_seconds is not None
        and isinstance(item.cold_load_seconds, (int, float))
        and not isinstance(item.cold_load_seconds, bool)
        and math.isfinite(item.cold_load_seconds)
        and item.cold_load_seconds >= 0
    ]
    peaks: list[int] = []
    cold_peaks: list[int] = []
    input_token_samples: list[int] = []
    output_token_samples: list[int] = []
    total_seconds_samples: list[float] = []
    for item in measured:
        for raw, samples in (
            (item.peak_memory_bytes, peaks),
            (item.cold_peak_memory_bytes, cold_peaks),
            (item.input_tokens, input_token_samples),
            (item.output_tokens, output_token_samples),
        ):
            metric = _optional_nonnegative_int(raw)
            if metric is not None:
                samples.append(metric)
        total_time = _optional_nonnegative_float(item.total_seconds)
        if total_time is not None:
            total_seconds_samples.append(total_time)
    mean_inference = sum(inference_times) / len(inference_times)
    mean_input_tokens = (
        sum(input_token_samples) / len(input_token_samples) if input_token_samples else None
    )
    mean_output_tokens = (
        sum(output_token_samples) / len(output_token_samples) if output_token_samples else None
    )
    cold_load = cold_loads[0] if cold_loads else None
    estimate = cold_load + 1000 * mean_inference if cold_load is not None else None
    result: dict[str, object] = {
        "sample_count": len(measured),
        "mean_inference_seconds": mean_inference,
        "cold_load_seconds": cold_load,
        "peak_memory_bytes_max": max(peaks) if peaks else None,
        "inference_peak_memory_bytes_max": max(peaks) if peaks else None,
        "cold_peak_memory_bytes_max": max(cold_peaks) if cold_peaks else None,
        "estimated_1000_calls_seconds": estimate,
        "input_token_sample_count": len(input_token_samples),
        "output_token_sample_count": len(output_token_samples),
        "input_tokens_total": sum(input_token_samples) if input_token_samples else None,
        "output_tokens_total": sum(output_token_samples) if output_token_samples else None,
        "mean_input_tokens_per_call": mean_input_tokens,
        "mean_output_tokens_per_call": mean_output_tokens,
        "estimated_1000_input_tokens": (
            mean_input_tokens * 1000 if mean_input_tokens is not None else None
        ),
        "estimated_1000_output_tokens": (
            mean_output_tokens * 1000 if mean_output_tokens is not None else None
        ),
    }
    if total_seconds_samples:
        result["total_seconds_samples"] = total_seconds_samples
    return result


__all__ = ["aggregate_real_receipts", "rank_disagreement", "repeatability_probe"]
