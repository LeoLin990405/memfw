#!/usr/bin/env python3
"""Compare fixed xmin=1 and automatic xmin on the saved 1k access-count sample."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memfw.stats.tail_fit import fit_discrete_tail


def run_comparison(
    input_path: Path,
    *,
    bootstrap_samples: int = 200,
    seed: int = 20261018,
    minimum_sample: int = 20,
) -> dict[str, Any]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    values = payload.get("access_counts")
    if not isinstance(values, list) or not all(type(value) is int for value in values):
        raise ValueError("input must contain integer `access_counts`")
    common = {
        "minimum_sample": minimum_sample,
        "bootstrap_samples": bootstrap_samples,
        "seed": seed,
    }
    return {
        "input_path": str(input_path),
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "records": len(values),
        "method": (
            "Exact discrete zeta alpha MLE by golden-section search; "
            "Hurwitz-zeta normalizer uses Euler-Maclaurin approximation."
        ),
        "same_access_count_sample": True,
        "bootstrap_samples_requested": bootstrap_samples,
        "seed": seed,
        "fits": {
            "fixed_xmin_1": asdict(fit_discrete_tail(values, xmin=1, **common)),
            "automatic_xmin": asdict(fit_discrete_tail(values, **common)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--minimum-sample", type=int, default=20)
    args = parser.parse_args()
    result = run_comparison(
        args.input,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
        minimum_sample=args.minimum_sample,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "records": result["records"]}, sort_keys=True))


if __name__ == "__main__":
    main()
