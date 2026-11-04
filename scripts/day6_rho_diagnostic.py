"""Run small, current-source access-correlation diagnostics without legacy builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from day5_storage_matrix import POLICIES, _write_fixture  # noqa: E402
from day5_storage_probe import build_base, run_budget  # noqa: E402

RHOS = (0.0, 0.3, 0.6)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def run(output_dir: Path, work_dir: Path, *, seed: int, count: int) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory must be fresh: {output_dir}")
    if work_dir.exists() and any(work_dir.iterdir()):
        raise FileExistsError(f"work directory must be fresh: {work_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "seed": seed,
        "count": count,
        "body_target_bytes": 1_100,
        "budget_bytes": 300_000,
        "important_prevalence": 0.10,
        "source_role": "Day6 current workspace source only; no Day5 comparison baseline",
        "profiles": [],
    }
    checkpoint_path = output_dir / "day6_rho_diagnostic_checkpoint.json"
    _write_json(checkpoint_path, result)
    for rho in RHOS:
        rho_name = f"rho{int(rho * 10):02d}"
        rho_fixture_dir = work_dir / "fixtures" / rho_name
        docs_path, accesses_path, labels_path, metadata = _write_fixture(
            rho_fixture_dir,
            count=count,
            seed=seed,
            target_body_bytes=1_100,
            rho_access=rho,
            important_prevalence=0.10,
        )
        base_dir = work_dir / "bases" / rho_name
        build_receipt = build_base(base_dir, docs_path, accesses_path, ROOT / "src")
        completed: dict[str, Any] = {
            "rho_access_requested": rho,
            "rho_metadata": metadata,
            "fixture_sha256": {
                "documents": _sha256(docs_path),
                "accesses": _sha256(accesses_path),
                "labels": _sha256(labels_path),
            },
            "build_receipt": build_receipt,
            "policies": {},
        }
        for policy in POLICIES:
            profile_dir = work_dir / "profiles" / rho_name / policy
            profile_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(base_dir, profile_dir)
            started = time.monotonic()
            profile = run_budget(
                profile_dir,
                source_root=ROOT / "src",
                policy=policy,
                budget_bytes=300_000,
                labels_path=labels_path,
                dry_run=False,
            )
            profile["elapsed_seconds"] = time.monotonic() - started
            completed["policies"][policy] = profile
            result["profiles"].append({"rho": rho, "policy": policy, **profile})
            _write_json(checkpoint_path, result)
            print(
                json.dumps(
                    {
                        "rho": rho,
                        "policy": policy,
                        "retained": profile["retained_count"],
                        "physical_bytes": profile["physical_after_bytes"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        result.setdefault("by_rho", []).append(completed)
        _write_json(checkpoint_path, result)
    final_path = output_dir / "day6_rho_diagnostic.json"
    _write_json(final_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--count", type=int, default=200)
    args = parser.parse_args()
    run(args.output_dir, args.work_dir, seed=args.seed, count=args.count)


if __name__ == "__main__":
    main()
