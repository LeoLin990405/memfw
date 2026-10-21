#!/usr/bin/env python3
"""Generate a fixed-seed bilingual, 30-day synthetic memory/capture stream."""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

SEED = 20261018
RECORD_COUNT = 1_000
SPAN_DAYS = 30
START = datetime(2026, 9, 18, tzinfo=UTC)
OUTPUT = Path(__file__).resolve().parents[1] / "reports" / "day3_synthetic.jsonl"

Importance = Literal["important", "normal", "noise"]

THEMES: tuple[tuple[str, float], ...] = (
    ("operations", 0.94),
    ("security", 0.91),
    ("research", 0.82),
    ("product", 0.70),
    ("learning", 0.56),
    ("travel", 0.38),
    ("chores", 0.20),
)

FACTS: dict[str, tuple[tuple[str, str], ...]] = {
    "operations": (
        ("Verify the nightly backup before archive rotation.", "归档轮换前检查夜间备份。"),
        ("Compare replica row counts with the source snapshot.", "将副本行数与源快照对比。"),
        (
            "Keep the rollback image until the health probe succeeds.",
            "健康探测通过前保留回滚镜像。",
        ),
        ("Record queue lag before restarting the worker pool.", "重启工作池前记录队列延迟。"),
        ("Check restore time against the recovery objective.", "对照恢复目标检查还原耗时。"),
        (
            "Drain the oldest shard before changing routing weights.",
            "调整路由权重前先排空最旧分片。",
        ),
        ("Verify the index generation after a clean rebuild.", "干净重建后核对索引代次。"),
        ("Keep the previous manifest while validating checksums.", "验证校验和时保留旧清单。"),
    ),
    "security": (
        (
            "Rotate the service credential after replica health checks.",
            "副本健康检查通过后轮换服务凭据。",
        ),
        ("Review least-privilege rules for the export worker.", "检查导出工作进程的最小权限规则。"),
        (
            "Expire the temporary token after the deployment window.",
            "部署窗口结束后使临时令牌失效。",
        ),
        ("Compare the signed manifest with the approved digest.", "将签名清单与批准的摘要比较。"),
        ("Keep audit metadata when removing a secret payload.", "删除秘密内容时保留审计元数据。"),
        ("Check the certificate chain before enabling the endpoint.", "启用端点前检查证书链。"),
        ("Restrict backup reads to the recovery identity.", "将备份读取权限限制给恢复身份。"),
        ("Record the key version before a staged rotation.", "分阶段轮换前记录密钥版本。"),
    ),
    "research": (
        ("Fix the random seed before comparing held-out runs.", "比较留出集结果前固定随机种子。"),
        (
            "Separate calibration examples from final evaluation data.",
            "将校准样例与最终评测数据分开。",
        ),
        ("Report confidence intervals beside the point estimate.", "在点估计旁报告置信区间。"),
        ("Keep annotation disagreements in the validation sample.", "在验证样本中保留标注分歧。"),
        ("Measure retrieval recall at each candidate cutoff.", "在每个候选截断点测量检索召回率。"),
        ("Record tokenizer versions with each benchmark.", "为每次基准测试记录分词器版本。"),
        ("Compare the baseline on the same frozen inputs.", "在相同冻结输入上比较基线。"),
        ("Keep the exclusion rule fixed across ablations.", "各项消融实验使用相同排除规则。"),
    ),
    "product": (
        (
            "Keep the recovery path visible after a settings change.",
            "设置变更后仍清晰展示恢复入口。",
        ),
        ("Show the current sync source beside the status label.", "在状态标签旁展示当前同步来源。"),
        ("Preserve the draft when validation returns an error.", "校验报错时保留草稿。"),
        ("Place the undo action beside the completed change.", "在已完成的更改旁放置撤销操作。"),
        ("Explain storage limits before the user confirms.", "用户确认前说明存储限制。"),
        (
            "Keep keyboard focus inside the open confirmation dialog.",
            "确认对话框打开时保留键盘焦点。",
        ),
        ("Show which source fields remain after a merge.", "合并前展示将保留哪些来源字段。"),
        ("Allow the user to inspect the exact proposed diff.", "允许用户查看准确的提议差异。"),
    ),
    "learning": (
        ("Review a short example before explaining the method.", "解释方法前先看一个简短例子。"),
        ("Write the definition before attempting the proof.", "尝试证明前先写出定义。"),
        ("Check an edge case after solving the main example.", "完成主例题后检查一个边界情况。"),
        ("Summarize feedback in a single revision note.", "用一条修订笔记总结反馈。"),
        ("Compare the diagrams before memorizing the labels.", "记忆标签前先比较图示。"),
        ("Use a counterexample to test the general claim.", "用反例检验一般性结论。"),
        ("Explain each step without skipping unit conversion.", "解释每个步骤时不要跳过单位换算。"),
        ("Keep the source page beside the extracted definition.", "将来源页与提取的定义放在一起。"),
    ),
    "travel": (
        ("Check transfer time before booking the evening train.", "预订晚班火车前检查换乘时间。"),
        ("Keep the address in the local language for the driver.", "为司机保留当地语言地址。"),
        ("Verify the last bus time before choosing the hotel.", "选择酒店前确认末班公交时间。"),
        ("Save the platform number with the departure time.", "把站台号和出发时间一起保存。"),
        ("Check baggage rules for the second carrier.", "检查第二家承运方的行李规则。"),
        ("Leave extra time between the airport and station.", "机场和车站之间预留额外时间。"),
        ("Keep an offline map for the final walking segment.", "为最后一段步行保留离线地图。"),
        ("Confirm the return ticket uses the correct date.", "确认返程票使用了正确日期。"),
    ),
    "chores": (
        ("Buy tea and water the small plant by the window.", "买茶叶并给窗边的小盆栽浇水。"),
        ("Replace the kitchen sponge after cleaning the counter.", "清洁台面后更换厨房海绵。"),
        ("Put the spare batteries in the desk drawer.", "把备用电池放进书桌抽屉。"),
        ("Check the milk date before writing the grocery list.", "写购物清单前检查牛奶日期。"),
        ("Hang the clean towel beside the bathroom shelf.", "把干净毛巾挂在浴室架旁。"),
        ("Return the borrowed book before the weekend.", "周末前归还借来的书。"),
        ("Charge the small lamp before the evening call.", "晚间通话前给小台灯充电。"),
        ("Move the recycling bin out after dinner.", "晚饭后把回收桶移出去。"),
    ),
}


