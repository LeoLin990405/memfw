#!/usr/bin/env python3
"""Compare matrix reports after removing runtime and run-location metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from day5_storage_matrix import semantic_result_hash  # noqa: E402


def load_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"matrix report must be a JSON object: {path}")
    return cast(dict[str, Any], value)


def compare(left_path: Path, right_path: Path) -> dict[str, Any]:
    left = load_report(left_path)
    right = load_report(right_path)
    left_hash = semantic_result_hash(left)
    right_hash = semantic_result_hash(right)
    return {
        "left": str(left_path),
        "right": str(right_path),
        "left_semantic_sha256": left_hash,
        "right_semantic_sha256": right_hash,
        "semantic_match": left_hash == right_hash,
        "excluded_fields": sorted(
            {
                "runtime_seconds",
                "elapsed_seconds",
                "wall_seconds",
                "started_utc",
                "finished_utc",
                "checkpoint_file",
                "work_directory_persisted",
            }
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    result = compare(args.left, args.right)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["semantic_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
