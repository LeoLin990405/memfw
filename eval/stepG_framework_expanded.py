"""Evaluate existing framework memory stores on the expanded 41-question set.

The framework arms already ingested the same 100 turns for the fixed comparison. This script
reuses those stores (no re-ingest) and only answers the expanded questions, so the framework
and memfw architecture comparisons share the same question set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_frameworks import make_framework_system  # noqa: E402
from eval.stepG_metrics import judge_model_config, run_judge  # noqa: E402
from eval.stepG_runner import (  # noqa: E402
    _read_rows,
    _resolve,
    load_config,
    make_chat,
    run_system,
)


def run_expanded(
    config: dict[str, Any],
    *,
    arms: list[str],
    limit_questions: int | None = None,
    skip_run: bool = False,
) -> None:
    results_dir = _resolve(config["paths"]["results_dir"])
    frameworks_dir = results_dir / "frameworks"
    subset = json.loads((results_dir / "subset_expanded.json").read_text(encoding="utf-8"))
    questions = subset["questions"]
    if limit_questions:
        questions = questions[:limit_questions]
    chat = make_chat(config)
    for arm in [] if skip_run else arms:
        runtime_root = frameworks_dir / "runtime" / arm
        if not runtime_root.exists():
            raise FileNotFoundError(
                f"missing ingested framework runtime for {arm}: {runtime_root}; "
                "run the fixed comparison first"
            )
        run_dir = frameworks_dir / "expanded" / "runs" / arm / "rep1"
        system = make_framework_system(arm, runtime_root, config)
        print(f"[framework-expanded] {arm}: {len(questions)} questions (reusing store)", flush=True)
        run_system(
            system=system,
            system_name=arm,
            run_id="rep1",
            turns=[],
            questions=questions,
            config=config,
            run_dir=run_dir,
            chat=chat,
            log=lambda message: print(message, flush=True),
        )
    judge_cfg = judge_model_config(config, "judge")
    if judge_cfg:
        rows_by_arm = {
            arm: {
                "rep1": _read_rows(
                    frameworks_dir / "expanded" / "runs" / arm / "rep1" / "questions.jsonl"
                )
            }
            for arm in arms
        }
        run_judge(
            config,
            rows_by_arm,
            out_path=frameworks_dir / "expanded" / "judge_primary.jsonl",
            judge_config=judge_cfg,
            label="framework-expanded",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Framework arms on the expanded question set")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--arms", default="langmem,memoryos")
    parser.add_argument("--limit-questions", type=int, default=None)
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]
    run_expanded(
        config,
        arms=arms,
        limit_questions=args.limit_questions,
        skip_run=args.skip_run,
    )
    print(json.dumps({"arms": arms}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
