"""Write the R0 baseline record. The question count comes from the power analysis."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from eval.retrieval_r0.baseline import build_report, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic R0 retrieval baseline")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval/retrieval_r0/baseline_report.json"),
    )
    parser.add_argument("--seed", type=int, default=20261108)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="memfw-r0-") as temporary:
        report = build_report(Path(temporary), seed=args.seed, verbose=True)
        write_report(args.out, report)
    power = report["power"]
    print(
        f"per_class={report['per_class']} meets_power={power['meets_power_target']} "
        f"estimated_power={power['estimated_power']}"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
