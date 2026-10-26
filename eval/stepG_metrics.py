"""Step G metrics: answer quality, LLM judge, efficiency, paired significance tests.

Reads the raw ``questions.jsonl`` files produced by ``eval/stepG_runner.py`` and
writes ``metrics.json`` + ``metrics.md``. No numbers are hand-entered; every
value in the outputs is computed from the run files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_runner import (  # noqa: E402
    SYSTEMS,
    _read_rows,
    _resolve,
    _write_json,
    latency_summary,
    load_config,
    make_chat,
    percentile,
)

JUDGE_PROMPT = """You are a strict grader comparing a predicted answer with the reference answer.

Question: {question}
Reference answer: {reference}
Predicted answer: {prediction}
Question category: {category}

Rules:
- Category 5 questions are adversarial: the correct behaviour is to state that the
  information is unknown / not mentioned. Mark them correct if the prediction declines
  to answer, otherwise incorrect.
- For other categories, mark correct only if the prediction expresses the same
  information as the reference (names, numbers and dates must agree); extra wording is
  allowed.
Return exactly one JSON object: {{"verdict": "correct"}} or {{"verdict": "incorrect"}}.
"""

_JUDGE_VERDICT = re.compile(r'"verdict"\s*:\s*"(correct|incorrect)"', re.IGNORECASE)
# Same abstention markers as answer_score (category 5), to keep judge and primary metric consistent.
ABSTAIN_MARKERS = ("unknown", "not mentioned")


def _run_dirs(results_dir: Path) -> list[Path]:
    runs = results_dir / "runs"
    if not runs.exists():
        return []
    return sorted(path for path in runs.iterdir() if path.is_dir())


def load_rows(results_dir: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Return ``{system: {run_id: rows}}`` for every completed run file."""
    collected: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for run_dir in _run_dirs(results_dir):
        if run_dir.name.startswith("dry-run"):
            continue
        for system_dir in sorted(run_dir.iterdir()):
            if not system_dir.is_dir():
                continue
            rows_path = system_dir / "questions.jsonl"
            if not rows_path.exists():
                continue
            rows = _read_rows(rows_path)
            if not rows:
                continue
            # Arm directory name is canonical; legacy rows may still say "mem0" for the
            # custom arm after a directory rename.
            for row in rows:
                row["system"] = system_dir.name
            collected.setdefault(system_dir.name, {})[run_dir.name] = rows
    return collected


