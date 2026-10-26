"""Expanded-question evaluation (exploratory): all eligible questions in the same window.

The fixed Step G subset is 18 questions (up to 4 per category, Day 11 manifest).
The same 100-turn window contains more questions whose complete gold evidence is
inside the window. Re-answering them with the already-ingested stores increases
statistical power without touching the fixed primary subset. This is exploratory
and is reported separately from the primary result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_metrics import (  # noqa: E402
    aggregate_system,
    judge_accuracy,
    judge_model_config,
    judge_per_question,
    paired_comparison,
    run_judge,
)
from eval.stepG_runner import (  # noqa: E402
    ANSWER_PROMPT,
    SYSTEMS,
    Mem0System,
    MemfwSystem,
    _append_row,
    _read_rows,
    _resolve,
    _runtime_dir,
    _write_json,
    answer_score,
    check_endpoints,
    fit_context,
    latency_summary,
    load_config,
    make_chat,
    make_embedder,
    sha256_text,
    token_f1,
    verify_data,
)

EXPANDED_SCHEMA = "stepg-subset-expanded-v1"


def build_expanded_subset(config: dict[str, Any]) -> dict[str, Any]:
    import eval_locomo  # noqa: PLC0415

    results_dir = _resolve(config["paths"]["results_dir"])
    manifest_path = _resolve(config["paths"]["subset_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = json.loads(verify_data(config).read_text(encoding="utf-8"))
    payload = samples[manifest["source"]["sample_index"]]
    turns, questions, stats = eval_locomo.adapt_with_stats(payload)
    window = set(manifest["scope"]["input_turn_ids"])
    input_turns = [turn for turn in turns if turn["dia_id"] in window]
    raw_by_text = {str(row.get("question")): row for row in payload.get("qa", [])}
    eligible: list[dict[str, Any]] = []
    for qa_index, question in enumerate(questions):
        evidence = list(question.get("evidence") or [])
        if not evidence or not set(evidence) <= window:
            continue
        raw = raw_by_text.get(question["question"], {})
        answer = raw.get("answer", raw.get("adversarial_answer"))
        eligible.append(
            {
                "question_id": sha256_text(question["question"]),
                "qa_index": qa_index,
                "category": int(question.get("category") or 0),
                "question": question["question"],
                "answer": None if answer is None else str(answer),
                "evidence_turn_ids": evidence,
            }
        )
    counts = {
        str(category): sum(1 for row in eligible if row["category"] == category)
        for category in range(1, 6)
    }
    expected = {
        str(category): int(
            (manifest["scope"].get("eligible_question_counts_by_category") or {}).get(
                str(category), 0
            )
        )
        for category in range(1, 6)
    }
    if expected and counts != expected:
        raise ValueError(f"expanded eligibility mismatch: {counts} != {expected}")
    subset = {
        "schema_version": EXPANDED_SCHEMA,
        "seed": int(config["seed"]),
        "source": {
            "sample_id": manifest["source"]["sample_id"],
            "data_sha256": hashlib.sha256(verify_data(config).read_bytes()).hexdigest(),
            "manifest_sha256": sha256_text(manifest_path.read_text(encoding="utf-8")),
        },
        "scope": {
            "input_turn_count": len(input_turns),
            "question_count": len(eligible),
            "eligible_counts_by_category": counts,
            "adapter_stats": stats,
        },
        "turns": input_turns,
        "questions": eligible,
    }
    path = results_dir / "subset_expanded.json"
    path.write_text(
        json.dumps(subset, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return subset


def load_expanded_subset(config: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(config["paths"]["results_dir"]) / "subset_expanded.json"
    if not path.exists():
        return build_expanded_subset(config)
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _make_system(arm: str, config: dict[str, Any], embedder: Any) -> Any:
    root = _runtime_dir(config, "rep1", arm)
    if not root.exists():
        raise FileNotFoundError(
            f"missing ingested runtime for arm {arm!r} at {root}; run the primary pipeline first"
        )
    if SYSTEMS.get(arm) == "memfw":
        system = MemfwSystem(root, embedder)
        memories_path = (
            _resolve(config["paths"]["results_dir"]) / "runs" / "rep1" / arm / "memories.json"
        )
        if memories_path.exists():
            import json as _json

            system.hydrate_from_export(_json.loads(memories_path.read_text(encoding="utf-8")))
        return system
    if SYSTEMS.get(arm) == "mem0_custom":
        return Mem0System(root, config, custom_instructions=True)
    if SYSTEMS.get(arm) == "mem0_stock":
        return Mem0System(root, config, custom_instructions=False)
    if SYSTEMS.get(arm) == "mem0_stock_day11ctx":
        return Mem0System(
            root,
            config,
            custom_instructions=False,
            num_ctx=int((config.get("mem0") or {}).get("day11_num_ctx", 8192)),
        )
    raise ValueError(f"unknown arm: {arm!r}")


def evaluate_arm(
    config: dict[str, Any],
    arm: str,
    questions: Sequence[dict[str, Any]],
    chat: Any,
    embedder: Any,
    *,
    log: Callable[[str], None] = lambda _message: None,
) -> list[dict[str, Any]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    run_dir = results_dir / "expanded" / "runs" / arm
    run_dir.mkdir(parents=True, exist_ok=True)
    rows_path = run_dir / "questions.jsonl"
    done = {row["question_id"] for row in _read_rows(rows_path)}
    system = _make_system(arm, config, embedder)
    top_k = int(config["retrieval"]["top_k"])
    context_budget = int(config["retrieval"]["context_token_budget"])
    retrieval_latencies: list[float] = []
    answer_latencies: list[float] = []
    for question in questions:
        if question["question_id"] in done:
            continue
        begin = time.perf_counter()
        retrieved = system.search(question["question"], top_k=top_k)
        retrieval_seconds = time.perf_counter() - begin
        context = fit_context(retrieved, context_budget)
        memories_text = "\n".join(f"- {item['memory']}" for item in context) or "- (none)"
        prompt = ANSWER_PROMPT.format(memories=memories_text, question=question["question"])
        begin = time.perf_counter()
        answer, usage = chat.chat(prompt, max_tokens=int(config["llm"].get("max_tokens", 64)))
        answer_seconds = time.perf_counter() - begin
        retrieval_latencies.append(retrieval_seconds)
        answer_latencies.append(answer_seconds)
        _append_row(
            rows_path,
            {
                "question_set": "expanded",
                "run_id": "expanded",
                "system": arm,
                "question_id": question["question_id"],
                "qa_index": question["qa_index"],
                "category": question["category"],
                "question": question["question"],
                "gold_answer": question["answer"],
                "evidence_turn_ids": question["evidence_turn_ids"],
                "retrieved": [
                    {
                        "memory_id": item.get("id"),
                        "dia_ids": item.get("dia_ids", []),
                        "score": item.get("score"),
                        "memory": item.get("memory"),
                    }
                    for item in retrieved
                ],
                "context_memories": [item.get("memory") for item in context],
                "context_items": [
                    {
                        "memory_id": item.get("id"),
                        "dia_ids": item.get("dia_ids", []),
                        "score": item.get("score"),
                        "memory": item.get("memory"),
                    }
                    for item in context
                ],
                "answer": answer,
                "answer_score": answer_score(answer, question["answer"], question["category"]),
                "token_f1": token_f1(answer, question["answer"] or ""),
                "retrieval_seconds": retrieval_seconds,
                "answer_seconds": answer_seconds,
                "answer_token_usage": usage,
                "memory_count": system.memory_count(),
                "storage_bytes": system.storage_bytes(),
            },
        )
        done.add(question["question_id"])
        log(f"[expanded:{arm}] {len(done)}/{len(questions)} answered")
    rows = _read_rows(rows_path)
    _write_json(
        run_dir / "summary.json",
        {
            "schema_version": "stepg-expanded-summary-v1",
            "arm": arm,
            "question_set": "expanded",
            "question_count": len(questions),
            "rows": len(rows),
            "retrieval_latency": latency_summary(retrieval_latencies),
            "answer_latency": latency_summary(answer_latencies),
            "mean_answer_score": statistics.fmean(row["answer_score"] for row in rows),
            "memory_count": system.memory_count(),
            "storage_bytes": system.storage_bytes(),
        },
    )
    return rows


def build_expanded_metrics(
    config: dict[str, Any],
    rows_by_arm: dict[str, list[dict[str, Any]]],
    *,
    judge: bool = True,
) -> dict[str, Any]:
    results_dir = _resolve(config["paths"]["results_dir"])
    systems: dict[str, Any] = {}
    for arm in sorted(rows_by_arm):
        systems[arm] = aggregate_system({arm: rows_by_arm[arm]})
    judge_block: dict[str, Any] = {}
    per_question_judge: dict[str, dict[str, float]] | None = None
    if judge:
        judge_cfg = judge_model_config(config, "judge")
        if judge_cfg:
            rows_for_judge = {arm: {"expanded": rows_by_arm[arm]} for arm in rows_by_arm}
            out_path = results_dir / "expanded_judge_primary.jsonl"
            verdicts = run_judge(
                config,
                rows_for_judge,
                out_path=out_path,
                judge_config=judge_cfg,
                label="expanded-primary",
            )
            per_question_judge = judge_per_question(rows_for_judge, verdicts)
            judge_block = {
                "model": judge_cfg.get("model"),
                "accuracy": judge_accuracy(rows_for_judge, verdicts),
                "judged_rows": len(verdicts),
                "source": str(out_path),
            }
    arms = list(systems)
    comparisons: list[dict[str, Any]] = []
    if "memfw" in systems and "mem0_stock" in systems:
        comparisons.append(
            _expanded_comparison(
                systems, per_question_judge, "memfw", "mem0_stock", "memfw vs mem0_stock（primary）"
            )
        )
    if "memfw" in systems and "mem0_custom" in systems:
        comparisons.append(
            _expanded_comparison(
                systems,
                per_question_judge,
                "memfw",
                "mem0_custom",
                "memfw vs mem0_custom（sensitivity）",
            )
        )
    if "mem0_stock" in systems and "mem0_custom" in systems:
        comparisons.append(
            _expanded_comparison(
                systems,
                per_question_judge,
                "mem0_stock",
                "mem0_custom",
                "mem0_stock vs mem0_custom",
            )
        )
    if "memfw" in systems and "mem0_stock_day11ctx" in systems:
        comparisons.append(
            _expanded_comparison(
                systems,
                per_question_judge,
                "memfw",
                "mem0_stock_day11ctx",
                "memfw vs mem0_stock_day11ctx（Day-11 context replication）",
            )
        )
    metrics = {
        "schema_version": "stepg-expanded-metrics-v1",
        "n_questions": len(rows_by_arm.get("memfw") or next(iter(rows_by_arm.values()), [])),
        "arms": arms,
        "systems": systems,
        "comparisons": comparisons,
        "judge": judge_block,
        "note": (
            "Exploratory expansion: all eligible questions in the same 100-turn window; "
            "reuses ingested stores; does not replace the fixed 18-question primary subset."
        ),
    }
    _write_json(results_dir / "expanded_metrics.json", metrics)
    (results_dir / "expanded_metrics.md").write_text(_render(metrics), encoding="utf-8")
    return metrics


def _expanded_comparison(
    systems: dict[str, Any],
    per_question_judge: dict[str, dict[str, float]] | None,
    a: str,
    b: str,
    label: str,
) -> dict[str, Any]:
    seed = 20261021
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
    if per_question_judge and a in per_question_judge and b in per_question_judge:
        comparison["judge_primary"] = paired_comparison(
            per_question_judge[a], per_question_judge[b], seed=seed
        )
    return comparison


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


def _render(metrics: dict[str, Any]) -> str:
    systems = metrics.get("systems", {})
    arms = metrics.get("arms", [])
    rows = []
    for arm in arms:
        aggregate = systems.get(arm) or {}
        rows.append(
            [
                arm,
                _fmt(aggregate.get("mean_answer_score")),
                _fmt(aggregate.get("mean_token_f1")),
                _pct(aggregate.get("mean_evidence_recall_postfit")),
                _pct(aggregate.get("micro_evidence_recall_postfit")),
                str(aggregate.get("question_count")),
            ]
        )
    lines = [
        "# Step G expanded question set (exploratory)",
        "",
        f"All {metrics.get('n_questions')} eligible questions inside the same 100-turn window; "
        "reuses the ingested stores. Not a replacement for the fixed 18-question subset.",
        "",
        _table(
            [
                "arm",
                "answer_score",
                "token_f1(cat1-4)",
                "recall(macro)",
                "recall(micro)",
                "questions",
            ],
            rows,
        ),
        "",
    ]
    judge = metrics.get("judge") or {}
    if judge:
        lines.append(
            "Judge（"
            + str(judge.get("model"))
            + "）："
            + ", ".join(
                f"{arm}:{_pct(acc.get('accuracy'))}"
                for arm, acc in sorted((judge.get("accuracy") or {}).items())
            )
        )
        lines.append("")
    for comparison in metrics.get("comparisons", []):
        lines.append(f"## {comparison['label']}")
        lines.append("")
        pair = comparison.get("answer_score") or {}
        lines.append(
            _table(
                [
                    "指标",
                    "A 均值",
                    "B 均值",
                    "均值差",
                    "95% CI",
                    "精确置换 p",
                    "Wilcoxon p",
                    "Cohen dz",
                    "MDE",
                ],
                [
                    [
                        "answer_score",
                        _fmt(pair.get("mean_memfw")),
                        _fmt(pair.get("mean_mem0")),
                        _fmt(pair.get("mean_difference")),
                        f"[{_fmt((pair.get('difference_ci95') or [None])[0])}, "
                        f"{_fmt((pair.get('difference_ci95') or [None, None])[1])}]",
                        _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                        _fmt((pair.get("exact_wilcoxon") or {}).get("p_value")),
                        _fmt(pair.get("cohen_dz")),
                        _fmt(pair.get("minimum_detectable_difference")),
                    ]
                ],
            )
        )
        lines.append("")
    return "\n".join(lines)


def run_expanded(
    config: dict[str, Any],
    *,
    judge: bool = True,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    subset = load_expanded_subset(config)
    arms = [SYSTEMS.get(name, name) for name in config["runs"].get("systems", [])]
    chat = make_chat(config)
    embedder = make_embedder(config)
    rows_by_arm: dict[str, list[dict[str, Any]]] = {}
    for arm in arms:
        log(f"=== expanded / {arm} ({subset['scope']['question_count']} questions) ===")
        rows_by_arm[arm] = evaluate_arm(config, arm, subset["questions"], chat, embedder, log=log)
    return build_expanded_metrics(config, rows_by_arm, judge=judge)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step G expanded question-set evaluation")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.build_only:
        subset = build_expanded_subset(config)
        print(json.dumps({"n_questions": subset["scope"]["question_count"]}))
        return 0
    check_endpoints(config)
    metrics = run_expanded(config, judge=not args.no_judge)
    print(json.dumps({"n_questions": metrics["n_questions"], "arms": metrics["arms"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
