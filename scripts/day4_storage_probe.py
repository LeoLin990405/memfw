#!/usr/bin/env python3
"""Run one real FIFO body-budget storage scenario in a selected source tree."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _physical_breakdown(root: Path, repository: Any) -> dict[str, int]:
    anchors = {
        doc.id for doc in repository.list() if doc.layer.value == "anchor" or bool(doc.anchor)
    }
    totals = {
        name: 0
        for name in ("events", "history", "memories", "anchors", "archives", "index", "other")
    }
    for path in root.rglob("*") if root.exists() else ():
        if (
            not path.is_file()
            or path.name in {".memfw.lock", ".pending.json"}
            or path.name.endswith(".tmp")
        ):
            continue
        parts = path.relative_to(root).parts
        if parts[0] in {"events.jsonl", "events.head", ".log"}:
            bucket = "events"
        elif parts[0] == ".history":
            bucket = "history"
        elif parts[0] == "memories":
            bucket = "anchors" if path.stem in anchors else "memories"
        elif parts[0] == ".archives":
            bucket = "archives"
        elif path.name.startswith("index.sqlite3"):
            bucket = "index"
        else:
            bucket = "other"
        stat = path.stat()
        totals[bucket] += stat.st_blocks * 512 if hasattr(stat, "st_blocks") else stat.st_size
    return totals


def _fit_body(text: str, byte_count: int) -> str:
    prefix = text.encode("utf-8")
    if len(prefix) > byte_count:
        raise ValueError("source text exceeds per-record target body size")
    line = (
        b" Follow-up: the date, source reference, observed result, and next review remain recorded."
    )
    remaining = byte_count - len(prefix)
    return (prefix + (line * (remaining // len(line) + 2))[:remaining]).decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--budget-bytes", type=int, required=True)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root))
    import memfw  # noqa: PLC0415
    from memfw.core.repository import Repository  # noqa: PLC0415
    from memfw.curation.engine import enforce_budget  # noqa: PLC0415
    from memfw.policies import PolicyName  # noqa: PLC0415

    resolved_module = Path(memfw.__file__).resolve()
    resolved_source = args.source_root.resolve()
    if resolved_source not in resolved_module.parents:
        raise RuntimeError(
            f"requested source root did not load: source={resolved_source}, "
            f"module={resolved_module}"
        )
    records = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()]
    if not records:
        raise ValueError("dataset is empty")
    fixture_total = args.budget_bytes + max(1, args.budget_bytes // 2)
    q, remainder = divmod(fixture_total, len(records))
    counter = iter(range(1, len(records) * 30 + 1))
    repository: Any = Repository(
        args.repo_root,
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        id_factory=lambda: f"d4-storage-{next(counter):08d}",
    )
    repository.init()
    config = repository.read_config()
    profile = "1MB" if args.budget_bytes == 1_000_000 else "10MB"
    config.update(
        {
            "budget_level": profile,
            "budget_mode": "body",
            "budget_scope": "all",
            "forget_policy": "fifo",
            "forget_action": "hard",
            "auto_enforce_on_write": False,
            "max_bytes": args.budget_bytes,
            "log_keep_recent": 100,
        }
    )
    repository.configure(config)
    for index, row in enumerate(records):
        target = q + (1 if index < remainder else 0)
        repository.add(
            f"Note {row['memory_id']}",
            _fit_body(str(row["text"]), target),
            memory_id=str(row["memory_id"]),
            source="synthetic-day4-storage",
            provenance="storage-size-fixture",
            tags=[],
        )
    # The metadata-only update exercises the history/version accounting path.
    repository.update(
        str(records[-1]["memory_id"]),
        summary="A later review recorded the source and verification outcome.",
        actor="human",
    )
    built = _physical_breakdown(args.repo_root, repository)
    anchors_before = sum(
        doc.layer.value == "anchor" or bool(doc.anchor) for doc in repository.list()
    )
    before_body_bytes = sum(len(doc.body.encode("utf-8")) for doc in repository.list())
    forget = enforce_budget(
        repository, policy_override=PolicyName.FIFO, dry_run=False, actor="mechanism"
    )
    precompact = _physical_breakdown(args.repo_root, repository)
    after_body_bytes = sum(len(doc.body.encode("utf-8")) for doc in repository.list())
    compact_result = repository.compact_log(keep_recent=100) if args.compact else None
    after = _physical_breakdown(args.repo_root, repository)
    summary = {
        "memory_count_before_forget": len(records),
        "retained_count": len(repository.list()),
        "budget_profile": profile,
        "budget_bytes": args.budget_bytes,
        "body_bytes_before_forget": before_body_bytes,
        "body_bytes_after_forget": after_body_bytes,
        "budget_satisfied": forget.satisfied_after_apply,
        "unsatisfied_reason": forget.unsatisfied_reason,
        "anchors_before": anchors_before,
        "anchors_retained": sum(
            doc.layer.value == "anchor" or bool(doc.anchor) for doc in repository.list()
        ),
        "physical_before_forget": built,
        "physical_after_forget_precompact": precompact,
        "physical_after_compaction": after,
        "physical_total_before_forget": sum(built.values()),
        "physical_total_after_forget_precompact": sum(precompact.values()),
        "physical_total_after_compaction": sum(after.values()),
        "compact_result": compact_result,
        "runtime_role": "new_runtime" if args.compact else "baseline_snapshot",
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
