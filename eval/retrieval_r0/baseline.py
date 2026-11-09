"""Run the R0 baseline on the report split and write the metric record."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from eval.retrieval_r0 import GENERATOR_VERSION
from eval.retrieval_r0.cases import CASE_TYPES, ClassCorpus, Question, generate
from eval.retrieval_r0.corpus import PATHS, PATHS_NOT_RUN, load_corpus, timed_search
from eval.retrieval_r0.metrics import all_evidence, evidence_recall, mean, percentile
from eval.retrieval_r0.power import PowerResult, required_per_class
from memfw.stats.keyness import tokenize

PRIMARY_K = 10
EXTRA_K = 5
RANKING_REPEATS = 1
LATENCY_REPEATS = 1


def _round(value: float) -> float:
    return round(value, 6)


def _question_digest(questions: list[Question]) -> str:
    payload = "\n".join(question.question_id for question in questions)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tokens(built_ids: dict[str, Any], retrieved: list[str], k: int) -> int:
    total = 0
    for identifier in retrieved[:k]:
        document = built_ids.get(identifier)
        if document is None:
            continue
        total += len(tokenize(f"{document.title}\n{document.body}"))
    return total


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    recalls = [float(row["evidence_recall@10"]) for row in rows]
    all_hits = [float(row["all_evidence@10"]) for row in rows]
    recalls_5 = [float(row["evidence_recall@5"]) for row in rows]
    all_hits_5 = [float(row["all_evidence@5"]) for row in rows]
    elapsed = [float(row["elapsed_seconds"]) for row in rows]
    tokens = [float(row["tokens@10"]) for row in rows]
    return {
        "n": len(rows),
        "ranking_repeats": RANKING_REPEATS,
        "latency_repeats": LATENCY_REPEATS,
        "evidence_recall@5": _round(mean(recalls_5)),
        "evidence_recall@10": _round(mean(recalls)),
        "all_evidence@5": _round(mean(all_hits_5)),
        "all_evidence@10": _round(mean(all_hits)),
        "p50_seconds": _round(percentile(elapsed, 50)),
        "p95_seconds": _round(percentile(elapsed, 95)),
        "mean_tokens@10": _round(mean(tokens)),
        "mean_hit_count": _round(mean([float(row["hit_count"]) for row in rows])),
        "forbidden_in_topk_rate": _round(mean([float(row["forbidden_hit"]) for row in rows])),
        "other_tenant_in_topk_rate": _round(mean([float(row["other_tenant_hit"]) for row in rows])),
        "poison_in_topk_rate": _round(mean([float(row["poison_hit"]) for row in rows])),
    }


def _score_question(
    question: Question,
    retrieved: list[str],
    elapsed: float,
    by_id: dict[str, Any],
) -> dict[str, Any]:
    top = retrieved[:PRIMARY_K]
    return {
        "question_id": question.question_id,
        "case_type": question.case_type,
        "hop": question.hop,
        "evidence_recall@5": evidence_recall(
            retrieved, question.evidence_ids, question.forbidden_ids, k=EXTRA_K
        ),
        "evidence_recall@10": evidence_recall(
            retrieved, question.evidence_ids, question.forbidden_ids, k=PRIMARY_K
        ),
        "all_evidence@5": all_evidence(
            retrieved, question.evidence_ids, question.forbidden_ids, k=EXTRA_K
        ),
        "all_evidence@10": all_evidence(
            retrieved, question.evidence_ids, question.forbidden_ids, k=PRIMARY_K
        ),
        "elapsed_seconds": elapsed,
        "tokens@10": _tokens(by_id, retrieved, PRIMARY_K),
        "hit_count": len(retrieved),
        "forbidden_hit": float(any(item in top for item in question.forbidden_ids)),
        "other_tenant_hit": float(any(item in top for item in question.other_tenant_ids)),
        "poison_hit": float(any(item in top for item in question.poison_ids)),
    }


def evaluate(
    corpora: dict[tuple[str, str], ClassCorpus],
    root: Path,
    *,
    split: str = "report",
    paths: tuple[str, ...] = PATHS,
    verbose: bool = False,
) -> dict[str, Any]:
    """Search the split with each shipped path. Gold is not passed to search."""

    per_path_rows: dict[str, list[dict[str, Any]]] = {path: [] for path in paths}
    by_class: dict[str, dict[str, Any]] = {}
    determinism: dict[str, dict[str, bool]] = {path: {} for path in paths}
    for case_type in CASE_TYPES:
        corpus = corpora[(split, case_type)]
        if verbose:
            print(
                f"load {split} {case_type} n={len(corpus.questions)}",
                file=sys.stderr,
                flush=True,
            )
        built = load_corpus(corpus, root / f"{split}-{case_type}")
        class_rows: dict[str, list[dict[str, Any]]] = {path: [] for path in paths}
        for path in paths:
            first_ids: list[str] | None = None
            for question in corpus.questions:
                retrieved, elapsed, meta = timed_search(path, built, question, limit=PRIMARY_K)
                if first_ids is None:
                    first_ids = list(retrieved)
                row = _score_question(question, retrieved, elapsed, built.by_id)
                row["mode"] = meta["mode"]
                row["degraded"] = meta["degraded"]
                row["entry"] = meta["entry"]
                class_rows[path].append(row)
            repeated, _elapsed, _meta = timed_search(
                path, built, corpus.questions[0], limit=PRIMARY_K
            )
            determinism[path][case_type] = repeated == first_ids
            per_path_rows[path].extend(class_rows[path])
        by_class[case_type] = {path: _aggregate(class_rows[path]) for path in paths}
    paths_report = {}
    for path in paths:
        modes = {row["mode"] for row in per_path_rows[path] if row.get("mode") is not None}
        degraded = {
            row["degraded"] for row in per_path_rows[path] if row.get("degraded") is not None
        }
        paths_report[path] = {
            "by_class": {case_type: by_class[case_type][path] for case_type in CASE_TYPES},
            "overall": _aggregate(per_path_rows[path]),
            "ranking_repeat_matched": all(determinism[path].values()),
            "observed_mode": next(iter(modes)) if len(modes) == 1 else None,
            "observed_degraded": next(iter(degraded)) if len(degraded) == 1 else None,
        }
    return {"split": split, "paths": paths_report}


def _power_payload(result: PowerResult) -> dict[str, Any]:
    return {
        "gap": result.gap,
        "alpha": result.alpha,
        "n_hypotheses": result.n_hypotheses,
        "holm_alpha": result.holm_alpha,
        "power_target": result.power_target,
        "baseline_rate": result.baseline_rate,
        "n_bootstrap": result.n_bootstrap,
        "n_sim": result.n_sim,
        "seed": result.seed,
        "per_class": result.per_class,
        "estimated_power": result.estimated_power,
        "meets_power_target": result.meets_power_target,
        "power_by_n": {str(n): power for n, power in sorted(result.power_by_n.items())},
        "method": result.method,
    }


def build_report(
    root: Path,
    *,
    power: PowerResult | None = None,
    per_class: int | None = None,
    seed: int = 20261108,
    paths: tuple[str, ...] = PATHS,
    verbose: bool = False,
) -> dict[str, Any]:
    """Generate both splits, score the report split, and return the record.

    Pass ``per_class`` only from tests. The published run leaves it unset so the
    power analysis chooses the size.
    """

    if per_class is None:
        sized = power or required_per_class(seed=seed, verbose=verbose)
        count = sized.per_class
    else:
        sized = power
        count = per_class
    corpora = generate(count, seed=seed)
    measured = evaluate(corpora, root, split="report", paths=paths, verbose=verbose)
    dev_questions = [
        question
        for (split, _case), corpus in corpora.items()
        if split == "dev"
        for question in corpus.questions
    ]
    report_questions = [
        question
        for (split, _case), corpus in corpora.items()
        if split == "report"
        for question in corpus.questions
    ]
    dev_ids = {question.question_id for question in dev_questions}
    report_ids = {question.question_id for question in report_questions}
    if not dev_ids.isdisjoint(report_ids):
        raise RuntimeError("dev and report question ids overlap")
    return {
        "phase": "R0",
        "generator_version": GENERATOR_VERSION,
        "synthetic": True,
        "gold_in_prompts": False,
        "prompts_sent": 0,
        "primary_k": PRIMARY_K,
        "ranking_repeats": RANKING_REPEATS,
        "latency_repeats": LATENCY_REPEATS,
        "empty_gold_policy": (
            "when evidence_ids is empty, evidence recall@k and AllEvidence@k "
            "are 1 only if no forbidden id is in the top k"
        ),
        "power": None if sized is None else _power_payload(sized),
        "per_class": count,
        "seed": seed,
        "dev": {
            "n_questions": len(dev_questions),
            "n_per_class": count,
            "question_sha256": _question_digest(dev_questions),
            "held_out": True,
        },
        "report": {
            "n_questions": len(report_questions),
            "n_per_class": count,
            "question_sha256": _question_digest(report_questions),
            "classes": list(CASE_TYPES),
        },
        "measured": measured,
        "paths_not_run": list(PATHS_NOT_RUN),
    }


def write_report(destination: Path, report: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "LATENCY_REPEATS",
    "PRIMARY_K",
    "RANKING_REPEATS",
    "build_report",
    "evaluate",
    "write_report",
]
