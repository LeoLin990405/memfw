#!/usr/bin/env python3
"""Compare five total-budget policies on the same offline Day5 storage fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gen_day5 import SEED, generate_dataset, generate_day6_dataset  # noqa: E402

POLICIES = ("fifo", "lru", "decay", "salience", "consolidate_then_forget")
BUDGETS = {"1MB": 1_000_000, "10MB": 10_000_000}
BASELINE_ARCHIVE = ROOT / "reports" / "day5_baseline_snapshot.zip"
DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0
NON_SEMANTIC_RESULT_KEYS = frozenset(
    {
        "runtime_seconds",
        "elapsed_seconds",
        "wall_seconds",
        "started_utc",
        "finished_utc",
        "checkpoint_file",
        "work_directory_persisted",
    }
)


_BLOCKED_NOISE_TOKENS = {"ufuck": "ukcuf", "ucunt": "utnuc", "ucock": "ukcoc"}


def _document_body(text: str, *, record_key: str, target_bytes: int = 1_100) -> str:
    body = text
    chunk = 0
    while len(body.encode("utf-8")) < target_bytes:
        digest = hashlib.sha256(f"{record_key}:{chunk}".encode()).digest()[:12]
        unique_tokens = " ".join(
            "u" + "".join(chr(97 + byte % 26) for byte in digest[offset : offset + 4])
            for offset in range(0, 12, 4)
        )
        for blocked, neutral in _BLOCKED_NOISE_TOKENS.items():
            unique_tokens = re.sub(rf"\b{blocked}\b", neutral, unique_tokens)
        body += f" {unique_tokens}"
        chunk += 1
    return body


def _write_fixture(
    output_dir: Path,
    *,
    count: int,
    seed: int,
    target_body_bytes: int = 1_100,
    rho_access: float | None = None,
    important_prevalence: float = 0.10,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    if rho_access is None:
        records, labels, accesses = generate_dataset(count=count, seed=seed, rho=0.6)
        rho_metadata: dict[str, Any] = {"rho_access_requested": None}
    else:
        records, labels, accesses, rho_metadata = generate_day6_dataset(
            count=count,
            seed=seed,
            rho_access=rho_access,
            important_prevalence=important_prevalence,
        )
    docs_path = output_dir / "day5_storage_fixture.jsonl"
    access_path = output_dir / "day5_storage_accesses.jsonl"
    labels_path = output_dir / "day5_storage_labels.jsonl"
    for path in (docs_path, access_path, labels_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    docs: list[dict[str, str]] = []
    for index, row in enumerate(records):
        duplicate_index = index if index >= 20 else index - (index % 2)
        duplicate_text = records[duplicate_index].text
        docs.append(
            {
                "memory_id": f"d5-storage-{index:05d}",
                "title": f"Review note {index:05d}",
                "body": _document_body(
                    duplicate_text,
                    record_key=f"{duplicate_index:05d}",
                    target_bytes=target_body_bytes,
                ),
                "created_at": row.timestamp,
            }
        )
    docs_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in docs),
        encoding="utf-8",
    )
    labels_by_old_id = {row.memory_id: row.importance for row in labels}
    labels_path.write_text(
        "".join(
            json.dumps(
                {
                    "memory_id": f"d5-storage-{index:05d}",
                    "importance": labels_by_old_id[row.memory_id],
                },
                sort_keys=True,
            )
            + "\n"
            for index, row in enumerate(records)
        ),
        encoding="utf-8",
    )
    id_map = {row.memory_id: f"d5-storage-{index:05d}" for index, row in enumerate(records)}
    mapped_accesses = [
        {"memory_id": id_map[row.memory_id], "timestamp": row.timestamp, "event_id": row.event_id}
        for row in accesses
    ]
    access_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in mapped_accesses), encoding="utf-8"
    )
    near_duplicate_candidates = _near_duplicate_count(docs)
    return (
        docs_path,
        access_path,
        labels_path,
        {
            "seed": seed,
            "record_count": count,
            "target_body_bytes_each": target_body_bytes,
            **rho_metadata,
            "important_prevalence_requested": (
                important_prevalence if rho_access is not None else None
            ),
            "capacity_text_note": (
                "Natural bilingual content is preserved; deterministic per-record alphabetic "
                f"capacity tokens bring each body to at least {target_body_bytes} bytes. This is "
                "synthetic capacity padding, not a natural corpus or a keyness generalization set. "
                "Only the ten declared duplicate pairs share their tokens."
            ),
            "body_bytes_min": min(len(row["body"].encode("utf-8")) for row in docs),
            "body_bytes_max": max(len(row["body"].encode("utf-8")) for row in docs),
            "access_event_count": len(mapped_accesses),
            "accesses_after_creation": all(
                next(doc["created_at"] for doc in docs if doc["memory_id"] == access["memory_id"])
                <= access["timestamp"]
                for access in mapped_accesses
            ),
            "labels_separate": True,
            "duplicate_pairs_for_consolidation": min(10, count // 2),
            "deterministic_judge_near_duplicate_candidates": near_duplicate_candidates,
        },
    )


def _safe_extract(archive_path: Path, target: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            windows_path = PureWindowsPath(member.filename)
            if (
                path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
                or "\\" in member.filename
            ):
                raise ValueError(f"unsafe baseline archive member: {member.filename}")
        archive.extractall(target)


def _near_duplicate_count(document_rows: list[dict[str, str]]) -> int:
    from memfw.agents import DeterministicJudge
    from memfw.core.models import MemoryDocument

    documents = [
        MemoryDocument(
            id=row["memory_id"],
            title=row["title"],
            body=row["body"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in document_rows
    ]
    return len(DeterministicJudge().find_near_duplicates(documents))


def _probe(
    *,
    source_root: Path,
    repo_root: Path,
    mode: str,
    fixture: Path | None = None,
    accesses: Path | None = None,
    labels: Path | None = None,
    policy: str = "fifo",
    budget: int = 1_000_000,
    dry_run: bool = False,
    timeout_seconds: float | None = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "day5_storage_probe.py"),
        "--mode",
        mode,
        "--source-root",
        str(source_root),
        "--repo-root",
        str(repo_root),
    ]
    if fixture:
        command.extend(["--fixture", str(fixture)])
    if accesses:
        command.extend(["--accesses", str(accesses)])
    if labels:
        command.extend(["--labels", str(labels)])
    command.extend(["--policy", policy, "--budget-bytes", str(budget)])
    if dry_run:
        command.append("--dry-run")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root)
    timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, env=env, timeout=timeout
    )
    return cast(dict[str, Any], json.loads(output.stdout))


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _semantic_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _semantic_projection(child)
            for key, child in value.items()
            if key not in NON_SEMANTIC_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_semantic_projection(child) for child in value]
    return value


def semantic_result_hash(value: Any) -> str:
    canonical = json.dumps(
        _semantic_projection(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _python_source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _save_checkpoint(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_matrix(
    output_dir: Path,
    *,
    count: int = 1_000,
    seed: int = SEED,
    baseline_archive: Path = BASELINE_ARCHIVE,
    work_dir: Path | None = None,
    checkpoint_path: Path | None = None,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    target_body_bytes: int = 1_100,
    rho_access: float | None = None,
    important_prevalence: float = 0.10,
    budget_limits: dict[str, int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if probe_timeout_seconds < 0:
        raise ValueError("probe_timeout_seconds must be non-negative (0 disables timeout)")
    if target_body_bytes < 1_100:
        raise ValueError("target_body_bytes must be at least 1100")
    docs_path, access_path, labels_path, fixture_meta = _write_fixture(
        output_dir,
        count=count,
        seed=seed,
        target_body_bytes=target_body_bytes,
        rho_access=rho_access,
        important_prevalence=important_prevalence,
    )
    matrix_budgets = budget_limits or BUDGETS
    if not matrix_budgets or any(value <= 0 for value in matrix_budgets.values()):
        raise ValueError("matrix budgets must contain positive byte limits")
    fixture_hashes = {
        "documents": hashlib.sha256(docs_path.read_bytes()).hexdigest(),
        "accesses": hashlib.sha256(access_path.read_bytes()).hexdigest(),
        "labels_evaluation_only": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
    }
    current_base = ROOT / "src"
    if work_dir is None:
        temporary = TemporaryDirectory(prefix="memfw-day5-storage-matrix-")
        temp_root = Path(temporary.name)
    else:
        temporary = None
        temp_root = work_dir.resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_path or output_dir / "day6_policy_matrix_checkpoint.json"
    wall_start = time.perf_counter()
    baseline_archive_hash = hashlib.sha256(baseline_archive.read_bytes()).hexdigest()
    baseline_checkout = temp_root / "day4-baseline"
    if not (baseline_checkout / "src").is_dir():
        if baseline_checkout.exists():
            shutil.rmtree(baseline_checkout)
        _safe_extract(baseline_archive, baseline_checkout)
    baseline_source = baseline_checkout / "src"
    identity = {
        "seed": seed,
        "record_count": count,
        "fixture_sha256": fixture_hashes,
        "day4_baseline_src_sha256": _python_source_hash(baseline_source),
        "day5_current_src_sha256": _python_source_hash(current_base),
        "matrix_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dataset_generator_sha256": hashlib.sha256(
            (ROOT / "scripts" / "gen_day5.py").read_bytes()
        ).hexdigest(),
        "probe_runner_sha256": hashlib.sha256(
            (ROOT / "scripts" / "day5_storage_probe.py").read_bytes()
        ).hexdigest(),
        "random_baseline_sha256": hashlib.sha256(
            (ROOT / "scripts" / "day6_statistics.py").read_bytes()
        ).hexdigest(),
        "baseline_archive_sha256": baseline_archive_hash,
        "configuration": {
            "history_keep": 2,
            "log_keep_recent": 40,
            "keyness_enabled": True,
            "keyness_window": 20,
            "probe_timeout_seconds": probe_timeout_seconds or None,
            "target_body_bytes": target_body_bytes,
            "rho_access": rho_access,
            "important_prevalence": important_prevalence,
            "budget_limits": matrix_budgets,
        },
    }
    current_profiles: dict[str, dict[str, Any]] = {
        budget_name: {} for budget_name in matrix_budgets
    }
    comparison: dict[str, Any] = {budget_name: {} for budget_name in matrix_budgets}
    if checkpoint.exists():
        previous = json.loads(checkpoint.read_text(encoding="utf-8"))
        if previous.get("identity") != identity:
            if temporary is not None:
                temporary.cleanup()
            raise ValueError("checkpoint identity differs from input/source; refusing to reuse")
        if previous.get("status") == "complete" and isinstance(previous.get("report"), dict):
            completed_report = previous["report"]
            current_profiles = completed_report.get("policy_profiles", current_profiles)
            comparison = completed_report.get("day4_baseline_vs_day5_current_fifo", comparison)
        else:
            current_profiles = previous.get("policy_profiles", current_profiles)
            comparison = previous.get("day4_baseline_vs_day5_current_fifo", comparison)

    def save_checkpoint(phase: str, **detail: Any) -> None:
        _save_checkpoint(
            checkpoint,
            {
                "status": "running",
                "phase": phase,
                "detail": detail,
                "identity": identity,
                "fixture": fixture_meta,
                "policy_profiles": current_profiles,
                "day4_baseline_vs_day5_current_fifo": comparison,
            },
        )

    save_checkpoint("ready")
    try:
        baseline_checkout = temp_root / "day4-baseline"
        baseline_source = baseline_checkout / "src"
        source_bases: dict[str, Path] = {}
        for role, source in (("day4_baseline", baseline_source), ("day5_current", current_base)):
            base = temp_root / f"{role}-base"
            base_marker = temp_root / f"{role}-base-receipt.json"
            base_receipt: dict[str, Any]
            if base_marker.exists() and base.is_dir():
                marker = json.loads(base_marker.read_text(encoding="utf-8"))
                if marker.get("identity") != identity:
                    raise ValueError(f"existing {role} base has a different input/source identity")
                base_receipt = cast(dict[str, Any], marker["receipt"])
                print(json.dumps({"phase": "reuse-base", "role": role}, sort_keys=True), flush=True)
            elif role in comparison.get("1MB", {}) and "base" in comparison["1MB"][role]:
                base_receipt = cast(dict[str, Any], comparison["1MB"][role]["base"])
            else:
                if base.exists():
                    shutil.rmtree(base)
                print(
                    json.dumps({"phase": "build-base-start", "role": role}, sort_keys=True),
                    flush=True,
                )
                base_receipt = _probe(
                    source_root=source,
                    repo_root=base,
                    mode="build",
                    fixture=docs_path,
                    accesses=access_path,
                    timeout_seconds=probe_timeout_seconds,
                )
                base_marker.write_text(
                    json.dumps({"identity": identity, "receipt": base_receipt}, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                print(
                    json.dumps(
                        {
                            "phase": "build-base-complete",
                            "role": role,
                            "physical_bytes": base_receipt["physical_bytes"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            source_bases[role] = base
            comparison["1MB"][role] = {"base": base_receipt}
            comparison["10MB"][role] = {"base": base_receipt}
            save_checkpoint("base-ready", role=role)
        for budget_name, budget in matrix_budgets.items():
            for policy in POLICIES:
                if policy in current_profiles[budget_name]:
                    continue
                profile_root = temp_root / f"current-{budget_name}-{policy}"
                if profile_root.exists():
                    shutil.rmtree(profile_root)
                shutil.copytree(source_bases["day5_current"], profile_root)
                # Even an inactive 10 MB budget is enforced and compacted so
                # before/after accounting uses the same real execution path.
                dry_run = False
                print(
                    json.dumps(
                        {"phase": "profile-start", "budget": budget_name, "policy": policy},
                        sort_keys=True,
                    ),
                    flush=True,
                )
                result = _probe(
                    source_root=current_base,
                    repo_root=profile_root,
                    mode="budget",
                    labels=labels_path,
                    policy=policy,
                    budget=budget,
                    dry_run=dry_run,
                    timeout_seconds=probe_timeout_seconds,
                )
                current_profiles[budget_name][policy] = result
                if policy == "fifo":
                    comparison[budget_name]["day5_current"]["fifo_result"] = result
                save_checkpoint("profile-complete", budget=budget_name, policy=policy)
                print(
                    json.dumps(
                        {
                            "phase": "profile-complete",
                            "budget": budget_name,
                            "policy": policy,
                            "retained": result["retained_count"],
                            "physical_bytes": result["physical_after_bytes"],
                            "budget_satisfied": result["budget_satisfied"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if (
                budget_name == "10MB"
                and int(comparison[budget_name]["day5_current"]["base"]["physical_bytes"]) <= budget
            ):
                current_ids = set(
                    next(iter(current_profiles[budget_name].values()))["surviving_ids"]
                )
                for _policy, result in current_profiles[budget_name].items():
                    result["budget_active"] = False
                    result["same_unmodified_survivors"] = (
                        set(result["surviving_ids"]) == current_ids
                    )
        for budget_name, budget in matrix_budgets.items():
            if "fifo_result" in comparison[budget_name].get("day4_baseline", {}):
                continue
            baseline_base = source_bases["day4_baseline"]
            run_root = temp_root / f"baseline-{budget_name}-fifo"
            if run_root.exists():
                shutil.rmtree(run_root)
            shutil.copytree(baseline_base, run_root)
            dry_run = False
            result = _probe(
                source_root=baseline_source,
                repo_root=run_root,
                mode="budget",
                labels=labels_path,
                policy="fifo",
                budget=budget,
                dry_run=dry_run,
                timeout_seconds=probe_timeout_seconds,
            )
            comparison[budget_name]["day4_baseline"]["fifo_result"] = result
            save_checkpoint("baseline-profile-complete", budget=budget_name)
            print(
                json.dumps(
                    {
                        "phase": "baseline-profile-complete",
                        "budget": budget_name,
                        "retained": result["retained_count"],
                        "physical_bytes": result["physical_after_bytes"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        jaccards: dict[str, dict[str, float]] = {}
        for budget_name, results in current_profiles.items():
            jaccards[budget_name] = {}
            for left_index, left in enumerate(POLICIES):
                for right in POLICIES[left_index + 1 :]:
                    jaccards[budget_name][f"{left}__{right}"] = _jaccard(
                        set(results[left]["surviving_ids"]), set(results[right]["surviving_ids"])
                    )
        wall = time.perf_counter() - wall_start
        report = {
            "dataset_seed": seed,
            "fixture": fixture_meta,
            "input_sha256": fixture_hashes,
            "source_sha256": identity,
            "budget_mode": (
                "actual total allocated-vault bytes enforced via repository budget_mode=total"
            ),
            "budgets_bytes": matrix_budgets,
            "policies": list(POLICIES),
            "policy_profiles": current_profiles,
            "pairwise_survivor_jaccard": jaccards,
            "day4_baseline_vs_day5_current_fifo": comparison,
            "baseline_archive": baseline_archive.name,
            "source_role_note": (
                "The comparison keys retain Day4/Day5 legacy names: the supplied frozen archive "
                "is the Day5 source snapshot, and the current checkout is the Day6 source."
            ),
            "same_fixture_for_all_sources_and_policies": True,
            "configuration": {
                "history_keep": 2,
                "log_keep_recent": 40,
                "keyness_enabled": True,
                "keyness_window": 20,
                "access_events_logged": fixture_meta["access_event_count"],
                "probe_timeout_seconds": probe_timeout_seconds or None,
                "target_body_bytes": target_body_bytes,
                "rho_access": rho_access,
                "important_prevalence": important_prevalence,
                "probe_timeout_note": (
                    "The subprocess timeout uses wall-clock time; scheduler SIGSTOP time counts. "
                    "Set to 0 to disable it for load-paused long runs."
                ),
            },
            "approval_simulation": (
                "Consolidation approvals are deterministic harness actions on isolated local "
                "vaults; no person or live model reviewed proposals. The Day6 source records "
                "simulated_approver, while the frozen Day5 baseline retains its legacy human-role "
                "simulation for compatibility."
            ),
            "budget_interpretation": (
                "Each tier is checked against its own initial vault bytes. A tier whose base is "
                "already within budget is inactive; identical survivor sets in that tier do not "
                "show policy equivalence under pressure."
            ),
            "checkpoint_file": checkpoint.name,
            "work_directory_persisted": work_dir is not None,
        }
        receipt = {
            "runtime_seconds": round(wall, 6),
            "completed": True,
            "profiles": len(POLICIES) * len(matrix_budgets),
            "comparison_runs": len(matrix_budgets) * 2,
        }
        _save_checkpoint(
            checkpoint,
            {
                "status": "complete",
                "phase": "complete",
                "identity": identity,
                "report": report,
                "receipt": receipt,
            },
        )
        if temporary is not None:
            temporary.cleanup()
        return report, receipt
    except BaseException as exc:
        save_checkpoint("failed", error=f"{type(exc).__name__}: {exc}")
        if temporary is not None:
            temporary.cleanup()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--count", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--baseline-archive", type=Path, default=BASELINE_ARCHIVE)
    parser.add_argument(
        "--work-dir", type=Path, help="Persistent work directory for resumable bases"
    )
    parser.add_argument("--rho-access", type=float, choices=(0.0, 0.3, 0.6))
    parser.add_argument("--important-prevalence", type=float, default=0.10)
    parser.add_argument(
        "--budget",
        action="append",
        metavar="NAME:BYTES",
        help="Override a budget tier; repeat for multiple tiers (for example 250KB:250000).",
    )
    parser.add_argument(
        "--target-body-bytes",
        type=int,
        default=1_100,
        help="Deterministic body size for each fixture record; use 15000 for an active 10 MB tier.",
    )
    parser.add_argument("--checkpoint", type=Path, help="Atomic per-stage checkpoint JSON path")
    parser.add_argument(
        "--probe-timeout-seconds",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT_SECONDS,
        help="Per-probe wall timeout; 0 disables. Scheduler-paused time counts toward timeout.",
    )
    args = parser.parse_args()
    parsed_budgets: dict[str, int] | None = None
    if args.budget:
        parsed_budgets = {}
        for value in args.budget:
            if ":" not in value:
                parser.error("--budget must use NAME:BYTES")
            name, raw_bytes = value.rsplit(":", 1)
            try:
                parsed_budgets[name] = int(raw_bytes)
            except ValueError:
                parser.error("--budget bytes must be an integer")
    report, receipt = run_matrix(
        args.output_dir,
        count=args.count,
        seed=args.seed,
        baseline_archive=args.baseline_archive,
        work_dir=args.work_dir,
        checkpoint_path=args.checkpoint,
        probe_timeout_seconds=args.probe_timeout_seconds,
        target_body_bytes=args.target_body_bytes,
        rho_access=args.rho_access,
        important_prevalence=args.important_prevalence,
        budget_limits=parsed_budgets,
    )
    (args.output_dir / "day6_policy_matrix.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "day6_storage_runtime_receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"report": "day6_policy_matrix.json", **receipt}, sort_keys=True))


if __name__ == "__main__":
    main()
