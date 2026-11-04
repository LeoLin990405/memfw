#!/usr/bin/env python3
"""Compare label-only capture upper bounds with deterministic text judges.

Evaluation truth is read only by the oracle and final metrics. The feature judge
receives text and past-only text references; it does not receive labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gen_day5 import SEED, Day5Label, Day5Record, generate_day6_dataset

from memfw.capture import CaptureSession, TimedSignal, pack_capture_candidates
from memfw.capture.confirm import confirm_candidates
from memfw.core.repository import Repository
from memfw.stats.keyness import KeynessScorer, tokenize

POLICIES = (
    "always_on",
    "existing_intensity",
    "deterministic_keyness_text",
    "oracle_upper_bound",
)
ACTION_TERMS = frozenset(
    "check compare keep retain record verify review rotate expire save leave fix report measure "
    "separate write return replace buy put check inspect confirm preserve".split()
)
ACTION_TERMS_ZH = ("检查", "比较", "保留", "记录", "验证", "复核", "保存", "归还", "更换")
_TEMPORAL = re.compile(r"\b(?:before|after|until|deadline|date|time|week|month|year)\b", re.I)
_NUMBER = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class UsageSample:
    label: str
    all_in_logical_bytes: int
    all_in_allocated_bytes: int
    staging_logical_bytes: int
    staging_allocated_bytes: int
    event_log_logical_bytes: int
    event_log_allocated_bytes: int
    temp_paths: tuple[str, ...]
    raw_stage_present: bool
    packed_stage_present: bool


class PeakAllocationTracker:
    """Sample actual vault files, including atomic-write temporary files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.samples: list[UsageSample] = []
        self.empty_baseline: UsageSample | None = None

    @staticmethod
    def _allocated(path: Path, size: int) -> int:
        try:
            stats = path.stat()
        except FileNotFoundError:
            return 0
        return stats.st_blocks * 512 if hasattr(stats, "st_blocks") else size

    def sample(self, label: str) -> UsageSample:
        totals = {
            "logical": 0,
            "allocated": 0,
            "stage_logical": 0,
            "stage_allocated": 0,
            "events_logical": 0,
            "events_allocated": 0,
        }
        temp_paths: list[str] = []
        if self.root.exists():
            for path in sorted(self.root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root)
                size = path.stat().st_size
                allocated = self._allocated(path, size)
                totals["logical"] += size
                totals["allocated"] += allocated
                if relative.parts and relative.parts[0] == ".staging":
                    totals["stage_logical"] += size
                    totals["stage_allocated"] += allocated
                if relative.parts and (
                    relative.parts[0] in {"events.jsonl", "events.head", ".log"}
                    or path.name.startswith("events.")
                ):
                    totals["events_logical"] += size
                    totals["events_allocated"] += allocated
                if path.name.endswith(".tmp"):
                    temp_paths.append(relative.as_posix())
        result = UsageSample(
            label=label,
            all_in_logical_bytes=totals["logical"],
            all_in_allocated_bytes=totals["allocated"],
            staging_logical_bytes=totals["stage_logical"],
            staging_allocated_bytes=totals["stage_allocated"],
            event_log_logical_bytes=totals["events_logical"],
            event_log_allocated_bytes=totals["events_allocated"],
            temp_paths=tuple(temp_paths),
            raw_stage_present=(self.root / ".staging" / "capture-candidates.jsonl").exists(),
            packed_stage_present=(self.root / ".staging" / "capture-candidates.jsonl.zst").exists(),
        )
        self.samples.append(result)
        return result

    def peak(self, field: str) -> UsageSample:
        if not self.samples:
            return self.sample("initial")
        return max(self.samples, key=lambda sample: getattr(sample, field))


def deterministic_keyness_text_score(
    memory_id: str,
    text: str,
    prior_texts: tuple[str, ...],
    *,
    top_k: int = 5,
) -> float:
    """Return a fixed weighted score from past-only keyness and text features."""
    keyness = 0.0
    if prior_texts:
        keyness = (
            KeynessScorer(
                prior_texts,
                top_k=top_k,
                minimum_term_frequency=1,
            )
            .score_documents({memory_id: text})[memory_id]
            .score
        )
    tokens = set(tokenize(text))
    action_hits = sum(term in tokens for term in ACTION_TERMS)
    action_hits += sum(marker in text for marker in ACTION_TERMS_ZH)
    actionability = min(1.0, action_hits / 2.0)
    specificity = 0.5 * float(bool(_TEMPORAL.search(text))) + 0.5 * float(
        bool(_NUMBER.search(text))
    )
    return min(1.0, max(0.0, 0.70 * keyness + 0.20 * actionability + 0.10 * specificity))


