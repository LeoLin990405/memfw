"""Synthetic dream evaluation: gold labels, metrics, and the real curate step."""

from __future__ import annotations

from eval.dreams.cases import cases
from eval.dreams.metrics import adoption_rate, score_cases


def test_gold_scores_perfect_and_adoption_stays_unknown() -> None:
    gold = cases()
    report = score_cases(gold, gold)
    assert report["dream_implemented"] is False
    assert report["merge_accuracy"] == 1
    assert report["conflict_detection_rate"] == 1
    assert report["false_merge_rate"] == 0
    assert report["stale_mark_recall"] == 1
    assert report["adopted_proposal_rate"] is None
    assert {item.kind for item in gold} == {"duplicate", "conflict", "stale"}


def test_labels_refer_to_memories_in_the_case() -> None:
    for item in cases():
        ids = {memory.memory_id for memory in item.memories}
        for source, target in item.merges:
            assert source in ids and target in ids
            assert source != target
        for conflict in item.conflicts:
            assert set(conflict.memory_ids) <= ids
            assert len(conflict.memory_ids) >= 2
        assert set(item.stale) <= ids


def test_duplicates_share_one_fact_and_conflicts_do_not_merge() -> None:
    by_id = {item.case_id: item for item in cases()}
    lamp = by_id["duplicate-lamp"]
    merged = {memory_id for pair in lamp.merges for memory_id in pair}
    assert "mem-desk" not in merged
    conflict = by_id["conflict-color"]
    assert conflict.merges == ()
    assert conflict.conflicts[0].attribute == "color"
    different = by_id["different-attributes"]
    assert different.merges == () and different.conflicts == ()
    superseded = by_id["stale-superseded"]
    assert superseded.stale == ("mem-policy-old",)
    assert superseded.merges == ()


def test_a_wrong_merge_is_a_false_merge_and_a_missed_conflict_is_a_miss() -> None:
    gold = cases()
    wrong = []
    for item in gold:
        merges = item.merges
        conflicts = item.conflicts
        if item.case_id == "duplicate-lamp":
            merges = (("mem-desk", "mem-lamp-kept"),)
        if item.case_id == "conflict-color":
            conflicts = ()
        wrong.append(
            type(item)(
                case_id=item.case_id,
                kind=item.kind,
                memories=item.memories,
                merges=merges,
                conflicts=conflicts,
                stale=item.stale,
            )
        )
    report = score_cases(tuple(wrong), gold)
    assert report["false_merge_rate"] > 0
    assert report["conflict_detection_rate"] < 1
    assert report["merge_accuracy"] < 1


def test_adoption_rate_is_online_only() -> None:
    assert adoption_rate(("p1", "p2"), None) is None
    assert adoption_rate(("p1", "p2"), ("p2",)) == 0.5


def test_memfw_dream_is_a_command_group_with_run() -> None:
    from memfw.cli import app

    groups = {group.name: group for group in app.registered_groups}
    assert "dream" in groups
    dream = groups["dream"].typer_instance
    assert dream is not None
    assert {command.name for command in dream.registered_commands} == {"run"}


def test_real_curate_scores_on_the_synthetic_cases() -> None:
    from eval.dreams.curate_adapter import predict_all

    gold = cases()
    report = score_cases(predict_all(gold), gold, implemented=True)
    assert report["dream_implemented"] is True
    assert report["false_merge_rate"] == 0
    assert report["merge_accuracy"] == 1
    assert report["conflict_detection_rate"] == 1
    assert report["stale_mark_recall"] == 1
    assert report["adopted_proposal_rate"] is None