@dataclass(frozen=True, slots=True)
class SyntheticRecord:
    memory_id: str
    timestamp: str
    theme: str
    topic_strength: float
    importance: Importance
    text: str
    duplicate_group: str | None
    capture_intensity: float
    awake: bool


def _awake_at(timestamp: datetime) -> bool:
    hour = timestamp.hour + timestamp.minute / 60
    return 8.5 <= hour < 11.5 or 13.5 <= hour < 17.5 or 20.0 <= hour < 22.0


def generate_dataset(*, seed: int = SEED, count: int = RECORD_COUNT) -> tuple[SyntheticRecord, ...]:
    """Return reproducible records; importance probability follows topic strength."""

    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    records: list[SyntheticRecord] = []
    for index in range(count):
        timestamp = START + timedelta(seconds=SPAN_DAYS * 86400 * index / count)
        theme, strength = THEMES[(index * len(THEMES)) // count % len(THEMES)]
        duplicate_group = f"near-{index // 2:04d}" if index < int(count * 0.4) else None
        draw = rng.random()
        awake = _awake_at(timestamp)
        intensity = round(rng.uniform(0.68, 1.0) if awake else rng.uniform(0.0, 0.28), 6)
        important_probability = 0.08 + 0.60 * strength + 0.22 * intensity
        noise_probability = 0.10 + 0.32 * (1.0 - strength) * (1.0 - intensity)
        if draw < important_probability:
            importance: Importance = "important"
        elif draw > 1.0 - noise_probability:
            importance = "noise"
        else:
            importance = "normal"
        pair_index = index // 2
        variant_rng = random.Random(
            seed + 17_003 * pair_index if duplicate_group else seed + 29_001 * index
        )
        english, chinese = variant_rng.choice(FACTS[theme])
        if duplicate_group is not None and index % 2:
            english = english.replace("before", "ahead of", 1)
            chinese = chinese.rstrip("。") + "；执行前再次确认。"
        size_index = pair_index if duplicate_group else index
        target_bytes = 3_500 + (size_index * 7_919 + 11_337) % 6_501
        trace_count = max(80, target_bytes // 9)
        trace_rng = random.Random(
            seed + pair_index * 101 if duplicate_group else seed + index * 1009
        )
        trace_items = [
            "trace-" + "".join(trace_rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(12))
            for _ in range(trace_count)
        ]
        text = f"[{theme}] {english}\n{chinese}\nSynthetic event trace: {' '.join(trace_items)}"
        records.append(
            SyntheticRecord(
                memory_id=f"d3-{index:04d}",
                timestamp=timestamp.isoformat(),
                theme=theme,
                topic_strength=strength,
                importance=importance,
                text=text,
                duplicate_group=duplicate_group,
                capture_intensity=intensity,
                awake=awake,
            )
        )
    return tuple(records)


def write_capture_stream(
    path: Path,
    *,
    records: tuple[SyntheticRecord, ...] | None = None,
) -> Path:
    """Write TimedSignal-compatible JSONL, including an explicit end boundary."""

    rows = records if records is not None else generate_dataset()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": row.timestamp,
                "source": "model",
                "action": "content",
                "intensity": row.capture_intensity,
                "text": row.text,
                "source_id": row.memory_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for row in rows
    ]
    if rows:
        end = datetime.fromisoformat(rows[-1].timestamp) + timedelta(
            seconds=SPAN_DAYS * 86400 / len(rows)
        )
        lines.append(
            json.dumps(
                {
                    "type": "end",
                    "timestamp": end.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_dataset(
    path: Path = OUTPUT,
    *,
    seed: int = SEED,
    capture_output: Path | None = None,
) -> Path:
    rows = generate_dataset(seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n" for record in rows
    )
    path.write_text(payload, encoding="utf-8")
    stream_path = capture_output or path.with_name(f"{path.stem}.capture.jsonl")
    write_capture_stream(stream_path, records=rows)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--capture-output", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    path = write_dataset(args.output, seed=args.seed, capture_output=args.capture_output)
    print(f"wrote {RECORD_COUNT} records to {path}")
    capture_path = args.capture_output or path.with_name(f"{path.stem}.capture.jsonl")
    print(f"wrote capture stream to {capture_path}")


if __name__ == "__main__":
    main()