def deterministic_judge_scores(
    records: tuple[Day5Record, ...], *, window_size: int = 20
) -> dict[str, float]:
    """Score text in chronological order using only prior document text."""
    if window_size < 1:
        raise ValueError("window_size must be positive")
    ordered = sorted(records, key=lambda row: (row.timestamp, row.memory_id))
    prior: list[str] = []
    scores: dict[str, float] = {}
    for row in ordered:
        scores[row.memory_id] = deterministic_keyness_text_score(
            row.memory_id, row.text, tuple(prior[-window_size:])
        )
        prior.append(row.text)
    return scores


def _record_sha(records: tuple[Day5Record, ...]) -> str:
    raw = "\n".join(
        json.dumps(
            {"memory_id": row.memory_id, "timestamp": row.timestamp, "text": row.text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in records
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _label_sha(labels: tuple[Day5Label, ...]) -> str:
    raw = "\n".join(
        json.dumps(
            {"memory_id": row.memory_id, "importance": row.importance},
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in labels
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _install_atomic_observer(tracker: PeakAllocationTracker) -> Any:
    """Return a context manager patching the repository write to sample temp peaks."""

    def observed_atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        tracker.sample(f"atomic_temp_fsynced:{path.name}")
        os.replace(temporary, path)
        tracker.sample(f"atomic_replaced:{path.name}")

    return patch.object(Repository, "_atomic_write", staticmethod(observed_atomic_write))


def _intensity(
    strategy: str,
    row: Day5Record,
    *,
    oracle_important: bool | None,
    judge_scores: dict[str, float],
) -> tuple[float, float]:
    if strategy == "always_on":
        return 1.0, 0.7
    if strategy == "existing_intensity":
        return row.capture_intensity, 0.7
    if strategy == "deterministic_keyness_text":
        score = judge_scores[row.memory_id]
        return score, 0.35
    if strategy == "oracle_upper_bound":
        if oracle_important is None:
            raise ValueError("oracle strategy requires an evaluation label")
        return (1.0 if oracle_important else 0.0), 0.7
    raise ValueError(f"unknown strategy: {strategy}")


def _run_strategy(
    strategy: str,
    records: tuple[Day5Record, ...],
    important_ids: set[str],
    judge_scores: dict[str, float],
    root: Path,
    *,
    session_index: int,
) -> dict[str, Any]:
    vault_root = root / f"{session_index:02d}-{strategy}"
    clock_value = [datetime(2026, 1, 1, tzinfo=UTC)]
    id_counter = [0]

    def deterministic_clock() -> datetime:
        return clock_value[0]

    def deterministic_id() -> str:
        id_counter[0] += 1
        return f"day7-event-{id_counter[0]:06d}"

    repository = Repository(vault_root, clock=deterministic_clock, id_factory=deterministic_id)
    tracker = PeakAllocationTracker(vault_root)
    with _install_atomic_observer(tracker):
        repository.init()
        empty = tracker.sample("initialized_empty_vault")
        session = CaptureSession(
            repository=repository,
            session_id=f"day7-{session_index}-{strategy}",
            high_threshold=0.35 if strategy == "deterministic_keyness_text" else 0.7,
            low_threshold=0.15 if strategy == "deterministic_keyness_text" else 0.3,
            standby_seconds=30.0,
            dormant_seconds=120.0,
            ring_seconds=30.0,
        )
        for row in records:
            row_time = datetime.fromisoformat(row.timestamp).astimezone(UTC)
            clock_value[0] = row_time + timedelta(seconds=1)
            oracle_label = (
                row.memory_id in important_ids if strategy == "oracle_upper_bound" else None
            )
            intensity, threshold = _intensity(
                strategy,
                row,
                oracle_important=oracle_label,
                judge_scores=judge_scores,
            )
            if session.high_threshold != threshold:
                session.high_threshold = threshold
                session.low_threshold = threshold * 0.5
            session.feed(
                TimedSignal(
                    timestamp=row_time,
                    source="model",
                    action="content",
                    intensity=intensity,
                    text=row.text,
                    source_id=row.memory_id,
                )
            )
            tracker.sample("after_signal_feed")
        candidates = session.items()
        candidate_ids = sorted({item.source_id for item in candidates if item.source_id})
        pack_result = pack_capture_candidates(repository)
        tracker.sample("after_pack_raw_removed")
        packed_stage = tracker.samples[-1]
        acquisition_and_pack_samples = list(tracker.samples)

        decisions = [f"{item.candidate_id}=keep" for item in candidates]
        if records:
            clock_value[0] = datetime.fromisoformat(records[-1].timestamp).astimezone(
                UTC
            ) + timedelta(days=1)
        confirm_candidates(repository, decisions)
        tracker.sample("after_human_keep_confirmation")
        persistent_ids = sorted(
            document.id for document in repository.list() if document.id in set(candidate_ids)
        )
        final = tracker.sample("final_after_confirmation")
        peak_all = tracker.peak("all_in_allocated_bytes")
        peak_stage = tracker.peak("staging_allocated_bytes")
        peak_all_logical = tracker.peak("all_in_logical_bytes")
        peak_stage_logical = tracker.peak("staging_logical_bytes")
        peak_acquisition_allocated = max(
            acquisition_and_pack_samples,
            key=lambda sample: sample.all_in_allocated_bytes,
        )
        peak_acquisition_logical = max(
            acquisition_and_pack_samples,
            key=lambda sample: sample.all_in_logical_bytes,
        )
        acquisition_sample_ids = {id(sample) for sample in acquisition_and_pack_samples}
        post_confirmation_samples = [
            sample for sample in tracker.samples if id(sample) not in acquisition_sample_ids
        ]
        peak_post_confirmation = max(
            post_confirmation_samples or acquisition_and_pack_samples,
            key=lambda sample: sample.all_in_allocated_bytes,
        )
        atomic_temp_samples = [
            sample for sample in tracker.samples if sample.label.startswith("atomic_temp_fsynced:")
        ]
        atomic_peak = max(
            atomic_temp_samples,
            key=lambda sample: sample.all_in_allocated_bytes,
            default=None,
        )
        pack_coexistence_samples = [
            sample
            for sample in tracker.samples
            if sample.packed_stage_present and sample.raw_stage_present
        ]
        # The state machine may leave a final temporary marker; a final sample
        # makes the reported endpoint explicit without replacing sampled peaks.
        return {
            "strategy": strategy,
            "high_threshold": session.high_threshold,
            "input_count": len(records),
            "candidate_count": len(candidate_ids),
            "candidate_fraction": len(candidate_ids) / len(records) if records else 0.0,
            "candidate_ids": candidate_ids,
            "important_candidate_count": len(set(candidate_ids) & important_ids),
            "important_total": len(important_ids),
            "important_recall": (
                len(set(candidate_ids) & important_ids) / len(important_ids)
                if important_ids
                else None
            ),
            "recall_eligible": (
                len(set(candidate_ids) & important_ids) / len(important_ids) >= 0.70
                if important_ids
                else False
            ),
            "persistent_memory_count": len(persistent_ids),
            "persistent_memory_fraction": len(persistent_ids) / len(records) if records else 0.0,
            "persistent_memory_ids": persistent_ids,
            "packed_stage_logical_bytes": packed_stage.staging_logical_bytes,
            "packed_stage_allocated_bytes": packed_stage.staging_allocated_bytes,
            "final_all_in_logical_bytes": final.all_in_logical_bytes,
            "final_all_in_allocated_bytes": final.all_in_allocated_bytes,
            "peak_stage_logical_bytes": peak_stage_logical.staging_logical_bytes,
            "peak_stage_allocated_bytes": peak_stage.staging_allocated_bytes,
            "peak_all_in_logical_bytes": peak_all_logical.all_in_logical_bytes,
            "peak_all_in_allocated_bytes": peak_all.all_in_allocated_bytes,
            "peak_sample": peak_all.label,
            "peak_sample_temp_paths": list(peak_all.temp_paths),
            "peak_acquisition_and_packing_logical_bytes": (
                peak_acquisition_logical.all_in_logical_bytes
            ),
            "peak_acquisition_and_packing_allocated_bytes": (
                peak_acquisition_allocated.all_in_allocated_bytes
            ),
            "peak_acquisition_and_packing_sample": peak_acquisition_allocated.label,
            "peak_after_simulated_confirmation_allocated_bytes": (
                peak_post_confirmation.all_in_allocated_bytes
            ),
            "peak_after_simulated_confirmation_sample": peak_post_confirmation.label,
            "atomic_temp_peak_all_in_allocated_bytes": (
                atomic_peak.all_in_allocated_bytes if atomic_peak is not None else None
            ),
            "atomic_temp_peak_staging_allocated_bytes": (
                atomic_peak.staging_allocated_bytes if atomic_peak is not None else None
            ),
            "atomic_temp_peak_sample": atomic_peak.label if atomic_peak is not None else None,
            "atomic_temp_peak_paths": list(atomic_peak.temp_paths) if atomic_peak else [],
            "pack_raw_coexistence_observed": bool(pack_coexistence_samples),
            "pack_raw_coexistence_peak_allocated_bytes": max(
                (sample.all_in_allocated_bytes for sample in pack_coexistence_samples),
                default=None,
            ),
            "empty_vault_logical_bytes": empty.all_in_logical_bytes,
            "empty_vault_allocated_bytes": empty.all_in_allocated_bytes,
            "event_log_baseline_logical_bytes": empty.event_log_logical_bytes,
            "event_log_baseline_allocated_bytes": empty.event_log_allocated_bytes,
            "event_log_final_logical_bytes": final.event_log_logical_bytes,
            "event_log_final_allocated_bytes": final.event_log_allocated_bytes,
            "event_log_growth_logical_bytes": max(
                0, final.event_log_logical_bytes - empty.event_log_logical_bytes
            ),
            "event_log_growth_allocated_bytes": max(
                0, final.event_log_allocated_bytes - empty.event_log_allocated_bytes
            ),
            "atomic_write_samples": len(atomic_temp_samples),
            "pack_summary": pack_result,
            "simulated_eval_keep_confirmations": len(decisions),
            "measurement_sample_count": len(tracker.samples),
        }


def run_gating(
    *, count: int, seed: int, rho_capture: float, rho_access: float, prevalence: float
) -> dict[str, Any]:
    if count < 2:
        raise ValueError("count must be at least 2")
    if not 0.0 < prevalence < 1.0:
        raise ValueError("prevalence must be between 0 and 1")
    records, labels, _access_events, generation = generate_day6_dataset(
        count=count,
        seed=seed,
        rho_capture=rho_capture,
        rho_access=rho_access,
        important_prevalence=prevalence,
    )
    important_ids = {item.memory_id for item in labels if item.importance == "important"}
    # The feature classifier is prepared without passing any label object.
    judge_scores = deterministic_judge_scores(records)
    input_sha = _record_sha(records)
    label_sha = _label_sha(labels)
    with tempfile.TemporaryDirectory(prefix="memfw-day7-gating-") as directory:
        temp_root = Path(directory)
        results = [
            _run_strategy(
                strategy,
                records,
                important_ids,
                judge_scores,
                temp_root,
                session_index=index,
            )
            for index, strategy in enumerate(POLICIES)
        ]
    always_on = next(result for result in results if result["strategy"] == "always_on")
    for result in results:
        result["packed_stage_logical_fraction_vs_always_on"] = (
            result["packed_stage_logical_bytes"] / always_on["packed_stage_logical_bytes"]
            if always_on["packed_stage_logical_bytes"]
            else 0.0
        )
        result["packed_stage_allocated_fraction_vs_always_on"] = (
            result["packed_stage_allocated_bytes"] / always_on["packed_stage_allocated_bytes"]
            if always_on["packed_stage_allocated_bytes"]
            else 0.0
        )
        result["final_all_in_logical_fraction_vs_always_on"] = (
            result["final_all_in_logical_bytes"] / always_on["final_all_in_logical_bytes"]
            if always_on["final_all_in_logical_bytes"]
            else 0.0
        )
        result["final_all_in_allocated_fraction_vs_always_on"] = (
            result["final_all_in_allocated_bytes"] / always_on["final_all_in_allocated_bytes"]
            if always_on["final_all_in_allocated_bytes"]
            else 0.0
        )
        result["peak_all_in_allocated_fraction_vs_always_on"] = (
            result["peak_all_in_allocated_bytes"] / always_on["peak_all_in_allocated_bytes"]
            if always_on["peak_all_in_allocated_bytes"]
            else 0.0
        )
        result["h6_recall_eligible"] = bool(result["recall_eligible"])
        result["h6_stage_allocated_fraction"] = result[
            "packed_stage_allocated_fraction_vs_always_on"
        ]
        result["h6_stage_logical_fraction"] = result["packed_stage_logical_fraction_vs_always_on"]
        result["h6_allocated_pass"] = result["h6_recall_eligible"] and (
            result["h6_stage_allocated_fraction"] <= 0.20
        )
        result["h6_logical_pass"] = result["h6_recall_eligible"] and (
            result["h6_stage_logical_fraction"] <= 0.20
        )
        result["h6_allocated_gap_to_20pct"] = result["h6_stage_allocated_fraction"] - 0.20
        result["h6_logical_gap_to_20pct"] = result["h6_stage_logical_fraction"] - 0.20
    oracle_result = next(row for row in results if row["strategy"] == "oracle_upper_bound")
    oracle_ideal_count = len(important_ids)
    oracle_captured_important = oracle_result["important_candidate_count"]
    oracle_overcapture = oracle_result["candidate_count"] - oracle_captured_important
    return {
        "schema_version": 1,
        "evaluation_only": True,
        "label_boundary": (
            "Only the oracle and metrics receive labels. The feature judge receives text only."
        ),
        "confirmation_boundary": (
            "For storage measurement, the evaluator simulates keep confirmation "
            "for every staged candidate."
        ),
        "peak_boundary": (
            "Operation-boundary peak, sampled after feed and at atomic-temp fsync before replace, "
            "after replace, and after pack. It is not continuous filesystem tracing."
        ),
        "oracle_boundary": (
            "The ideal selector is label-important IDs only. The realized upper-bound strategy "
            "passes the labels through the same capture state machine; report ideal and realized "
            "counts and overcapture separately."
        ),
        "seed": seed,
        "count": count,
        "rho_capture_requested": rho_capture,
        "rho_access_requested": rho_access,
        "important_prevalence_requested": prevalence,
        "generation": generation,
        "oracle_ideal_candidate_count": oracle_ideal_count,
        "oracle_ideal_candidate_ids": sorted(important_ids),
        "oracle_realized_candidate_count": oracle_result["candidate_count"],
        "oracle_realized_important_count": oracle_captured_important,
        "oracle_realized_overcapture_count": oracle_overcapture,
        "oracle_realized_missed_important_count": (oracle_ideal_count - oracle_captured_important),
        "oracle_realized_recall": oracle_result["important_recall"],
        "oracle_realized_matches_ideal": oracle_result["candidate_count"] == oracle_ideal_count,
        "input_sha256": input_sha,
        "label_sha256": label_sha,
        "judges": {
            "oracle_upper_bound": (
                "Capture label-important records; evaluation-only upper bound, "
                "not production policy."
            ),
            "deterministic_keyness_text": (
                "0.70 rolling G2 keyness + 0.20 fixed action-term features + "
                "0.10 temporal/numeric specificity; past-only window=20, cutoff=.35."
            ),
            "existing_intensity": "Generated intensity baseline, threshold=.7.",
            "always_on": "intensity=1.0 for every input record",
        },
        "metrics": {
            "important_recall": "captured important source IDs / all label-important IDs",
            "candidate_fraction": "unique staged full-capture IDs / input IDs",
            "persistent_memory_fraction": "confirmed persistent source IDs / input IDs",
            "storage_fractions": (
                "Candidate and persistent-ID fractions are separate. Stage/all-in logical and "
                "allocated bytes are divided by matching always-on values."
            ),
            "peak": "Operation-boundary samples; not continuous filesystem tracing.",
            "recall_gate": "eligible iff important recall >= 0.70",
            "h6_gate": (
                "Eligible iff recall >= .70. Pass is reported separately for packed-stage "
                "allocated bytes and logical bytes <= 20% of always-on, with gap to threshold."
            ),
            "event_log": "Empty-vault baseline and event-growth are reported separately.",
        },
        "strategies": results,
        "recall_eligible_strategies": sum(bool(row["recall_eligible"]) for row in results),
        "h6_allocated_passes": sum(bool(row["h6_allocated_pass"]) for row in results),
        "h6_allocated_eligible_strategies": sum(bool(row["h6_recall_eligible"]) for row in results),
        "h6_logical_passes": sum(bool(row["h6_logical_pass"]) for row in results),
        "h6_logical_eligible_strategies": sum(bool(row["h6_recall_eligible"]) for row in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/day7_gating.json"))
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--rho-capture", type=float, default=0.3)
    parser.add_argument("--rho-access", type=float, default=0.3)
    parser.add_argument("--important-prevalence", type=float, default=0.10)
    parser.add_argument(
        "--quick", action="store_true", help="cap the deterministic gate run at 40 records"
    )
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be at least 2")
    count = min(args.count, 40) if args.quick else args.count
    report = run_gating(
        count=count,
        seed=args.seed,
        rho_capture=args.rho_capture,
        rho_access=args.rho_access,
        prevalence=args.important_prevalence,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps(
            {"output": str(args.output), "count": count, "input_sha256": report["input_sha256"]},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
