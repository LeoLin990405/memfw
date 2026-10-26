"""Render ``results/stepG/REPORT.md`` from the artifacts produced by the pipeline.

Every number in the report is read from a JSON/CSV/TXT file under
``results/stepG/``; the source file is printed next to each table. Nothing is
copied from an earlier session or typed in by hand (preliminary numbers are
loaded from the committed Day 11 JSON and are always labelled as preliminary).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_runner import (  # noqa: E402
    SYSTEMS,
    _resolve,
    load_config,
    load_subset,
    redact_text,
)

PRELIMINARY_SOURCE = "reports/day11_baseline_comparison.md"
DAY11_ARM_MAP = {
    "memfw": "memfw",
    "mem0_stock": "mem0",
    "mem0_stock_day11ctx": "mem0",
}


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


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    total = int(round(float(value)))
    return f"{total // 3600}h{(total % 3600) // 60:02d}m{total % 60:02d}s"


def _env_field(env_text: str, key: str) -> str | None:
    for line in env_text.splitlines():
        name, _, value = line.partition(": ")
        if name.strip() == key and value.strip():
            return value.strip()
    return None


def _load_preliminary() -> dict[str, Any] | None:
    path = REPO_ROOT / PRELIMINARY_SOURCE.replace(".md", ".json")
    if not path.exists():
        return None
    data = _read_json(path)
    try:
        conditions = data["results"]["conditions"]
        memfw100 = conditions["memfw"]["retention_100pct"]
        mem0100 = conditions["mem0"]["retention_100pct"]
        audit = data["results"]["provenance_audit"]["systems"]
        return {
            "source_json": _relative(path),
            "answer_memfw": float(memfw100["answer_score_mean"]),
            "answer_mem0": float(mem0100["answer_score_mean"]),
            "recall_micro_memfw": float(memfw100["evidence_recall"]),
            "recall_micro_mem0": float(mem0100["evidence_recall"]),
            "write_p50_memfw": float(memfw100["write_latency"]["p50_seconds"]),
            "write_p50_mem0": float(mem0100["write_latency"]["p50_seconds"]),
            "memories_memfw": int(memfw100["retained_memory_count"]),
            "memories_mem0": int(mem0100["retained_memory_count"]),
            "prov_flagged_memfw": int(audit["memfw"]["flagged_count"]),
            "prov_flagged_mem0": int(audit["mem0"]["flagged_count"]),
            "prov_sample": int(audit["mem0"]["sample_size"]),
            "model": data.get("settings", {}).get("answer_model"),
            "embedding": data.get("settings", {}).get("embedding_model"),
            "num_ctx": data.get("settings", {}).get("ollama_num_ctx"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _load_reproducibility(results_dir: Path, arms: Sequence[str]) -> dict[str, Any]:
    """Compare the formal per-question results with the Day 11 JSON, per arm."""
    path = REPO_ROOT / PRELIMINARY_SOURCE.replace(".md", ".json")
    if not path.exists():
        return {}
    day11_payload = _read_json(path)["results"]["per_question"]["retention_100pct"]
    day11 = {row["qa_index"]: row for row in day11_payload}
    preliminary = _load_preliminary() or {}
    output: dict[str, Any] = {}
    for arm, day11_key in DAY11_ARM_MAP.items():
        if arm not in arms:
            continue
        rows_path = results_dir / "runs" / "rep1" / arm / "questions.jsonl"
        if not rows_path.exists():
            continue
        rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
        identical = 0
        jaccard: list[float] = []
        comparable = 0
        for row in rows:
            reference = day11.get(row["qa_index"])
            if reference is None:
                continue
            comparable += 1
            old_score = (reference.get("answer_score") or {}).get(day11_key)
            if old_score is not None and abs(float(old_score) - float(row["answer_score"])) < 1e-9:
                identical += 1
            formal_ids: set[str] = set()
            for item in row.get("retrieved") or []:
                formal_ids.update(item.get("dia_ids") or [])
            old_ids = set((reference.get("retrieved_turn_ids") or {}).get(day11_key) or [])
            union = formal_ids | old_ids
            jaccard.append(len(formal_ids & old_ids) / len(union) if union else 1.0)
        memories_path = results_dir / "runs" / "rep1" / arm / "memories.json"
        memories = len(_read_json(memories_path)) if memories_path.exists() else None
        output[arm] = {
            "day11_system": day11_key,
            "comparable": comparable,
            "identical_scores": identical,
            "mean_retrieval_jaccard": sum(jaccard) / len(jaccard) if jaccard else None,
            "formal_memories": memories,
            "day11_memories": preliminary.get(f"memories_{day11_key}"),
        }
    return output


def _load_expanded(results_dir: Path) -> dict[str, Any] | None:
    path = results_dir / "expanded_metrics.json"
    if not path.exists():
        return None
    return _read_json(path)


def _status_rows(
    config: dict[str, Any],
    results_dir: Path,
    metrics: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
    expanded: dict[str, Any] | None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    env_exists = (results_dir / "env.txt").exists()
    rows.append(
        [
            "G1 环境",
            "完成" if env_exists else "部分",
            f"env.txt={env_exists}, pip-freeze.txt={(results_dir / 'pip-freeze.txt').exists()}",
        ]
    )
    judge_config = config.get("judge") or {}
    sensitivity = config.get("judge_sensitivity") or {}
    same_answer_model = config["llm"]["model"] == sensitivity.get("model")
    cross_judge = judge_config.get("model") and judge_config.get("model") != config["llm"]["model"]
    rows.append(
        [
            "G2 统一模型",
            "完成" if cross_judge and same_answer_model else "部分",
            f"llm={config['llm']['model']}, embedding={config['embedding']['model']}, "
            f"judge(primary)={judge_config.get('model')}, "
            f"judge(sensitivity)={sensitivity.get('model')}",
        ]
    )
    rows.append(
        [
            "G3 runner + 单测",
            "完成"
            if (REPO_ROOT / "eval" / "stepG_runner.py").exists()
            and (results_dir / "test-results.txt").exists()
            else "部分",
            f"pytest_log={(results_dir / 'test-results.txt').exists()}",
        ]
    )
    arms = metrics.get("arms", []) if metrics else []
    complete_arms = 0
    if metrics:
        for arm in arms:
            aggregate = metrics["systems"].get(arm) or {}
            if (
                int(aggregate.get("repeats") or 0) >= 3
                and int(aggregate.get("question_count") or 0) == 18
            ):
                complete_arms += 1
    rows.append(
        [
            "G4 正式运行",
            "完成" if arms and complete_arms == len(arms) else "部分",
            f"arms complete 3×18: {complete_arms}/{len(arms)}",
        ]
    )
    rows.append(["G5 指标", "完成" if metrics else "未完成", f"metrics.json={metrics is not None}"])
    rows.append(
        [
            "G6 provenance",
            "完成" if provenance else "未完成",
            f"arms audited: {len((provenance or {}).get('systems', {}))}",
        ]
    )
    rows.append(["G7 报告", "完成" if metrics and provenance else "部分", "REPORT.md（本文件）"])
    if expanded:
        rows.append(
            [
                "附加：扩题集",
                "完成",
                f"n={expanded.get('n_questions')}（exploratory, 非固定 18 题子集）",
            ]
        )
    return rows


def _quality_and_efficiency(metrics: dict[str, Any]) -> str:
    systems = metrics.get("systems", {})
    arms = metrics.get("arms", [])
    judge_models = (metrics.get("judge") or {}).get("models", [])
    quality_rows: list[list[str]] = []
    for arm in arms:
        aggregate = systems.get(arm)
        if not aggregate:
            continue
        judge_values = [
            _pct(((model.get("accuracy") or {}).get(arm) or {}).get("accuracy"))
            for model in judge_models
        ]
        quality_rows.append(
            [
                arm,
                _fmt(aggregate["mean_answer_score"]),
                _fmt(aggregate["mean_token_f1"]),
                _pct(aggregate.get("mean_evidence_recall_postfit")),
                _pct(aggregate.get("micro_evidence_recall_postfit")),
                *judge_values,
                str(aggregate["question_count"]),
                str(aggregate["repeats"]),
            ]
        )
    quality_headers = [
        "arm",
        "answer_score",
        "token_f1（cat1–4）",
        "evidence recall（post-fit macro）",
        "evidence recall（post-fit micro）",
    ]
    quality_headers += [f"judge：{model.get('label')}" for model in judge_models]
    quality_headers += ["题数", "重复数"]
    efficiency_rows: list[list[str]] = []
    for arm in arms:
        aggregate = systems.get(arm)
        if not aggregate:
            continue
        write = aggregate.get("write_latency") or {}
        efficiency_rows.append(
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
    return "\n\n".join(
        [
            "### 质量（每题跨重复均值；来源 `results/stepG/metrics.json` → `systems.<arm>`）\n\n"
            + _table(quality_headers, quality_rows),
            "### 效率与规模（来源同上；每轮写入 = 一次 add/写入操作）\n\n"
            + _table(
                [
                    "arm",
                    "总写入耗时(s)",
                    "单轮均值(s)",
                    "写入 P50(s)",
                    "写入 P95(s)",
                    "检索 P50(s)",
                    "检索 P95(s)",
                    "回答 P50(s)",
                    "prompt tokens",
                    "completion tokens",
                    "记忆条数",
                    "存储字节",
                ],
                efficiency_rows,
            ),
            "> 注：memfw 逐字写入、无 LLM 抽取调用；Mem0 两个 arm 都跑 `add(infer=True)` 抽取。"
            "写入/存储差异同时反映“是否调用 LLM 抽取”和“存储形态”，不能单独解读为工程实现效率。",
        ]
    )


def _comparison_table(comparison: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for metric_key, metric_label in (
        ("answer_score", "answer_score"),
        ("token_f1", "token_f1（cat1–4）"),
        ("evidence_recall", "evidence recall（post-fit macro）"),
    ):
        pair = comparison.get(metric_key)
        if not pair:
            continue
        ci = pair.get("difference_ci95") or [None, None]
        rows.append(
            [
                metric_label,
                _fmt(pair.get("mean_memfw")),
                _fmt(pair.get("mean_mem0")),
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
                f"LLM judge（{key.removeprefix('judge_')}）",
                _fmt(pair.get("mean_memfw")),
                _fmt(pair.get("mean_mem0")),
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
            "均值差 (A−B)",
            "95% bootstrap CI",
            "精确置换 p",
            "Wilcoxon p",
            "Cohen dz",
            "MDE(80%)",
        ],
        rows,
    )


def _significance(metrics: dict[str, Any]) -> str:
    lines: list[str] = []
    comparisons = metrics.get("comparisons") or []
    primary = next((c for c in comparisons if "primary" in c.get("label", "")), None)
    if primary:
        lines.append("### 主比较（固定 18 题）")
        lines.append("")
        lines.append(f"{primary['a']} vs {primary['b']}；每题跨重复均值后做配对检验。")
        lines.append("")
        lines.append(_comparison_table(primary))
        lines.append("")
    for comparison in comparisons:
        if comparison is primary:
            continue
        lines.append(f"### {comparison['label']}")
        lines.append("")
        lines.append(_comparison_table(comparison))
        lines.append("")
    return "\n".join(lines)


def _judge_section(metrics: dict[str, Any]) -> str:
    judge = metrics.get("judge") or {}
    models = judge.get("models") or []
    if not models:
        return "未启用 LLM judge。"
    rows = []
    for model in models:
        rows.append(
            [
                model.get("label"),
                model.get("model"),
                str(model.get("judged_rows")),
                ", ".join(
                    f"{arm}:{_pct(acc.get('accuracy'))}"
                    for arm, acc in sorted((model.get("accuracy") or {}).items())
                ),
            ]
        )
    return _table(
        ["label", "judge model", "judged rows", "per-arm accuracy（每题跨重复均值）"], rows
    )


def _provenance_table(provenance: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for system in sorted(provenance.get("systems", {})):
        info = provenance["systems"][system]
        derived = info["derived_frame"]
        all_frame = info["all_frame"]
        day11 = info.get("day11_frame") or {}
        rows.append(
            [
                system,
                str(info["memories_total"]),
                str(info["derived_total"]),
                f"{derived['sampled']} / {derived['derived']} / {derived['flagged']}",
                f"{all_frame['sampled']} / {all_frame['derived']} / {all_frame['flagged']}",
                f"{day11.get('sampled', 0)} / {day11.get('derived', 0)} / "
                f"{day11.get('flagged', 0)}",
                str(info.get("derived_shortfall", 0)),
            ]
        )
    return _table(
        [
            "arm",
            "记忆总数",
            "派生记忆数",
            "derived_frame (样本/派生/FLAGGED)",
            "all_frame (样本/派生/FLAGGED)",
            "day11_frame (样本/派生/FLAGGED)",
            "派生样本缺口",
        ],
        rows,
    )


def _reproducibility_table(repro: dict[str, Any]) -> str:
    rows = []
    for arm in sorted(repro):
        info = repro[arm]
        rows.append(
            [
                arm,
                info.get("day11_system"),
                f"{info.get('identical_scores')}/{info.get('comparable')}",
                _fmt(info.get("mean_retrieval_jaccard"), 3),
                str(info.get("formal_memories")),
                str(info.get("day11_memories")),
            ]
        )
    return _table(
        [
            "arm",
            "Day 11 对应系统",
            "逐题 answer_score 相同",
            "检索集合 Jaccard",
            "本次记忆数",
            "Day 11 记忆数",
        ],
        rows,
    )


def _expanded_section(expanded: dict[str, Any]) -> str:
    systems = expanded.get("systems", {})
    comparisons = expanded.get("comparisons") or []
    arms = expanded.get("arms", sorted(systems))
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
        f"扩题集：同一 100 轮窗口内全部 {expanded.get('n_questions')} 道有完整 gold evidence 的题"
        "（固定 18 题子集之外的 exploratory 补充，不替代主结果）。",
        "",
        _table(
            ["arm", "answer_score", "token_f1(cat1-4)", "recall(macro)", "recall(micro)", "题数"],
            rows,
        ),
    ]
    if comparisons:
        primary = comparisons[0]
        lines.append("")
        lines.append(
            f"主比较（{primary.get('a')} vs {primary.get('b')}，"
            f"n={(primary.get('answer_score') or {}).get('n_pairs')}）："
        )
        lines.append("")
        lines.append(_comparison_table(primary))
    lines.append("")
    lines.append(
        "> 扩题集复用同一批已写入的记忆，只新增“回答+judge”，用于提高统计功效；"
        "属于探索性分析，不做族错误校正。"
    )
    return "\n".join(lines)


def write_report(config: dict[str, Any]) -> Path:
    results_dir = _resolve(config["paths"]["results_dir"])
    metrics_path = results_dir / "metrics.json"
    metrics = _read_json(metrics_path) if metrics_path.exists() else None
    provenance_path = results_dir / "provenance_summary.json"
    provenance = _read_json(provenance_path) if provenance_path.exists() else None
    subset = load_subset(config)
    env_text = (
        (results_dir / "env.txt").read_text(encoding="utf-8")
        if (results_dir / "env.txt").exists()
        else "env.txt 缺失"
    )
    runtime_path = results_dir / "runtime_info.json"
    runtime_info = _read_json(runtime_path) if runtime_path.exists() else {}
    expanded = _load_expanded(results_dir)
    arms = (metrics or {}).get("arms") or [
        SYSTEMS.get(name, name) for name in config["runs"].get("systems", [])
    ]
    reproducibility = _load_reproducibility(results_dir, arms)
    preliminary = _load_preliminary()

    lines: list[str] = []
    lines.append("# Step G：memfw vs Mem0 正式对比实验报告（迭代版）")
    lines.append("")
    lines.append(
        "> 本报告由 `eval/stepG_report.py` 从 `results/stepG/` 下的运行产物自动生成；"
        "所有数字均可在标注的来源文件中核对。实验仅使用公开 LoCoMo 数据。"
    )
    lines.append("")
    lines.append(
        "- 迭代内容：新增 **mem0_stock** arm（stock Mem0）作为主比较；"
        "**mem0_custom**（schema 兼容 instructions）保留为敏感性 arm；"
        "**mem0_stock_day11ctx** 用 Day 11 的 `num_ctx=8192` 复刻 Mem0 抽取，"
        "用于逐字节可复现性核对；主 judge 换成不同模型（qwen3.5:4b），"
        "同模型 judge 保留为诊断；新增**扩题集**（exploratory）提高统计功效。"
    )
    lines.append("")
    lines.append("## 1. 执行状态")
    lines.append("")
    lines.append(
        _table(
            ["步骤", "状态", "依据"],
            _status_rows(config, results_dir, metrics, provenance, expanded),
        )
    )
    lines.append("")
    lines.append("## 2. 实验设置")
    lines.append("")
    judge_config = config.get("judge") or {}
    sensitivity = config.get("judge_sensitivity") or {}
    lines.append(
        _table(
            ["项目", "值"],
            [
                ["数据", f"公开 LoCoMo `locomo10.json`，SHA-256 `{config['data']['sha256']}`"],
                [
                    "子集",
                    f"`{subset['source']['sample_id']}`，前 "
                    f"{subset['scope']['input_turn_count']} 轮，"
                    f"{subset['scope']['selected_question_count']} 题"
                    "（`results/stepG/subset.json`）",
                ],
                ["seed", str(config["seed"])],
                [
                    "arms",
                    f"`{'`, `'.join(arms)}`（每 arm {config['runs']['repeats']} 次重复）",
                ],
                [
                    "Answer LLM（各 arm 相同）",
                    f"`{config['llm']['model']}` via {config['llm']['transport']}，"
                    f"temperature={config['llm']['temperature']}",
                ],
                [
                    "Answer embedding（各 arm 相同）",
                    f"`{config['embedding']['model']}`，dims={config['embedding']['dims']}",
                ],
                [
                    "Primary judge（不同模型）",
                    f"`{judge_config.get('model')}`（与 answer 模型不同）",
                ],
                ["Sensitivity judge（同模型）", f"`{sensitivity.get('model')}`（仅作诊断）"],
                [
                    "检索",
                    f"top_k={config['retrieval']['top_k']}，上下文预算 "
                    f"{config['retrieval']['context_token_budget']} tokens",
                ],
                [
                    "Mem0 配置",
                    "mem0_stock 使用默认抽取提示；mem0_custom 额外传入 schema 兼容的 "
                    "custom instructions（敏感性 arm）；mem0_stock_day11ctx 为默认提示 + "
                    "Day 11 的 num_ctx=8192（复现 arm）",
                ],
                [
                    "与 Day 11 的配置差异",
                    "Day 11 mem0 `num_ctx`="
                    f"{preliminary.get('num_ctx') if preliminary else 'n/a'}；"
                    f"本轮默认 `num_ctx`={config['llm'].get('num_ctx')}。"
                    "mem0_stock 使用本机默认窗口（抽取结果只作近似复现）；"
                    "mem0_stock_day11ctx 使用 Day 11 的 8192 窗口，用于逐题复现核对。"
                    "两臂的答案生成 prompt 很短，窗口差异不影响回答阶段",
                ],
                ["硬件", runtime_info.get("hardware") or "见 env.txt（platform/machine）"],
                ["总耗时", _seconds(runtime_info.get("elapsed_seconds"))],
                [
                    "API 花费",
                    f"${float(runtime_info.get('api_cost_usd') or 0.0):.2f}"
                    "（本地 Ollama，无外部 API）",
                ],
                ["模型 digest", _env_field(env_text, "ollama_model_digests") or "见 env.txt"],
            ],
        )
    )
    lines.append("")
    lines.append("`env.txt`：")
    lines.append("")
    lines.append("```")
    lines.append(env_text.strip())
    lines.append("```")
    lines.append("")
    lines.append("## 3. 主结果")
    lines.append("")
    if metrics:
        lines.append(_quality_and_efficiency(metrics))
        lines.append("")
        lines.append("### LLM judge（每题跨重复均值）")
        lines.append("")
        lines.append(_judge_section(metrics))
        lines.append("")
        lines.append(
            "### Provenance 审计（G6；来源 `provenance_summary.json` / `provenance_audit.csv`）"
        )
        lines.append("")
        if provenance:
            lines.append(_provenance_table(provenance))
            lines.append("")
            lines.append(
                "- `derived_frame`：每个 arm 最多抽 30 条派生记忆；"
                "`FLAGGED` 表示派生文本被记录成用户原话（R3）。"
            )
            lines.append(
                "- `all_frame`：30 条均匀抽样（固定分母兜底）；"
                "`day11_frame`：Day 11 的均匀抽样 + R3 规则。"
            )
            lines.append(
                "- 审计只取 `rep1`；mem0_custom 的 custom instructions 请求 "
                "`attributed_to=user`，其 R3 含 prompt 强制成分；mem0_stock 无此强制。"
            )
        else:
            lines.append("未生成 provenance 汇总。")
        lines.append("")
        lines.append("## 4. 显著性检验")
        lines.append("")
        if metrics:
            lines.append(_significance(metrics))
        lines.append("")
        lines.append("## 5. 与 Day 11 的可复现性核对")
        lines.append("")
        lines.append(
            "以 Day 11 报告 JSON（`reports/day11_baseline_comparison.json`）的逐题结果为参照："
            "`memfw` 与 Day 11 `memfw` 同实现；`mem0_stock` 与 Day 11 `mem0` 同为 stock 抽取配置"
            "（但本机 16k 上下文会改变抽取）；`mem0_stock_day11ctx` 使用 Day 11 的 "
            "`num_ctx=8192`，是用于逐题核对 Mem0 抽取复现的臂；"
            "`mem0_custom` 没有 Day 11 对应项（敏感性 arm）。"
        )
        lines.append("")
        if reproducibility:
            lines.append(_reproducibility_table(reproducibility))
        else:
            lines.append("缺少 Day 11 JSON 或本轮 runs，无法核对。")
        lines.append("")
        if expanded:
            lines.append("## 6. 扩题集（exploratory）")
            lines.append("")
            lines.append(_expanded_section(expanded))
            lines.append("")
        lines.append("## 7. 与初步结果的对比")
        lines.append("")
        if preliminary and metrics:
            stock = metrics["systems"].get("mem0_stock") or {}
            memfw = metrics["systems"].get("memfw") or {}
            repro_stock = reproducibility.get("mem0_stock") or {}
            primary: dict[str, Any] = next(
                (c for c in metrics.get("comparisons") or [] if "primary" in c.get("label", "")),
                {},
            )
            primary_answer = primary.get("answer_score") or {}
            prov = (provenance or {}).get("systems", {})
            memfw_frame = (prov.get("memfw") or {}).get("day11_frame") or {}
            stock_frame = (prov.get("mem0_stock") or {}).get("day11_frame") or {}
            answer_delta = abs(
                float(memfw.get("mean_answer_score") or 0) - preliminary["answer_memfw"]
            )
            recall_delta = abs(
                float(memfw.get("micro_evidence_recall_postfit") or 0)
                - preliminary["recall_micro_memfw"]
            )
            stock_delta = abs(
                float(stock.get("mean_answer_score") or 0) - preliminary["answer_mem0"]
            )
            ratio = float((stock.get("write_latency_turns") or {}).get("p50_seconds") or 0) / float(
                (memfw.get("write_latency_turns") or {}).get("p50_seconds") or 1
            )
            p_value = (primary_answer.get("exact_sign_flip") or {}).get("p_value")
            prelim_recall_diff = (
                preliminary["recall_micro_memfw"] - preliminary["recall_micro_mem0"]
            )
            formal_recall_diff = float(memfw.get("micro_evidence_recall_postfit") or 0) - float(
                stock.get("micro_evidence_recall_postfit") or 0
            )
            checks = [
                (
                    "H1 memfw answer_score 与 Day 11 接近（|Δ|≤0.02）",
                    answer_delta <= 0.02,
                    f"|Δ|={answer_delta:.4f}",
                ),
                (
                    "H2 mem0_stock answer_score 与 Day 11 接近（|Δ|≤0.02）",
                    stock_delta <= 0.02,
                    f"|Δ|={stock_delta:.4f}",
                ),
                (
                    "H3 mem0_stock 逐题复现 Day 11（≥16/18 题分数相同）",
                    int(repro_stock.get("identical_scores") or 0) >= 16,
                    f"{repro_stock.get('identical_scores')}/{repro_stock.get('comparable')}",
                ),
                (
                    "H4 memfw 写入 P50 显著低于 mem0_stock（比值 ≥10×）",
                    ratio >= 10.0,
                    f"比值={ratio:.1f}×",
                ),
                (
                    "H5 主比较未检测到显著差异（精确置换 p>0.05）",
                    p_value is not None and p_value > 0.05,
                    f"p={_fmt(p_value)}",
                ),
                (
                    "H6 memfw post-fit micro recall 复现（|Δ|≤0.05）",
                    recall_delta <= 0.05,
                    f"|Δ|={recall_delta:.4f}",
                ),
                (
                    "H7 两系统 recall 差距方向与 Day 11 一致",
                    (prelim_recall_diff >= 0) == (formal_recall_diff >= 0),
                    f"Day11 memfw−mem0={prelim_recall_diff:+.4f}；本轮={formal_recall_diff:+.4f}",
                ),
                (
                    "H8 mem0 的派生记忆多数被记为无标注用户记忆"
                    "（stock arm day11_frame FLAGGED ≥ 半数）",
                    int(stock_frame.get("sampled") or 0) > 0
                    and int(stock_frame.get("flagged") or 0) * 2
                    >= int(stock_frame.get("sampled") or 0),
                    f"stock {stock_frame.get('flagged')}/{stock_frame.get('sampled')}；"
                    f"Day11 {preliminary['prov_flagged_mem0']}/{preliminary['prov_sample']}",
                ),
            ]
            repro_day11ctx = reproducibility.get("mem0_stock_day11ctx") or {}
            day11ctx = metrics["systems"].get("mem0_stock_day11ctx") or {}
            if repro_day11ctx:
                day11ctx_delta = abs(
                    float(day11ctx.get("mean_answer_score") or 0) - preliminary["answer_mem0"]
                )
                checks.extend(
                    [
                        (
                            "H9 mem0_stock_day11ctx 逐题复现 Day 11（≥16/18 题分数相同）",
                            int(repro_day11ctx.get("identical_scores") or 0) >= 16,
                            f"{repro_day11ctx.get('identical_scores')}/"
                            f"{repro_day11ctx.get('comparable')}；记忆数 "
                            f"{repro_day11ctx.get('formal_memories')} vs "
                            f"{repro_day11ctx.get('day11_memories')}",
                        ),
                        (
                            "H10 mem0_stock_day11ctx answer_score 与 Day 11 接近（|Δ|≤0.02）",
                            day11ctx_delta <= 0.02,
                            f"|Δ|={day11ctx_delta:.4f}",
                        ),
                    ]
                )
            lines.append(
                _table(
                    ["项目", "Day 11 初步", "本轮正式", "来源"],
                    [
                        [
                            "answer_score memfw",
                            _fmt(preliminary["answer_memfw"]),
                            _fmt(memfw.get("mean_answer_score")),
                            "metrics.json",
                        ],
                        [
                            "answer_score mem0（stock）",
                            _fmt(preliminary["answer_mem0"]),
                            _fmt(stock.get("mean_answer_score")),
                            "metrics.json",
                        ],
                        [
                            "evidence recall micro memfw",
                            _pct(preliminary["recall_micro_memfw"]),
                            _pct(memfw.get("micro_evidence_recall_postfit")),
                            "metrics.json",
                        ],
                        [
                            "evidence recall micro mem0（stock）",
                            _pct(preliminary["recall_micro_mem0"]),
                            _pct(stock.get("micro_evidence_recall_postfit")),
                            "metrics.json",
                        ],
                        [
                            "写入 P50 memfw",
                            _fmt(preliminary["write_p50_memfw"], 4) + "s",
                            _fmt((memfw.get("write_latency_turns") or {}).get("p50_seconds"), 4)
                            + "s",
                            "write_turns.jsonl",
                        ],
                        [
                            "写入 P50 mem0（stock）",
                            _fmt(preliminary["write_p50_mem0"], 4) + "s",
                            _fmt((stock.get("write_latency_turns") or {}).get("p50_seconds"), 4)
                            + "s",
                            "write_turns.jsonl",
                        ],
                        [
                            "provenance FLAGGED（day11_frame）",
                            f"memfw {preliminary['prov_flagged_memfw']}/"
                            f"{preliminary['prov_sample']}；mem0 "
                            f"{preliminary['prov_flagged_mem0']}/{preliminary['prov_sample']}",
                            f"memfw {memfw_frame.get('flagged', 0)}/"
                            f"{memfw_frame.get('sampled', 0)}；mem0_stock "
                            f"{stock_frame.get('flagged', 0)}/{stock_frame.get('sampled', 0)}",
                            "provenance_summary.json",
                        ],
                    ],
                )
            )
            lines.append("")
            lines.append("判定（阈值写死在本函数中，结果由数据计算）：")
            lines.append("")
            for label, passed, detail in checks:
                lines.append(f"- **{'确认' if passed else '未确认'}**：{label}（{detail}）。")
            custom = metrics["systems"].get("mem0_custom") or {}
            if custom:
                lines.append(
                    f"- **配置敏感性**：mem0_custom answer_score="
                    f"{_fmt(custom.get('mean_answer_score'))}、记忆数="
                    f"{_fmt(custom.get('memory_count_mean'), 1)}，与 stock 不同；"
                    "因此两套 Mem0 数字不能混用。"
                )
            lines.append(
                f"- **复现解释**：mem0_stock 与 Day 11 mem0 的逐题分数一致率 "
                f"{repro_stock.get('identical_scores')}/{repro_stock.get('comparable')}，"
                f"记忆数 {repro_stock.get('formal_memories')} vs "
                f"{repro_stock.get('day11_memories')}；本轮 mem0 wrapper 使用 "
                f"num_ctx={config['llm'].get('num_ctx')}，Day 11 使用 "
                f"{preliminary.get('num_ctx')}，抽取 prompt 是否被截断会改变抽取文本，"
                "因此 stock arm 是“同默认抽取提示 + 本机协议”的近似复现，而非逐字节复现。"
            )
            lines.append("- 正式结论以 §3–§5 与 `metrics.json` 为准；初步数字仅作参照。")
        else:
            lines.append("缺少初步 JSON 或 metrics.json，无法生成对比。")
        lines.append("")
        lines.append("## 8. 未完成项与局限")
        lines.append("")
        if metrics:
            for item in metrics.get("limitations", []):
                lines.append(f"- {item}")
        if provenance:
            for arm in sorted(provenance.get("systems", {})):
                info = provenance["systems"][arm]
                if int(info.get("derived_shortfall") or 0) > 0:
                    lines.append(
                        f"- {arm}: derived_frame 只有 {info.get('derived_total')} 条派生记忆"
                        f"（缺口 {info.get('derived_shortfall')}），该 arm 无对象可审计。"
                    )
        if not expanded:
            lines.append("- 扩题集（exploratory）未生成：仅使用固定 18 题子集，统计功效有限。")
        lines.append(
            "- 重复确定性为观测结果（temperature=0），不能代替独立样本方差；judge/模型身份见 §2。"
        )
        lines.append(
            "- 运行快照：`env.txt` 的 git_head 是运行开始时 HEAD；"
            "实际运行代码由本报告对应的提交保存。"
        )
        lines.append("")
        lines.append("## 9. 复现命令")
        lines.append("")
        lines.append("```sh")
        lines.append("cd ~/Documents/Codex/2026-10-18/memfw-stepG-worktree")
        lines.append("MEM0_DIR=$PWD/results/stepG/runtime/mem0_home \\")
        lines.append("  .venv-stepg/bin/python eval/stepG_pipeline.py --stage all")
        lines.append("")
        lines.append("# 只重跑某个阶段")
        lines.append("  .venv-stepg/bin/python eval/stepG_pipeline.py --stage metrics")
        lines.append("  .venv-stepg/bin/python eval/stepG_pipeline.py --stage provenance")
        lines.append("  .venv-stepg/bin/python eval/stepG_expanded.py   # 扩题集（exploratory）")
        lines.append("```")
        lines.append("")
        lines.append("## 10. 产物清单")
        lines.append("")
        lines.append(
            _table(
                ["文件", "内容"],
                [
                    ["results/stepG/config.yaml", "arms、模型、judge(s)、seed、repeats"],
                    ["results/stepG/subset.json", "固定 18 题子集（conv-49 前 100 轮）"],
                    ["results/stepG/subset_expanded.json", "扩题集（exploratory）"],
                    ["results/stepG/env.txt / pip-freeze.txt", "环境、模型 digest、git HEAD"],
                    [
                        "results/stepG/runs/rep*/<arm>/",
                        "每题 JSONL、每轮写入、记忆导出、run summary",
                    ],
                    [
                        "results/stepG/judge_*.jsonl",
                        "primary（qwen3.5:4b）与 sensitivity（qwen3.5:9b）judge 逐条判定",
                    ],
                    ["results/stepG/metrics.json / metrics.md", "质量、效率、规模、配对检验"],
                    ["results/stepG/expanded_metrics.json / md", "扩题集指标（exploratory）"],
                    [
                        "results/stepG/provenance_rubric.md / audit.csv / summary.json",
                        "provenance 规则、逐条判定、汇总",
                    ],
                    ["results/stepG/validation.json", "产物完整性校验"],
                    ["results/stepG/REPORT.md", "本报告"],
                ],
            )
        )
        lines.append("")
    else:
        lines.append("尚未生成 `metrics.json`，无法汇总主结果。")
        lines.append("")
    report_path = results_dir / "REPORT.md"
    report_path.write_text(redact_text("\n".join(lines)), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    config = load_config(
        Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    path = write_report(config)
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
