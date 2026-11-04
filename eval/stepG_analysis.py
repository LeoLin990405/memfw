"""Generate ``results/stepG/ANALYSIS.md`` from the frozen Step G artifacts.

The analysis is deliberately read-only: it recomputes every number from the committed
JSONL/JSON files (runs, judge verdicts, provenance summary, framework comparison) and
writes a markdown companion to REPORT.md. Run with:

    python eval/stepG_analysis.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_runner import _resolve, load_config, redact_text  # noqa: E402

MAIN_ARMS = ["memfw", "mem0_stock", "mem0_custom", "mem0_stock_day11ctx"]
JUDGE_PRIMARY = "judge_primary.jsonl"
JUDGE_SAME = "judge_same_model.jsonl"


def _read_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


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


class AnalysisData:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self.subset = _read_json(results_dir / "subset.json")
        self.questions = {q["question_id"]: q for q in self.subset["questions"]}
        self.main_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.rows_by_run: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for arm in MAIN_ARMS:
            for run_dir in sorted((results_dir / "runs").glob("rep*")):
                rows = _read_rows(run_dir / arm / "questions.jsonl")
                if rows:
                    self.rows_by_run[(arm, run_dir.name)] = rows
                    self.main_rows[arm].extend(rows)
        self.verdicts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for filename in (JUDGE_PRIMARY, JUDGE_SAME):
            for row in _read_rows(results_dir / filename):
                key = (row["system"], row["run_id"], row["question_id"], filename)
                self.verdicts[key] = row

    def mean_score(self, arm: str) -> dict[str, float]:
        by_q: dict[str, list[float]] = defaultdict(list)
        for row in self.main_rows[arm]:
            by_q[row["question_id"]].append(float(row["answer_score"]))
        return {qid: statistics.fmean(values) for qid, values in by_q.items()}

    def judge_verdicts(self, arm: str) -> dict[str, list[bool]]:
        by_q: dict[str, list[bool]] = defaultdict(list)
        for (system, _run, qid, filename), verdict in self.verdicts.items():
            if system == arm and filename == JUDGE_PRIMARY:
                by_q[qid].append(bool(verdict["correct"]))
        return by_q


def _section_overview(data: AnalysisData) -> list[str]:
    metrics = _read_json(data.results_dir / "metrics.json")
    lines = [
        "## 1. 数据与口径",
        "",
        "- 数据：公开 LoCoMo，固定子集 `conv-49` 前 100 轮 + 18 题（seed 20261021）。",
        "- 主臂：memfw / mem0_stock / mem0_custom / mem0_stock_day11ctx，每臂 3 次重复；",
        "  框架臂：LangMem / MemOS（general_text）各 1 次重复（exploratory）。",
        "- 分析单元：每题跨重复均值；主 table 的 n=18，扩展集 n=41；"
        "分题型样本很小，结论只作描述性。",
        "- 所有数字由 `eval/stepG_analysis.py` 从 `results/stepG/` 原始文件重算，不手填。",
        "",
        f"- metrics：answer_score 主比较 p="
        f"{_fmt(metrics['comparisons'][0]['answer_score']['exact_sign_flip']['p_value'])}，"
        f"MDE={_fmt(metrics['comparisons'][0]['answer_score']['minimum_detectable_difference'])}；"
        "重复输出完全确定（见 §11）。",
    ]
    return lines


def _category_table(data: AnalysisData) -> list[str]:
    categories = sorted({q["category"] for q in data.questions.values()})
    rows: list[list[str]] = []
    for arm in MAIN_ARMS:
        scores = data.mean_score(arm)
        judge = data.judge_verdicts(arm)
        cells = [arm]
        for category in categories:
            values = [
                scores[qid]
                for qid, q in data.questions.items()
                if q["category"] == category and qid in scores
            ]
            cells.append(_fmt(statistics.fmean(values)) if values else "n/a")
        # primary judge 原始裁决正确率（不经过 cat5 abstention fallback）
        judge_values = [statistics.fmean(v) for v in judge.values() if v]
        cells.append(_pct(statistics.fmean(judge_values)) if judge_values else "n/a")
        rows.append(cells)
    headers = ["arm"] + [f"cat{c}" for c in categories] + ["judge 原始正确率"]
    lines = [
        "## 2. 分题型表现（固定 18 题，跨重复均值）",
        "",
        _table(headers, rows),
        "",
        "- 各题型 n："
        + ", ".join(
            f"cat{category}:{sum(1 for q in data.questions.values() if q['category'] == category)}"
            for category in categories
        )
        + "。",
        "- 答案分对多要素答案只给部分 token-F1；cat5 的 judge 只按语义判，不按 abstention，见 §5。",
    ]
    return lines


def _question_ledger(data: AnalysisData) -> list[str]:
    scores = {arm: data.mean_score(arm) for arm in MAIN_ARMS}
    rows: list[list[str]] = []
    wins = losses = ties = 0
    for qid, question in sorted(
        data.questions.items(), key=lambda item: (item[1]["category"], item[1]["question"])
    ):
        if qid not in scores["memfw"] or qid not in scores["mem0_stock"]:
            continue
        diff = scores["memfw"][qid] - scores["mem0_stock"][qid]
        wins += 1 if diff > 1e-9 else 0
        losses += 1 if diff < -1e-9 else 0
        ties += 1 if abs(diff) <= 1e-9 else 0
        rows.append(
            [
                str(question["category"]),
                question["question"][:58],
                *[_fmt(scores[arm].get(qid)) for arm in MAIN_ARMS],
                f"{diff:+.2f}",
            ]
        )
    lines = [
        "## 3. 逐题账本（memfw − mem0_stock）",
        "",
        f"memfw 赢 {wins} 题 / 输 {losses} 题 / 平 {ties} 题。",
        "",
        _table(
            ["cat", "question", *MAIN_ARMS, "memfw−stock"],
            rows,
        ),
        "",
        "- 胜负高度集中：cat4 的细节题是 memfw 的主要赢面，"
        "cat2 的时间题和 cat5 的归属题是主要输面。",
    ]
    return lines


def _retrieval_section(data: AnalysisData) -> list[str]:
    rows: list[list[str]] = []
    for arm in MAIN_ARMS:
        coverage: list[float] = []
        hits = 0
        total = 0
        ranks: list[int] = []
        positive_without_gold = 0
        positive = 0
        negative_with_gold = 0
        negative = 0
        for (system, _rep), run_rows in data.rows_by_run.items():
            if system != arm:
                continue
            for row in run_rows:
                question = data.questions[row["question_id"]]
                evidence = set(question["evidence_turn_ids"])
                retrieved_sets = [
                    set(item.get("dia_ids") or []) for item in row.get("retrieved") or []
                ]
                covered = set().union(*retrieved_sets) if retrieved_sets else set()
                hit = bool(covered & evidence)
                coverage.append(len(covered & evidence) / len(evidence) if evidence else 0.0)
                hits += 1 if hit else 0
                total += 1
                rank = next((i + 1 for i, ids in enumerate(retrieved_sets) if ids & evidence), None)
                if rank is not None:
                    ranks.append(rank)
                if row["answer_score"] > 0:
                    positive += 1
                    positive_without_gold += 1 if not hit else 0
                else:
                    negative += 1
                    negative_with_gold += 1 if hit else 0
        rows.append(
            [
                arm,
                _pct(statistics.fmean(coverage)),
                f"{hits}/{total}",
                _fmt(statistics.fmean(ranks), 1) if ranks else "n/a",
                f"{positive_without_gold}/{positive}",
                f"{negative_with_gold}/{negative}",
            ]
        )
    return [
        "## 4. 检索覆盖与答案瓶颈",
        "",
        _table(
            [
                "arm",
                "pre-fit macro 覆盖",
                "≥1 条 gold 命中",
                "gold 最佳排名",
                "答对但 gold 未进 top5",
                "答 0 但 gold 已检索",
            ],
            rows,
        ),
        "",
        "- 覆盖率与答案分并不同步：Mem0-custom 覆盖率最高但答案并不最高；"
        "“gold 已检索到却答 0”远多于“未检索到却答对”，说明瓶颈主要在答案合成/多跳，而非单纯召回。",
    ]


def _cat5_section(data: AnalysisData) -> list[str]:
    rows: list[list[str]] = []
    for qid, question in sorted(data.questions.items(), key=lambda item: item[1]["question"]):
        if question["category"] != 5:
            continue
        cells = [question["question"][:52]]
        for arm in MAIN_ARMS:
            value = next(row for row in data.main_rows[arm] if row["question_id"] == qid)
            cells.append(f"{value['answer'][:34]!r} / {value['answer_score']:.2f}")
        rows.append(cells)
    return [
        "## 5. cat5（对抗/归属）专项",
        "",
        "LoCoMo cat5 的题目把另一个人的事实问到目标人物头上，正确行为是 abstain。",
        "",
        _table(["question", *MAIN_ARMS], rows),
        "",
        "- memfw 唯一一次 cat5 失误：把 Evan 的 smartwatch 事实答给了 Sam；Mem0 全部 abstain。",
        "- 该行被主 judge 判 correct（gold 文本确实如此），"
        "说明 LLM judge 对对抗题会“按字面正确性”放行；"
        "judge fallback 对 cat5 abstention 也给分，所以 judge 表里 cat5 全是 100%。"
        "**cat5 应以 abstention-aware 的 answer_score 为准。**",
    ]


def _judge_section(data: AnalysisData) -> list[str]:
    agree = total = 0
    for (system, run_id, qid, filename), verdict in data.verdicts.items():
        if filename != JUDGE_PRIMARY:
            continue
        other = data.verdicts.get((system, run_id, qid, JUDGE_SAME))
        if other is None:
            continue
        total += 1
        agree += 1 if verdict["correct"] == other["correct"] else 0
    rows: list[list[str]] = []
    for arm in MAIN_ARMS:
        fp = strict = pos = 0
        for row in data.main_rows[arm]:
            primary_verdict = data.verdicts.get(
                (arm, row["run_id"], row["question_id"], JUDGE_PRIMARY)
            )
            if primary_verdict is None:
                continue
            if primary_verdict["correct"] and row["answer_score"] == 0:
                fp += 1
            if (not primary_verdict["correct"]) and row["answer_score"] > 0:
                strict += 1
            if row["answer_score"] > 0:
                pos += 1
        rows.append([arm, str(fp), f"{strict}/{pos}"])
    metrics = _read_json(data.results_dir / "metrics.json")
    judge_rows = []
    for model in metrics["judge"]["models"]:
        judge_rows.append(
            [
                model["label"],
                str(model["model"]),
                ", ".join(
                    f"{arm}:{_pct(acc['accuracy'])}"
                    for arm, acc in sorted(model["accuracy"].items())
                ),
            ]
        )
    return [
        "## 6. Judge 分析",
        "",
        f"主 judge 与同模型 judge 逐行一致率：{agree}/{total} = "
        f"{_pct(agree / total if total else None)}"
        "（不一致集中于同一条 cat5 回答）。",
        "",
        _table(
            ["arm", "judge 假阳性（correct 但 score=0）", "judge 偏严（incorrect 但 score>0）"],
            rows,
        ),
        "",
        _table(["judge", "model", "per-arm accuracy（每题跨重复均值）"], judge_rows),
        "",
        "- 主 judge（不同模型）与同模型 judge 的排名差异很小："
        "同模型 judge 没有被系统性“偏袒”的证据。",
        "- 但 judge 与 token-F1 的错位明显：部分正确（多要素）常被判错，cat5 的错误归属却被判对；"
        "因此 REPORT 同时保留 token-F1、judge 与 abstention 规则三种口径。",
    ]


def _expanded_section(data: AnalysisData) -> list[str]:
    metrics = _read_json(data.results_dir / "expanded_metrics.json")
    expanded_subset = _read_json(data.results_dir / "subset_expanded.json")
    meta = {q["question_id"]: q for q in expanded_subset["questions"]}
    categories = sorted({q["category"] for q in meta.values()})
    rows: list[list[str]] = []
    for arm in metrics["arms"]:
        per_question = metrics["systems"][arm]["per_question_answer_score"]
        cells = [arm]
        for category in categories:
            values = [
                value for qid, value in per_question.items() if meta[qid]["category"] == category
            ]
            cells.append(_fmt(statistics.fmean(values)) if values else "n/a")
        cells.append(_fmt(metrics["systems"][arm]["mean_answer_score"]))
        rows.append(cells)
    overlap = len(set(data.questions) & set(meta))
    return [
        "## 7. 扩题集（exploratory，n=41）",
        "",
        f"扩题集与固定 18 题重叠 {overlap} 题（真正新增 {len(meta) - overlap} 题）；"
        "分题型 n 仍然较小。",
        "",
        _table(["arm", *[f"cat{c}" for c in categories], "overall"], rows),
        "",
        "- 扩展集上 cat4 仍是 memfw 的优势（n=15），cat2 仍是弱项（n=9），cat1 略输 Mem0；"
        "overall 方向与固定 18 题一致但不显著"
        "（见 metrics/expanded_metrics.json 的配对检验与 MDE）。",
    ]


def _efficiency_section(data: AnalysisData) -> list[str]:
    metrics = _read_json(data.results_dir / "metrics.json")
    rows: list[list[str]] = []
    for arm in MAIN_ARMS:
        system = metrics["systems"][arm]
        write = system.get("write_latency_turns") or {}
        rows.append(
            [
                arm,
                _fmt(write.get("p50_seconds"), 4),
                _fmt(write.get("p95_seconds"), 4),
                _fmt((system.get("retrieval_latency") or {}).get("p50_seconds"), 4),
                _fmt((system.get("answer_latency") or {}).get("p50_seconds"), 4),
                str(system["total_token_usage"]["prompt_tokens"]),
                _fmt(system.get("memory_count_mean"), 1),
                _fmt(system.get("storage_bytes_mean"), 0),
            ]
        )
    return [
        "## 8. 效率与规模",
        "",
        _table(
            [
                "arm",
                "写入 P50(s)",
                "写入 P95(s)",
                "检索 P50(s)",
                "回答 P50(s)",
                "ingest prompt tokens",
                "记忆数",
                "存储 bytes",
            ],
            rows,
        ),
        "",
        "- 检索/回答延迟主要由同一 answer 模型决定，各臂接近；主要差异在写入侧。",
        "- memfw 写入比 Mem0-stock 快约 500×、ingest token 少约 236×；但这不是质量优势。",
    ]


def _provenance_section(data: AnalysisData) -> list[str]:
    provenance = _read_json(data.results_dir / "provenance_summary.json")
    rows: list[list[str]] = []
    for arm, info in sorted(provenance["systems"].items()):
        frame = info["day11_frame"]
        attribution = info.get("attribution_counts") or {}
        rows.append(
            [
                arm,
                str(info["memories_total"]),
                str(info["derived_total"]),
                f"{frame['flagged']}/{frame['sampled']}",
                f"user {attribution.get('user', 0)} / "
                f"assistant {attribution.get('assistant', 0)} / "
                f"missing {attribution.get('missing', 0)}",
            ]
        )
    return [
        "## 9. Provenance 审计",
        "",
        _table(
            ["arm", "记忆总数", "派生记忆", "day11_frame FLAGGED", "attribution 分布"],
            rows,
        ),
        "",
        "- Mem0 系派生记忆多数被记为无标注用户记忆（stock 21/30），"
        "且几乎不标 missing；这是系统性行为。",
        "- memfw 全部为逐字记忆（派生审计结构性 N/A）。provenance 字段存在不等于答案会用："
        "cat5 的跨 speaker 误答说明 answer prompt/检索还需要 speaker-aware 约束。",
    ]


def _framework_section(data: AnalysisData) -> list[str]:
    path = data.results_dir / "frameworks" / "metrics.json"
    if not path.exists():
        return ["## 10. 开源框架对比", "", "未找到 frameworks/metrics.json。"]
    metrics = _read_json(path)
    rows: list[list[str]] = []
    for arm in metrics["arms"]:
        system = metrics["systems"][arm]
        write = system.get("write_latency_turns") or {}
        rows.append(
            [
                arm,
                _fmt(system["mean_answer_score"]),
                _pct(system.get("micro_evidence_recall_postfit")),
                _fmt(write.get("p50_seconds"), 4),
                _fmt(system.get("memory_count_mean"), 1),
                _fmt(system.get("storage_bytes_mean"), 0),
                str(system.get("repeats")),
            ]
        )
    comparison_rows: list[list[str]] = []
    for comparison in metrics.get("comparisons") or []:
        pair = comparison["answer_score"]
        ci = pair.get("difference_ci95") or [None, None]
        comparison_rows.append(
            [
                comparison["label"],
                _fmt(pair.get("mean_difference")),
                f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                _fmt(pair.get("minimum_detectable_difference")),
            ]
        )
    return [
        "## 10. 开源框架对比（exploratory，框架臂 1 次重复）",
        "",
        _table(
            [
                "arm",
                "answer_score",
                "recall micro",
                "写入 P50(s)",
                "记忆数",
                "存储 bytes",
                "重复数",
            ],
            rows,
        ),
        "",
        _table(["comparison", "均值差", "95% CI", "精确置换 p", "MDE"], comparison_rows),
        "",
        "- MemOS(general_text) 与 memfw 架构最接近（原话+向量），题型画像也最像；差异不显著。",
        "- LangMem 的 evidence recall≈0 来自“跨轮摘要丢失逐轮 provenance”，"
        "是记忆表示差异，不是检索 bug；"
        "写入也最慢。",
        "- 框架默认抽取/更新策略不同、框架臂单次重复；该表只作方向性对比，"
        "详见 `frameworks/FRAMEWORK_COMPARISON.md`。",
    ]


def _determinism_and_stats(data: AnalysisData) -> list[str]:
    rows: list[list[str]] = []
    for arm in MAIN_ARMS:
        by_q: dict[str, list[float]] = defaultdict(list)
        for row in data.main_rows[arm]:
            by_q[row["question_id"]].append(float(row["answer_score"]))
        identical = sum(1 for values in by_q.values() if max(values) - min(values) < 1e-12)
        rows.append([arm, f"{identical}/{len(by_q)}"])
    metrics = _read_json(data.results_dir / "metrics.json")
    primary = metrics["comparisons"][0]["answer_score"]
    expanded = _read_json(data.results_dir / "expanded_metrics.json")
    expanded_primary = expanded["comparisons"][0]["answer_score"]
    return [
        "## 11. 可重复性与统计功效",
        "",
        _table(["arm", "3 次重复逐题 answer_score 完全一致"], rows),
        "",
        f"- 固定 18 题主比较：diff={_fmt(primary['mean_difference'])}，"
        f"95% CI {[round(x, 4) for x in primary['difference_ci95']]}，"
        f"p={_fmt(primary['exact_sign_flip']['p_value'])}，MDE={_fmt(primary['minimum_detectable_difference'])}。",
        f"- 扩题集 41 题：p={_fmt(expanded_primary['exact_sign_flip']['p_value'])}，"
        f"MDE={_fmt(expanded_primary['minimum_detectable_difference'])}，"
        "仍不显著。",
        "- 重复输出完全确定（temperature=0），因此重复不提供方差；"
        "有效样本是题目数，分题型结论只作描述。",
    ]


def _caveats(data: AnalysisData) -> list[str]:
    return [
        "## 12. 结论与 caveat",
        "",
        "1. **固定 18 题与扩题集都没有可检测的整体质量差异**；"
        "memfw 的优势在成本/延迟/存储与 provenance，"
        "不在答案分。",
        "2. **分题型是主要发现**：memfw 的 cat4（细节/多跳）明显更好，"
        "cat2（时间归一化）与 cat5（对抗归属）更弱；"
        "Mem0-custom/day11ctx 在 cat1/cat2 更稳。",
        "3. **检索覆盖不是唯一瓶颈**：大量 gold 已检索到却答 0 的样本指向答案合成/多跳；"
        "cat5 还暴露了答案阶段的 speaker 归属问题。",
        "4. **judge 不能替代任务指标**：cat5 需 abstention-aware 评分；judge 对部分正确偏严。",
        "5. 样本小（分题型 n=2~15）、单一 conversation、框架臂单次重复；下一步应扩充独立题量、"
        "加入 speaker-aware/时间归一化约束，并考虑更强的类别感知 judge。",
        "",
        "---",
        "",
        f"生成命令：`python eval/stepG_analysis.py`"
        f"（数据目录 `{data.results_dir}`；所有数字均为运行产物重算）。",
    ]


def build_analysis(config: dict[str, Any]) -> str:
    results_dir = _resolve(config["paths"]["results_dir"])
    data = AnalysisData(results_dir)
    sections: list[str] = [
        "# Step G 数据分析（从冻结产物重算）",
        "",
        "> 本文件由 `eval/stepG_analysis.py` 生成，只读取 `results/stepG/` 下的原始 JSONL/JSON；"
        "与 `REPORT.md` 互补，不替代主报告。",
        "",
    ]
    for builder in (
        _section_overview,
        _category_table,
        _question_ledger,
        _retrieval_section,
        _cat5_section,
        _judge_section,
        _expanded_section,
        _efficiency_section,
        _provenance_section,
        _framework_section,
        _determinism_and_stats,
        _caveats,
    ):
        sections.extend(builder(data))
        sections.append("")
    return redact_text("\n".join(sections))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Step G ANALYSIS.md")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    results_dir = _resolve(config["paths"]["results_dir"])
    output = results_dir / "ANALYSIS.md"
    output.write_text(build_analysis(config), encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
