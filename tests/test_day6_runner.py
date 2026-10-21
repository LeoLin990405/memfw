from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_day6_jobs import Job, load_jobs, run_jobs


def _sequence(values: list[float]) -> Any:
    remaining = iter(values)
    last = values[-1]

    def read() -> float:
        nonlocal last
        last = next(remaining, last)
        return last

    return read


def test_load_jobs_requires_named_argument_arrays(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([{"name": "one", "argv": [sys.executable, "-c", "pass"]}]))
    jobs = load_jobs(path)
    assert jobs == [Job("one", (sys.executable, "-c", "pass"))]

    path.write_text(json.dumps([{"name": "bad", "argv": "echo hello"}]))
    with pytest.raises(ValueError, match="argv"):
        load_jobs(path)


def test_run_jobs_gates_start_and_records_sampled_peak(tmp_path: Path) -> None:
    job = Job("gate", (sys.executable, "-c", "print('done')"))
    result = run_jobs(
        [job],
        run_root=tmp_path / "logs",
        temp_root=tmp_path / "temp",
        max_workers=1,
        load_limit=9.0,
        sample_interval=0.005,
        load_reader=_sequence([9.5, 0.5, 0.4]),
    )
    start = next(event for event in result["transitions"] if event["action"] == "START")
    assert start["load1"] < 9.0
    assert result["max_observed_load"] == 9.5
    assert result["status"] == "completed"
    assert (tmp_path / "logs" / "gate.log").read_text() == "done\n"


def test_run_jobs_stops_and_resumes_known_process_group(tmp_path: Path) -> None:
    job = Job("pause-resume", (sys.executable, "-c", "import time; time.sleep(0.06)"))
    result = run_jobs(
        [job],
        run_root=tmp_path / "logs",
        temp_root=tmp_path / "temp",
        max_workers=1,
        load_limit=9.0,
        sample_interval=0.005,
        load_reader=_sequence([0.0, 10.0, 0.0, 0.0, 0.0]),
    )
    actions = [event["action"] for event in result["transitions"]]
    assert "SIGSTOP" in actions
    assert "SIGCONT" in actions
    assert result["jobs"][0]["process_group"] == result["jobs"][0]["pid"]


def test_run_jobs_rejects_more_than_four_workers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 4"):
        run_jobs(
            [Job("one", (sys.executable, "-c", "pass"))],
            run_root=tmp_path / "logs",
            temp_root=tmp_path / "temp",
            max_workers=5,
        )


def test_process_slots_bound_nested_experiment_children(tmp_path: Path) -> None:
    jobs = [
        Job(
            f"matrix-{index}",
            (sys.executable, "-c", "import time; time.sleep(0.04)"),
            process_slots=2,
        )
        for index in range(2)
    ]
    result = run_jobs(
        jobs,
        run_root=tmp_path / "logs",
        temp_root=tmp_path / "temp",
        max_workers=2,
        max_process_slots=2,
        sample_interval=0.005,
        load_reader=_sequence([0.0]),
    )
    starts = [event for event in result["transitions"] if event["action"] == "START"]
    exits = [event for event in result["transitions"] if event["action"] == "EXIT"]
    assert len(starts) == len(exits) == 2
    assert starts[1]["elapsed_seconds"] >= exits[0]["elapsed_seconds"]
    assert result["max_process_slots"] == 2
