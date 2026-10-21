#!/usr/bin/env python3
"""Run bounded offline Day 6 jobs with sampled load gating and process-group control.

The scheduler gates new launches on the one-minute load average, limits concurrent
children, and pauses known child process groups when sampled load reaches the
configured ceiling. Sampling is advisory: unrelated work can exceed the ceiling
between samples or independently of managed children.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ROOT = ROOT / "reports" / "day6_runs"
DEFAULT_TEMP_ROOT = Path("/private/tmp/memfw-day6")


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    argv: tuple[str, ...]
    cwd: Path = ROOT
    env: dict[str, str] = field(default_factory=dict)
    process_slots: int = 1


@dataclass(slots=True)
class RunningJob:
    job: Job
    process: subprocess.Popen[bytes]
    log_path: Path
    log_file: Any
    started_at: str
    stopped: bool = False


def load_jobs(path: Path) -> list[Job]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("jobs file must contain a non-empty JSON list")
    jobs: list[Job] = []
    names: set[str] = set()
    filenames: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"job {index} must be an object")
        name, raw_argv = item.get("name"), item.get("argv")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"job {index} needs a unique non-empty name")
        filename = _safe_name(name)
        if filename in filenames:
            raise ValueError(f"job {name!r} collides with another job log filename")
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or not all(isinstance(arg, str) and arg for arg in raw_argv)
        ):
            raise ValueError(f"job {name!r} argv must be a non-empty list of strings")
        cwd_value = item.get("cwd", str(ROOT))
        env_value = item.get("env", {})
        process_slots = item.get("process_slots", 1)
        if (
            not isinstance(cwd_value, str)
            or not isinstance(env_value, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in env_value.items()
            )
            or not isinstance(process_slots, int)
            or isinstance(process_slots, bool)
            or not 1 <= process_slots <= 4
        ):
            raise ValueError(f"job {name!r} has invalid cwd, env, or process_slots")
        names.add(name)
        filenames.add(filename)
        jobs.append(Job(name, tuple(raw_argv), Path(cwd_value), dict(env_value), process_slots))
    return jobs


def _load_average() -> float:
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        raise RuntimeError("os.getloadavg is unavailable on this platform")
    return float(getter()[0])


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "job"


def _signal_group(child: RunningJob, sig: int) -> None:
    try:
        os.killpg(child.process.pid, sig)
    except ProcessLookupError:
        pass


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_jobs(
    jobs: list[Job],
    *,
    run_root: Path = DEFAULT_RUN_ROOT,
    temp_root: Path = DEFAULT_TEMP_ROOT,
    max_workers: int = 2,
    max_process_slots: int = 4,
    load_limit: float = 9.0,
    sample_interval: float = 1.0,
    load_reader: Any = _load_average,
) -> dict[str, Any]:
    if not jobs:
        raise ValueError("at least one job is required")
    if not 1 <= max_workers <= 4:
        raise ValueError("max_workers must be between 1 and 4")
    if not 1 <= max_process_slots <= 4:
        raise ValueError("max_process_slots must be between 1 and 4")
    if any(job.process_slots > max_process_slots for job in jobs):
        raise ValueError("each job process_slots value must fit max_process_slots")
    if load_limit <= 0 or sample_interval <= 0:
        raise ValueError("load_limit and sample_interval must be positive")
    run_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    receipt_path = run_root / "scheduler_receipt.json"
    pending = deque(jobs)
    active: dict[int, RunningJob] = {}
    samples: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    started_monotonic = time.monotonic()
    max_observed_load = 0.0
    sample_count = 0
    base_env = os.environ.copy()
    base_env.update({"TMPDIR": str(temp_root), "TMP": str(temp_root), "TEMP": str(temp_root)})

    def save(status: str) -> None:
        _write_receipt(
            receipt_path,
            {
                "status": status,
                "started_utc": datetime.fromtimestamp(
                    time.time() - (time.monotonic() - started_monotonic), tz=UTC
                ).isoformat(),
                "sample_interval_seconds": sample_interval,
                "load_metric": "os.getloadavg()[0] (one-minute load average)",
                "load_limit": load_limit,
                "max_observed_load": max_observed_load,
                "samples": sample_count,
                "max_workers": max_workers,
                "max_process_slots": max_process_slots,
                "active_process_slots": sum(child.job.process_slots for child in active.values()),
                "managed_child_count": len(jobs),
                "temp_root": str(temp_root),
                "sampling_note": (
                    "Sampled control applies to known child process groups only. Load may cross "
                    "the limit between samples or due to unrelated processes; no hard ceiling "
                    "is guaranteed."
                ),
                "load_samples": samples,
                "transitions": transitions,
                "jobs": outcomes,
            },
        )

    try:
        while pending or active:
            load = float(load_reader())
            sample_count += 1
            max_observed_load = max(max_observed_load, load)
            samples.append(
                {
                    "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                    "load1": load,
                }
            )
            overloaded = load >= load_limit

            for child in active.values():
                if overloaded and not child.stopped and child.process.poll() is None:
                    _signal_group(child, signal.SIGSTOP)
                    child.stopped = True
                    transitions.append(
                        {
                            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                            "name": child.job.name,
                            "pid": child.process.pid,
                            "action": "SIGSTOP",
                            "load1": load,
                        }
                    )
                elif not overloaded and child.stopped:
                    _signal_group(child, signal.SIGCONT)
                    child.stopped = False
                    transitions.append(
                        {
                            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                            "name": child.job.name,
                            "pid": child.process.pid,
                            "action": "SIGCONT",
                            "load1": load,
                        }
                    )

            while (
                pending
                and len(active) < max_workers
                and sum(child.job.process_slots for child in active.values())
                + pending[0].process_slots
                <= max_process_slots
                and load < load_limit
            ):
                job = pending.popleft()
                log_path = run_root / f"{_safe_name(job.name)}.log"
                log_file = log_path.open("wb")
                env = base_env | job.env
                env.update(
                    {"TMPDIR": str(temp_root), "TMP": str(temp_root), "TEMP": str(temp_root)}
                )
                child_proc = subprocess.Popen(
                    list(job.argv),
                    cwd=job.cwd,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                running = RunningJob(
                    job=job,
                    process=child_proc,
                    log_path=log_path,
                    log_file=log_file,
                    started_at=datetime.now(UTC).isoformat(),
                )
                active[child_proc.pid] = running
                outcomes.append(
                    {
                        "name": job.name,
                        "argv": list(job.argv),
                        "pid": child_proc.pid,
                        "process_group": child_proc.pid,
                        "process_slots": job.process_slots,
                        "started_utc": running.started_at,
                        "log": str(log_path),
                        "status": "running",
                    }
                )
                transitions.append(
                    {
                        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                        "name": job.name,
                        "pid": child_proc.pid,
                        "action": "START",
                        "load1": load,
                    }
                )

            finished: list[int] = []
            for pid, child in active.items():
                return_code = child.process.poll()
                if return_code is None:
                    continue
                if child.stopped:
                    _signal_group(child, signal.SIGCONT)
                    child.stopped = False
                child.log_file.close()
                outcome = next(item for item in outcomes if item["pid"] == pid)
                outcome.update(
                    {
                        "status": "completed" if return_code == 0 else "failed",
                        "return_code": return_code,
                        "finished_utc": datetime.now(UTC).isoformat(),
                    }
                )
                transitions.append(
                    {
                        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
                        "name": child.job.name,
                        "pid": pid,
                        "action": "EXIT",
                        "return_code": return_code,
                    }
                )
                finished.append(pid)
            for pid in finished:
                del active[pid]

            status = (
                "running"
                if pending or active
                else (
                    "completed" if all(item["return_code"] == 0 for item in outcomes) else "failed"
                )
            )
            save(status)
            if pending or active:
                time.sleep(sample_interval)
    except BaseException:
        for child in active.values():
            if child.stopped:
                _signal_group(child, signal.SIGCONT)
                child.stopped = False
            _signal_group(child, signal.SIGTERM)
        for child in active.values():
            try:
                child.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_group(child, signal.SIGKILL)
                child.process.wait()
            child.log_file.close()
        save("interrupted")
        raise
    return cast(dict[str, Any], json.loads(receipt_path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs", type=Path, required=True, help="JSON array of named argv commands"
    )
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--temp-root", type=Path, default=DEFAULT_TEMP_ROOT)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--max-process-slots", type=int, default=4)
    parser.add_argument("--load-limit", type=float, default=9.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 4:
        parser.error("--max-workers must be between 1 and 4")
    if not 1 <= args.max_process_slots <= 4:
        parser.error("--max-process-slots must be between 1 and 4")
    if args.load_limit <= 0 or args.sample_interval <= 0:
        parser.error("--load-limit and --sample-interval must be positive")
    try:
        jobs = load_jobs(args.jobs)
        result = run_jobs(
            jobs,
            run_root=args.run_root,
            temp_root=args.temp_root,
            max_workers=args.max_workers,
            max_process_slots=args.max_process_slots,
            load_limit=args.load_limit,
            sample_interval=args.sample_interval,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {key: result[key] for key in ("status", "samples", "max_observed_load", "jobs")},
            indent=2,
        )
    )
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
