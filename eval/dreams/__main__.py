"""Write the gold self-score. This does not implement ``memfw dream``."""

from __future__ import annotations

import json
from pathlib import Path

from eval.dreams.cases import cases
from eval.dreams.metrics import score_cases


def measure() -> dict[str, object]:
    gold = cases()
    report = score_cases(gold, gold)
    report["note"] = (
        "Gold scored against itself. A dream implementation is not in this package. "
        "adopted_proposal_rate stays null until a person adopts proposals."
    )
    return report


def main() -> int:
    report = measure()
    out = Path(__file__).with_name("dreams_report.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"merge_accuracy={report['merge_accuracy']} "
        f"conflict_detection_rate={report['conflict_detection_rate']} "
        f"false_merge_rate={report['false_merge_rate']} "
        f"stale_mark_recall={report['stale_mark_recall']}"
    )
    ok = (
        report["merge_accuracy"] == 1
        and report["conflict_detection_rate"] == 1
        and report["false_merge_rate"] == 0
        and report["stale_mark_recall"] == 1
        and report["adopted_proposal_rate"] is None
        and report["dream_implemented"] is False
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
