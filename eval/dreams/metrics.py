"""Score a dream proposal against the synthetic gold labels.

Merge accuracy is the fraction of gold ``(source, target)`` pairs that the
proposal repeats with the same kept target. A reversed pair is not a false
merge; it is a miss. False-merge rate is the fraction of proposed pairs whose
two ids are not a gold pair. The target for that rate is 0.

Conflict detection rate is the fraction of gold conflict candidates that
appear with the same concept, attribute, and memory ids. Stale-mark recall is
the fraction of gold stale ids the proposal marks. Adoption rate is an online
metric: it stays unknown until a person labels which proposals were adopted.
"""

from __future__ import annotations

from typing import Any

from eval.dreams.cases import Case, Conflict


def score_cases(
    found: tuple[Case, ...], gold: tuple[Case, ...], *, implemented: bool = False
) -> dict[str, Any]:
    """Score one proposal set. ``found`` uses the same case ids as ``gold``."""

    by_id = {item.case_id: item for item in found}
    missing = [item.case_id for item in gold if item.case_id not in by_id]
    if missing:
        raise ValueError("proposal is missing cases: " + ", ".join(missing))
    merge_hits = 0
    merge_total = 0
    false_merges = 0
    proposed_merges = 0
    conflict_hits = 0
    conflict_total = 0
    stale_hits = 0
    stale_total = 0
    per_case: list[dict[str, Any]] = []
    for item in gold:
        predicted = by_id[item.case_id]
        row = _score_one(predicted, item)
        per_case.append(row)
        merge_hits += row["merge_hits"]
        merge_total += row["merge_total"]
        false_merges += row["false_merges"]
        proposed_merges += row["proposed_merges"]
        conflict_hits += row["conflict_hits"]
        conflict_total += row["conflict_total"]
        stale_hits += row["stale_hits"]
        stale_total += row["stale_total"]
    return {
        "synthetic": True,
        "dream_implemented": implemented,
        "cases": len(gold),
        "merge_accuracy": _rate(merge_hits, merge_total, empty=1.0),
        "conflict_detection_rate": _rate(conflict_hits, conflict_total, empty=1.0),
        "false_merge_rate": _rate(false_merges, proposed_merges, empty=0.0),
        "stale_mark_recall": _rate(stale_hits, stale_total, empty=1.0),
        "adopted_proposal_rate": None,
        "adopted_proposal_rate_online": True,
        "per_case": per_case,
    }


def adoption_rate(
    proposed_ids: tuple[str, ...],
    adopted_ids: tuple[str, ...] | None,
) -> float | None:
    """Online metric. ``None`` means a person has not labeled adoption."""

    if adopted_ids is None:
        return None
    if not proposed_ids:
        return 1.0 if not adopted_ids else 0.0
    adopted = set(adopted_ids)
    return len([item for item in proposed_ids if item in adopted]) / len(proposed_ids)


def _score_one(predicted: Case, gold: Case) -> dict[str, Any]:
    gold_pairs = {_pair(item) for item in gold.merges}
    merge_hits = sum(1 for item in gold.merges if item in predicted.merges)
    false_merges = sum(1 for item in predicted.merges if _pair(item) not in gold_pairs)
    gold_conflicts = {_conflict_key(item) for item in gold.conflicts}
    found_conflicts = {_conflict_key(item) for item in predicted.conflicts}
    gold_stale = set(gold.stale)
    found_stale = set(predicted.stale)
    return {
        "case_id": gold.case_id,
        "kind": gold.kind,
        "merge_hits": merge_hits,
        "merge_total": len(gold.merges),
        "false_merges": false_merges,
        "proposed_merges": len(predicted.merges),
        "conflict_hits": len(gold_conflicts & found_conflicts),
        "conflict_total": len(gold_conflicts),
        "stale_hits": len(gold_stale & found_stale),
        "stale_total": len(gold_stale),
    }


def _pair(item: tuple[str, str]) -> frozenset[str]:
    return frozenset(item)


def _conflict_key(item: Conflict) -> tuple[str, str, frozenset[str]]:
    return (item.about, item.attribute, frozenset(item.memory_ids))


def _rate(hits: int, total: int, *, empty: float) -> float:
    if total == 0:
        return empty
    return round(hits / total, 6)
