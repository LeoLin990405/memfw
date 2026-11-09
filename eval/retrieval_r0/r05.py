"""R0.5 short-text expansion experiment on the R0 report split.

No judge and no interface change. The multihop interval is the R2 gate.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

from eval.retrieval_r0.baseline import PRIMARY_K
from eval.retrieval_r0.cases import CASE_TYPES, generate
from eval.retrieval_r0.corpus import load_corpus, search_path
from eval.retrieval_r0.expand import (
    PASSAGE_K,
    RRF_K,
    document_frequencies,
    expanded_search,
)
from eval.retrieval_r0.metrics import evidence_recall
from eval.retrieval_r0.power import bootstrap_mean_interval


def gain_interval(
    differences: list[float],
    *,
    alpha: float = 0.05,
    n_bootstrap: int = 2000,
    seed: int = 20261108,
) -> dict[str, Any]:
    """Paired percentile interval for the mean recall change."""

    if not differences:
        raise ValueError("differences must be non-empty")
    lower, upper = bootstrap_mean_interval(
        differences,
        alpha=alpha,
        n_bootstrap=n_bootstrap,
        rng=random.Random(seed),
    )
    return {
        "n": len(differences),
        "mean": round(sum(differences) / len(differences), 6),
        "lower": round(lower, 6),
        "upper": round(upper, 6),
        "alpha": alpha,
        "n_bootstrap": n_bootstrap,
        "seed": seed,
        "repeats": 1,
    }


def _recall(retrieved: list[str], question: Any) -> float:
    return evidence_recall(retrieved, question.evidence_ids, question.forbidden_ids, k=PRIMARY_K)


def run_experiment(
    root: Path,
    *,
    per_class: int,
    seed: int = 20261108,
    n_bootstrap: int = 2000,
    verbose: bool = False,
) -> dict[str, Any]:
    """Compare expansion with first-hop BM25 on the report split."""

    corpora = generate(per_class, seed=seed)
    multihop: list[float] = []
    single_hop: list[float] = []
    by_class: dict[str, dict[str, Any]] = {}
    for case_type in CASE_TYPES:
        corpus = corpora[("report", case_type)]
        if verbose:
            print(f"r05 {case_type} n={len(corpus.questions)}", file=sys.stderr, flush=True)
        built = load_corpus(corpus, root / case_type)
        frequencies = document_frequencies(built)
        differences: list[float] = []
        for question in corpus.questions:
            baseline = search_path("memory_bm25", built, question, limit=PRIMARY_K)
            expanded = expanded_search(built, question, frequencies)
            differences.append(_recall(expanded, question) - _recall(baseline, question))
        summary = gain_interval(differences, n_bootstrap=n_bootstrap, seed=seed)
        by_class[case_type] = summary
        if case_type == "multihop":
            multihop.extend(differences)
        else:
            single_hop.extend(differences)
    multihop_interval = gain_interval(multihop, n_bootstrap=n_bootstrap, seed=seed)
    single_interval = gain_interval(single_hop, n_bootstrap=n_bootstrap, seed=seed)
    return {
        "phase": "R0.5",
        "synthetic": True,
        "judge": False,
        "interface_change": False,
        "path": "memory_bm25",
        "passage_k": PASSAGE_K,
        "rrf_k": RRF_K,
        "primary_k": PRIMARY_K,
        "per_class": per_class,
        "seed": seed,
        "ranking_repeats": 1,
        "templates_per_class": 3,
        "prd": "memfw-retrieval-roadmap.md",
        "assumptions": [
            "paired gains are treated as monotonic",
            "the interval does not add misses beyond the measured paired differences",
        ],
        "multihop": multihop_interval,
        "single_hop": single_interval,
        "by_class": by_class,
        "gate": "stop" if multihop_interval["lower"] <= 0 else "continue",
    }


def write_experiment(destination: Path, report: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["gain_interval", "run_experiment", "write_experiment"]
