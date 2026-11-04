#!/usr/bin/env python3
"""Run the bounded, offline Day 4 capture and retrieval evaluation."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gen_day4 import SEED, Day4Record, EvaluationLabel, generate_dataset, write_dataset  # noqa: E402

from memfw.agents import DeterministicJudge  # noqa: E402
from memfw.budget import BudgetLimits  # noqa: E402
from memfw.capture import CaptureSession, TimedSignal  # noqa: E402
from memfw.core.models import CaptureState, EventKind, MemoryDocument  # noqa: E402
from memfw.core.repository import Repository  # noqa: E402
from memfw.curation.engine import access_projection  # noqa: E402
from memfw.policies import PolicyCandidate, PolicyName, make_policy  # noqa: E402
from memfw.stats.keyness import KeynessScorer, observable_strength  # noqa: E402
from memfw.stats.runtime import measure_vault  # noqa: E402
from memfw.stats.tail_fit import fit_discrete_tail  # noqa: E402

THRESHOLDS = (0.35, 0.45, 0.55, 0.65, 0.75, 0.85)
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _event_id_factory(prefix: str, counter: Iterator[int]) -> Callable[[], str]:
    def next_id() -> str:
        return f"{prefix}-{next(counter):08d}"

    return next_id


def _fixed_clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _capture_sweep(
    records: tuple[Day4Record, ...],
    labels: tuple[EvaluationLabel, ...],
    work_root: Path,
    *,
    ring_intervals: float = 2.0,
) -> list[dict[str, Any]]:
    truth = {label.memory_id: label.importance for label in labels}
    important_total = sum(value == "important" for value in truth.values())
    noise_total = sum(value == "noise" for value in truth.values())
    output: list[dict[str, Any]] = []
    interval_seconds = (
        (
            datetime.fromisoformat(records[1].timestamp)
            - datetime.fromisoformat(records[0].timestamp)
        ).total_seconds()
        if len(records) > 1
        else 0.0
    )
    ring_seconds = interval_seconds * ring_intervals
    work_root.mkdir(parents=True, exist_ok=True)
    baseline_counter = iter(range(1, len(records) * 2 + 100))
    always_on = Repository(
        work_root / "always-on",
        clock=lambda: NOW,
        id_factory=_event_id_factory("d4-allon", baseline_counter),
    )
    always_on.init()
    baseline_session = CaptureSession(
        repository=always_on,
        session_id="d4-always-on",
        initial_state=CaptureState.DORMANT,
    )
    first_timestamp = datetime.fromisoformat(records[0].timestamp)
    baseline_session.feed(
        TimedSignal(timestamp=first_timestamp, source="human", action="remember", intensity=1.0)
    )
    for row in records:
        baseline_session.feed(
            TimedSignal(
                timestamp=datetime.fromisoformat(row.timestamp),
                source="model",
                action="content",
                intensity=row.capture_intensity,
                text=row.text,
                source_id=row.memory_id,
            )
        )
    always_on_bytes = measure_vault(always_on).physical_vault_bytes
    for threshold in THRESHOLDS:
        event_counter = iter(range(1, len(records) * 3 + 1))
        repository: Any = Repository(
            work_root / f"threshold-{int(threshold * 100)}",
            clock=lambda: NOW,
            id_factory=_event_id_factory("d4-event", event_counter),
        )
        repository.init()
        session = CaptureSession(
            repository=repository,
            session_id=f"d4-threshold-{int(threshold * 100)}",
            initial_state=CaptureState.DORMANT,
            high_threshold=threshold,
            low_threshold=0.25,
            standby_seconds=30 * 60,
            dormant_seconds=48 * 60 * 60,
            ring_seconds=ring_seconds,
        )
        for row in records:
            timestamp = datetime.fromisoformat(row.timestamp)
            session.feed(
                TimedSignal(
                    timestamp=timestamp,
                    source="model",
                    action="content",
                    intensity=row.capture_intensity,
                    text=row.text,
                    source_id=row.memory_id,
                )
            )
        end = datetime.fromisoformat(records[0].timestamp) + timedelta(days=30)
        session.advance_to(end)
        captured_ids = {item.source_id for item in session.items() if item.source_id}
        staged_candidates = {item.candidate_id: item for item in session.items()}
        backfilled_ids = {
            candidate.source_id
            for event in cast(Any, repository).events()
            if event.kind == EventKind.CAPTURE_RECORD
            and isinstance(event.details.get("candidate_id"), str)
            and (candidate := staged_candidates.get(str(event.details["candidate_id"]))) is not None
            and candidate.source_id is not None
            and 0 < (event.timestamp - candidate.timestamp).total_seconds() <= ring_seconds
        }
        captured_important = sum(
            truth.get(identifier) == "important" for identifier in captured_ids
        )
        captured_noise = sum(truth.get(identifier) == "noise" for identifier in captured_ids)
        elapsed = (end - datetime.fromisoformat(records[0].timestamp)).total_seconds()
        duty_seconds = 0.0
        prior = datetime.fromisoformat(records[0].timestamp)
        state = CaptureState.DORMANT
        for event in session.events:
            delta = max(0.0, (event.timestamp - prior).total_seconds())
            if state == CaptureState.FULL:
                duty_seconds += delta
            prior, state = event.timestamp, event.state
        if state == CaptureState.FULL:
            duty_seconds += max(0.0, (end - prior).total_seconds())
        wake_events = [event for event in session.events if event.kind == EventKind.CAPTURE_WAKE]
        by_timestamp = {datetime.fromisoformat(row.timestamp): row.memory_id for row in records}
        wake_labels = [
            truth[by_timestamp[event.timestamp]]
            for event in wake_events
            if event.timestamp in by_timestamp
        ]
        wakes = len(wake_events)
        physical = measure_vault(repository).as_dict()
        output.append(
            {
                "threshold": threshold,
                "important_captured": captured_important,
                "important_total": important_total,
                "important_recall": captured_important / important_total
                if important_total
                else 0.0,
                "noise_captured": captured_noise,
                "noise_total": noise_total,
                "noise_retention": captured_noise / noise_total if noise_total else 0.0,
                "candidate_count": len(captured_ids),
                "ring_backfilled_candidates": len(backfilled_ids),
                "physical_vault_bytes": physical["physical_vault_bytes"],
                "always_on_physical_vault_bytes": always_on_bytes,
                "h6_storage_ratio": physical["physical_vault_bytes"] / always_on_bytes
                if always_on_bytes
                else 0.0,
                "h6_meets_20_percent": physical["physical_vault_bytes"] / always_on_bytes <= 0.20
                if always_on_bytes
                else False,
                "physical_breakdown": {
                    "events": physical["events_bytes"],
                    "history": physical["history_bytes"],
                    "memories": physical["memories_bytes"],
                    "anchors": physical["anchors_bytes"],
                    "archives": physical["archive_bytes"],
                    "index": physical["index_bytes"],
                    "other": physical["other_bytes"],
                },
                "wake_count": wakes,
                "false_wakes": sum(value == "noise" for value in wake_labels),
                "false_wake_rate": sum(value == "noise" for value in wake_labels) / wakes
                if wakes
                else 0.0,
                "elapsed_seconds": elapsed,
                "ring_window_seconds": ring_seconds,
                "record_interval_seconds": interval_seconds,
                "standby_seconds": 30 * 60,
                "dormant_seconds": 48 * 60 * 60,
                "full_duty_seconds": duty_seconds,
                "full_duty_fraction": duty_seconds / elapsed if elapsed else 0.0,
                "denominators": {
                    "important_records": important_total,
                    "noise_records": noise_total,
                    "wake_events": wakes,
                    "elapsed_seconds": elapsed,
                },
            }
        )
    return output


def _keyness_eval(
    records: tuple[Day4Record, ...], labels: tuple[EvaluationLabel, ...]
) -> dict[str, Any]:
    scores = KeynessScorer().score_rolling_documents(
        [(row.memory_id, datetime.fromisoformat(row.timestamp), row.text) for row in records],
        window_size=20,
    )
    truth = {label.memory_id: label.importance for label in labels}
    ordered = sorted(records, key=lambda row: (-scores[row.memory_id].score, row.memory_id))
    top_n = max(1, len(records) // 10)
    top_ids = {row.memory_id for row in ordered[:top_n]}
    important_scores = [scores[key].score for key, label in truth.items() if label == "important"]
    other_scores = [scores[key].score for key, label in truth.items() if label != "important"]
    mock_scores: dict[str, float] = {}
    with TemporaryDirectory(prefix="memfw-day4-observables-") as temp:
        event_counter = iter(range(1, len(records) * 8 + 10))
        repository = Repository(
            Path(temp) / "vault",
            clock=lambda: NOW,
            id_factory=_event_id_factory("day4-observable", event_counter),
        )
        repository.init()
        for row in records:
            timestamp = datetime.fromisoformat(row.timestamp)
            repository.clock = _fixed_clock(timestamp)
            repository.add(
                f"Note {row.memory_id}",
                row.text,
                memory_id=row.memory_id,
                source="synthetic-day4",
                provenance="synthetic-source",
                tags=[],
            )
        access_events: list[tuple[datetime, str]] = []
        for index, row in enumerate(records):
            created = datetime.fromisoformat(row.timestamp)
            available_seconds = max(0.0, (NOW - created).total_seconds())
            count = index % 4
            for ordinal in range(count):
                fraction = (ordinal + 1) / (count + 1)
                access_events.append(
                    (created + timedelta(seconds=available_seconds * fraction), row.memory_id)
                )
        for timestamp, memory_id in sorted(access_events):
            repository.clock = _fixed_clock(timestamp)
            repository.capture_event(
                EventKind.MEMORY_ACCESS,
                actor="mechanism",
                timestamp=timestamp,
                details={"memory_id": memory_id, "operation": "retrieve"},
            )
        access = access_projection(repository)
        raw_documents = repository.list()
        memory_docs: list[MemoryDocument] = []
        candidates: list[PolicyCandidate] = []
        for document in raw_documents:
            last_accessed, access_count = access.get(document.id, (None, 0))
            keyness_score = scores[document.id].score
            strength = observable_strength(
                access_count,
                last_accessed,
                NOW,
                keyness_score,
                created_at=document.created_at,
            )
            enriched = document.model_copy(
                update={
                    "strength": strength,
                }
            )
            memory_docs.append(enriched)
            candidates.append(
                replace(
                    PolicyCandidate.from_document(enriched),
                    last_accessed_at=last_accessed,
                    access_count=access_count,
                    keyness=keyness_score,
                )
            )
        logged_access_count = sum(count for _, count in access.values())
        access_counts = [access.get(document.id, (None, 0))[1] for document in raw_documents]
        reference_counts = [document.reference_count for document in raw_documents]
        topic_frequencies = list(Counter(label.theme for label in labels).values())
        tail_diagnostics = {
            "access_count": asdict(fit_discrete_tail(access_counts)),
            "reference_count": asdict(fit_discrete_tail(reference_counts)),
            "evaluation_only_topic_frequency": asdict(fit_discrete_tail(topic_frequencies)),
            "minimum_sample": 20,
            "interpretation": (
                "Continuous-tail approximation for discrete samples; KS is descriptive only; "
                "no bootstrap goodness-of-fit p-value or Clauset statistical-support claim."
            ),
        }
        policy_plan = make_policy(PolicyName.SALIENCE).plan(
            candidates,
            BudgetLimits(max_count=max(1, len(records) * 4 // 5)),
            now=NOW,
        )
    judge_proposals = DeterministicJudge().score_salience(memory_docs)
    for proposal in judge_proposals:
        value = proposal.output.get("score", 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("mock judge returned a nonnumeric score")
        mock_scores[proposal.target_ids[0]] = float(value)
    judge_top_ids = {
        key for key, _ in sorted(mock_scores.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    }
    judge_payload = json.dumps(
        [item.model_dump(mode="json") for item in judge_proposals], sort_keys=True
    )
    policy_payload = json.dumps(
        [
            {
                "id": item.id,
                "body": item.body,
                "created_at": item.created_at.isoformat(),
                "last_accessed_at": (
                    item.last_accessed_at.isoformat() if item.last_accessed_at else None
                ),
                "access_count": item.access_count,
                "strength": item.strength,
                "keyness": item.keyness,
            }
            for item in candidates
        ],
        sort_keys=True,
    )
    forbidden = ("important", "noise", *{label.theme for label in labels})
    if any(
        token in payload.casefold()
        for payload in (judge_payload, policy_payload)
        for token in forbidden
    ):
        raise RuntimeError("evaluation labels leaked into judge or policy payload")
    retained_important = sum(
        truth.get(identifier) == "important" for identifier in policy_plan.retained_ids
    )
    retained_noise = sum(
        truth.get(identifier) == "noise" for identifier in policy_plan.retained_ids
    )
    return {
        "top_decile_count": len(top_ids),
        "top_decile_important": sum(truth[key] == "important" for key in top_ids),
        "top_decile_important_total": sum(value == "important" for value in truth.values()),
        "top_decile_precision": sum(truth[key] == "important" for key in top_ids) / len(top_ids),
        "overall_important_rate": sum(value == "important" for value in truth.values())
        / len(truth),
        "important_mean_keyness": sum(important_scores) / len(important_scores),
        "other_mean_keyness": sum(other_scores) / len(other_scores),
        "keyness_top_decile_mean": sum(scores[key].score for key in top_ids) / len(top_ids),
        "mock_judge": {
            "score_count": len(mock_scores),
            "important_mean_score": sum(
                mock_scores[key] for key, label in truth.items() if label == "important"
            )
            / len(important_scores),
            "other_mean_score": sum(
                mock_scores[key] for key, label in truth.items() if label != "important"
            )
            / len(other_scores),
            "top_decile_important_precision": sum(
                truth[key] == "important" for key in judge_top_ids
            )
            / len(judge_top_ids),
            "top_decile_overlap_with_keyness": len(judge_top_ids & top_ids) / top_n,
            "payload_label_leakage_checked": True,
            "interpretation": "deterministic hash baseline only; not a semantic model score",
        },
        "keyness_vs_mock_top_decile_overlap": len(judge_top_ids & top_ids) / top_n,
        "comparison_note": (
            "Both rankers saw the same label-free document text; labels were consulted "
            "only for the reported evaluation metrics."
        ),
        "observable_strength_example": observable_strength(3, NOW - timedelta(days=2), NOW, 0.7),
        "observable_strength_inputs": {
            "logged_access_events": logged_access_count,
            "documents_with_access": sum(count > 0 for _, count in access.values()),
            "documents_without_access": len(records)
            - sum(count > 0 for _, count in access.values()),
            "mean_strength": sum(doc.strength for doc in memory_docs) / len(memory_docs),
            "policy": "salience",
            "policy_candidate_count": len(candidates),
            "policy_retained_count": len(policy_plan.retained_ids),
            "policy_forgotten_count": len(policy_plan.forgotten_ids),
            "policy_retained_important": retained_important,
            "policy_retained_important_total": sum(
                value == "important" for value in truth.values()
            ),
            "policy_important_retention": retained_important
            / sum(value == "important" for value in truth.values()),
            "policy_retained_noise": retained_noise,
            "policy_retained_noise_total": sum(value == "noise" for value in truth.values()),
            "policy_noise_retention": retained_noise
            / sum(value == "noise" for value in truth.values()),
            "policy_keyness_mean": sum(item.keyness for item in candidates) / len(candidates),
            "policy_reference_count_total": sum(item.reference_count for item in candidates),
            "actual_inputs_include_access_recency_keyness": True,
            "judge_and_policy_label_leakage_checked": True,
        },
        "score_count": len(scores),
        "keyness_view": "rolling previous-document reference",
        "keyness_window_size": 20,
        "tail_diagnostics": tail_diagnostics,
        "mean_keyness": sum(value.score for value in scores.values()) / len(scores),
        "labels_used_for_scoring": False,
        "random_rank_reference": {
            "expected_top_decile_precision": sum(value == "important" for value in truth.values())
            / len(truth)
        },
    }


def run_experiment(
    output_dir: Path, *, seed: int = SEED, count: int = 1_000, ring_intervals: float = 2.0
) -> dict[str, Any]:
    records, labels = generate_dataset(seed=seed, count=count)
    dataset_path = output_dir / "day4_synthetic.jsonl"
    labels_path = output_dir / "day4_labels.jsonl"
    capture_path = output_dir / "day4_capture.jsonl"
    write_dataset(
        dataset_path, labels_output=labels_path, capture_output=capture_path, seed=seed, count=count
    )
    with TemporaryDirectory(prefix="memfw-day4-capture-") as capture_temp:
        capture_metrics = _capture_sweep(
            records, labels, Path(capture_temp), ring_intervals=ring_intervals
        )
    return {
        "seed": seed,
        "record_count": count,
        "ring_intervals": ring_intervals,
        "theme_count": len({label.theme for label in labels}),
        "importance_counts": dict(Counter(label.importance for label in labels)),
        "quiet_important_count": sum(
            label.importance == "important" and row.capture_intensity < 0.35
            for row, label in zip(records, labels, strict=True)
        ),
        "capture_thresholds": capture_metrics,
        "keyness": _keyness_eval(records, labels),
        "artifacts": {
            "dataset": dataset_path.name,
            "labels": labels_path.name,
            "capture_stream": capture_path.name,
        },
        "status": "partial: storage size matrix and legacy policy comparison pending integration",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--count", type=int, default=1_000)
    parser.add_argument("--ring-intervals", type=float, default=2.0)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.ring_intervals <= 0:
        parser.error("--ring-intervals must be positive")
    report = run_experiment(
        args.output_dir, seed=args.seed, count=args.count, ring_intervals=args.ring_intervals
    )
    runtime_seconds = round(time.perf_counter() - started, 6)
    target = args.json or args.output_dir / "day4_experiment.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    receipt = {
        "runtime_seconds": runtime_seconds,
        "record_count": args.count,
        "bounded_quick_run_under_5m": runtime_seconds < 300,
    }
    (args.output_dir / "day4_runtime_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"report": str(target), **receipt}, sort_keys=True))


if __name__ == "__main__":
    main()
