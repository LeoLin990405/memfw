"""Offline training-candidate export and data-quality checks.

This module does not train anything. It turns explicitly collected feedback
into provenance-bearing pairs for a later, separately approved experiment.
Uncited results stay unknown and are never silently used as negatives.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .feedback import FeedbackStore

LabelSource = Literal["citation", "correction", "teacher", "human"]
Split = Literal["train", "validation", "test", "unassigned"]


@dataclass(frozen=True)
class TrainingPair:
    query: str
    positive_path: str
    negative_paths: tuple[str, ...] = ()
    label_source: LabelSource = "citation"
    confidence: float = 0.0
    task_id: str = ""
    trace_id: str = ""
    split: Split = "unassigned"


@dataclass
class ExportReport:
    pairs: list[TrainingPair] = field(default_factory=list)
    skipped_unknown: int = 0
    skipped_missing: int = 0
    skipped_duplicate: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    by_split: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": [
                {
                    "query": pair.query,
                    "positive_path": pair.positive_path,
                    "negative_paths": list(pair.negative_paths),
                    "label_source": pair.label_source,
                    "confidence": pair.confidence,
                    "task_id": pair.task_id,
                    "trace_id": pair.trace_id,
                    "split": pair.split,
                }
                for pair in self.pairs
            ],
            "skipped_unknown": self.skipped_unknown,
            "skipped_missing": self.skipped_missing,
            "skipped_duplicate": self.skipped_duplicate,
            "by_source": self.by_source,
            "by_split": self.by_split,
        }


def _split_for_task(task_id: str) -> Split:
    bucket = int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8], 16) % 10
    if bucket < 7:
        return "train"
    if bucket < 9:
        return "validation"
    return "test"


def export_pairs(
    store: FeedbackStore,
    *,
    min_confidence: float = 0.0,
    max_pairs: int | None = None,
) -> ExportReport:
    traces = store.traces()
    feedback = store.feedback()
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for event in feedback:
        by_trace.setdefault(str(event["trace_id"]), []).append(event)
    report = ExportReport()
    seen: set[tuple[str, str]] = set()
    for trace in traces:
        task_id = str(trace.get("task_id") or "")
        trace_id = str(trace.get("trace_id") or "")
        if not task_id or not trace_id:
            report.skipped_missing += 1
            continue
        events = by_trace.get(trace_id, [])
        if not events:
            report.skipped_unknown += 1
            continue
        positives: list[tuple[str, float]] = []
        negatives: list[tuple[str, float]] = []
        for event in events:
            kind = str(event.get("kind") or "")
            path = str(event.get("candidate_path") or "")
            relevance = str(event.get("relevance") or "unknown")
            correctness = str(event.get("correctness") or "unknown")
            confidence = float(event.get("confidence") or 0.0)
            if confidence < min_confidence:
                continue
            if kind == "citation" and relevance in {"relevant", "weak_positive"} and path:
                positives.append((path, confidence))
            elif kind == "citation" and correctness == "incorrect" and path:
                negatives.append((path, confidence))
            elif kind == "correction" and path:
                negatives.append((path, confidence))
        if not positives:
            report.skipped_unknown += 1
            continue
        negative_paths = tuple(
            path for path, _ in negatives if path and path not in {p for p, _ in positives}
        )
        for path, confidence in positives:
            key = (str(trace.get("query") or ""), path)
            if key in seen:
                report.skipped_duplicate += 1
                continue
            seen.add(key)
            report.pairs.append(
                TrainingPair(
                    query=str(trace.get("query") or ""),
                    positive_path=path,
                    negative_paths=negative_paths,
                    label_source="citation",
                    confidence=confidence,
                    task_id=task_id,
                    trace_id=trace_id,
                    split=_split_for_task(task_id),
                )
            )
            if max_pairs is not None and len(report.pairs) >= max_pairs:
                break
        if max_pairs is not None and len(report.pairs) >= max_pairs:
            break
    report.by_source = dict(Counter(pair.label_source for pair in report.pairs))
    report.by_split = dict(Counter(pair.split for pair in report.pairs))
    return report


def write_pairs(report: ExportReport, path: Path | str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in report.as_dict()["pairs"]),
        encoding="utf-8",
    )


def check_pairs(pairs: list[TrainingPair]) -> dict[str, Any]:
    """Data-quality checks; returns counts and errors, never modifies labels."""
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        if not pair.query.strip() or not pair.positive_path:
            errors.append("pair missing query or positive_path")
        if pair.positive_path in pair.negative_paths:
            errors.append("positive path appears in negatives")
        key = (pair.query, pair.positive_path)
        if key in seen:
            errors.append("duplicate pair")
        seen.add(key)
    splits = Counter(pair.split for pair in pairs)
    task_splits: dict[str, set[str]] = {}
    for pair in pairs:
        task_splits.setdefault(pair.task_id, set()).add(pair.split)
    leaked = sorted(task for task, values in task_splits.items() if len(values) > 1)
    if leaked:
        errors.append(f"task leakage across splits: {', '.join(leaked[:5])}")
    return {"pairs": len(pairs), "splits": dict(splits), "errors": errors}


__all__ = [
    "ExportReport",
    "Split",
    "TrainingPair",
    "check_pairs",
    "export_pairs",
    "write_pairs",
]
