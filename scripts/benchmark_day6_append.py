#!/usr/bin/env python3
"""Measure public metadata-event append throughput and full verification time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import memfw.log.store
from memfw.core.models import EventKind
from memfw.core.repository import Repository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--label", default="append")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    root = args.root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("--root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    sequence = 0
    base_time = datetime(2026, 10, 18, tzinfo=UTC)

    def id_factory() -> str:
        nonlocal sequence
        sequence += 1
        return f"day6-{sequence:08d}"

    def clock() -> datetime:
        return base_time + timedelta(microseconds=sequence)

    repository = Repository(root, clock=clock, id_factory=id_factory)
    repository.init()
    started = time.perf_counter()
    for _ in range(args.count):
        repository.capture_event(
            EventKind.MEMORY_ACCESS,
            actor="human",
            details={"memory_id": "benchmark-target", "operation": "access"},
        )
    append_seconds = time.perf_counter() - started
    verify_started = time.perf_counter()
    verified_count = len(repository.events())
    verify_seconds = time.perf_counter() - verify_started
    event_state = (
        json.loads(repository.event_state_path.read_text(encoding="utf-8"))
        if repository.event_state_path.exists()
        else {}
    )
    legacy_head = root / "events.head"
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except OSError:
        load_1m = load_5m = load_15m = -1.0
    print(
        json.dumps(
            {
                "label": args.label,
                "count": args.count,
                "verified_count": verified_count,
                "append_seconds": round(append_seconds, 6),
                "append_events_per_second": round(args.count / append_seconds, 2),
                "full_verify_seconds": round(verify_seconds, 6),
                "events_jsonl_bytes": repository.events_path.stat().st_size,
                "events_jsonl_sha256": hashlib.sha256(
                    repository.events_path.read_bytes()
                ).hexdigest(),
                "events_head": (
                    event_state.get("last_hash")
                    or (
                        legacy_head.read_text(encoding="ascii").strip()
                        if legacy_head.exists()
                        else None
                    )
                ),
                "event_state_bytes": (
                    repository.event_state_path.stat().st_size
                    if repository.event_state_path.exists()
                    else 0
                ),
                "event_state_count": event_state.get("event_count", verified_count),
                "cursor_bytes": (
                    (root / "events.cursor.json").stat().st_size
                    if (root / "events.cursor.json").exists()
                    else 0
                ),
                "load_average": [load_1m, load_5m, load_15m],
                "source_module": str(Path(memfw.log.store.__file__).resolve()),
                "vault": str(root),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
