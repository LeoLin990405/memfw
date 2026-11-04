from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import day4_experiment
import gen_day4

from memfw.capture import CaptureSession, TimedSignal
from memfw.core.models import CaptureState


def test_generator_is_deterministic_interleaved_and_labels_are_separate(tmp_path: Path) -> None:
    rows, labels = gen_day4.generate_dataset(seed=20261018, count=350)
    again, labels_again = gen_day4.generate_dataset(seed=20261018, count=350)
    assert rows == again
    assert labels == labels_again
    assert len({label.theme for label in labels}) == 7
    assert len(set(label.theme for label in labels[:14])) > 3
    assert {label.importance for label in labels} == {"important", "normal", "noise"}
    quiet_important = [
        row
        for row, label in zip(rows, labels, strict=True)
        if label.importance == "important" and row.capture_intensity < 0.35
    ]
    assert len(quiet_important) >= 10

    content_path = tmp_path / "content.jsonl"
    label_path = tmp_path / "labels.jsonl"
    stream_path = tmp_path / "capture.jsonl"
    gen_day4.write_dataset(
        content_path, labels_output=label_path, capture_output=stream_path, count=350
    )
    content = content_path.read_text(encoding="utf-8")
    stream = stream_path.read_text(encoding="utf-8")
    assert '"importance"' not in content and '"theme"' not in content
    assert "important" not in content.lower() and "noise" not in content.lower()
    assert '"importance"' not in stream and '"theme"' not in stream
    assert "important" not in stream.lower() and "noise" not in stream.lower()
    assert len(label_path.read_text(encoding="utf-8").splitlines()) == 350


def test_standby_ring_backfills_recent_content_on_real_wake() -> None:
    start = datetime(2026, 10, 18, tzinfo=UTC)
    session = CaptureSession(
        session_id="day4-backfill",
        initial_state=CaptureState.DORMANT,
        high_threshold=0.8,
        low_threshold=0.3,
        standby_seconds=10,
        dormant_seconds=120,
        ring_seconds=15,
    )
    session.feed(TimedSignal(timestamp=start, source="model", intensity=0.9, action="signal"))
    session.feed(
        TimedSignal(
            timestamp=start + timedelta(seconds=1), source="model", intensity=0.2, action="signal"
        )
    )
    session.feed(
        TimedSignal(
            timestamp=start + timedelta(seconds=11),
            source="model",
            intensity=0.2,
            action="content",
            source_id="d4-ring-a",
            text="Quiet observation A",
        )
    )
    session.feed(
        TimedSignal(
            timestamp=start + timedelta(seconds=20),
            source="model",
            intensity=0.2,
            action="content",
            source_id="d4-ring-b",
            text="Quiet observation B",
        )
    )
    session.feed(
        TimedSignal(
            timestamp=start + timedelta(seconds=25), source="model", intensity=0.9, action="signal"
        )
    )
    assert session.current_state() == CaptureState.FULL
    assert {item.source_id for item in session.items()} == {"d4-ring-a", "d4-ring-b"}


def test_judge_and_policy_use_observables_without_evaluation_labels(tmp_path: Path) -> None:
    report = day4_experiment.run_experiment(tmp_path, count=70)
    capture = report["capture_thresholds"]
    assert any(row["ring_backfilled_candidates"] > 0 for row in capture)
    assert all(row["ring_window_seconds"] == row["record_interval_seconds"] * 2 for row in capture)
    observables = report["keyness"]["observable_strength_inputs"]
    assert observables["logged_access_events"] > 0
    assert observables["documents_with_access"] > 0
    assert observables["documents_without_access"] > 0
    assert observables["policy_candidate_count"] == 70
    assert observables["policy_forgotten_count"] == 14
    assert observables["actual_inputs_include_access_recency_keyness"] is True
    assert observables["judge_and_policy_label_leakage_checked"] is True
    tails = report["keyness"]["tail_diagnostics"]
    assert tails["access_count"]["status"] == "descriptive_approximation"
    assert tails["reference_count"]["status"] == "insufficient_sample"
    assert tails["evaluation_only_topic_frequency"]["status"] == "insufficient_sample"
    assert "no bootstrap" in tails["interpretation"]


@pytest.mark.parametrize("threshold", [0.35, 0.45, 0.55, 0.65, 0.75, 0.85])
def test_threshold_range_is_valid(threshold: float) -> None:
    assert 0.0 < threshold < 1.0
