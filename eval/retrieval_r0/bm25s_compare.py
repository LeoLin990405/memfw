"""bm25s adapter vs built-in FTS on the synthetic R0 report split (a report, not a gate).

Three coordinators over the same vault, each with the ``compat_fts`` policy the
MCP ``two_stage`` default uses (tenant, source, the index's temporal clause):
``fts`` (``FtsProvider`` alone, today's ``two_stage``), ``bm25s`` (the
``memfw-adapter-bm25s`` provider alone, in FTS's place) and ``fused`` (both,
RRF). The R0 corpora carry no ``privacy`` field and set no
``legacy_privacy_default``, so their memories are ``local-private``; the bm25s
arm therefore indexes every level (``public,shared,local-private``) to see the
same memories as FTS. Its index goes to a temporary directory, never the
vault. Synthetic data only, no model, no network.

    python -m eval.retrieval_r0.bm25s_compare [--per-class N]

writes ``eval/retrieval_r0/bm25s_report.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from memfw_adapter_bm25s import BM25S_VERSION
from memfw_adapter_bm25s import factory as bm25s_factory

from eval.retrieval_r0.cases import CASE_TYPES, generate
from eval.retrieval_r0.corpus import assert_offline_env, load_corpus
from eval.retrieval_r0.metrics import all_evidence, evidence_recall, mean, percentile
from memfw.recall import Coordinator, FilterPolicy
from memfw.recall.builtin import FtsProvider, default_resolvers

PRIMARY_K = 10
ARMS = ("fts", "bm25s", "fused")


def _arms(root: Path, data_dir: Path) -> dict[str, Coordinator]:
    resolvers = default_resolvers(root)

    def bm25s() -> Any:
        return bm25s_factory(
            {
                "vault": str(root),
                "clearance": "public,shared,local-private",
                "data_dir": str(data_dir),
            }
        )

    return {
        "fts": Coordinator([FtsProvider(root)], resolvers=resolvers),
        "bm25s": Coordinator([bm25s()], resolvers=resolvers),
        "fused": Coordinator([FtsProvider(root), bm25s()], resolvers=resolvers),
    }


def _round(value: float) -> float:
    return round(value, 6)


def measure(per_class: int = 160, *, seed: int = 20261108) -> dict[str, Any]:
    """Score every report-split class with each arm."""

    assert_offline_env()
    generated = generate(per_class, seed=seed)
    by_class: dict[str, dict[str, dict[str, float]]] = {}
    overall: dict[str, dict[str, list[float]]] = {
        arm: {"recall": [], "all": [], "forbidden": [], "other_tenant": [], "seconds": []}
        for arm in ARMS
    }
    for case_type in CASE_TYPES:
        corpus = generated[("report", case_type)]
        with tempfile.TemporaryDirectory(prefix="memfw-bm25s-") as directory:
            built = load_corpus(corpus, Path(directory))
            arms = _arms(built.repository.root, Path(directory) / "bm25s-data")
            # Build the bm25s index before timing (a deployment runs
            # ``memfw-adapter-bm25s build`` ahead of the first search).
            for name in ("bm25s", "fused"):
                arms[name].search("warm up", limit=1, policy=FilterPolicy.compat_fts(tenant=""))
            rows: dict[str, dict[str, list[float]]] = {
                arm: {"recall": [], "all": [], "forbidden": [], "other_tenant": [], "seconds": []}
                for arm in ARMS
            }
            for question in corpus.questions:
                policy = FilterPolicy.compat_fts(tenant=question.tenant)
                for arm, coordinator in arms.items():
                    started = time.perf_counter()
                    result = coordinator.search(question.question, limit=PRIMARY_K, policy=policy)
                    elapsed = time.perf_counter() - started
                    top = list(result.ids)[:PRIMARY_K]
                    row = rows[arm]
                    row["recall"].append(
                        evidence_recall(
                            top, question.evidence_ids, question.forbidden_ids, k=PRIMARY_K
                        )
                    )
                    row["all"].append(
                        all_evidence(
                            top, question.evidence_ids, question.forbidden_ids, k=PRIMARY_K
                        )
                    )
                    row["forbidden"].append(
                        float(any(item in top for item in question.forbidden_ids))
                    )
                    row["other_tenant"].append(
                        float(any(item in top for item in question.other_tenant_ids))
                    )
                    row["seconds"].append(elapsed)
            by_class[case_type] = {}
            for arm in ARMS:
                by_class[case_type][arm] = {
                    "n": float(len(rows[arm]["recall"])),
                    "evidence_recall@10": _round(mean(rows[arm]["recall"])),
                    "all_evidence@10": _round(mean(rows[arm]["all"])),
                    "forbidden_in_topk_rate": _round(mean(rows[arm]["forbidden"])),
                    "other_tenant_in_topk_rate": _round(mean(rows[arm]["other_tenant"])),
                }
                for key, values in rows[arm].items():
                    overall[arm][key].extend(values)
            print(
                f"bm25s {case_type} "
                + " ".join(
                    f"{arm}={by_class[case_type][arm]['evidence_recall@10']:.4f}" for arm in ARMS
                ),
                file=sys.stderr,
            )
    summary = {
        arm: {
            "n": float(len(values["recall"])),
            "evidence_recall@10": _round(mean(values["recall"])),
            "all_evidence@10": _round(mean(values["all"])),
            "forbidden_in_topk_rate": _round(mean(values["forbidden"])),
            "other_tenant_in_topk_rate": _round(mean(values["other_tenant"])),
            "p50_seconds": _round(percentile(values["seconds"], 50)),
            "p95_seconds": _round(percentile(values["seconds"], 95)),
        }
        for arm, values in overall.items()
    }
    return {
        "phase": "adapter-bm25s",
        "synthetic": True,
        "gate": False,
        "seed": seed,
        "per_class": per_class,
        "primary_k": PRIMARY_K,
        "policy": "compat_fts",
        "bm25s_version": BM25S_VERSION,
        "bm25s_clearance": "public,shared,local-private",
        "by_class": by_class,
        "overall": summary,
        "recall_delta_bm25s_minus_fts": _round(
            summary["bm25s"]["evidence_recall@10"] - summary["fts"]["evidence_recall@10"]
        ),
        "recall_delta_fused_minus_fts": _round(
            summary["fused"]["evidence_recall@10"] - summary["fts"]["evidence_recall@10"]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--per-class", type=int, default=160)
    parser.add_argument("--seed", type=int, default=20261108)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("bm25s_report.json"))
    args = parser.parse_args(argv)
    report = measure(args.per_class, seed=args.seed)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    overall = report["overall"]
    print(
        " ".join(f"{arm}={overall[arm]['evidence_recall@10']}" for arm in ARMS)
        + f" delta_bm25s={report['recall_delta_bm25s_minus_fts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
