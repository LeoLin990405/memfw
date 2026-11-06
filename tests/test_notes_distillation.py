from __future__ import annotations

import json
from pathlib import Path

from memfw.notes.distillation import check_pairs, export_pairs, write_pairs
from memfw.notes.feedback import CandidateTrace, FeedbackEvent, FeedbackStore, RetrievalTrace


def test_export_uses_only_explicit_feedback(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.sqlite")
    trace = RetrievalTrace(
        trace_id="t1",
        task_id="task-a",
        query="corpus",
        mode="hybrid",
        candidates=(
            CandidateTrace("a.md", 1, 0.9),
            CandidateTrace("b.md", 2, 0.5),
        ),
    )
    store.record_trace(trace)
    store.add_feedback(
        FeedbackEvent(
            trace_id="t1",
            task_id="task-a",
            kind="citation",
            candidate_path="a.md",
            relevance="weak_positive",
            confidence=0.7,
        )
    )
    store.add_feedback(
        FeedbackEvent(
            trace_id="t1",
            task_id="task-a",
            kind="citation",
            candidate_path="b.md",
            correctness="incorrect",
            confidence=0.9,
        )
    )
    store.record_trace(
        RetrievalTrace(trace_id="t2", task_id="task-b", query="unknown", mode="hybrid")
    )
    report = export_pairs(store, min_confidence=0.5)
    assert len(report.pairs) == 1
    pair = report.pairs[0]
    assert pair.positive_path == "a.md"
    assert pair.negative_paths == ("b.md",)
    assert report.skipped_unknown == 1
    assert check_pairs(report.pairs)["errors"] == []
    output = tmp_path / "pairs.jsonl"
    write_pairs(report, output)
    assert json.loads(output.read_text(encoding="utf-8").splitlines()[0])["positive_path"] == "a.md"


def test_export_does_not_turn_uncited_results_into_negatives(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.sqlite")
    store.record_trace(
        RetrievalTrace(
            trace_id="t3",
            task_id="task-c",
            query="corpus",
            mode="hybrid",
            candidates=(CandidateTrace("a.md", 1, 0.9), CandidateTrace("b.md", 2, 0.5)),
        )
    )
    store.add_feedback(
        FeedbackEvent(
            trace_id="t3",
            task_id="task-c",
            kind="citation",
            candidate_path="a.md",
            relevance="relevant",
            confidence=0.9,
        )
    )
    report = export_pairs(store)
    assert report.pairs[0].negative_paths == ()


def test_check_pairs_detects_missing_and_leakage() -> None:
    from memfw.notes.distillation import TrainingPair

    pairs = [
        TrainingPair("q", "a.md", task_id="same", split="train"),
        TrainingPair("q2", "b.md", task_id="same", split="test"),
        TrainingPair("", "", task_id="x"),
    ]
    result = check_pairs(pairs)
    assert result["errors"]
    assert any("leakage" in error for error in result["errors"])
    assert any("missing" in error for error in result["errors"])
