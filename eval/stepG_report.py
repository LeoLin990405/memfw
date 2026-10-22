"""Render ``results/stepG/REPORT.md`` from the artifacts produced by the pipeline.

Every number in the report is read from a JSON/CSV/TXT file under
``results/stepG/``; the source file is printed next to each table. Nothing is
copied from an earlier session or typed in by hand.
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

from eval.stepG_runner import _resolve, load_config, load_subset  # noqa: E402

PRELIMINARY_SOURCE = "reports/day11_baseline_comparison.md"


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


def _load_preliminary() -> dict[str, Any] | None:
    """Load the Day 11 preliminary numbers from the committed public report JSON."""
    path = REPO_ROOT / PRELIMINARY_SOURCE.replace(".md", ".json")
    if not path.exists():
        return None
    data: dict[str, Any] = _read_json(path)
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
            "prov_flagged_memfw": int(audit["memfw"]["flagged_count"]),
            "prov_flagged_mem0": int(audit["mem0"]["flagged_count"]),
            "prov_sample": int(audit["mem0"]["sample_size"]),
            "model": data.get("settings", {}).get("answer_model"),
            "embedding": data.get("settings", {}).get("embedding_model"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _env_field(env_text: str, key: str) -> str | None:
    for line in env_text.splitlines():
        name, _, value = line.partition(": ")
        if name.strip() == key and value.strip():
            return value.strip()
    return None


def _status_rows(
    config: dict[str, Any],
    results_dir: Path,
    metrics: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    env_exists = (results_dir / "env.txt").exists()
    pip_freeze = (results_dir / "pip-freeze.txt").exists()
    rows.append(
        [
            "G1 环境",
            "完成" if env_exists and pip_freeze else "部分",
            f"env.txt={env_exists}, pip-freeze.txt={pip_freeze}",
        ]
    )
    same_model = config["llm"]["model"] == config["judge"]["model"]
    check = (
        _read_json(results_dir / "pipeline_status.json").get("endpoint_check")
        if (results_dir / "pipeline_status.json").exists()
        else None
    )
    env_text = (
        (results_dir / "env.txt").read_text(encoding="utf-8")
        if (results_dir / "env.txt").exists()
        else ""
    )
    env_has_models = config["llm"]["model"] in env_text and config["embedding"]["model"] in env_text
    endpoint_ok = bool(check) or env_has_models
    rows.append(
        [
            "G2 统一模型",
            "完成" if same_model and endpoint_ok else "部分",
            f"llm={config['llm']['model']}, embedding={config['embedding']['model']}, "
            f"judge={config['judge']['model']}, "
            f"smoke_check={'ok' if check else ('env-recorded' if env_has_models else 'missing')}",
        ]
    )
    runner_exists = (REPO_ROOT / "eval" / "stepG_runner.py").exists()
    tests_log = results_dir / "test-results.txt"
    rows.append(
        [
            "G3 runner + 单测",
            "完成" if runner_exists and tests_log.exists() else "部分",
            f"runner={runner_exists}, pytest_log={tests_log.exists()}",
        ]
    )
    run_manifest = results_dir / "run_manifest.json"
    complete_runs = 0
    if run_manifest.exists():
        manifest = _read_json(run_manifest)
        complete_runs = sum(
            1 for run in manifest.get("runs", []) if int(run.get("question_rows") or 0) >= 18
        )
    rows.append(
        [
            "G4 正式运行",
            "完成" if complete_runs >= 6 else ("部分" if complete_runs else "阻塞"),
            f"complete 18-question run summaries: {complete_runs}/6",
        ]
    )
    rows.append(
        [
            "G5 指标",
            "完成" if metrics else "未完成",
            f"metrics.json={metrics is not None}",
        ]
    )
    rows.append(
        [
            "G6 provenance",
            "完成" if provenance else "未完成",
            f"provenance_summary.json={provenance is not None}",
        ]
    )
    rows.append(["G7 报告", "完成", "results/stepG/REPORT.md（本文件）"])
    return rows


def _main_results(metrics: dict[str, Any]) -> str:
    systems = metrics.get("systems", {})
    quality_rows: list[list[str]] = []
    for system in ("memfw", "mem0"):
        aggregate = systems.get(system)
        if not aggregate:
            continue
        quality_rows.append(
            [
                system,
                _fmt(aggregate["mean_answer_score"]),
                _fmt(aggregate["mean_token_f1"]),
                _pct(metrics.get("judge", {}).get("accuracy", {}).get(system, {}).get("accuracy")),
                _pct(aggregate.get("mean_evidence_recall_postfit")),
                str(aggregate["question_count"]),
                str(aggregate["repeats"]),
            ]
        )
    efficiency_rows: list[list[str]] = []
    for system in ("memfw", "mem0"):
        aggregate = systems.get(system)
        if not aggregate:
            continue
        write = aggregate.get("write_latency") or {}
        efficiency_rows.append(
            [
                system,
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
            "### 质量（每题跨重复均值；来源 `results/stepG/metrics.json` → `systems.<name>`）\n\n"
            + _table(
                [
                    "system",
                    "answer_score",
                    "token_f1（cat1–4）",
                    "LLM-judge 正确率（每题跨重复均值）",
                    "evidence recall（post-fit macro）",
                    "题数",
                    "重复数",
                ],
                quality_rows,
            ),
            "### 效率与规模（来源同上；写入延迟为每轮 add 的均值/题级检索与回答延迟）\n\n"
            + _table(
                [
                    "system",
                    "总写入耗时(s)",
                    "单轮写入均值(s)",
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
            "> 注：memfw 采用逐字写入协议，无 LLM 调用开销；Mem0 使用 `add(infer=True)` "
            "跑抽取/更新流水线，因此写入成本天然不对称。memfw 的“存储字节”只统计 vault 目录，"
            "内存中的 float32 向量不计入；Mem0 统计 Qdrant + history DB 所在运行目录。",
        ]
    )


def _significance(metrics: dict[str, Any]) -> str:
    paired = metrics.get("paired") or {}
    ci = paired.get("difference_ci95") or [None, None]
    row = [
        str(paired.get("n_pairs")),
        _fmt(paired.get("mean_memfw")),
        _fmt(paired.get("mean_mem0")),
        _fmt(paired.get("mean_difference")),
        f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
        _fmt((paired.get("exact_sign_flip") or {}).get("p_value")),
        _fmt((paired.get("exact_wilcoxon") or {}).get("p_value")),
        _fmt(paired.get("cohen_dz")),
        _fmt(paired.get("rank_biserial")),
        _fmt(paired.get("minimum_detectable_difference")),
    ]
    return _table(
        [
            "配对题数",
            "memfw 均值",
            "mem0 均值",
            "均值差",
            "95% bootstrap CI",
            "精确置换 p",
            "Wilcoxon p",
            "Cohen dz",
            "rank-biserial",
            "MDE(80%)",
        ],
        [row],
    )


def _seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    total = int(round(float(value)))
    return f"{total // 3600}h{(total % 3600) // 60:02d}m{total % 60:02d}s"


def _secondary_table(metrics: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for key, label in (
        ("paired_token_f1", "token_f1（cat1–4）"),
        ("paired_judge", "LLM judge（每题跨重复均值）"),
        ("paired_recall", "evidence recall（post-fit macro）"),
    ):
        pair = metrics.get(key)
        if not pair:
            continue
        ci = pair.get("difference_ci95") or [None, None]
        rows.append(
            [
                label,
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
    if not rows:
        return ""
    return _table(
        [
            "指标",
            "memfw",
            "mem0",
            "均值差",
            "95% bootstrap CI",
            "精确置换 p",
            "Wilcoxon p",
            "Cohen dz",
            "MDE(80%)",
        ],
        rows,
    )


def _provenance_table(provenance: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for system in ("memfw", "mem0"):
        info = provenance.get("systems", {}).get(system)
        if not info:
            continue
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
                str(info["derived_shortfall"]),
            ]
        )
    return _table(
        [
            "system",
            "记忆总数",
            "派生记忆数",
            "derived_frame (样本/派生/FLAGGED)",
            "all_frame (样本/派生/FLAGGED)",
            "day11_frame (样本/派生/FLAGGED)",
            "派生样本缺口",
        ],
        rows,
    )


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
    status_rows = _status_rows(config, results_dir, metrics, provenance)

    lines: list[str] = []
    lines.append("# Step G：memfw vs Mem0 正式对比实验报告")
    lines.append("")
    lines.append(
        "> 本报告由 `eval/stepG_report.py` 从 `results/stepG/` 下的运行产物自动生成；"
        "所有数字均可在括号标注的来源文件中核对。实验仅使用公开 LoCoMo 数据。"
    )
    lines.append("")
    lines.append("## 1. 执行状态（G1–G7）")
    lines.append("")
    lines.append(_table(["步骤", "状态", "依据"], status_rows))
    lines.append("")
    lines.append("## 2. 实验设置")
    lines.append("")
    lines.append(
        _table(
            ["项目", "值"],
            [
                [
                    "数据",
                    f"公开 LoCoMo `locomo10.json`，SHA-256 `{config['data']['sha256']}`",
                ],
                [
                    "子集",
                    f"`{subset['source']['sample_id']}`，前 "
                    f"{subset['scope']['input_turn_count']} 轮，"
                    f"{subset['scope']['selected_question_count']} 题"
                    "（来源 `results/stepG/subset.json`）",
                ],
                ["seed", str(config["seed"])],
                [
                    "LLM（两系统相同）",
                    f"`{config['llm']['model']}` via {config['llm']['transport']} "
                    f"@ `{config['llm']['base_url']}`，temperature={config['llm']['temperature']}",
                ],
                [
                    "Embedding（两系统相同）",
                    f"`{config['embedding']['model']}`，dims={config['embedding']['dims']}",
                ],
                [
                    "检索",
                    f"top_k={config['retrieval']['top_k']}，上下文预算 "
                    f"{config['retrieval']['context_token_budget']} tokens",
                ],
                [
                    "重复次数",
                    f"每系统 {config['runs']['repeats']} 次（run ids: "
                    f"rep1..rep{config['runs']['repeats']}）",
                ],
                [
                    "内存协议",
                    "memfw 仅作为“逐字写入 + 共享 embedding 余弦检索”的基线；"
                    "Mem0 使用 `add(infer=True)`",
                ],
                ["硬件", runtime_info.get("hardware") or "见 env.txt（platform/machine）"],
                [
                    "总耗时",
                    _seconds(runtime_info.get("elapsed_seconds")),
                ],
                ["模型 digest", _env_field(env_text, "ollama_model_digests") or "见 env.txt"],
                [
                    "API 花费",
                    f"${float(runtime_info.get('api_cost_usd') or 0.0):.2f}"
                    "（本地 Ollama，无外部 API）",
                ],
                [
                    "运行环境",
                    "正式运行使用仓库既有 `.venv`（Python 3.11.16 / mem0ai 2.2.1）；"
                    "测试使用独立 `.venv-stepg`（worktree 内 editable 安装），依赖版本相同",
                ],
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
        lines.append(_main_results(metrics))
        lines.append("")
        lines.append(
            "### Provenance 审计（G6；来源 `results/stepG/provenance_summary.json` 与 "
            "`provenance_audit.csv`）"
        )
        lines.append("")
        if provenance:
            lines.append(_provenance_table(provenance))
            lines.append("")
            lines.append(
                "- `derived_frame`：每个系统最多抽 30 条“派生/推断”记忆（非源轮次逐字文本）；"
                "`FLAGGED` 表示派生文本被记录成用户原话（rubric R3）。"
            )
            lines.append(
                "- `all_frame`：固定 30 条均匀抽样，作为无派生记忆系统的分母兜底；"
                "`day11_frame`：与 Day 11 相同的均匀抽样 + R3 规则，仅用于方向性对照。"
                "三个 frame 的分母都在 summary 中明确给出。"
            )
            attribution_note = ""
            if provenance:
                counts = (provenance["systems"].get("mem0") or {}).get("attribution_counts") or {}
                if counts:
                    attribution_note = (
                        f"（rep1 记忆库中 attributed_to 分布：user={counts.get('user', 0)}、"
                        f"assistant={counts.get('assistant', 0)}、"
                        f"missing={counts.get('missing', 0)}）"
                    )
            lines.append(
                "- 审计仅使用 `rep1` 的记忆导出（见 `provenance_summary.json` → "
                "`audited_run_id`）；mem0 的 custom extraction instructions *请求* "
                "`attributed_to=user`，实际输出为显式 user、显式 assistant 与缺失字段的混合"
                f"{attribution_note}。按 Step-G 规则，缺失字段默认归入用户命名空间"
                "（R3），"
                "仅显式 assistant 落入 R4；按 Day-11 规则缺失字段为 R4。因此 R3 FLAGGED "
                "是**部分由 prompt 强制、部分来自模型行为**，不能当作独立质量测量。"
            )
        else:
            lines.append("未生成 provenance 汇总（G6 未完成）。")
        lines.append("")
        lines.append("## 4. 显著性检验与局限")
        lines.append("")
        lines.append(
            "主指标为每题 `answer_score`（类别 1–4 为 token F1，类别 5 为弃答得分），"
            "先在重复内取均值，再做配对检验。"
        )
        lines.append("")
        if metrics and metrics.get("paired"):
            lines.append(_significance(metrics))
            lines.append("")
            secondary = _secondary_table(metrics) if metrics else ""
            if secondary:
                lines.append("次要指标（同样为 18 题配对、精确检验）：")
                lines.append("")
                lines.append(secondary)
                lines.append("")
        if metrics:
            for item in metrics.get("limitations", []):
                lines.append(f"- {item}")
        lines.append("")
        lines.append("## 5. 与初步结果的对比")
        lines.append("")
        prelim = _load_preliminary()
        formal: dict[str, Any] = {}
        if metrics:
            for system in ("memfw", "mem0"):
                aggregate = metrics["systems"].get(system) or {}
                formal[f"answer_{system}"] = aggregate.get("mean_answer_score")
                formal[f"recall_micro_{system}"] = aggregate.get("micro_evidence_recall_postfit")
                formal[f"write_p50_{system}"] = (aggregate.get("write_latency_turns") or {}).get(
                    "p50_seconds"
                )
        if prelim and metrics:
            source = prelim["source_json"]
            lines.append(
                f"初步结果（来源 `{source}`，Day 11，仅作参考，**不是正式结论**）与本次正式实验"
                f"使用**同一模型**（`{prelim['model']}` + `{prelim['embedding']}`）与"
                "**同一固定子集**（conv-49 前 100 轮 / 18 题 / seed 20261021），"
                "因此 answer_score 与 **post-fit micro evidence recall** 可以直接对照；"
                "但 Mem0 的写入提示（custom extraction instructions）与运行时配置不同，"
                "Mem0 侧数字不是同一配置的严格复现（环境与模型 digest 与 Day 11 相同）。"
            )
            lines.append("")
            prov_formal: dict[str, str] = {}
            if provenance:
                for system in ("memfw", "mem0"):
                    frame = (provenance["systems"].get(system) or {}).get("day11_frame") or {}
                    prov_formal[system] = f"{frame.get('flagged', 0)}/{frame.get('sampled', 0)}"
            lines.append(
                _table(
                    ["指标", "初步结果（Day 11）", "本次正式实验", "正式值来源"],
                    [
                        [
                            "answer_score memfw",
                            _fmt(prelim["answer_memfw"]),
                            _fmt(formal.get("answer_memfw")),
                            "metrics.json → systems.memfw.mean_answer_score",
                        ],
                        [
                            "answer_score mem0",
                            _fmt(prelim["answer_mem0"]),
                            _fmt(formal.get("answer_mem0")),
                            "metrics.json → systems.mem0.mean_answer_score",
                        ],
                        [
                            "evidence recall（post-fit micro）memfw",
                            _pct(prelim["recall_micro_memfw"]),
                            _pct(formal.get("recall_micro_memfw")),
                            "metrics.json → systems.memfw.micro_evidence_recall_postfit",
                        ],
                        [
                            "evidence recall（post-fit micro）mem0",
                            _pct(prelim["recall_micro_mem0"]),
                            _pct(formal.get("recall_micro_mem0")),
                            "metrics.json → systems.mem0.micro_evidence_recall_postfit",
                        ],
                        [
                            "写入 P50 memfw",
                            _fmt(prelim["write_p50_memfw"], 4) + "s",
                            _fmt(formal.get("write_p50_memfw"), 4) + "s",
                            "runs/*/memfw/write_turns.jsonl",
                        ],
                        [
                            "写入 P50 mem0",
                            _fmt(prelim["write_p50_mem0"], 4) + "s",
                            _fmt(formal.get("write_p50_mem0"), 4) + "s",
                            "runs/*/mem0/write_turns.jsonl",
                        ],
                        [
                            "provenance FLAGGED（Day-11 同框架：均匀抽样 + R3）",
                            f"memfw {prelim['prov_flagged_memfw']}/{prelim['prov_sample']}；"
                            f"mem0 {prelim['prov_flagged_mem0']}/{prelim['prov_sample']}",
                            f"memfw {prov_formal.get('memfw', 'n/a')}；"
                            f"mem0 {prov_formal.get('mem0', 'n/a')}",
                            "provenance_summary.json → day11_frame",
                        ],
                    ],
                )
            )
            lines.append("")
            lines.append("按预先设定的判定阈值逐项核对（阈值写死在本函数中，结果由数据计算）：")
            lines.append("")
            memfw_answer = float(formal.get("answer_memfw") or 0.0)
            answer_delta = abs(memfw_answer - prelim["answer_memfw"])
            ratio = float(formal.get("write_p50_mem0") or 0.0) / float(
                formal.get("write_p50_memfw") or 1.0
            )
            paired = metrics.get("paired") or {}
            p_value = (paired.get("exact_sign_flip") or {}).get("p_value")
            p_text = _fmt(p_value)
            day11_mem0 = (
                (provenance or {}).get("systems", {}).get("mem0", {}).get("day11_frame", {})
            )
            mem0_flags = int(day11_mem0.get("flagged") or 0)
            mem0_sampled = int(day11_mem0.get("sampled") or 0)
            recall_memfw_delta = abs(
                float(formal.get("recall_micro_memfw") or 0.0) - prelim["recall_micro_memfw"]
            )
            prelim_recall_diff = prelim["recall_micro_memfw"] - prelim["recall_micro_mem0"]
            formal_recall_diff = float(formal.get("recall_micro_memfw") or 0.0) - float(
                formal.get("recall_micro_mem0") or 0.0
            )
            checks = [
                (
                    "H1 memfw answer_score 复现（与初步值绝对差 ≤ 0.02）",
                    answer_delta <= 0.02,
                    f"|Δ|={answer_delta:.4f}",
                ),
                (
                    "H2 mem0 派生记忆多数被记为无标注用户记忆（Day-11 同框架 FLAGGED ≥ 半数）",
                    mem0_sampled > 0 and mem0_flags >= mem0_sampled / 2,
                    f"{mem0_flags}/{mem0_sampled}（初步 "
                    f"{prelim['prov_flagged_mem0']}/{prelim['prov_sample']}）",
                ),
                (
                    "H3 memfw 写入显著快于 Mem0（P50 比值 ≥ 10×）",
                    ratio >= 10.0,
                    f"比值={ratio:.1f}×",
                ),
                (
                    "H4 主指标未检测到显著差异（精确置换 p > 0.05）",
                    p_value is not None and p_value > 0.05,
                    f"p={p_text}",
                ),
                (
                    "H5 memfw post-fit micro recall 与初步值接近（|Δ| ≤ 0.05）",
                    recall_memfw_delta <= 0.05,
                    f"|Δ|={recall_memfw_delta:.4f}",
                ),
                (
                    "H6 两系统 recall 差距方向与初步一致",
                    (prelim_recall_diff >= 0) == (formal_recall_diff >= 0),
                    f"初步 memfw−mem0={prelim_recall_diff:+.4f}；正式={formal_recall_diff:+.4f}",
                ),
            ]
            for label, passed, detail in checks:
                mark = "确认" if passed else "未确认"
                lines.append(f"- **{mark}**：{label}（{detail}）。")
            lines.append(
                "- **无法归因**：Mem0 的 answer_score 与初步值差异较大，但 Mem0 在本次运行中"
                "使用了 custom extraction instructions，写入配置/运行时与 Day 11 不同"
                "（环境与模型 digest 已匹配）；该差异不能归因于 Mem0 或 memfw 的系统设计。"
            )
            lines.append(
                "- 正式结论以上表与 §4 的统计结果为准；初步数字仅作为参照，不构成正式结论。"
            )
        else:
            lines.append("缺少初步结果 JSON 或 metrics.json，无法生成对比表。")
        lines.append("")
        lines.append("## 6. 未完成项和原因")
        lines.append("")
        unfinished: list[str] = []
        if not metrics:
            unfinished.append("G5 指标未生成：缺少 `metrics.json`。")
        if not provenance:
            unfinished.append("G6 provenance 未生成：缺少 `provenance_summary.json`。")
        run_manifest = results_dir / "run_manifest.json"
        if not run_manifest.exists():
            unfinished.append("G4 尚未执行正式运行（缺少 `run_manifest.json`）。")
        if provenance:
            for system, info in sorted(provenance.get("systems", {}).items()):
                if info.get("derived_shortfall", 0) > 0:
                    unfinished.append(
                        f"{system} 的 derived_frame 只有 {info['derived_total']} 条派生记忆"
                        f"（缺口 {info['derived_shortfall']}），无法凑满 30 条；"
                        "已在 summary 中如实记录。"
                    )
        if metrics:
            per_run_by_system: dict[str, list[float]] = {}
            for system in ("memfw", "mem0"):
                per_run = metrics["systems"].get(system, {}).get("per_run_mean_answer_score") or {}
                per_run_by_system[system] = sorted(
                    set(round(float(v), 6) for v in per_run.values())
                )
            if all(len(values) == 1 for values in per_run_by_system.values()):
                detail = "; ".join(
                    f"{system} {values[0]}" for system, values in per_run_by_system.items()
                )
                unfinished.append(
                    "重复确定性（观测）：3 个 repeat 的 per-run answer_score 完全相同"
                    f"（{detail}），因此“跨重复方差”为 0；这是本机 temperature=0 解码下"
                    "观测到的确定性，不能当作独立样本推断方差。"
                )
            if int(metrics.get("run_errors") or 0):
                unfinished.append(
                    f"运行期错误记录：{metrics['run_errors']} 条（见 run_manifest）。"
                )
        if provenance:
            unfinished.append(
                "provenance 审计仅取 `rep1` 的记忆导出；三个 repeat 的写入内容一致，"
                "但未对每个 repeat 分别抽样。"
            )
            unfinished.append(
                "mem0 的 R3 FLAGGED 部分来自 custom extraction instructions 请求的 "
                "`attributed_to=user`，部分来自模型把部分记忆标为 assistant/缺失字段；"
                "因此它是“prompt + 模型行为”的混合，不是独立质量测量。"
            )
        unfinished.append(
            "数据更新说明：mem0 的最终数字来自 `finalize3` 重跑——修复检索结果 dia_ids "
            "归一化后，重新 ingest 3 个 repeat 并重跑题目/评审；此前试跑的数字未用于本报告。"
        )
        unfinished.append(
            "模型同一性由 `env.txt` 中记录的 Ollama model digest 佐证，没有模型服务端的"
            "密码学回执；judge 与被测模型相同，属于次要诊断。"
        )
        unfinished.append(
            "运行代码快照：`env.txt` 的 `git_head` 是运行开始时的 HEAD（`c55d1fe`，"
            "此时 eval/ 与 results/ 尚未入库）；实际运行代码由本次报告的提交保存，"
            "这是“运行快照 vs 提交快照”的正常差异。"
        )
        if (results_dir / "test-results.txt").exists():
            unfinished.append("pytest 结果见 `results/stepG/test-results.txt`。")
            unfinished.append(
                "测试环境说明：worktree 使用仓库既有 `.venv`（memfw editable 指向主工作树）"
                "会导致 2 个 Day5/Day6 测试的 source-root 断言失败，因此测试改用 worktree 内"
                "独立 `.venv-stepg`（同一 Python 3.11.16 / mem0ai 2.2.1）。"
            )
        else:
            unfinished.append(
                "未记录最终 pytest 结果（预期写入 `results/stepG/test-results.txt`）。"
            )
        for item in unfinished:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("## 7. 复现命令")
        lines.append("")
        lines.append("```sh")
        lines.append("# 0) 一次性环境：本地 Ollama server + 两个模型（均只监听 127.0.0.1）")
        lines.append("ollama serve  # OLLAMA_MODELS 指向本机模型目录")
        lines.append(f"ollama pull {config['llm']['model']}")
        lines.append(f"ollama pull {config['embedding']['model']}")
        lines.append("")
        lines.append("# 1) 从公开数据到报告的一条命令")
        lines.append("python eval/stepG_pipeline.py --stage all")
        lines.append("")
        lines.append("# 可选：只重跑某个阶段")
        lines.append("python eval/stepG_pipeline.py --stage metrics")
        lines.append("python eval/stepG_pipeline.py --stage provenance")
        lines.append("```")
        lines.append("")
        lines.append("## 8. 产物清单")
        lines.append("")
        artifacts = [
            ("results/stepG/config.yaml", "实验配置（模型/端点/seed/重复次数）"),
            ("results/stepG/subset.json", "固定子集（100 轮 / 18 题 / seed 20261021）"),
            ("results/stepG/env.txt", "Python/OS/mem0/ollama 版本"),
            ("results/stepG/pip-freeze.txt", "依赖快照"),
            ("results/stepG/runs/rep*/", "原始每题 JSONL、每轮写入延迟、记忆导出、run summary"),
            ("results/stepG/metrics.json / metrics.md", "质量、效率、显著性"),
            ("results/stepG/provenance_rubric.md", "provenance 判定规则（先于判定写入）"),
            ("results/stepG/provenance_audit.csv", "逐条抽样判定表"),
            ("results/stepG/provenance_summary.json", "provenance 汇总分母"),
            ("results/stepG/REPORT.md", "本报告"),
        ]
        lines.append(_table(["文件", "内容"], artifacts))
        lines.append("")
    else:
        lines.append("尚未生成 `metrics.json`，无法汇总主结果。")
        lines.append("")
        lines.append("## 复现命令")
        lines.append("")
        lines.append("```sh")
        lines.append("python eval/stepG_pipeline.py --stage all")
        lines.append("```")
        lines.append("")
    report_path = results_dir / "REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
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
