#!/usr/bin/env python3
"""Generate a clean, deterministic Day 4 memory and capture dataset.

Evaluation labels are written to a separate file and must only be loaded after
capture decisions have been made.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

SEED = 20261018
RECORD_COUNT = 1_000
SPAN_DAYS = 30
START = datetime(2026, 9, 18, tzinfo=UTC)

Importance = Literal["important", "normal", "noise"]
THEMES: tuple[str, ...] = (
    "operations",
    "security",
    "research",
    "product",
    "learning",
    "travel",
    "chores",
)
# Theme-specific language creates natural topical cues without category-name markers.
FACTS: dict[str, tuple[str, ...]] = {
    "operations": (
        "Verify the nightly backup before archive rotation.",
        "Compare replica row counts with the source snapshot.",
        "Keep the rollback image until the health probe succeeds.",
        "Record queue lag before restarting the worker pool.",
        "Check restore time against the recovery objective.",
    ),
    "security": (
        "Rotate the service credential after replica health checks.",
        "Review least-privilege rules for the export worker.",
        "Expire the temporary token after the deployment window.",
        "Compare the signed manifest with the approved digest.",
        "Check the certificate chain before enabling the endpoint.",
    ),
    "research": (
        "Fix the random seed before comparing held-out runs.",
        "Separate calibration examples from final evaluation data.",
        "Report confidence intervals beside the point estimate.",
        "Measure retrieval recall at each candidate cutoff.",
        "Compare the baseline on the same frozen inputs.",
    ),
    "product": (
        "Keep the recovery path visible after a settings change.",
        "Show the current sync source beside the status label.",
        "Preserve the draft when validation returns an error.",
        "Place the undo action beside the completed change.",
        "Explain storage limits before the user confirms.",
    ),
    "learning": (
        "Review a short example before explaining the method.",
        "Write the definition before attempting the proof.",
        "Check an edge case after solving the main example.",
        "Summarize feedback in a single revision note.",
        "Use a counterexample to test the general claim.",
    ),
    "travel": (
        "Check transfer time before booking the evening train.",
        "Keep the address in the local language for the driver.",
        "Verify the last bus time before choosing the hotel.",
        "Save the platform number with the departure time.",
        "Leave extra time between the airport and station.",
    ),
    "chores": (
        "Buy tea and water the small plant by the window.",
        "Replace the kitchen sponge after cleaning the counter.",
        "Put the spare batteries in the desk drawer.",
        "Check the milk date before writing the grocery list.",
        "Return the borrowed book before the weekend.",
    ),
}


@dataclass(frozen=True, slots=True)
class Day4Record:
    memory_id: str
    timestamp: str
    text: str
    capture_intensity: float
    awake: bool


@dataclass(frozen=True, slots=True)
class EvaluationLabel:
    memory_id: str
    theme: str
    importance: Importance


def awake_at(timestamp: datetime) -> bool:
    hour = timestamp.hour + timestamp.minute / 60
    return 8.5 <= hour < 11.5 or 13.5 <= hour < 17.5 or 20.0 <= hour < 22.0


def generate_dataset(
    *, seed: int = SEED, count: int = RECORD_COUNT
) -> tuple[tuple[Day4Record, ...], tuple[EvaluationLabel, ...]]:
    """Return interleaved content and separate evaluation-only truth labels."""
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    records: list[Day4Record] = []
    labels: list[EvaluationLabel] = []
    theme_count = len(THEMES)
    for index in range(count):
        timestamp = START + timedelta(seconds=SPAN_DAYS * 86400 * index / count)
        # Interleaving prevents each topic from occupying a single contiguous block.
        theme = THEMES[(index * 5 + index // theme_count) % theme_count]
        awake = awake_at(timestamp)
        intensity = round(
            rng.betavariate(2.3, 1.8) if awake else rng.betavariate(1.5, 3.8) * 0.9, 6
        )
        topical_affinity = (THEMES.index(theme) + 1) / theme_count
        quality = rng.random()
        # Topic and quality drive labels; intensity contributes modestly, so quiet
        # high-value items remain common rather than being hidden by wake strength.
        p_important = min(0.78, 0.12 + 0.36 * topical_affinity + 0.28 * quality + 0.08 * intensity)
        p_noise = 0.12 + 0.24 * (1.0 - topical_affinity) * (1.0 - quality)
        draw = rng.random()
        importance: Importance = (
            "important" if draw < p_important else ("noise" if draw > 1.0 - p_noise else "normal")
        )
        fact = rng.choice(FACTS[theme])
        # No inline theme, label, importance, or noise markers.
        detail = f"Review note {index % 19 + 1}: {fact}"
        if rng.random() < 0.72:
            detail += " 后续记录保留日期、来源和处理结果，方便之后核对。"
        if index % 3 == 0:
            detail += " Follow-up: retain the date and source reference for later review."
        record = Day4Record(
            memory_id=f"d4-{index:04d}",
            timestamp=timestamp.isoformat(),
            text=detail,
            capture_intensity=intensity,
            awake=awake,
        )
        records.append(record)
        labels.append(EvaluationLabel(record.memory_id, theme, importance))
    return tuple(records), tuple(labels)


def write_dataset(
    output: Path,
    *,
    labels_output: Path,
    capture_output: Path,
    seed: int = SEED,
    count: int = RECORD_COUNT,
) -> None:
    records, labels = generate_dataset(seed=seed, count=count)
    for path in (output, labels_output, capture_output):
        path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in records
        ),
        encoding="utf-8",
    )
    labels_output.write_text(
        "".join(
            json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in labels
        ),
        encoding="utf-8",
    )
    entries = [
        {
            "timestamp": row.timestamp,
            "source": "model",
            "action": "content",
            "intensity": row.capture_intensity,
            "text": row.text,
            "source_id": row.memory_id,
        }
        for row in records
    ]
    end = datetime.fromisoformat(records[-1].timestamp) + timedelta(
        seconds=SPAN_DAYS * 86400 / len(records)
    )
    entries.append({"type": "end", "timestamp": end.isoformat()})
    capture_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in entries),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/day4_synthetic.jsonl"))
    parser.add_argument("--labels-output", type=Path, default=Path("reports/day4_labels.jsonl"))
    parser.add_argument("--capture-output", type=Path, default=Path("reports/day4_capture.jsonl"))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--count", type=int, default=RECORD_COUNT)
    args = parser.parse_args()
    write_dataset(
        args.output,
        labels_output=args.labels_output,
        capture_output=args.capture_output,
        seed=args.seed,
        count=args.count,
    )
    print(f"wrote {args.count} content records, isolated labels, and a timed capture stream")


if __name__ == "__main__":
    main()