def load_ingest_token_usage(results_dir: Path) -> dict[str, dict[str, int]]:
    """Per-system LLM token usage during ingest, summed from ``write_turns.jsonl``."""
    collected: dict[str, dict[str, int]] = {}
    for run_dir in _run_dirs(results_dir):
        if run_dir.name.startswith("dry-run"):
            continue
        for system_dir in sorted(run_dir.iterdir()):
            path = system_dir / "write_turns.jsonl"
            if not system_dir.is_dir() or not path.exists():
                continue
            totals = collected.setdefault(
                system_dir.name, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                usage = json.loads(line).get("token_usage") or {}
                for key in totals:
                    totals[key] += int(usage.get(key) or 0)
    return collected


def load_write_latencies(results_dir: Path) -> dict[str, list[float]]:
    """Per-turn write latencies read from ``write_turns.jsonl`` (public run artifacts)."""
    collected: dict[str, list[float]] = {}
    for run_dir in _run_dirs(results_dir):
        if run_dir.name.startswith("dry-run"):
            continue
        for system_dir in sorted(run_dir.iterdir()):
            path = system_dir / "write_turns.jsonl"
            if not system_dir.is_dir() or not path.exists():
                continue
            values = [
                float(json.loads(line)["seconds"])
                for line in path.read_text().splitlines()
                if line.strip()
            ]
            collected.setdefault(system_dir.name, []).extend(values)
    return collected


def load_summaries(results_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    summaries: dict[str, dict[str, dict[str, Any]]] = {}
    for run_dir in _run_dirs(results_dir):
        if run_dir.name.startswith("dry-run"):
            continue
        for system_dir in sorted(run_dir.iterdir()):
            summary_path = system_dir / "summary.json"
            if not system_dir.is_dir() or not summary_path.exists():
                continue
            summaries.setdefault(system_dir.name, {})[run_dir.name] = json.loads(
                summary_path.read_text(encoding="utf-8")
            )
    return summaries


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _mean_or_none(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return _mean(present) if present else None


def _context_dia_ids(row: dict[str, Any]) -> set[str]:
    """Dia ids of the memories that survived the 128-token context fit."""
    context_items = row.get("context_items")
    if isinstance(context_items, list) and context_items:
        ids: set[str] = set()
        for item in context_items:
            if isinstance(item, dict):
                ids.update(item.get("dia_ids") or [])
        return ids
    # Legacy rows store only context texts; map them back to the retrieved items.
    text_to_ids: dict[str, set[str]] = {}
    for item in row.get("retrieved") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("memory") or "")
        text_to_ids.setdefault(text, set()).update(item.get("dia_ids") or [])
    ids = set()
    for text in row.get("context_memories") or []:
        ids.update(text_to_ids.get(str(text), set()))
    return ids


def _retrieved_dia_ids(row: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in row.get("retrieved") or []:
        if isinstance(item, dict):
            ids.update(item.get("dia_ids") or [])
    return ids


def aggregate_system(
    rows_by_run: dict[str, list[dict[str, Any]]],
    summaries_by_run: dict[str, dict[str, Any]] | None = None,
    write_latency_values: Sequence[float] | None = None,
    ingest_tokens: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate one system across repeats; per-question means are the analysis unit."""
    run_ids = sorted(rows_by_run)
    per_question: dict[str, list[float]] = {}
    per_question_f1: dict[str, list[float]] = {}
    per_question_recall_pre: dict[str, list[float]] = {}
    per_question_recall_post: dict[str, list[float]] = {}
    micro_pre = [0.0, 0.0]  # hits, evidence total
    micro_post = [0.0, 0.0]
    for run_id in run_ids:
        for row in rows_by_run[run_id]:
            category = int(row.get("category") or 0)
            per_question.setdefault(row["question_id"], []).append(float(row["answer_score"]))
            if category != 5:
                per_question_f1.setdefault(row["question_id"], []).append(float(row["token_f1"]))
            evidence = set(row.get("evidence_turn_ids") or [])
            if evidence:
                pre_hits = len(evidence & _retrieved_dia_ids(row))
                post_hits = len(evidence & _context_dia_ids(row))
                per_question_recall_pre.setdefault(row["question_id"], []).append(
                    pre_hits / len(evidence)
                )
                per_question_recall_post.setdefault(row["question_id"], []).append(
                    post_hits / len(evidence)
                )
                micro_pre[0] += pre_hits
                micro_pre[1] += len(evidence)
                micro_post[0] += post_hits
                micro_post[1] += len(evidence)
    question_scores = {key: statistics.fmean(values) for key, values in per_question.items()}
    question_f1 = {key: statistics.fmean(values) for key, values in per_question_f1.items()}
    question_recall_pre = {
        key: statistics.fmean(values) for key, values in per_question_recall_pre.items()
    }
    question_recall_post = {
        key: statistics.fmean(values) for key, values in per_question_recall_post.items()
    }
    all_rows = [row for run in rows_by_run.values() for row in run]
    write_latencies: list[float] = []
    ingest_seconds: list[float] = []
    storage_bytes: list[float] = []
    memory_counts: list[float] = []
    retrieval_latencies = [float(row["retrieval_seconds"]) for row in all_rows]
    answer_latencies = [float(row["answer_seconds"]) for row in all_rows]
    token_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    answer_tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    errors: list[Any] = []
    if ingest_tokens:
        for key in token_usage:
            token_usage[key] += int(ingest_tokens.get(key) or 0)
    for run_id in run_ids:
        summary = (summaries_by_run or {}).get(run_id)
        if summary:
            ingest_seconds.append(float(summary.get("ingest_seconds") or 0.0))
            if summary.get("write_latency"):
                write_latencies.append(float(summary["write_latency"].get("mean_seconds") or 0.0))
            storage_bytes.append(float(summary.get("storage_bytes") or 0.0))
            memory_counts.append(float(summary.get("memory_count") or 0.0))
            usage = summary.get("llm_token_usage") or {}
            if not token_usage["calls"]:
                for key in token_usage:
                    token_usage[key] += int(usage.get(key) or 0)
            tokens = summary.get("answer_token_usage") or {}
            for key in answer_tokens:
                answer_tokens[key] += int(tokens.get(key) or 0)
            errors.extend(summary.get("errors") or [])
    per_run_means = {
        run_id: statistics.fmean(float(row["answer_score"]) for row in rows)
        for run_id, rows in sorted(rows_by_run.items())
    }
    return {
        "repeats": len(run_ids),
        "run_ids": run_ids,
        "question_count": len(question_scores),
        "scored_question_count_non_adversarial": len(question_f1),
        "mean_answer_score": statistics.fmean(question_scores.values())
        if question_scores
        else None,
        "mean_token_f1": statistics.fmean(question_f1.values()) if question_f1 else None,
        "per_run_mean_answer_score": per_run_means,
        "per_question_answer_score": dict(sorted(question_scores.items())),
        "per_question_token_f1": dict(sorted(question_f1.items())),
        "mean_evidence_recall": (
            statistics.fmean(question_recall_post.values()) if question_recall_post else None
        ),
        "mean_evidence_recall_prefit": (
            statistics.fmean(question_recall_pre.values()) if question_recall_pre else None
        ),
        "mean_evidence_recall_postfit": (
            statistics.fmean(question_recall_post.values()) if question_recall_post else None
        ),
        "micro_evidence_recall_prefit": (micro_pre[0] / micro_pre[1] if micro_pre[1] else None),
        "micro_evidence_recall_postfit": (micro_post[0] / micro_post[1] if micro_post[1] else None),
        "per_question_evidence_recall_prefit": dict(sorted(question_recall_pre.items())),
        "per_question_evidence_recall_postfit": dict(sorted(question_recall_post.items())),
        "write_latency": (
            {
                "mean_seconds": _mean_or_none(write_latencies),
                "per_run_means": dict(zip(run_ids, write_latencies, strict=False)),
            }
            if write_latencies
            else None
        ),
        "write_latency_turns": (
            latency_summary(list(write_latency_values)) if write_latency_values else None
        ),
        "retrieval_latency": latency_summary(retrieval_latencies),
        "answer_latency": latency_summary(answer_latencies),
        "ingest_seconds_mean": _mean_or_none(ingest_seconds),
        "storage_bytes_mean": _mean_or_none(storage_bytes),
        "memory_count_mean": _mean_or_none(memory_counts),
        "llm_token_usage": token_usage,
        "answer_token_usage": answer_tokens,
        "total_token_usage": {
            "prompt_tokens": token_usage["prompt_tokens"] + answer_tokens["prompt_tokens"],
            "completion_tokens": token_usage["completion_tokens"]
            + answer_tokens["completion_tokens"],
        },
        "run_errors": len(errors),
        "error_samples": errors[:5],
    }


def exact_paired_permutation(diffs: Sequence[float]) -> dict[str, Any]:
    """Two-sided exact sign-flip test on the mean paired difference (n <= 20)."""
    values = list(diffs)
    if not values:
        return {"n": 0, "mean_difference": None, "p_value": None}
    n = len(values)
    observed = abs(statistics.fmean(values))
    if all(abs(value) < 1e-15 for value in values):
        return {"n": n, "mean_difference": 0.0, "p_value": 1.0, "method": "exact_sign_flip"}
    if n > 20:  # pragma: no cover - the fixed subset has 18 questions
        rng = random.Random(20261021)
        count = 0
        for _ in range(20000):
            signed = [value if rng.random() < 0.5 else -value for value in values]
            if abs(statistics.fmean(signed)) >= observed - 1e-15:
                count += 1
        return {
            "n": n,
            "mean_difference": statistics.fmean(values),
            "p_value": (count + 1) / 20001,
            "method": "monte_carlo_sign_flip",
        }
    extreme = 0
    total = 1 << n
    for mask in range(total):
        signed = [-value if (mask >> index) & 1 else value for index, value in enumerate(values)]
        if abs(statistics.fmean(signed)) >= observed - 1e-15:
            extreme += 1
    return {
        "n": n,
        "mean_difference": statistics.fmean(values),
        "p_value": extreme / total,
        "method": "exact_sign_flip",
        "sign_flips": total,
    }


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        rank = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = rank
        position = end + 1
    return ranks


def exact_wilcoxon(diffs: Sequence[float]) -> dict[str, Any]:
    """Two-sided exact Wilcoxon signed-rank test (zeros dropped, ties averaged)."""
    nonzero = [value for value in diffs if abs(value) > 1e-15]
    n = len(nonzero)
    if n == 0:
        return {"n": 0, "statistic": None, "p_value": 1.0, "method": "exact_wilcoxon"}
    ranks = _average_ranks([abs(value) for value in nonzero])
    w_plus = sum(rank for rank, value in zip(ranks, nonzero, strict=True) if value > 0)
    total_rank = sum(ranks)
    observed = abs(w_plus - total_rank / 2.0)
    extreme = 0
    total = 1 << n
    for mask in range(total):
        signed = sum(rank if (mask >> index) & 1 else -rank for index, rank in enumerate(ranks))
        if abs(signed / 2.0) >= observed - 1e-15:
            extreme += 1
    return {
        "n": n,
        "statistic": w_plus,
        "p_value": extreme / total,
        "method": "exact_wilcoxon",
        "signed_rank_combinations": total,
    }


def bootstrap_mean_ci(
    values: Sequence[float], *, repeats: int = 10000, seed: int = 20261021
) -> list[float | None]:
    if not values:
        return [None, None]
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choice(values) for _ in values) for _ in range(repeats))
    return [percentile(means, 0.025), percentile(means, 0.975)]


def cohen_dz(diffs: Sequence[float]) -> float | None:
    if len(diffs) < 2:
        return None
    spread = statistics.stdev(diffs)
    if spread == 0:
        return 0.0
    return statistics.fmean(diffs) / spread


def rank_biserial(diffs: Sequence[float]) -> float | None:
    nonzero = [value for value in diffs if abs(value) > 1e-15]
    if not nonzero:
        return 0.0
    ranks = _average_ranks([abs(value) for value in nonzero])
    positive = sum(rank for rank, value in zip(ranks, nonzero, strict=True) if value > 0)
    negative = sum(rank for rank, value in zip(ranks, nonzero, strict=True) if value < 0)
    total = positive + negative
    return (positive - negative) / total if total else 0.0


def minimum_detectable_difference(diffs: Sequence[float]) -> float | None:
    if len(diffs) < 2:
        return None
    return 2.8 * statistics.stdev(diffs) / math.sqrt(len(diffs))


def paired_comparison(
    memfw: dict[str, float],
    mem0: dict[str, float],
    *,
    seed: int = 20261021,
) -> dict[str, Any]:
    common = sorted(set(memfw) & set(mem0))
    diffs = [memfw[key] - mem0[key] for key in common]
    permutation = exact_paired_permutation(diffs)
    wilcoxon = exact_wilcoxon(diffs)
    return {
        "n_pairs": len(common),
        "mean_memfw": statistics.fmean([memfw[key] for key in common]) if common else None,
        "mean_mem0": statistics.fmean([mem0[key] for key in common]) if common else None,
        "mean_difference": permutation.get("mean_difference"),
        "median_difference": statistics.median(diffs) if diffs else None,
        "difference_ci95": bootstrap_mean_ci(diffs, seed=seed),
        "exact_sign_flip": permutation,
        "exact_wilcoxon": wilcoxon,
        "cohen_dz": cohen_dz(diffs),
        "rank_biserial": rank_biserial(diffs),
        "minimum_detectable_difference": minimum_detectable_difference(diffs),
        "per_question_differences": dict(zip(common, diffs, strict=True)),
    }


# ---------------------------------------------------------------------------
# LLM judge (multiple judge models; primary judge differs from the answerer)
# ---------------------------------------------------------------------------

JUDGE_SCHEMA_VERSION = "stepg-judge-v2"
JUDGE_PROMPT_VERSION = "judge-v1"


def _parse_verdict(text: str) -> str | None:
    match = _JUDGE_VERDICT.search(text)
    if match:
        return match.group(1).lower()
    lowered = text.strip().lower()
    if "incorrect" in lowered:
        return "incorrect"
    if "correct" in lowered:
        return "correct"
    return None


def judge_is_correct(row: dict[str, Any], verdict: str) -> bool:
    """Verdict first; fall back only for the same abstention markers as answer_score."""
    if verdict == "correct":
        return True
    if int(row["category"]) == 5:
        answer = str(row.get("answer", "")).lower()
        return any(marker in answer for marker in ABSTAIN_MARKERS)
    return False


def _prediction_hash(row: dict[str, Any]) -> str:
    payload = f"{row.get('answer', '')}\x00{row.get('gold_answer', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def judge_model_config(config: dict[str, Any], key: str) -> dict[str, Any] | None:
    block = config.get(key) or {}
    if not block or block.get("enabled") is False:
        return None
    return dict(block)


def run_judge(
    config: dict[str, Any],
    rows: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    out_path: Path,
    judge_config: dict[str, Any],
    label: str,
    chat: Any | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> list[dict[str, Any]]:
    """Run one judge model over every row; resume keyed by prediction hash."""
    judge_cfg = dict(judge_config)
    if chat is None:
        chat = make_chat({"llm": judge_cfg, "seed": config["seed"]})
    existing: list[dict[str, Any]] = []
    if out_path.exists():
        raw_lines = [
            line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        parsed = [json.loads(line) for line in raw_lines]
        if all(row.get("schema_version") == JUDGE_SCHEMA_VERSION for row in parsed):
            existing = parsed
        else:  # stale schema: re-judge everything
            out_path.write_text("", encoding="utf-8")
    done = {
        (row["run_id"], row["system"], row["question_id"], row.get("prediction_sha256"))
        for row in existing
    }
    verdicts: list[dict[str, Any]] = list(existing)
    judge_model = str(judge_cfg.get("model"))
    with out_path.open("a", encoding="utf-8") as stream:
        for system, by_run in sorted(rows.items()):
            for run_id, run_rows in sorted(by_run.items()):
                for row in run_rows:
                    prediction_sha = _prediction_hash(row)
                    key = (run_id, system, row["question_id"], prediction_sha)
                    if key in done:
                        continue
                    prompt = JUDGE_PROMPT.format(
                        question=row["question"],
                        reference=row["gold_answer"],
                        prediction=row["answer"],
                        category=row["category"],
                    )
                    try:
                        text, usage = chat.chat(
                            prompt, max_tokens=int(judge_cfg.get("max_tokens", 96))
                        )
                    except Exception as error:  # noqa: BLE001 - recorded honestly
                        text = f'{{"verdict": "incorrect", "error": "{type(error).__name__}"}}'
                        usage = {}
                    verdict = _parse_verdict(text)
                    record = {
                        "schema_version": JUDGE_SCHEMA_VERSION,
                        "judge_label": label,
                        "judge_model": judge_model,
                        "prompt_version": JUDGE_PROMPT_VERSION,
                        "run_id": run_id,
                        "system": system,
                        "question_id": row["question_id"],
                        "category": row["category"],
                        "prediction_sha256": prediction_sha,
                        "verdict": verdict,
                        "correct": bool(verdict == "correct"),
                        "raw": text[:400],
                        "usage": usage,
                    }
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    verdicts.append(record)
                    done.add(key)
                    log(
                        f"[judge:{label}] {system} {run_id} q={row['question_id'][:8]} -> {verdict}"
                    )
    return verdicts


def judge_per_question(
    rows: dict[str, dict[str, list[dict[str, Any]]]],
    verdicts: Sequence[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Per-question mean judge correctness across repeats, per arm."""
    lookup = {
        (row["run_id"], row["system"], row["question_id"]): row
        for system in rows
        for run in rows[system].values()
        for row in run
    }
    collected: dict[str, dict[str, list[float]]] = {}
    for verdict in verdicts:
        row = lookup.get((verdict["run_id"], verdict["system"], verdict["question_id"]))
        if row is None:
            continue
        if verdict.get("prediction_sha256") not in (None, _prediction_hash(row)):
            continue
        correct = float(judge_is_correct(row, str(verdict.get("verdict"))))
        collected.setdefault(verdict["system"], {}).setdefault(row["question_id"], []).append(
            correct
        )
    return {
        system: {qid: statistics.fmean(values) for qid, values in sorted(values.items())}
        for system, values in sorted(collected.items())
    }


def judge_accuracy(
    rows: dict[str, dict[str, list[dict[str, Any]]]],
    verdicts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    lookup = {
        (row["run_id"], row["system"], row["question_id"]): row
        for system in rows
        for run in rows[system].values()
        for row in run
    }
    row_values: dict[str, list[float]] = {}
    for verdict in verdicts:
        row = lookup.get((verdict["run_id"], verdict["system"], verdict["question_id"]))
        if row is None:
            continue
        if verdict.get("prediction_sha256") not in (None, _prediction_hash(row)):
            continue
        row_values.setdefault(verdict["system"], []).append(
            float(judge_is_correct(row, str(verdict.get("verdict"))))
        )
    result: dict[str, Any] = {}
    for system, values in sorted(row_values.items()):
        result[system] = {
            "judged": len(values),
            "accuracy": statistics.fmean(values) if values else None,
        }
    return result


# ---------------------------------------------------------------------------
# comparison helpers
# ---------------------------------------------------------------------------


def _comparison_pair(
    systems: dict[str, Any],
    judge_per_question_by_label: dict[str, dict[str, dict[str, float]]],
    a: str,
    b: str,
    label: str,
    seed: int,
) -> dict[str, Any] | None:
    if a not in systems or b not in systems:
        return None
    comparison: dict[str, Any] = {
        "label": label,
        "a": a,
        "b": b,
        "answer_score": paired_comparison(
            systems[a]["per_question_answer_score"],
            systems[b]["per_question_answer_score"],
            seed=seed,
        ),
        "token_f1": paired_comparison(
            systems[a]["per_question_token_f1"], systems[b]["per_question_token_f1"], seed=seed
        ),
        "evidence_recall": paired_comparison(
            systems[a]["per_question_evidence_recall_postfit"],
            systems[b]["per_question_evidence_recall_postfit"],
            seed=seed,
        ),
    }
    for judge_label, per_question in judge_per_question_by_label.items():
        if a in per_question and b in per_question:
            comparison[f"judge_{judge_label}"] = paired_comparison(
                per_question[a], per_question[b], seed=seed
            )
    return comparison


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):.1f}%"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _comparison_table(comparison: dict[str, Any]) -> str:
    rows = []
    for metric_key, metric_label in (
        ("answer_score", "answer_score"),
        ("token_f1", "token_f1(cat1-4)"),
        ("evidence_recall", "evidence_recall(post-fit macro)"),
    ):
        pair = comparison.get(metric_key)
        if not pair:
            continue
        ci = pair.get("difference_ci95") or [None, None]
        rows.append(
            [
                metric_label,
                _fmt(pair.get("mean_memfw") if "mean_memfw" in pair else pair.get("mean_a")),
                _fmt(pair.get("mean_mem0") if "mean_mem0" in pair else pair.get("mean_b")),
                _fmt(pair.get("mean_difference")),
                f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                _fmt((pair.get("exact_wilcoxon") or {}).get("p_value")),
                _fmt(pair.get("cohen_dz")),
                _fmt(pair.get("minimum_detectable_difference")),
            ]
        )
    for key, pair in sorted(comparison.items()):
        if not key.startswith("judge_"):
            continue
        ci = pair.get("difference_ci95") or [None, None]
        rows.append(
            [
                f"{key}",
                _fmt(pair.get("mean_memfw") if "mean_memfw" in pair else pair.get("mean_a")),
                _fmt(pair.get("mean_mem0") if "mean_mem0" in pair else pair.get("mean_b")),
                _fmt(pair.get("mean_difference")),
                f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                _fmt((pair.get("exact_wilcoxon") or {}).get("p_value")),
                _fmt(pair.get("cohen_dz")),
                _fmt(pair.get("minimum_detectable_difference")),
            ]
        )
    return _table(
        [
            "指标",
            "A 均值",
            "B 均值",
            "均值差(A-B)",
            "95% CI",
            "精确置换 p",
            "Wilcoxon p",
            "Cohen dz",
            "MDE",
        ],
        rows,
    )


def render_markdown(metrics: dict[str, Any]) -> str:
    lines = [
        "# Step G metrics",
        "",
        "All numbers are computed from `results/stepG/runs/*/*/questions.jsonl`.",
        "",
    ]
    arms = metrics.get("arms", [])
    lines.append("## Arms")
    lines.append("")
    lines.append(", ".join(arms))
    lines.append("")
    lines.append("## Answer quality (per-question mean across repeats)")
    lines.append("")
    rows = []
    judge_models = metrics.get("judge", {}).get("models", [])
    for arm in arms:
        aggregate = metrics["systems"].get(arm)
        if not aggregate:
            rows.append([arm] + ["n/a"] * 7)
            continue
        judge_values = [
            _pct(model.get("accuracy", {}).get(arm, {}).get("accuracy")) for model in judge_models
        ]
        rows.append(
            [
                arm,
                _fmt(aggregate["mean_answer_score"]),
                _fmt(aggregate["mean_token_f1"]),
                _pct(aggregate["mean_evidence_recall_postfit"]),
                _pct(aggregate.get("micro_evidence_recall_postfit")),
                *judge_values,
                str(aggregate["question_count"]),
            ]
        )
    headers = [
        "arm",
        "answer_score",
        "token_f1(cat1-4)",
        "recall(post-fit macro)",
        "recall(post-fit micro)",
    ]
    headers += [f"judge:{model['label']}" for model in judge_models]
    headers += ["questions"]
    lines.append(_table(headers, rows))
    lines.append("")
    lines.append("## Efficiency")
    lines.append("")
    rows = []
    for arm in arms:
        aggregate = metrics["systems"].get(arm)
        if not aggregate:
            continue
        write = aggregate.get("write_latency") or {}
        rows.append(
            [
                arm,
                _fmt(aggregate.get("ingest_seconds_mean"), 2),
                _fmt(write.get("mean_seconds"), 4),
                _fmt((aggregate.get("write_latency_turns") or {}).get("p50_seconds"), 4),
                _fmt((aggregate.get("write_latency_turns") or {}).get("p95_seconds"), 4),
                _fmt(aggregate["retrieval_latency"].get("p50_seconds"), 4),
                _fmt(aggregate["retrieval_latency"].get("p95_seconds"), 4),
                _fmt(aggregate["answer_latency"].get("p50_seconds"), 4),
                str(aggregate["total_token_usage"]["prompt_tokens"]),
                str(aggregate["total_token_usage"]["completion_tokens"]),
                _fmt(aggregate.get("memory_count_mean"), 1),
                _fmt(aggregate.get("storage_bytes_mean"), 0),
            ]
        )
    lines.append(
        _table(
            [
                "arm",
                "ingest_s",
                "write_mean_s",
                "write_p50_s",
                "write_p95_s",
                "retrieval_p50_s",
                "retrieval_p95_s",
                "answer_p50_s",
                "prompt_tokens",
                "completion_tokens",
                "memories",
                "storage_bytes",
            ],
            rows,
        )
    )
    lines.append("")
    lines.append("## Paired comparisons")
    lines.append("")
    for comparison in metrics.get("comparisons", []):
        lines.append(f"### {comparison['label']}")
        lines.append("")
        lines.append(_comparison_table(comparison))
        lines.append("")
    lines.append("## Limits")
    lines.append("")
    for item in metrics.get("limitations", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _validate_runs(
    config: dict[str, Any],
    rows: dict[str, dict[str, list[dict[str, Any]]]],
    systems: dict[str, Any],
) -> None:
    """Fail loudly if the run inventory does not match the configured design."""
    results_dir = _resolve(config["paths"]["results_dir"])
    expected_questions = 18
    repeats = int(config["runs"].get("repeats", 3))
    expected_arms = [SYSTEMS.get(name, name) for name in config["runs"].get("systems", [])]
    for arm in expected_arms:
        if arm not in rows:
            raise ValueError(f"missing arm in results: {arm}")
    for system, by_run in rows.items():
        if len(by_run) != repeats:
            raise ValueError(
                f"{system}: expected {repeats} repeats, found {len(by_run)}: {sorted(by_run)}"
            )
        for run_id, run_rows in by_run.items():
            if len(run_rows) != expected_questions:
                raise ValueError(
                    f"{system}/{run_id}: expected {expected_questions} question rows, "
                    f"found {len(run_rows)}"
                )
            write_turns = results_dir / "runs" / run_id / system / "write_turns.jsonl"
            if not write_turns.exists():
                raise ValueError(f"{system}/{run_id}: missing write_turns.jsonl")
            dia_ids = [
                json.loads(line)["dia_id"]
                for line in write_turns.read_text().splitlines()
                if line.strip()
            ]
            if len(dia_ids) != 100 or len(set(dia_ids)) != 100:
                raise ValueError(
                    f"{system}/{run_id}: expected 100 unique ingested turns, "
                    f"found {len(dia_ids)} lines / {len(set(dia_ids))} unique"
                )
        if int(systems[system].get("run_errors") or 0):
            raise ValueError(f"{system}: run recorded {systems[system]['run_errors']} errors")


def build_metrics(
    config: dict[str, Any],
    *,
    judge: bool = True,
    judge_sensitivity: bool = True,
) -> dict[str, Any]:
    results_dir = _resolve(config["paths"]["results_dir"])
    rows = load_rows(results_dir)
    summaries = load_summaries(results_dir)
    write_values = load_write_latencies(results_dir)
    ingest_tokens = load_ingest_token_usage(results_dir)
    systems: dict[str, Any] = {}
    for arm in sorted(rows):
        systems[arm] = aggregate_system(
            rows[arm], summaries.get(arm), write_values.get(arm), ingest_tokens.get(arm)
        )
    _validate_runs(config, rows, systems)
    run_errors = sum(int(systems[arm].get("run_errors") or 0) for arm in systems)

    judge_blocks: dict[str, dict[str, Any]] = {}
    judge_per_question_by_label: dict[str, dict[str, dict[str, float]]] = {}
    for label, key, enabled in (
        ("primary", "judge", judge),
        ("same_model", "judge_sensitivity", judge_sensitivity),
    ):
        judge_cfg = judge_model_config(config, key)
        if judge_cfg is None or not enabled:
            continue
        out_path = results_dir / f"judge_{label}.jsonl"
        verdicts = run_judge(
            config,
            rows,
            out_path=out_path,
            judge_config=judge_cfg,
            label=label,
        )
        accuracy = judge_accuracy(rows, verdicts)
        per_question = judge_per_question(rows, verdicts)
        judge_per_question_by_label[label] = per_question
        judge_blocks[label] = {
            "label": label,
            "model": judge_cfg.get("model"),
            "accuracy": accuracy,
            "judged_rows": len(verdicts),
            "source": str(out_path),
            "paired": {
                a: paired_comparison(per_question[a], per_question[b], seed=int(config["seed"]))
                for a, b in (
                    (idx_a, idx_b)
                    for idx_a in per_question
                    for idx_b in per_question
                    if idx_a < idx_b
                )
            }
            if per_question
            else {},
        }

    comparisons: list[dict[str, Any]] = []
    reference = "memfw"
    primary_arm = (
        "mem0_stock"
        if "mem0_stock" in systems
        else next((arm for arm in systems if arm != reference), "")
    )
    if reference in systems and primary_arm in systems:
        comparison = _comparison_pair(
            systems,
            judge_per_question_by_label,
            reference,
            primary_arm,
            f"{reference} vs {primary_arm}（primary）",
            int(config["seed"]),
        )
        if comparison:
            comparisons.append(comparison)
    if "mem0_custom" in systems and reference in systems:
        comparison = _comparison_pair(
            systems,
            judge_per_question_by_label,
            reference,
            "mem0_custom",
            f"{reference} vs mem0_custom（sensitivity）",
            int(config["seed"]),
        )
        if comparison:
            comparisons.append(comparison)
    if "mem0_stock" in systems and "mem0_custom" in systems:
        comparison = _comparison_pair(
            systems,
            judge_per_question_by_label,
            "mem0_stock",
            "mem0_custom",
            "mem0_stock vs mem0_custom（configuration effect）",
            int(config["seed"]),
        )
        if comparison:
            comparisons.append(comparison)

    metrics: dict[str, Any] = {
        "schema_version": "stepg-metrics-v2",
        "config": {
            "seed": config["seed"],
            "llm": config["llm"],
            "embedding": config["embedding"],
            "retrieval": config["retrieval"],
            "repeats": config["runs"].get("repeats"),
            "arms": [SYSTEMS.get(name, name) for name in config["runs"].get("systems", [])],
        },
        "arms": [SYSTEMS.get(name, name) for name in config["runs"].get("systems", [])],
        "systems": systems,
        "comparisons": comparisons,
        "judge": {
            "models": [
                {
                    "label": block["label"],
                    "model": block["model"],
                    "judged_rows": block["judged_rows"],
                    "accuracy": block["accuracy"],
                    "source": block["source"],
                }
                for block in judge_blocks.values()
            ],
            "paired": {label: block["paired"] for label, block in judge_blocks.items()},
        },
        "run_errors": run_errors,
        "sources": {
            "runs_dir": str(results_dir / "runs"),
            "questions_glob": str(results_dir / "runs" / "*" / "*" / "questions.jsonl"),
            "summaries_glob": str(results_dir / "runs" / "*" / "*" / "summary.json"),
        },
        "limitations": [
            "Small fixed subset: one public LoCoMo conversation, first 100 turns, "
            "18 questions; results do not represent full LoCoMo.",
            "Single dataset (LoCoMo) and a single local answer model; absolute scores are "
            "model-dependent.",
            "All retention is 100% for the primary comparison; forgetting is not exercised "
            "in this formal run.",
            "memfw stores verbatim turns by design in this protocol; Mem0 arms run extraction "
            "pipelines, so write costs are not symmetric.",
            "Mean token-F1 and paired token-F1 exclude category-5 adversarial questions "
            "(abstention rule instead).",
            "Two evidence-recall definitions are reported: pre-fit top-k and post-fit "
            "(128-token context). The post-fit micro value matches the Day 11 metric "
            "definition; the macro value is the paired-analysis unit.",
            "Primary judge is a different model (qwen3.5:4b); the same-model judge is a "
            "sensitivity diagnostic, not ground truth.",
            "Provenance audit uses run rep1 only; mem0_custom's custom instructions request "
            "attributed_to='user', so its R3 rate is partly prompt-forced (mem0_stock is "
            "not prompt-forced).",
            "Paired tests use 18 fixed-subset pairs; the minimum detectable difference is "
            "reported. Secondary arms/tests are exploratory and not family-corrected.",
            "Memory-text form differs by protocol: memfw stores verbatim turns (speaker "
            "prefix, long sentences) while Mem0 stores extracted short statements; under "
            "the same 128-token budget the information density differs, so recall/answer "
            "gaps partly reflect this asymmetry rather than retrieval quality alone.",
            "Model identity is evidenced by env.txt digests, not by a cryptographic receipt "
            "from the model server.",
        ],
    }
    primary_answer = next(
        (
            comparison["answer_score"]
            for comparison in comparisons
            if "primary" in comparison["label"]
        ),
        None,
    )
    if primary_answer:
        metrics["paired"] = primary_answer  # legacy field
    _write_json(results_dir / "metrics.json", metrics)
    (results_dir / "metrics.md").write_text(render_markdown(metrics), encoding="utf-8")
    return metrics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step G metrics")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--no-judge-sensitivity", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    metrics = build_metrics(
        config,
        judge=not args.no_judge,
        judge_sensitivity=not args.no_judge_sensitivity,
    )
    print(
        json.dumps(
            {"arms": metrics["arms"], "comparisons": len(metrics["comparisons"])}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
