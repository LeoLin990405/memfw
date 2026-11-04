"""Compare memfw with third-party open-source memory frameworks under the Step G protocol.

The fixed Step G subset (conv-49, first 100 turns, 18 questions) is replayed for each
framework adapter. The answerer, embedder, judge, context budget and retrieval top-k are
identical to the main experiment; only the memory layer changes.

Reference rows (memfw, mem0_stock) are read from the main ``results/stepG/runs`` tree so
the comparison reuses the already-frozen reference runs instead of re-running them.
Framework arms run once by default (exploratory breadth) while the reference arms keep
their three repeats; the report states this explicitly.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_frameworks import framework_arm_names, make_framework_system  # noqa: E402
from eval.stepG_metrics import (  # noqa: E402
    aggregate_system,
    judge_is_correct,
    judge_model_config,
    judge_per_question,
    paired_comparison,
    run_judge,
)
from eval.stepG_runner import (  # noqa: E402
    _read_rows,
    _resolve,
    _write_json,
    load_config,
    load_subset,
    make_chat,
    run_system,
)

REFERENCE_ARMS = ("memfw", "mem0_stock")
DEFAULT_REPEATS = 1


def _write_latencies(run_dir: Path) -> list[float]:
    path = run_dir / "write_turns.jsonl"
    if not path.exists():
        return []
    values: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            values.append(float(json.loads(line)["seconds"]))
    return values


def _ingest_tokens(run_dir: Path) -> dict[str, int]:
    path = run_dir / "write_turns.jsonl"
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if not path.exists():
        return usage
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        token_usage = row.get("token_usage") or {}
        usage["prompt_tokens"] += int(token_usage.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(token_usage.get("completion_tokens") or 0)
    return usage


def _load_reference(results_dir: Path, arm: str) -> dict[str, Any]:
    rows: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    for run_dir in sorted((results_dir / "runs").glob(f"*/{arm}")):
        run_id = run_dir.parent.name
        rows[run_id] = _read_rows(run_dir / "questions.jsonl")
        summary = run_dir / "summary.json"
        if summary.exists():
            summaries[run_id] = json.loads(summary.read_text(encoding="utf-8"))
        latencies.extend(_write_latencies(run_dir))
        ingested = _ingest_tokens(run_dir)
        tokens["prompt_tokens"] += ingested["prompt_tokens"]
        tokens["completion_tokens"] += ingested["completion_tokens"]
    if not rows:
        raise FileNotFoundError(f"no reference runs for arm {arm!r} under {results_dir / 'runs'}")
    return {
        "rows": rows,
        "aggregate": aggregate_system(rows, summaries, latencies, tokens),
    }


def _main_judge_per_question(
    results_dir: Path, rows_by_arm: dict[str, dict[str, list[dict[str, Any]]]]
) -> dict[str, dict[str, float]]:
    """Reuse per-question judge means from the main judge file for reference arms."""
    path = results_dir / "judge_primary.jsonl"
    if not path.exists():
        return {}
    verdicts = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    lookup = {
        (row["run_id"], row["system"], row["question_id"]): row
        for system, by_run in rows_by_arm.items()
        for run_id, rows in by_run.items()
        for row in rows
    }
    collected: dict[str, dict[str, list[float]]] = {}
    for verdict in verdicts:
        key = (verdict["run_id"], verdict["system"], verdict["question_id"])
        row = lookup.get(key)
        if row is None:
            continue
        collected.setdefault(verdict["system"], {}).setdefault(row["question_id"], []).append(
            float(judge_is_correct(row, str(verdict.get("verdict"))))
        )
    return {
        system: {qid: statistics.fmean(values) for qid, values in sorted(by_q.items())}
        for system, by_q in collected.items()
    }


def _format(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):.1f}%"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _comparison_table(comparison: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for metric_key, metric_label in (
        ("answer_score", "answer_score"),
        ("token_f1", "token_f1(cat1-4)"),
        ("evidence_recall", "evidence recall(post-fit macro)"),
        ("judge_primary", "LLM judge(primary)"),
    ):
        pair = comparison.get(metric_key)
        if not pair:
            continue
        ci = pair.get("difference_ci95") or [None, None]
        rows.append(
            [
                metric_label,
                _format(pair.get("mean_memfw")),
                _format(pair.get("mean_mem0")),
                _format(pair.get("mean_difference")),
                f"[{_format(ci[0])}, {_format(ci[1])}]",
                _format((pair.get("exact_sign_flip") or {}).get("p_value")),
                _format((pair.get("exact_wilcoxon") or {}).get("p_value")),
                _format(pair.get("cohen_dz")),
                _format(pair.get("minimum_detectable_difference")),
            ]
        )
    return _table(
        [
            "指标",
            "memfw 均值",
            "框架均值",
            "均值差",
            "95% CI",
            "精确置换 p",
            "Wilcoxon p",
            "Cohen dz",
            "MDE(80%)",
        ],
        rows,
    )


def build_markdown(metrics: dict[str, Any]) -> str:
    arms = metrics["arms"]
    systems = metrics["systems"]
    rows: list[list[str]] = []
    for arm in arms:
        aggregate = systems.get(arm)
        if not aggregate:
            continue
        rows.append(
            [
                arm,
                _format(aggregate["mean_answer_score"]),
                _format(aggregate["mean_token_f1"]),
                _pct(aggregate.get("mean_evidence_recall_postfit")),
                _pct(aggregate.get("micro_evidence_recall_postfit")),
                _pct(
                    ((metrics.get("judge") or {}).get("accuracy") or {})
                    .get(arm, {})
                    .get("accuracy")
                ),
                _format((aggregate.get("write_latency_turns") or {}).get("p50_seconds")),
                str(aggregate.get("memory_count_mean")),
                str(aggregate.get("total_token_usage", {}).get("prompt_tokens")),
                str(aggregate.get("repeats")),
            ]
        )
    lines = [
        "# 开源记忆框架对比（Step G 固定子集，exploratory）",
        "",
        "同一 LoCoMo 子集（conv-49，前 100 轮，18 题，seed 20261021）；answerer/embedder/judge/"
        "top-k/上下文预算与主实验一致，仅替换记忆层。参考臂（memfw、mem0_stock）复用主实验的 "
        "3 次重复结果；框架臂默认 1 次重复（探索性、控制总时长），表格中的“重复数”列显式标注。",
        "",
        _table(
            [
                "arm",
                "answer_score",
                "token_f1(cat1-4)",
                "recall macro",
                "recall micro",
                "judge primary",
                "写入 P50(s)",
                "记忆数",
                "ingest prompt tokens",
                "重复数",
            ],
            rows,
        ),
        "",
    ]
    for comparison in metrics.get("comparisons", []):
        lines.append(f"## {comparison['label']}")
        lines.append("")
        lines.append(_comparison_table(comparison))
        lines.append("")
    lines.extend(
        [
            "## 适配与公平性说明",
            "",
            "- 所有框架的抽取 LLM 与 embedding 均替换为本机同一 `qwen3.5:9b` + `nomic-embed-text`；"
            "答案阶段使用同一 answer prompt 与 judge。",
            "- 各框架的抽取提示、记忆数据模型、更新/去重策略保持其默认实现（适配器只做协议转换）。",
            "- 框架臂只跑 1 次重复，配对检验的 n=18（题），不是 18×重复；"
            "MDE 较大，结论只作方向性参考。",
            "- 写入延迟包含框架自身的抽取/索引成本；不同框架调用 LLM 的次数不同，"
            "该列不构成实现优劣结论。",
            "- LangMem 适配器把最近 5 条记忆作为 existing 上下文传入"
            "（框架默认 store manager 的 query_limit=5），并且只应用插入、"
            "不应用更新/删除，以保持与 Mem0 插入型协议的差异可解释。",
            "- LangMem 抽取出的记忆常是跨轮摘要，框架未保留逐轮 provenance；"
            "因此本表的 evidence recall 对 LangMem 接近 0，这是记忆表示差异，"
            "不是检索实现 bug。",
            "- Cognee 曾尝试纳入：本地 embedding 连接测试 30s 超时，最小 add/cognify "
            "smoke 也超过合理时长，未完成适配，因此不纳入本表（尝试日志未提交）。",
        ]
    )
    return "\n".join(lines)


def run_comparison(
    config: dict[str, Any],
    *,
    arms: list[str],
    repeats: int = DEFAULT_REPEATS,
    limit_turns: int | None = None,
    limit_questions: int | None = None,
    skip_judge: bool = False,
) -> dict[str, Any]:
    results_dir = _resolve(config["paths"]["results_dir"])
    frameworks_dir = results_dir / "frameworks"
    frameworks_dir.mkdir(parents=True, exist_ok=True)
    subset = load_subset(config)
    turns = subset["turns"][:limit_turns] if limit_turns else subset["turns"]
    questions = subset["questions"][:limit_questions] if limit_questions else subset["questions"]
    chat = make_chat(config)

    rows_by_arm: dict[str, Any] = {}
    summaries: dict[str, dict[str, Any]] = {}
    systems: dict[str, Any] = {}
    for arm in REFERENCE_ARMS:
        reference = _load_reference(results_dir, arm)
        rows_by_arm[arm] = reference["rows"]
        systems[arm] = reference["aggregate"]

    for arm in arms:
        runtime_root = frameworks_dir / "runtime" / arm
        run_root = frameworks_dir / "runs" / arm
        complete = all(
            (run_root / f"rep{repeat}" / "questions.jsonl").exists()
            and len(_read_rows(run_root / f"rep{repeat}" / "questions.jsonl")) == len(questions)
            for repeat in range(1, repeats + 1)
        )
        if complete:
            print(f"[framework] {arm}: reusing {len(turns)}-turn runs", flush=True)
        else:
            for repeat in range(1, repeats + 1):
                run_id = f"rep{repeat}"
                run_dir = run_root / run_id
                if run_dir.exists():
                    import shutil

                    shutil.rmtree(run_dir)
                system = make_framework_system(arm, runtime_root, config)
                print(f"[framework] {arm} {run_id}: ingest {len(turns)} turns", flush=True)
                run_system(
                    system=system,
                    system_name=arm,
                    run_id=run_id,
                    turns=turns,
                    questions=questions,
                    config=config,
                    run_dir=run_dir,
                    chat=chat,
                    limit_questions=limit_questions,
                    log=lambda message: print(message, flush=True),
                )
        rows_by_arm[arm] = {
            run_dir.name: _read_rows(run_dir / "questions.jsonl")
            for run_dir in sorted(run_root.glob("rep*"))
        }
        summaries[arm] = {
            run_dir.name: json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            for run_dir in sorted(run_root.glob("rep*"))
            if (run_dir / "summary.json").exists()
        }
        latencies: list[float] = []
        tokens = {"prompt_tokens": 0, "completion_tokens": 0}
        for run_dir in sorted(run_root.glob("rep*")):
            latencies.extend(_write_latencies(run_dir))
            ingested = _ingest_tokens(run_dir)
            tokens["prompt_tokens"] += ingested["prompt_tokens"]
            tokens["completion_tokens"] += ingested["completion_tokens"]
        systems[arm] = aggregate_system(rows_by_arm[arm], summaries[arm], latencies, tokens)

    judge_cfg = judge_model_config(config, "judge")
    judge_accuracy: dict[str, Any] = {}
    judge_means: dict[str, dict[str, float]] = _main_judge_per_question(results_dir, rows_by_arm)
    if judge_cfg and not skip_judge:
        framework_rows = {arm: rows_by_arm[arm] for arm in arms}
        out_path = frameworks_dir / "judge_primary.jsonl"
        verdicts = run_judge(
            config,
            framework_rows,
            out_path=out_path,
            judge_config=judge_cfg,
            label="framework-primary",
        )
        judge_means.update(judge_per_question(framework_rows, verdicts))
        from eval.stepG_metrics import judge_accuracy as _judge_accuracy

        judge_accuracy = _judge_accuracy(framework_rows, verdicts)
        for reference_arm in REFERENCE_ARMS:
            if reference_arm in judge_means:
                entries = judge_means[reference_arm].values()
                judge_accuracy[reference_arm] = {
                    "judged": len(judge_means[reference_arm]),
                    "accuracy": statistics.fmean(entries) if entries else None,
                }
        judge_accuracy = {
            "model": judge_cfg.get("model"),
            "source": str(out_path),
            "accuracy": judge_accuracy,
        }

    comparisons = []
    for arm in arms:
        if arm not in judge_means or "memfw" not in judge_means:
            judge_pair = None
        else:
            judge_pair = paired_comparison(
                judge_means["memfw"], judge_means[arm], seed=int(config["seed"])
            )
        comparison = {
            "label": f"memfw vs {arm}",
            "a": "memfw",
            "b": arm,
            "answer_score": paired_comparison(
                systems["memfw"]["per_question_answer_score"],
                systems[arm]["per_question_answer_score"],
                seed=int(config["seed"]),
            ),
            "token_f1": paired_comparison(
                systems["memfw"]["per_question_token_f1"],
                systems[arm]["per_question_token_f1"],
                seed=int(config["seed"]),
            ),
            "evidence_recall": paired_comparison(
                systems["memfw"]["per_question_evidence_recall_postfit"],
                systems[arm]["per_question_evidence_recall_postfit"],
                seed=int(config["seed"]),
            ),
        }
        if judge_pair:
            comparison["judge_primary"] = judge_pair
        comparisons.append(comparison)

    metrics = {
        "schema_version": "stepg-frameworks-v1",
        "protocol": {
            "sample_id": subset["source"]["sample_id"],
            "turns": len(turns),
            "questions": len(questions),
            "seed": config["seed"],
            "framework_repeats": repeats,
            "reference_repeats": 3,
            "top_k": config["retrieval"]["top_k"],
            "context_token_budget": config["retrieval"]["context_token_budget"],
        },
        "arms": [
            "memfw",
            *arms,
            *(arm for arm in REFERENCE_ARMS if arm != "memfw" and arm not in arms),
        ],
        "systems": systems,
        "judge": judge_accuracy,
        "comparisons": comparisons,
        "notes": [
            "Framework arms are exploratory single-repeat runs; reference arms keep three repeats.",
            "Framework extraction prompts/update policies are their defaults; "
            "only the LLM/embedder were swapped.",
            "Adapter details and fairness caveats are listed in the report.",
        ],
    }
    _write_json(frameworks_dir / "metrics.json", metrics)
    (frameworks_dir / "FRAMEWORK_COMPARISON.md").write_text(
        build_markdown(metrics), encoding="utf-8"
    )
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare memfw with open-source memory frameworks")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument(
        "--arms", default="langmem", help=f"comma list; known: {framework_arm_names()}"
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--limit-turns", type=int, default=None)
    parser.add_argument("--limit-questions", type=int, default=None)
    parser.add_argument("--skip-judge", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    metrics = run_comparison(
        config,
        arms=arms,
        repeats=args.repeats,
        limit_turns=args.limit_turns,
        limit_questions=args.limit_questions,
        skip_judge=args.skip_judge,
    )
    print(
        json.dumps(
            {"arms": metrics["arms"], "comparisons": [c["label"] for c in metrics["comparisons"]]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
