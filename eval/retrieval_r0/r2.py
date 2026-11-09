"""R2 measurement: shipped MCP mode=multihop against the BM25 single hop.

Synthetic report split only. No judge and no HTTP mode change.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from eval.retrieval_r0.cases import generate
from eval.retrieval_r0.corpus import assert_offline_env, load_corpus
from eval.retrieval_r0.metrics import evidence_recall, mean, percentile

PRIMARY_K = 10
SINGLE_HOP = ("time_update", "conflict", "delete", "multi_tenant", "poisoning")


def measure(per_class: int = 160, *, seed: int = 20261108) -> dict[str, Any]:
    """Score the report split. Returns means, gains, and latency."""

    assert_offline_env()
    generated = generate(per_class, seed=seed)
    by_class: dict[str, dict[str, float]] = {}
    multi_times: list[float] = []
    single_times: list[float] = []
    for case_type in ("time_update", "conflict", "delete", "multi_tenant", "poisoning", "multihop"):
        corpus = generated[("report", case_type)]
        with tempfile.TemporaryDirectory(prefix="memfw-r2-") as directory:
            built = load_corpus(corpus, Path(directory))
            multi_scores: list[float] = []
            single_scores: list[float] = []
            for question in corpus.questions:
                if question.split != "report":
                    continue
                started = time.perf_counter()
                ranked = built.index.ranked_search(
                    question.question, limit=PRIMARY_K, ranking="bm25"
                )
                single_ids = [item[0] for item in ranked]
                single_times.append(time.perf_counter() - started)
                started = time.perf_counter()
                expanded = built.gateway.search(question.question, limit=PRIMARY_K, mode="multihop")
                multi_times.append(time.perf_counter() - started)
                multi_ids = [str(item["id"]) for item in expanded["results"]]
                single_scores.append(
                    evidence_recall(
                        single_ids,
                        question.evidence_ids,
                        question.forbidden_ids,
                        k=PRIMARY_K,
                    )
                )
                multi_scores.append(
                    evidence_recall(
                        multi_ids,
                        question.evidence_ids,
                        question.forbidden_ids,
                        k=PRIMARY_K,
                    )
                )
        single_mean = mean(single_scores)
        multi_mean = mean(multi_scores)
        by_class[case_type] = {
            "n": float(len(multi_scores)),
            "single_recall": round(single_mean, 6),
            "multihop_recall": round(multi_mean, 6),
            "gain": round(multi_mean - single_mean, 6),
        }
        print(
            f"r2 {case_type} single={single_mean:.4f} multi={multi_mean:.4f} "
            f"gain={multi_mean - single_mean:.4f}",
            file=sys.stderr,
        )
    multihop_gain = by_class["multihop"]["gain"]
    single_gains = [by_class[name]["gain"] for name in SINGLE_HOP]
    single_drop = -mean(single_gains)
    multi_p95 = percentile(multi_times, 95)
    single_p95 = percentile(single_times, 95)
    return {
        "phase": "R2",
        "synthetic": True,
        "judge": False,
        "http_multihop": False,
        "seed": seed,
        "per_class": per_class,
        "primary_k": PRIMARY_K,
        "rounds": 1,
        "by_class": by_class,
        "multihop_gain": round(multihop_gain, 6),
        "single_hop_drop": round(single_drop, 6),
        "multihop_p95": round(multi_p95, 6),
        "single_hop_p95": round(single_p95, 6),
        "p95_ratio": round(multi_p95 / single_p95, 6) if single_p95 else None,
        "gate_gain": multihop_gain >= 0.05,
        "gate_drop": single_drop <= 0.01,
        "gate_p95": multi_p95 <= 2 * single_p95,
    }


def main() -> int:
    report = measure()
    out = Path(__file__).with_name("r2_report.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"multihop_gain={report['multihop_gain']} single_drop={report['single_hop_drop']} "
        f"p95_ratio={report['p95_ratio']} "
        f"gates={report['gate_gain']} {report['gate_drop']} {report['gate_p95']}"
    )
    return 0 if report["gate_gain"] and report["gate_drop"] and report["gate_p95"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
