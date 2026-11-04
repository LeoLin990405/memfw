from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from day7_bootstrap import paired_stratified_bootstrap  # noqa: E402


def fixture_rows() -> list[dict[str, object]]:
    return [
        {
            "sample_id": f"conv-{question // 4}",
            "question_index": question,
            "category": question % 5 + 1,
            "evidence_total": 1,
            "hits": {
                "oracle": 1,
                "text": int(question % 4 != 0),
                "intensity": int(question % 2 == 0),
                "bm25_fifo": 0,
                "always_on": 0,
            },
        }
        for question in range(12)
    ]


def test_bootstrap_is_deterministic_stratified_and_reports_all_pairs() -> None:
    first = paired_stratified_bootstrap(fixture_rows(), repeats=200, seed=41)
    second = paired_stratified_bootstrap(fixture_rows(), repeats=200, seed=41)
    assert first == second
    overall = first["scopes"]["overall"]
    assert overall["question_count"] == 12
    assert overall["evidence_total"] == 12
    assert set(overall["systems"]) == {
        "oracle",
        "text",
        "intensity",
        "bm25_fifo",
        "always_on",
    }
    assert overall["holm_family_size"] == 10
    assert len(overall["pairwise"]) == 10
    assert overall["systems"]["oracle"]["micro_recall"] == pytest.approx(1.0)
    assert "within each conversation" in first["method"]
    assert "not a general population guarantee" in first["limitations"]


def test_exact_identity_is_zero_difference_and_not_significant() -> None:
    rows = fixture_rows()
    for row in rows:
        hits = row["hits"]
        assert isinstance(hits, dict)
        hits["always_on"] = hits["bm25_fifo"]
    report = paired_stratified_bootstrap(rows, repeats=100, seed=2)
    pair = report["scopes"]["overall"]["pairwise"]["bm25_fifo__minus__always_on"]
    assert pair["exact_identity"] is True
    assert pair["difference_in_micro_recall"] == 0.0
    assert pair["paired_bootstrap_95_ci"] == [0.0, 0.0]
    assert pair["two_sided_bootstrap_p_unadjusted"] == 1.0
    assert pair["two_sided_bootstrap_p_holm"] == 1.0


def test_strong_paired_difference_and_per_category_intervals() -> None:
    rows = fixture_rows()
    report = paired_stratified_bootstrap(rows, repeats=1000, seed=9)
    overall = report["scopes"]["overall"]["pairwise"]["oracle__minus__bm25_fifo"]
    assert overall["difference_in_micro_recall"] == pytest.approx(1.0)
    assert overall["paired_bootstrap_95_ci"] == [1.0, 1.0]
    assert overall["two_sided_bootstrap_p_unadjusted"] < 0.01
    assert overall["two_sided_bootstrap_p_holm"] >= overall["two_sided_bootstrap_p_unadjusted"]
    for category in range(1, 6):
        result = report["scopes"][f"category_{category}"]
        assert result["question_count"] >= 1
        assert result["systems"]["oracle"]["paired_bootstrap_95_ci"] == [1.0, 1.0]
        assert len(result["pairwise"]) == 10


def test_accepts_sample_index_and_validates_hit_bounds() -> None:
    rows = [
        {
            "sample_index": 0,
            "category": 1,
            "evidence_total": 1,
            "hits": {"a": 1, "b": 0},
        },
        {
            "sample_index": 1,
            "category": 1,
            "evidence_total": 1,
            "hits": {"a": 0, "b": 1},
        },
    ]
    result = paired_stratified_bootstrap(rows, repeats=10)
    assert result["scopes"]["overall"]["question_count"] == 2
    rows[0]["hits"] = {"a": 2, "b": 0}
    with pytest.raises(ValueError, match="cannot exceed evidence_total"):
        paired_stratified_bootstrap(rows, repeats=10)


def test_pairwise_family_can_be_smaller_than_interval_system_set() -> None:
    names = ("oracle", "text", "intensity", "bm25_fifo", "always_on")
    report = paired_stratified_bootstrap(
        fixture_rows(),
        strategies=names,
        pairwise_strategies=("oracle", "text", "intensity"),
        repeats=30,
    )
    overall = report["scopes"]["overall"]
    assert set(overall["systems"]) == set(names)
    assert len(overall["pairwise"]) == 3
    assert overall["holm_family_size"] == 3
