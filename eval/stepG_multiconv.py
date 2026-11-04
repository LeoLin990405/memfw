"""Multi-conversation validation for the memfw Step G protocol.

The frozen primary experiment uses one conversation (``conv-49``, first 100 turns). To test
whether the architecture iteration generalises and to gain statistical power, this module builds
the same protocol on all ten public LoCoMo conversations (first 100 turns each), runs the same
arms with the same answerer/embedder/judge, and reports pooled + per-conversation paired tests.

No raw LoCoMo file is committed; only the derived question/turn metadata and run outputs are.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval.stepG_metrics import (  # noqa: E402
    aggregate_system,
    judge_model_config,
    judge_per_question,
    run_judge,
)
from eval.stepG_runner import (  # noqa: E402
    _read_rows,
    _resolve,
    _write_json,
    load_config,
    make_chat,
    make_embedder,
    run_system,
    sha256_text,
)

CONVERSATIONS = [
    "conv-26",
    "conv-30",
    "conv-41",
    "conv-42",
    "conv-43",
    "conv-44",
    "conv-47",
    "conv-48",
    "conv-49",
    "conv-50",
]
TURNS = 100
ARMS = ["memfw", "memfw_v6", "memfw_v6_hybrid", "mem0_stock", "memoryos"]
ARCH_ARMS = {"memfw_v6": ("dense", "v6"), "memfw_v6_hybrid": ("hybrid_weighted", "v6")}


def subset_path(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["results_dir"]) / "multiconv" / "subset.json"


def _data_path(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["data"])


def build_subset(config: dict[str, Any]) -> dict[str, Any]:
    import eval_locomo  # noqa: PLC0415

    data_path = _data_path(config)
    if not data_path.exists():
        raise FileNotFoundError(f"public LoCoMo data missing at {data_path}")
    samples = json.loads(data_path.read_text(encoding="utf-8"))
    by_id = {str(sample.get("sample_id")): sample for sample in samples}
    conversations = []
    total_questions = 0
    for sample_id in CONVERSATIONS:
        sample = by_id.get(sample_id)
        if sample is None:
            raise ValueError(f"conversation {sample_id} missing from LoCoMo")
        turns, questions, _stats = eval_locomo.adapt_with_stats(sample)
        selected_turns = turns[:TURNS]
        window = {turn["dia_id"] for turn in selected_turns}
        raw_by_text = {str(row.get("question")): row for row in sample.get("qa", [])}
        selected_questions = []
        seen_question_ids: set[str] = set()
        for qa_index, question in enumerate(questions):
            evidence = list(question.get("evidence") or [])
            if not evidence or not set(evidence) <= window:
                continue
            raw = raw_by_text.get(str(question["question"]), {})
            answer = raw.get("answer", raw.get("adversarial_answer"))
            if answer is None:
                continue
            question_id = sha256_text(f"{sample_id}\x00{question['question']}")
            if question_id in seen_question_ids:
                continue
            seen_question_ids.add(question_id)
            selected_questions.append(
                {
                    "question_id": question_id,
                    "qa_index": qa_index,
                    "category": int(question.get("category") or 0),
                    "question": question["question"],
                    "answer": None if answer is None else str(answer),
                    "evidence_turn_ids": evidence,
                }
            )
        total_questions += len(selected_questions)
        conversations.append(
            {
                "sample_id": sample_id,
                "turns": selected_turns,
                "questions": selected_questions,
            }
        )
    payload = {
        "schema_version": "stepg-multiconv-v1",
        "seed": int(config["seed"]),
        "turns_per_conversation": TURNS,
        "conversation_count": len(conversations),
        "question_count": total_questions,
        "conversations": conversations,
    }
    path = subset_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def load_subset(config: dict[str, Any]) -> dict[str, Any]:
    path = subset_path(config)
    if not path.exists():
        return build_subset(config)
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _multiconv_root(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["results_dir"]) / "multiconv"


def _make_arm_system(arm: str, root: Path, config: dict[str, Any], embedder: Any) -> Any:
    if arm == "memfw":
        from eval.stepG_runner import MemfwSystem  # noqa: PLC0415

        return MemfwSystem(root, embedder)
    from eval.stepG_arch import VARIANTS, make_arch_system  # noqa: PLC0415

    if arm in VARIANTS:
        return make_arch_system(arm, root, embedder)
    if arm == "mem0_stock":
        from eval.stepG_runner import Mem0System  # noqa: PLC0415

        return Mem0System(root, config, custom_instructions=False)
    if arm == "memoryos":
        from eval.stepG_frameworks import MemOSSystem  # noqa: PLC0415

        return MemOSSystem(root, config)
    raise ValueError(f"unknown arm: {arm}")


def run_arm(
    config: dict[str, Any],
    arm: str,
    *,
    repeats: int = 1,
    limit_turns: int | None = None,
    limit_questions: int | None = None,
    conversations: list[str] | None = None,
    reuse_runtime: bool = False,
    run_suffix: str = "",
    reuse_source: str = "memfw_v6_hybrid",
) -> None:
    subset = load_subset(config)
    root = _multiconv_root(config)
    chat = make_chat(config)
    embedder = make_embedder(config)
    selected = [
        conversation
        for conversation in subset["conversations"]
        if not conversations or conversation["sample_id"] in conversations
    ]
    for conversation in selected:
        sample_id = conversation["sample_id"]
        turns = conversation["turns"][:limit_turns] if limit_turns else conversation["turns"]
        questions = (
            conversation["questions"][:limit_questions]
            if limit_questions
            else conversation["questions"]
        )
        for repeat in range(1, repeats + 1):
            run_id = f"rep{repeat}"
            if reuse_runtime:
                source_runtime = root / "runtime" / reuse_source / sample_id / "rep1"
                runtime_root = root / "runtime_reuse" / arm / (run_suffix or "reuse") / sample_id
                if not runtime_root.exists():
                    if not source_runtime.exists():
                        raise FileNotFoundError(
                            f"missing runtime to reuse for {arm}/{sample_id}: {source_runtime}"
                        )
                    shutil.copytree(source_runtime, runtime_root)
                    marker = runtime_root / ".ingested"
                    if marker.exists():
                        marker.unlink()
                run_dir = root / "runs" / f"{arm}{run_suffix}" / sample_id / "rep1"
                turns = []
            else:
                runtime_root = root / "runtime" / arm / sample_id / run_id
                run_dir = root / "runs" / arm / sample_id / run_id
            existing = _read_rows(run_dir / "questions.jsonl")
            if len(existing) == len(questions):
                print(f"[multiconv] {arm} {sample_id} {run_id}: reuse", flush=True)
                continue
            system = _make_arm_system(arm, runtime_root, config, embedder)
            print(
                f"[multiconv] {arm} {sample_id} {run_id}: {len(turns)} turns, "
                f"{len(questions)} questions",
                flush=True,
            )
            prompt = None
            prompt_version = "base"
            from eval.stepG_arch import VARIANTS, arch_prompt  # noqa: PLC0415

            if arm in VARIANTS:
                prompt, prompt_version = arch_prompt(arm)
            kwargs = {
                "system": system,
                "system_name": arm,
                "run_id": run_id,
                "turns": turns,
                "questions": questions,
                "config": config,
                "run_dir": run_dir,
                "chat": chat,
            }
            if prompt is not None:
                run_system(
                    **kwargs,
                    answer_prompt=prompt,
                    prompt_version=prompt_version,
                    log=lambda message: print(message, flush=True),
                )
            else:
                run_system(**kwargs, log=lambda message: print(message, flush=True))


def _arm_rows(config: dict[str, Any], arm: str) -> dict[str, list[dict[str, Any]]]:
    root = _multiconv_root(config) / "runs" / arm
    rows: dict[str, list[dict[str, Any]]] = {}
    if not root.exists():
        return rows
    for sample_dir in sorted(root.iterdir()):
        for run_dir in sorted(sample_dir.glob("rep*")):
            if (run_dir / "questions.jsonl").exists():
                rows[f"{sample_dir.name}_{run_dir.name}"] = _read_rows(run_dir / "questions.jsonl")
    return rows


def _arm_aggregate(config: dict[str, Any], arm: str) -> dict[str, Any] | None:
    root = _multiconv_root(config) / "runs" / arm
    if not root.exists():
        return None
    rows_by_run: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    for sample_dir in sorted(root.iterdir()):
        for run_dir in sorted(sample_dir.glob("rep*")):
            if not (run_dir / "questions.jsonl").exists():
                continue
            key = f"{sample_dir.name}_{run_dir.name}"
            rows_by_run[key] = _read_rows(run_dir / "questions.jsonl")
            summary_path = run_dir / "summary.json"
            if summary_path.exists():
                summaries[key] = json.loads(summary_path.read_text(encoding="utf-8"))
            write_path = run_dir / "write_turns.jsonl"
            if write_path.exists():
                for line in write_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        latencies.append(float(row["seconds"]))
                        usage = row.get("token_usage") or {}
                        tokens["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                        tokens["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    if not rows_by_run:
        return None
    return aggregate_system(rows_by_run, summaries, latencies, tokens)


def _judge(
    config: dict[str, Any], arms: list[str]
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    root = _multiconv_root(config)
    rows_by_arm: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for arm in arms:
        by_conv = _arm_rows(config, arm)
        combined = [row for rows in by_conv.values() for row in rows]
        rows_by_arm[arm] = {"rep1": combined}
    judge_cfg = judge_model_config(config, "judge")
    if not judge_cfg:
        return {}, {}
    out_path = root / "judge_primary.jsonl"
    verdicts = run_judge(
        config,
        rows_by_arm,
        out_path=out_path,
        judge_config=judge_cfg,
        label="multiconv-primary",
    )
    accuracy = judge_per_question(rows_by_arm, verdicts)
    return accuracy, {
        "model": judge_cfg.get("model"),
        "source": str(out_path),
        "rows": len(verdicts),
    }


def _cluster_bootstrap(
    per_conversation: dict[str, list[float]],
    *,
    seed: int,
    samples: int = 5000,
) -> tuple[float, float, float]:
    import random

    conversations = [values for values in per_conversation.values() if values]
    if not conversations:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        chosen = [conversations[rng.randrange(len(conversations))] for _ in conversations]
        pooled = [value for chunk in chosen for value in chunk]
        means.append(statistics.fmean(pooled))
    means.sort()
    point = statistics.fmean(value for chunk in conversations for value in chunk)
    return point, means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]


def _paired_large(
    first: dict[str, float],
    second: dict[str, float],
    *,
    seed: int,
    samples: int = 20000,
) -> dict[str, Any]:
    """Paired comparison for large n: MC sign-flip + exact sign test + bootstrap CI.

    The exact subset-sign-flip/Wilcoxon routines used for the 18/41-question subsets are
    exponential and do not apply at n≈300; this mirrors the same analysis unit (per-question
    paired differences) with Monte Carlo / sign-test inference and reports the method.
    """
    common = sorted(set(first) & set(second))
    diffs = [float(first[key]) - float(second[key]) for key in common]
    n = len(diffs)
    if n == 0:
        raise ValueError("no paired questions")
    mean_difference = statistics.fmean(diffs)
    standard_deviation = statistics.stdev(diffs) if n > 1 else 0.0
    rng = random.Random(seed)
    bootstrap = []
    for _ in range(5000):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        bootstrap.append(statistics.fmean(sample))
    bootstrap.sort()
    ci = [bootstrap[int(0.025 * len(bootstrap))], bootstrap[int(0.975 * len(bootstrap)) - 1]]
    observed = abs(mean_difference)
    extreme = 0
    for _ in range(samples):
        flipped = statistics.fmean(diff if rng.random() < 0.5 else -diff for diff in diffs)
        if abs(flipped) >= observed:
            extreme += 1
    permutation_p = (extreme + 1) / (samples + 1)
    positive = sum(1 for diff in diffs if diff > 1e-9)
    negative = sum(1 for diff in diffs if diff < -1e-9)
    sign_n = positive + negative
    sign_p = 1.0
    if sign_n:
        k = min(positive, negative)
        tail = sum(math.comb(sign_n, index) for index in range(k + 1)) / 2**sign_n
        sign_p = min(1.0, 2 * tail)
    cohen_dz = mean_difference / standard_deviation if standard_deviation else 0.0
    mde = 2.802 * standard_deviation / math.sqrt(n) if n else None
    return {
        "n_pairs": n,
        "mean_memfw": statistics.fmean(float(first[key]) for key in common),
        "mean_mem0": statistics.fmean(float(second[key]) for key in common),
        "mean_difference": mean_difference,
        "median_difference": statistics.median(diffs),
        "difference_ci95": ci,
        "exact_sign_flip": {"p_value": permutation_p, "method": "monte-carlo-20000"},
        "exact_wilcoxon": {"p_value": sign_p, "method": "exact-sign-test"},
        "cohen_dz": cohen_dz,
        "minimum_detectable_difference": mde,
        "rank_biserial": None,
        "per_question_differences": {key: float(first[key]) - float(second[key]) for key in common},
        "wins": positive,
        "losses": negative,
        "ties": n - positive - negative,
    }


def _fmt(value: Any, digits: int = 4) -> str:
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


def build_report(config: dict[str, Any], arms: list[str]) -> dict[str, Any]:
    subset = load_subset(config)
    root = _multiconv_root(config)
    systems: dict[str, Any] = {}
    for arm in arms:
        aggregate = _arm_aggregate(config, arm)
        if aggregate:
            systems[arm] = aggregate
    judge_means, judge_info = _judge(config, arms)
    comparisons: list[dict[str, Any]] = []
    for arm in arms:
        if arm in {"memfw", "mem0_stock", "memoryos"}:
            continue
        if arm not in systems:
            continue
        for other in ("memfw", "mem0_stock", "memoryos"):
            if other not in systems:
                continue
            comparison = {
                "label": f"{arm} vs {other}",
                "a": arm,
                "b": other,
                "answer_score": _paired_large(
                    systems[arm]["per_question_answer_score"],
                    systems[other]["per_question_answer_score"],
                    seed=int(config["seed"]),
                ),
                "token_f1": _paired_large(
                    systems[arm]["per_question_token_f1"],
                    systems[other]["per_question_token_f1"],
                    seed=int(config["seed"]),
                ),
                "evidence_recall": _paired_large(
                    systems[arm]["per_question_evidence_recall_postfit"],
                    systems[other]["per_question_evidence_recall_postfit"],
                    seed=int(config["seed"]),
                ),
            }
            if arm in judge_means and other in judge_means:
                comparison["judge_primary"] = _paired_large(
                    judge_means[arm], judge_means[other], seed=int(config["seed"])
                )
            comparisons.append(comparison)
    qid_to_conv: dict[str, str] = {}
    for conversation in subset["conversations"]:
        for question in conversation["questions"]:
            qid_to_conv[question["question_id"]] = conversation["sample_id"]
    cluster_stats: dict[str, Any] = {}
    for comparison in comparisons:
        answer_pair = cast(dict[str, Any], comparison["answer_score"])
        diffs = cast(dict[str, float], answer_pair["per_question_differences"])
        by_conv: dict[str, list[float]] = defaultdict(list)
        for qid, value in diffs.items():
            by_conv[qid_to_conv.get(qid, "?")].append(float(value))
        point, low, high = _cluster_bootstrap(by_conv, seed=int(config["seed"]))
        positive = sum(1 for value in diffs.values() if value > 1e-9)
        negative = sum(1 for value in diffs.values() if value < -1e-9)
        cluster_stats[cast(str, comparison["label"])] = {
            "cluster_bootstrap_mean": point,
            "cluster_bootstrap_ci95": [low, high],
            "wins": positive,
            "losses": negative,
            "ties": len(diffs) - positive - negative,
            "per_conversation_means": {
                conversation: statistics.fmean(values)
                for conversation, values in sorted(by_conv.items())
            },
        }
    metrics = {
        "schema_version": "stepg-multiconv-v1",
        "conversations": subset["conversation_count"],
        "questions": subset["question_count"],
        "arms": [arm for arm in arms if arm in systems],
        "systems": systems,
        "judge": {
            "accuracy": {
                arm: {"accuracy": statistics.fmean(values.values())}
                for arm, values in judge_means.items()
            },
            **judge_info,
        },
        "comparisons": comparisons,
        "cluster_stats": cluster_stats,
        "notes": [
            "All ten public LoCoMo conversations, first 100 turns each; "
            "same answerer/embedder/judge.",
            "One repeat per arm (temperature=0; the fixed subset showed byte-identical repeats).",
            "Questions within a conversation are not independent; "
            "cluster bootstrap by conversation is reported.",
        ],
    }
    _write_json(root / "metrics.json", metrics)
    (root / "MULTICONV_REPORT.md").write_text(render_report(metrics, subset), encoding="utf-8")
    return metrics


def render_report(metrics: dict[str, Any], subset: dict[str, Any]) -> str:
    systems = metrics["systems"]
    rows = []
    for arm in metrics["arms"]:
        system = systems[arm]
        judge = (metrics.get("judge") or {}).get("accuracy", {}).get(arm, {})
        write = system.get("write_latency_turns") or {}
        rows.append(
            [
                arm,
                _fmt(system.get("mean_answer_score")),
                _fmt(system.get("mean_token_f1")),
                _pct(system.get("micro_evidence_recall_postfit")),
                _pct(judge.get("accuracy") if isinstance(judge, dict) else judge),
                _fmt(write.get("p50_seconds"), 4),
                _fmt(system.get("memory_count_mean"), 1),
                str(system.get("question_count")),
            ]
        )
    comparison_rows = []
    cluster_rows = []
    for comparison in metrics.get("comparisons") or []:
        pair = comparison["answer_score"]
        ci = pair.get("difference_ci95") or [None, None]
        comparison_rows.append(
            [
                comparison["label"],
                _fmt(pair.get("mean_difference")),
                f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                _fmt((pair.get("exact_wilcoxon") or {}).get("p_value")),
                _fmt(pair.get("minimum_detectable_difference")),
            ]
        )
        stats = (metrics.get("cluster_stats") or {}).get(comparison["label"], {})
        cci = stats.get("cluster_bootstrap_ci95") or [None, None]
        cluster_rows.append(
            [
                comparison["label"],
                _fmt(stats.get("cluster_bootstrap_mean")),
                f"[{_fmt(cci[0])}, {_fmt(cci[1])}]",
                f"{stats.get('wins', 0)}/{stats.get('losses', 0)}/{stats.get('ties', 0)}",
            ]
        )
    lines = [
        "# memfw 多对话扩展验证（public LoCoMo，10 conversations）",
        "",
        "> 由 `eval/stepG_multiconv.py` 生成；只使用公开 LoCoMo 数据，"
        "同一 answerer/embedder/judge/top-k/上下文预算。每臂 1 次重复（temperature=0；"
        "固定子集已验证重复逐字节一致）。",
        "",
        f"数据：{subset['conversation_count']} 个 conversation × 前 "
        f"{subset['turns_per_conversation']} 轮，共 {subset['question_count']} 道有完整 "
        "gold evidence 的题。",
        "",
        _table(
            [
                "arm",
                "answer_score",
                "token_f1",
                "recall micro",
                "judge",
                "write P50",
                "memories",
                "questions",
            ],
            rows,
        ),
        "",
        "## 配对检验（question-level）",
        "",
        _table(
            ["comparison", "answer 均值差", "95% bootstrap CI", "精确置换 p", "Wilcoxon p", "MDE"],
            comparison_rows,
        ),
        "",
        "## 对话级稳健性（cluster bootstrap + 胜负计数）",
        "",
        _table(
            ["comparison", "cluster 均值差", "对话 cluster 95% CI", "wins/losses/ties"],
            cluster_rows,
        ),
        "",
        "## 说明与 caveat",
        "",
        "- 同一 conversation 内的题目不独立；同时报告 question-level 检验与"
        "按 conversation 的 cluster bootstrap。",
        "- 框架臂（Mem0 stock、MemOS general_text）各 1 次重复；"
        "LangMem 因单轮写入成本过高未纳入多对话扩展，",
        "固定 18 题对比已显示其显著落后。",
        "- 所有题库/答案均来自公开 LoCoMo；不提交原始数据文件。",
        "- 结论以 pooled 与 per-conversation 的一致性为准，而不是单个子集的点估计。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-conversation memfw validation")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--stage", choices=["build", "run", "metrics", "all"], default="all")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit-turns", type=int, default=None)
    parser.add_argument("--limit-questions", type=int, default=None)
    parser.add_argument("--conversations", default="", help="comma list; empty = all")
    parser.add_argument("--reuse-runtime", action="store_true")
    parser.add_argument("--run-suffix", default="")
    parser.add_argument("--reuse-source", default="memfw_v6_hybrid")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    if args.stage in {"build", "all"}:
        subset = build_subset(config)
        print(
            json.dumps(
                {
                    "conversations": subset["conversation_count"],
                    "questions": subset["question_count"],
                }
            )
        )
    selected_conversations = [
        item.strip() for item in args.conversations.split(",") if item.strip()
    ]
    if args.stage in {"run", "all"}:
        for arm in arms:
            run_arm(
                config,
                arm,
                repeats=args.repeats,
                limit_turns=args.limit_turns,
                limit_questions=args.limit_questions,
                conversations=selected_conversations or None,
                reuse_runtime=args.reuse_runtime,
                run_suffix=args.run_suffix,
                reuse_source=args.reuse_source,
            )
    if args.stage in {"metrics", "all"}:
        metrics = build_report(config, arms)
        print(json.dumps({"arms": metrics["arms"], "questions": metrics["questions"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
