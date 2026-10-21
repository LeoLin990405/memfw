#!/usr/bin/env python3
"""Paired conversation-stratified bootstrap for evidence-recall comparisons."""

from __future__ import annotations

import itertools
import random
from collections.abc import Mapping, Sequence
from typing import Any


def _as_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _prepare_records(
    records: Sequence[Mapping[str, object]],
    strategies: Sequence[str] | None,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    if not records:
        raise ValueError("at least one question record is required")
    first_hits = records[0].get("hits")
    if not isinstance(first_hits, Mapping) or not first_hits:
        raise ValueError("each record must contain a nonempty hits mapping")
    names = tuple(str(name) for name in (strategies or first_hits.keys()))
    if len(names) < 2 or len(set(names)) != len(names):
        raise ValueError("at least two distinct strategies are required")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        sample_id = record.get("sample_id", record.get("sample_index"))
        if not isinstance(sample_id, (str, int)) or isinstance(sample_id, bool):
            raise ValueError(f"record {index} must have a string/integer sample_id or sample_index")
        evidence_total = _as_nonnegative_int(record.get("evidence_total"), "evidence_total")
        if evidence_total == 0:
            raise ValueError("evidence_total must be positive")
        raw_category = record.get("category")
        if raw_category is None:
            category = None
        else:
            category = _as_nonnegative_int(raw_category, "category")
            if category not in range(1, 6):
                raise ValueError("category must be 1..5 or null")
        hits = record.get("hits")
        if not isinstance(hits, Mapping) or set(hits) != set(names):
            raise ValueError("every record must provide hits for exactly the selected strategies")
        clean_hits = {name: _as_nonnegative_int(hits[name], f"hits[{name}]") for name in names}
        if any(count > evidence_total for count in clean_hits.values()):
            raise ValueError("a system's hits cannot exceed evidence_total")
        normalized.append(
            {
                "sample_id": sample_id,
                "question_index": record.get("question_index", index),
                "category": category,
                "evidence_total": evidence_total,
                "hits": clean_hits,
            }
        )
    return tuple(normalized), names


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _holm_adjust(p_values: Mapping[str, float | None]) -> dict[str, float | None]:
    valid = sorted(
        ((name, value) for name, value in p_values.items() if value is not None),
        key=lambda item: item[1],
    )
    adjusted: dict[str, float | None] = {name: None for name in p_values}
    running = 0.0
    total = len(valid)
    for rank, (name, value) in enumerate(valid):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[name] = running
    return adjusted


def paired_stratified_bootstrap(
    records: Sequence[Mapping[str, object]],
    *,
    strategies: Sequence[str] | None = None,
    pairwise_strategies: Sequence[str] | None = None,
    repeats: int = 1000,
    seed: int = 20261018,
) -> dict[str, Any]:
    """Estimate paired micro-recall uncertainty by resampling within conversations.

    Each replicate draws the same number of questions with replacement inside each
    ``sample_id`` stratum. The overall and category scopes use separate within-sample
    resampling frames. Pairwise p-values are two-sided bootstrap sign-tail estimates
    with a plus-one correction; Holm adjustment is applied within each scope.
    """
    if repeats < 1:
        raise ValueError("repeats must be positive")
    prepared, names = _prepare_records(records, strategies)
    pair_names_requested = tuple(pairwise_strategies or names)
    if (
        len(pair_names_requested) < 2
        or len(set(pair_names_requested)) != len(pair_names_requested)
        or not set(pair_names_requested).issubset(names)
    ):
        raise ValueError("pairwise_strategies must contain at least two distinct selected systems")
    strata: dict[str | int, list[dict[str, Any]]] = {}
    for row in prepared:
        strata.setdefault(row["sample_id"], []).append(row)
    ordered_strata = tuple(strata[key] for key in sorted(strata, key=lambda value: str(value)))
    rng = random.Random(seed)
    scopes: dict[str, list[tuple[dict[str, Any], ...]]] = {"overall": []}
    scopes.update({f"category_{category}": [] for category in range(1, 6)})
    for scope, scope_groups in scopes.items():
        for stratum in ordered_strata:
            selected = (
                stratum
                if scope == "overall"
                else [row for row in stratum if row["category"] == int(scope.rsplit("_", 1)[1])]
            )
            if selected:
                scope_groups.append(tuple(selected))

    output_scopes: dict[str, dict[str, Any]] = {}
    for scope_name, groups in scopes.items():
        if not groups:
            output_scopes[scope_name] = {
                "question_count": 0,
                "evidence_total": 0,
                "systems": {
                    name: {"micro_recall": None, "paired_bootstrap_95_ci": None} for name in names
                },
                "pairwise": {},
                "holm_family_size": 0,
            }
            continue
        all_rows = tuple(row for group in groups for row in group)
        evidence_total = sum(row["evidence_total"] for row in all_rows)
        point_hits = {name: sum(row["hits"][name] for row in all_rows) for name in names}
        point_recall = {name: point_hits[name] / evidence_total for name in names}
        replicate_recalls: dict[str, list[float]] = {name: [] for name in names}
        pair_names = tuple(itertools.combinations(pair_names_requested, 2))
        replicate_differences: dict[tuple[str, str], list[float]] = {
            (left, right): [] for left, right in pair_names
        }
        for _ in range(repeats):
            drawn: list[dict[str, Any]] = []
            for bootstrap_group in groups:
                drawn.extend(rng.choice(bootstrap_group) for _ in bootstrap_group)
            denominator = sum(row["evidence_total"] for row in drawn)
            numerators = {name: sum(row["hits"][name] for row in drawn) for name in names}
            recall = {name: numerators[name] / denominator for name in names}
            for name in names:
                replicate_recalls[name].append(recall[name])
            for left, right in pair_names:
                replicate_differences[(left, right)].append(recall[left] - recall[right])

        systems_output: dict[str, Any] = {}
        for name in names:
            sorted_values = sorted(replicate_recalls[name])
            systems_output[name] = {
                "micro_recall": point_recall[name],
                "paired_bootstrap_95_ci": [
                    _percentile(sorted_values, 0.025),
                    _percentile(sorted_values, 0.975),
                ],
            }
        raw_p: dict[str, float | None] = {}
        pair_output: dict[str, Any] = {}
        for left, right in pair_names:
            key = f"{left}__minus__{right}"
            values = sorted(replicate_differences[(left, right)])
            observed = point_recall[left] - point_recall[right]
            identity = all(row["hits"][left] == row["hits"][right] for row in all_rows)
            if identity:
                p_value = 1.0
                interval = [0.0, 0.0]
            else:
                lower_tail = (sum(value <= 0.0 for value in values) + 1) / (repeats + 1)
                upper_tail = (sum(value >= 0.0 for value in values) + 1) / (repeats + 1)
                p_value = min(1.0, 2.0 * min(lower_tail, upper_tail))
                interval = [_percentile(values, 0.025), _percentile(values, 0.975)]
            raw_p[key] = p_value
            pair_output[key] = {
                "left": left,
                "right": right,
                "difference_in_micro_recall": observed,
                "paired_bootstrap_95_ci": interval,
                "two_sided_bootstrap_p_unadjusted": p_value,
                "exact_identity": identity,
                "replicates": repeats,
            }
        adjusted = _holm_adjust(raw_p)
        for key, pair in pair_output.items():
            pair["two_sided_bootstrap_p_holm"] = adjusted[key]
        output_scopes[scope_name] = {
            "question_count": len(all_rows),
            "evidence_total": evidence_total,
            "systems": systems_output,
            "pairwise": pair_output,
            "holm_family_size": sum(value is not None for value in raw_p.values()),
        }

    return {
        "repeats": repeats,
        "seed": seed,
        "systems": list(names),
        "pairwise_strategies": list(pair_names_requested),
        "method": (
            "paired question bootstrap, resampled with replacement within each conversation; "
            "micro evidence recall=sum(hits)/sum(evidence_total); percentile 95% intervals; "
            "two-sided sign-tail bootstrap p-values with plus-one correction; Holm adjustment "
            "within each overall/category family"
        ),
        "limitations": (
            "Intervals describe this fixture under within-conversation question resampling, "
            "not a general population guarantee; questions may overlap in evidence "
            "and are dependent."
        ),
        "scopes": output_scopes,
    }
