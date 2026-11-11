"""Score ``memfw dream``'s curate step (operation ②) on the synthetic gold set.

Writes ``dreams_report.json`` with two blocks: the gold scored against itself
(a sanity check of the metrics) and the real dream's proposals.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.dreams.cases import cases
from eval.dreams.curate_adapter import predict_all
from eval.dreams.metrics import score_cases

_KEYS = ("merge_accuracy", "conflict_detection_rate", "false_merge_rate", "stale_mark_recall")


def measure() -> dict[str, object]:
    gold = cases()
    self_score = score_cases(gold, gold)
    dream = score_cases(predict_all(gold), gold, implemented=True)
    dream["note"] = (
        "memfw.dream.curate proposals on the synthetic cases. adopted_proposal_rate "
        "stays null until a person adopts proposals."
    )
    return {"gold_self_score": self_score, "dream": dream}


def main() -> int:
    report = measure()
    out = Path(__file__).with_name("dreams_report.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    dream = report["dream"]
    assert isinstance(dream, dict)
    print(" ".join(f"{key}={dream[key]}" for key in _KEYS))
    ok = dream["false_merge_rate"] == 0 and dream["adopted_proposal_rate"] is None
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
