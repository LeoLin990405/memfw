#!/usr/bin/env python3
"""Analyze the Day 7 LoCoMo null result and run lower retention budgets offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from day7_bootstrap import paired_stratified_bootstrap
from day7_eval import DEFAULT_CONDITIONS, evaluate_file

CATEGORIES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
POLICIES = ("fifo_bm25", "lru_bm25", "decay_bm25", "salience_bm25", "consolidate_then_forget_bm25")


def _records(result: dict[str, Any], condition: str, ratio: str) -> list[dict[str, Any]]:
    rows = []
    for sample in result["samples"]:
        for row in sample["scopes"][condition]["profiles"][ratio]["per_question"]:
            rows.append({**row, "sample_index": sample["sample_index"]})
    return rows


def _summarize(rows: list[dict[str, Any]], strategies: tuple[str, ...]) -> dict[str, Any]:
    denom = sum(int(r["evidence_total"]) for r in rows)
    return {
        "questions": len(rows),
        "evidence_ids": denom,
        "recall": {s: sum(int(r["hits"][s]) for r in rows) / denom for s in strategies},
    }


def _holm(values: dict[str, float]) -> dict[str, float]:
    ranked = sorted(values.items(), key=lambda x: x[1])
    result: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(ranked):
        running = max(running, min(1.0, p * (len(ranked) - i)))
        result[name] = running
    return result


def _signflip_p(rows: list[dict[str, Any]], a: str, b: str) -> float:
    """Exact two-sided paired sign-flip test on conversation-level recall numerators."""
    clusters: dict[int, int] = {}
    for row in rows:
        sample = int(row["sample_index"])
        clusters[sample] = clusters.get(sample, 0) + int(row["hits"][a]) - int(row["hits"][b])
    diffs = tuple(clusters.values())
    observed = abs(sum(diffs))
    if not observed:
        return 1.0
    extreme = 0
    total = 1 << len(diffs)
    for mask in range(total):
        permuted = sum(value if mask & (1 << i) else -value for i, value in enumerate(diffs))
        if abs(permuted) >= observed:
            extreme += 1
    return extreme / total


def _power(rows: list[dict[str, Any]], a: str, b: str, alpha: float) -> dict[str, Any]:
    """Normal-approximation MDE from paired influence values; cluster SE by conversation."""
    total_e = sum(float(r["evidence_total"]) for r in rows)
    delta = sum(float(r["hits"][a]) - float(r["hits"][b]) for r in rows) / total_e
    mean_e = total_e / len(rows)
    influence = [
        ((float(r["hits"][a]) - float(r["hits"][b])) - delta * float(r["evidence_total"])) / mean_e
        for r in rows
    ]
    se_question = statistics.stdev(influence) / math.sqrt(len(influence)) if len(rows) > 1 else 0.0
    clusters: dict[int, float] = {}
    for row in rows:
        i = row["sample_index"]
        clusters[i] = clusters.get(i, 0.0) + (
            float(row["hits"][a]) - float(row["hits"][b]) - delta * float(row["evidence_total"])
        )
    g = len(clusters)
    se_cluster = (
        math.sqrt(g / (g - 1) * sum(v * v for v in clusters.values())) / total_e if g > 1 else None
    )
    norm = NormalDist()
    z = norm.inv_cdf(1 - alpha / 2) + norm.inv_cdf(0.8)
    z_10 = norm.inv_cdf(1 - alpha / (2 * 10)) + norm.inv_cdf(0.8)
    identity = all(int(r["hits"][a]) == int(r["hits"][b]) for r in rows)
    return {
        "observed_difference": delta,
        "n_questions": len(rows),
        "n_conversations": g,
        "se_question_independence": se_question,
        "se_cluster_robust_10_conversations": se_cluster,
        "identical_question_outcomes": identity,
        "mde80_pp_question_se": (z * se_question * 100 if not identity else None),
        "mde80_pp_cluster_se": (
            z * se_cluster * 100 if se_cluster is not None and se_cluster > 0 else None
        ),
        "mde80_pp_cluster_se_bonferroni_10": (
            z_10 * se_cluster * 100 if se_cluster is not None and se_cluster > 0 else None
        ),
    }


def _condition_analysis(
    result: dict[str, Any], condition: str, ratio: str, repeats: int, seed: int
) -> dict[str, Any]:
    rows = _records(result, condition, ratio)
    paired = paired_stratified_bootstrap(
        rows,
        strategies=result["strategies"],
        pairwise_strategies=POLICIES,
        repeats=repeats,
        seed=seed,
    )
    overall = _summarize(rows, POLICIES)
    categories: dict[str, Any] = {}
    all_category_p: dict[str, float] = {}
    for code, label in CATEGORIES.items():
        subset = [r for r in rows if r.get("category") == code]
        summary: dict[str, Any] = (
            _summarize(subset, POLICIES)
            if subset
            else {"questions": 0, "evidence_ids": 0, "recall": {}}
        )
        pvals = {}
        legacy_pvals = {}
        pairs = {}
        for a, b in combinations(POLICIES, 2):
            key = f"{a}__minus__{b}"
            boot = (
                paired.get("scopes", {})
                .get(f"category_{code}", {})
                .get("pairwise", {})
                .get(key, {})
            )
            p = boot.get("two_sided_bootstrap_p_unadjusted")
            if p is not None:
                exact_p = _signflip_p(subset, a, b)
                pvals[key] = exact_p
                legacy_pvals[key] = float(p)
                all_category_p[f"{code}:{key}"] = exact_p
            pairs[key] = {
                "delta_pp": 100 * (summary["recall"].get(a, 0) - summary["recall"].get(b, 0)),
                "bootstrap_ci_pp": [100 * x for x in boot.get("paired_bootstrap_95_ci", [0, 0])],
                "p_raw": p,
                "p_holm_within_category_family": boot.get("two_sided_bootstrap_p_holm"),
                "signflip_p_exact": exact_p if p is not None else None,
                "power": _power(subset, a, b, 0.05) if subset else None,
            }
        exact_holm = _holm(pvals)
        legacy_holm = _holm(legacy_pvals)
        for key, pair in pairs.items():
            pair["signflip_p_holm_within_category_10_contrasts"] = exact_holm.get(key)
            pair["legacy_bootstrap_sign_tail_p_holm"] = legacy_holm.get(key)
        categories[str(code)] = {"name": label, **summary, "pairwise": pairs}
    overall_pairs = paired["scopes"]["overall"]["pairwise"]
    overall_exact = {
        key: _signflip_p(rows, value["left"], value["right"])
        for key, value in overall_pairs.items()
    }
    overall_holm = _holm(overall_exact)
    return {
        "overall": overall,
        "overall_pairwise": {
            key: {
                "delta_pp": 100 * v["difference_in_micro_recall"],
                "bootstrap_ci_pp": [100 * x for x in v["paired_bootstrap_95_ci"]],
                "p_raw": v["two_sided_bootstrap_p_unadjusted"],
                "legacy_bootstrap_sign_tail_p_holm_overall_10_contrasts": v[
                    "two_sided_bootstrap_p_holm"
                ],
                "signflip_p_exact": overall_exact[key],
                "signflip_p_holm_overall_10_contrasts": overall_holm[key],
                "exact_identity": v["exact_identity"],
                "power": _power(rows, v["left"], v["right"], 0.05),
            }
            for key, v in overall_pairs.items()
        },
        "categories": categories,
        "category_cross_family_holm": _holm(all_category_p),
        "raw_p_count_category_all_50_contrasts": len(all_category_p),
    }


def _retention(result: dict[str, Any], ratio: str) -> dict[str, Any]:
    samples = []
    systems = (
        "fifo_bm25",
        "lru_bm25",
        "decay_bm25",
        "salience_bm25",
        "consolidate_then_forget_bm25",
    )
    pairwise = list(combinations(systems, 2))
    jaccard: dict[str, list[float]] = {f"{a}__vs__{b}": [] for a, b in pairwise}
    trigger_counts = {s: 0 for s in systems}
    bytes_total = {s: 0 for s in systems}
    count_total = {s: 0 for s in systems}
    mean_lengths: dict[str, list[float]] = {s: [] for s in systems}
    capacities = 0
    by_condition = {}
    for condition in DEFAULT_CONDITIONS:
        condition_counts = {s: 0 for s in systems}
        condition_jaccard: dict[str, list[float]] = {f"{a}__vs__{b}": [] for a, b in pairwise}
        for sample in result["samples"]:
            profile = sample["scopes"][condition]["profiles"][ratio]
            capacities += int(profile["capacity_bytes"])
            sets = {}
            for system in systems:
                item = profile["systems"][system]
                ids = set(item["retained_ids"])
                sets[system] = ids
                bytes_total[system] += int(item["retained_body_utf8_bytes"])
                count_total[system] += int(item["retained_count"])
                if item["retained_count"]:
                    mean_lengths[system].append(
                        int(item["retained_body_utf8_bytes"]) / int(item["retained_count"])
                    )
                triggered = int(len(ids) < int(profile["initial_turns"]))
                trigger_counts[system] += triggered
                condition_counts[system] += triggered
            for a, b in pairwise:
                union = sets[a] | sets[b]
                value = len(sets[a] & sets[b]) / len(union) if union else 1.0
                jaccard[f"{a}__vs__{b}"].append(value)
                condition_jaccard[f"{a}__vs__{b}"].append(value)
        by_condition[condition] = {
            "sample_count_budget_triggered": condition_counts,
            "pairwise_retained_id_jaccard_mean": {
                k: statistics.mean(v) for k, v in condition_jaccard.items()
            },
            "identical_retained_id_sample_count": {
                f"{a}__vs__{b}": sum(
                    set(
                        sample["scopes"][condition]["profiles"][ratio]["systems"][a]["retained_ids"]
                    )
                    == set(
                        sample["scopes"][condition]["profiles"][ratio]["systems"][b]["retained_ids"]
                    )
                    for sample in result["samples"]
                )
                for a, b in pairwise
            },
        }
    for sample in result["samples"]:
        samples.append({"sample_id": sample["sample_id"], "initial_turns": sample["turn_count"]})
    return {
        "sample_count": len(samples),
        "condition_budget_units": len(samples) * len(DEFAULT_CONDITIONS),
        "sample_count_budget_triggered": trigger_counts,
        "sum_retained_bytes_across_sample_condition_stores": bytes_total,
        "sum_retained_count_across_sample_condition_stores": count_total,
        "mean_retained_body_bytes_per_turn_by_policy": {
            k: statistics.mean(v) for k, v in mean_lengths.items()
        },
        "sum_capacity_bytes_across_sample_condition_stores": capacities,
        "pairwise_retained_id_jaccard_mean": {k: statistics.mean(v) for k, v in jaccard.items()},
        "by_condition": by_condition,
    }


def build_report(
    history_path: Path,
    pressure_path: Path,
    data_path: Path,
    output_path: Path,
    *,
    repeats: int = 1000,
    seed: int = 20261018,
    elapsed: float = 0.0,
) -> None:
    history = json.loads(history_path.read_text())
    pressure = json.loads(pressure_path.read_text())
    source_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
    if history["dataset"]["sha256"] != source_hash or pressure["dataset"]["sha256"] != source_hash:
        raise ValueError("historical, pressure, and current public data SHA-256 values must match")
    if history["seed"] != seed or pressure["seed"] != seed:
        raise ValueError("historical and pressure seeds must match the requested seed")
    if history["query_context_budget_tokens"] != pressure["query_context_budget_tokens"]:
        raise ValueError("historical and pressure retrieval context budgets must match")
    conditions = {}
    retention = {}
    for condition in DEFAULT_CONDITIONS:
        conditions[f"{condition}/10%"] = _condition_analysis(
            history, condition, "10%", repeats, seed ^ 10
        )
        conditions[f"{condition}/25%"] = _condition_analysis(
            history, condition, "25%", repeats, seed ^ 25
        )
        conditions[f"{condition}/1%"] = _condition_analysis(
            pressure, condition, "1%", repeats, seed ^ 1
        )
        conditions[f"{condition}/5%"] = _condition_analysis(
            pressure, condition, "5%", repeats, seed ^ 5
        )
    for ratio in ("10%", "25%"):
        retention[ratio] = _retention(history, ratio)
    for ratio in ("1%", "5%"):
        retention[ratio] = _retention(pressure, ratio)
    history_hash = hashlib.sha256(history_path.read_bytes()).hexdigest()
    pressure_hash = hashlib.sha256(pressure_path.read_bytes()).hexdigest()
    body = {
        "data_sha256": source_hash,
        "historical_result_sha256": history_hash,
        "pressure_result_sha256": pressure_hash,
        "seed": seed,
        "bootstrap_repeats": repeats,
        "pressure_runtime_seconds": elapsed,
        "conditions": conditions,
        "retention": retention,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Save compact numeric evidence alongside the human-readable interpretation.
    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    fifo_lru_identical_10 = retention["10%"]["by_condition"]["keyness_only"][
        "identical_retained_id_sample_count"
    ]["fifo_bm25__vs__lru_bm25"]
    fifo_lru_identical_25 = retention["25%"]["by_condition"]["keyness_only"][
        "identical_retained_id_sample_count"
    ]["fifo_bm25__vs__lru_bm25"]
    lines = [
        "# Day 10: Why the five LoCoMo retention policies showed no clear difference",
        "",
        "## Finding",
        "",
        f"The Day 7 result is a bounded failure to detect an overall policy difference on this corpus and these retrieval settings. It does not establish equivalence. FIFO and LRU retained identical IDs in {fifo_lru_identical_10}/{len(history['samples'])} no-access samples at 10% and {fifo_lru_identical_25}/{len(history['samples'])} at 25%. Other policy pairs ranged from strong overlap to low overlap, which may contribute to some small observed gaps. The benchmark has only ten conversation clusters, which limits sensitivity but does not make point estimates close. The score is evidence-ID recall, not answer accuracy.",  # noqa: E501
        "",
        "## Dataset and category mix",
        "",
        f"Public local input SHA-256: `{source_hash}`. The input remains outside the repository. The analysis uses {len(history['samples'])} conversations, {sum(s['turn_count'] for s in history['samples']):,} turns, and {sum(sum(v['valid_evidence_questions'] for v in s['adapter_counts']['category_evidence_counts'].values()) for s in history['samples']):,} questions with valid evidence ({sum(history['bootstrap']['keyness_only/10%']['scopes'][f'category_{c}']['evidence_total'] for c in range(1, 6)):,} evidence IDs). Official category mapping: 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. Category results are exploratory because of sparse counts and multiple comparison families.",  # noqa: E501
        "",
        "| Category | Valid questions | Evidence IDs |",
        "|---|---:|---:|",
    ]
    counts = history["samples"]
    bycat = {str(i): [0, 0] for i in range(1, 6)}
    for sample in counts:
        for cat, v in sample["adapter_counts"]["category_evidence_counts"].items():
            bycat[cat][0] += v["valid_evidence_questions"]
    for c, name in CATEGORIES.items():
        vals = history["bootstrap"]["keyness_only/10%"]["scopes"][f"category_{c}"]
        bycat[str(c)][1] = vals["evidence_total"]
        lines.append(f"| {c} {name} | {bycat[str(c)][0]} | {bycat[str(c)][1]} |")
    pre_hits = sum(
        sample["scopes"]["keyness_only"]["before_budget"]["systems"]["memfw_bm25"][
            "evidence_retrieved"
        ]
        for sample in history["samples"]
    )
    pre_evidence = sum(
        sample["scopes"]["keyness_only"]["before_budget"]["systems"]["memfw_bm25"]["evidence_total"]
        for sample in history["samples"]
    )
    oracle_hits = sum(
        sample["scopes"]["keyness_only"]["profiles"]["10%"]["systems"][
            "oracle_question_conditioned"
        ]["evidence_retrieved"]
        for sample in history["samples"]
    )
    lines.insert(
        5,
        f"Even before retention, the default full-corpus BM25 retrieval recalled {100 * pre_hits / pre_evidence:.2f}% of evidence IDs within the fixed 128-token context ({pre_hits:,}/{pre_evidence:,}); the query-conditioned oracle reached {100 * oracle_hits / pre_evidence:.2f}% under the same context. This points to a retrieval/context ceiling as one possible limit on policy separation; it does not explain or prove a zero policy effect.",  # noqa: E501
    )
    lines.insert(6, "")
    lines.insert(
        7,
        f"Category 3 has {bycat['3'][0]} valid questions, which makes its category-specific estimates especially imprecise.",  # noqa: E501
    )
    lines += [
        "",
        "## Category outcomes and power",
        "",
        "For each of 10%, 25%, 5%, and 1%, the paired comparison uses the five BM25 policy arms. The JSON companion gives each category’s recall, paired delta and interval, within-category Holm p-value, and an additional Holm correction across all 50 category contrasts per condition/budget. This larger correction is conservative; no category is treated as confirmatory. The official category mapping is documented in the [LoCoMo evaluator](https://github.com/snap-research/locomo/blob/main/task_eval/evaluation.py#L186-L202) and its [maintainer-confirmed mapping discussion](https://github.com/snap-research/locomo/issues/29).",  # noqa: E501
        "",
        "Power is a normal paired-influence MDE approximation at 80% power. It reports question-independence and conversation-cluster standard errors, conditional on these ten conversations. The cluster Bonferroni MDE uses alpha=.05/10 for ten overall policy contrasts. Identical question outcomes have an unidentifiable MDE (null in JSON), not a true MDE of zero. With only ten clusters, the cluster approximation is itself uncertain. Existing bootstrap p-values are legacy sign-tail diagnostics and are not null-centered tests; intervals and p-values condition on these ten conversations.",  # noqa: E501
        "",
        "| Condition/budget | FIFO recall | Salience recall | Salience − FIFO (pp) | 95% paired CI (pp) | Exact sign-flip Holm p | Significant overall contrasts / 10 | 5-policy recall range (pp) | Cluster MDE80 (pp) |",  # noqa: E501
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, value in conditions.items():
        overall = value["overall"]["recall"]
        reverse = value["overall_pairwise"]["fifo_bm25__minus__salience_bm25"]
        contrast = {
            **reverse,
            "delta_pp": -reverse["delta_pp"],
            "bootstrap_ci_pp": [-reverse["bootstrap_ci_pp"][1], -reverse["bootstrap_ci_pp"][0]],
        }
        powv = contrast["power"]["mde80_pp_cluster_se_bonferroni_10"]
        ci = contrast["bootstrap_ci_pp"]
        exact_p = contrast["signflip_p_holm_overall_10_contrasts"]
        sig_count = sum(
            v["signflip_p_holm_overall_10_contrasts"] < 0.05
            for v in value["overall_pairwise"].values()
        )
        recall_range = 100 * (max(overall.values()) - min(overall.values()))
        lines.append(
            f"| {key} | {100 * overall['fifo_bm25']:.2f}% | {100 * overall['salience_bm25']:.2f}% | {contrast['delta_pp']:+.2f} | [{ci[0]:+.2f}, {ci[1]:+.2f}] | {exact_p:.4f} | {sig_count}/10 | {recall_range:.2f} | {'not identifiable' if powv is None or contrast['power']['se_cluster_robust_10_conversations'] == 0 else f'{powv:.2f}'} |"  # noqa: E501
        )
    lines += [
        "",
        "The exact two-sided sign-flip test works on the ten conversation-level paired hit-count differences, under the assumption that strategy labels are exchangeable under the null. Holm correction covers the 10 overall contrasts within each condition/budget; the count column reports how many pass .05 in that family. No cross-scope overall family is claimed. The intervals and legacy percentile-derived sign-tail p-values resample questions within the same ten conversations and are descriptive diagnostics. Cluster-robust estimates expose the limitation from having only ten clusters. MDE is a sensitivity scale under the stated normal approximation, not observed power and not evidence that smaller effects are absent.",  # noqa: E501
        "",
        "The 5-policy recall range column compares how separated the arms are at each budget. A tighter cap can widen gaps, leave gaps similar, or collapse all arms toward a low-recall floor; no monotonic increase is assumed.",  # noqa: E501
        "",
        "## Category recall detail",
        "",
        "The table reports evidence-ID recall by the five official question types for every condition and budget.",  # noqa: E501
        "",
        "| Condition/budget | Type | Questions | Evidence IDs | FIFO | LRU | Decay | Salience | Consolidate |",  # noqa: E501
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, value in conditions.items():
        for code, cat in value["categories"].items():
            recall = cat["recall"]
            lines.append(
                f"| {key} | {code} {cat['name']} | {cat['questions']} | {cat['evidence_ids']} | "
                + " | ".join(f"{100 * recall[s]:.2f}%" for s in POLICIES)
                + " |"
            )
    lines += [
        "",
        "## Did the budget or forgetting pressure activate?",
        "",
        "Each ratio is a per-conversation UTF-8 body-byte cap, independently applied to both signal conditions. Counts and bytes below sum separate sample stores; they are not one pooled vault. A trigger means fewer turns retained than existed at intake. The 1% and 5% runs include all five policies, both conditions, all questions, and fixed seed.",  # noqa: E501
        "",
        "| Budget | Triggered stores / sample-condition (policy counts) | Retained bytes summed across 5 policies and 2 conditions | Pairwise retained-ID Jaccard (mean range) |",  # noqa: E501
        "|---|---|---|---|",
    ]
    for ratio, info in retention.items():
        trigger = ", ".join(
            f"{k}: {v}/{info['condition_budget_units']}"
            for k, v in info["sample_count_budget_triggered"].items()
        )
        b = sum(info["sum_retained_bytes_across_sample_condition_stores"].values())
        jac = list(info["pairwise_retained_id_jaccard_mean"].values())
        lines.append(
            f"| {ratio} | {trigger} | {b:,} summed across policies and both conditions | {min(jac):.3f}–{max(jac):.3f} |"  # noqa: E501
        )
    lines += [
        "",
        "High Jaccard means the strategies often kept the same turns. The tighter caps may push several strategies to a floor where almost no useful turns remain, so a lower budget does not guarantee a larger measured gap. Mean retained body bytes per turn are in the JSON companion; different retained counts at the same byte cap may reflect turn length, but this is not a causal explanation. Full per-policy bytes, counts, trigger counts and pairwise Jaccards are also in the JSON companion.",  # noqa: E501
        "",
        "## Limits and reproducibility",
        "",
        "The experiment authorization metadata was checked read-only with `./.venv/bin/python scripts/approval_metadata_status.py --approval-sha256 25ea0d719e5f4e68baa1c87c5ddfb43e1e3e760b79943f34984d751b65fc1d23`: two matching approval events were found and the sidecar matched that SHA-256. The current query snapshot content was not revalidated. The previous approval-info display was hard-coded false and was corrected in user commit `f02e490`.",  # noqa: E501
        "",
        "No LLM inference or answer generation was used. Consolidation still uses deterministic mock proposals and simulated approvals. The evaluator measures retrieval of gold evidence IDs; it does not measure whether a generated answer is correct. The 10/25% results reuse the frozen Day 7 aggregate with per-question outcomes; 1/5% reruns the deterministic local evaluator. The new pressure results show all strategies were already budget-constrained at 10% and 25%, so 'budget never triggered' does not explain the null result. Tighter caps may create a floor with little evidence left. No user-level or general-population claim follows from this benchmark.",  # noqa: E501
        "",
        f"Pressure result SHA-256: `{pressure_hash}`. Seed: `{seed}`. Bootstrap repeats: {repeats}. New pressure evaluation wall time: {elapsed:.2f} s. Command: `python3 scripts/day10_null_analysis.py --data {data_path} --history reports/day7_official.json --pressure-output reports/day10_pressure.json --output reports/day10_null_result_analysis.md --seed {seed} --bootstrap-repeats {repeats}`.",  # noqa: E501
        "",
        "Numeric tables and complete paired contrasts: [day10_null_result_analysis.json](day10_null_result_analysis.json).",  # noqa: E501
    ]
    output_path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    pressure_group = parser.add_mutually_exclusive_group(required=True)
    pressure_group.add_argument("--pressure-output", type=Path)
    pressure_group.add_argument(
        "--pressure-input", type=Path, help="Reuse a saved pressure result and rebuild the report"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--bootstrap-repeats", type=int, default=1000)
    parser.add_argument(
        "--pressure-runtime-seconds",
        type=float,
        default=0.0,
        help="Measured runtime to preserve when rebuilding from a saved pressure result",
    )
    args = parser.parse_args()
    started = time.perf_counter()
    pressure_path = args.pressure_input or args.pressure_output
    if args.pressure_input:
        pressure = json.loads(args.pressure_input.read_text())
    else:
        pressure = evaluate_file(
            args.data, seed=args.seed, ratios=(0.01, 0.05), bootstrap_repeats=args.bootstrap_repeats
        )
    # Keep only primary BM25 policy arms in the saved pressure artifact.
    if not args.pressure_input:
        for sample in pressure["samples"]:
            for condition in DEFAULT_CONDITIONS:
                for profile in sample["scopes"][condition]["profiles"].values():
                    profile["systems"] = {name: profile["systems"][name] for name in POLICIES}
                    profile.pop("per_question_systems", None)
                    for row in profile["per_question"]:
                        row["hits"] = {name: row["hits"][name] for name in POLICIES}
        pressure["strategies"] = list(POLICIES)
        pressure["bootstrap"] = {}
        for condition in DEFAULT_CONDITIONS:
            for ratio in (0.01, 0.05):
                label = f"{ratio:.0%}"
                rows = _records(pressure, condition, label)
                pressure["bootstrap"][f"{condition}/{label}"] = paired_stratified_bootstrap(
                    rows,
                    strategies=pressure["strategies"],
                    pairwise_strategies=POLICIES,
                    repeats=args.bootstrap_repeats,
                    seed=args.seed ^ int(ratio * 1000) ^ int(condition.endswith("access")),
                )
        args.pressure_output.parent.mkdir(parents=True, exist_ok=True)
        for sample in pressure["samples"]:
            for condition in DEFAULT_CONDITIONS:
                for profile in sample["scopes"][condition]["profiles"].values():
                    profile.pop("before_budget", None)
                    profile.pop("before_per_question", None)
        args.pressure_output.write_text(
            json.dumps(pressure, sort_keys=True, separators=(",", ":")) + "\n"
        )
    elapsed = (
        time.perf_counter() - started if not args.pressure_input else args.pressure_runtime_seconds
    )
    build_report(
        args.history,
        pressure_path,
        args.data,
        args.output,
        repeats=args.bootstrap_repeats,
        seed=args.seed,
        elapsed=elapsed,
    )
    print(f"pressure evaluation complete in {elapsed:.2f}s; report={args.output}")


if __name__ == "__main__":
    main()
