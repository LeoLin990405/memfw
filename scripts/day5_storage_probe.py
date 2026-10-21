#!/usr/bin/env python3
"""Build or enforce one deterministic Day5 total-budget vault in a selected source tree."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _physical_breakdown(root: Path, repository: Any) -> dict[str, int]:
    from memfw.stats import measure_physical_breakdown

    result = measure_physical_breakdown(repository)
    expected = sum(
        item.stat().st_blocks * 512 if hasattr(item.stat(), "st_blocks") else item.stat().st_size
        for item in root.rglob("*")
        if item.is_file()
        and item.name not in {".memfw.lock", ".pending.json"}
        and not item.name.endswith(".tmp")
    )
    if sum(result.values()) != expected:
        raise RuntimeError("physical categories do not sum to allocated vault bytes")
    return result


def _logical_breakdown(root: Path, repository: Any) -> dict[str, int]:
    categories = {
        name: 0
        for name in ("events", "history", "memories", "anchors", "archives", "index", "other")
    }
    anchors = {
        doc.id for doc in repository.list() if doc.layer.value == "anchor" or bool(doc.anchor)
    }
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.name in {".memfw.lock", ".pending.json"}
            or path.name.endswith(".tmp")
        ):
            continue
        parts = path.relative_to(root).parts
        if parts[0] in {"events.jsonl", "events.head", ".log"}:
            name = "events"
        elif parts[0] == ".history":
            name = "history"
        elif parts[0] == "memories":
            name = "anchors" if path.stem in anchors else "memories"
        elif parts[0] == ".archives":
            name = "archives"
        elif path.name.startswith("index.sqlite3"):
            name = "index"
        else:
            name = "other"
        categories[name] += path.stat().st_size
    return categories


def build_base(
    repo_root: Path, fixture_path: Path, access_path: Path, source_root: Path
) -> dict[str, Any]:
    sys.path.insert(0, str(source_root))
    import memfw  # noqa: PLC0415
    from memfw.core.models import EventKind  # noqa: PLC0415
    from memfw.core.repository import Repository  # noqa: PLC0415

    module_path = Path(memfw.__file__).resolve()
    if source_root.resolve() not in module_path.parents:
        raise RuntimeError("requested source root did not supply imported memfw")
    documents = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines()]
    accesses = [json.loads(line) for line in access_path.read_text(encoding="utf-8").splitlines()]
    sequence = count(1)
    current_clock = [datetime(2026, 9, 18, tzinfo=UTC)]
    repository = Repository(
        repo_root,
        clock=lambda: current_clock[0],
        id_factory=lambda: f"d5-base-{next(sequence):08d}",
    )
    repository.init()
    config = repository.read_config()
    config.update(
        {
            "budget_mode": "total",
            "max_bytes": 1_000_000,
            "budget_scope": "all",
            "forget_action": "hard",
            "auto_enforce_on_write": False,
            "history_keep": 2,
            "log_keep_recent": 40,
            "keyness_enabled": True,
            "keyness_window": 20,
        }
    )
    repository.configure(config)
    timeline: list[tuple[datetime, int, dict[str, str]]] = []
    for row in documents:
        timeline.append((datetime.fromisoformat(row["created_at"]), 0, {"kind": "create", **row}))
    for row in accesses:
        timeline.append((datetime.fromisoformat(row["timestamp"]), 1, {"kind": "access", **row}))
    timeline.sort(
        key=lambda item: (item[0], item[1], item[2]["memory_id"], item[2].get("event_id", ""))
    )
    created_ids: set[str] = set()
    for timestamp, _, row in timeline:
        current_clock[0] = timestamp
        if row["kind"] == "create":
            repository.add(
                title=row["title"],
                body=row["body"],
                memory_id=row["memory_id"],
                source="synthetic-day5-storage",
                provenance="offline-fixture",
                tags=[],
            )
            created_ids.add(row["memory_id"])
        else:
            if row["memory_id"] not in created_ids:
                raise RuntimeError("access was scheduled before its memory creation")
            repository.capture_event(
                EventKind.MEMORY_ACCESS,
                actor="mechanism",
                timestamp=timestamp,
                details={"memory_id": row["memory_id"], "operation": "retrieve"},
            )
    physical = _physical_breakdown(repo_root, repository)
    logical = _logical_breakdown(repo_root, repository)
    return {
        "module_role": "baseline" if "baseline" in str(source_root) else "current",
        "memory_count": len(repository.list()),
        "access_event_count": len(accesses),
        "physical_bytes": sum(physical.values()),
        "logical_breakdown": logical,
        "physical_breakdown": physical,
        "physical_categories_sum": sum(physical.values()),
        "logical_categories_sum": sum(logical.values()),
        "configured_history_keep": config["history_keep"],
        "configured_log_keep_recent": config["log_keep_recent"],
    }


def run_budget(
    repo_root: Path,
    *,
    source_root: Path,
    policy: str,
    budget_bytes: int,
    labels_path: Path,
    dry_run: bool,
    simulate_approvals: bool = True,
) -> dict[str, Any]:
    sys.path.insert(0, str(source_root))
    from day6_statistics import hypergeometric_random_retention  # noqa: PLC0415

    import memfw  # noqa: PLC0415
    from memfw.core.repository import Repository  # noqa: PLC0415
    from memfw.curation.engine import enforce_budget  # noqa: PLC0415
    from memfw.curation.review import apply_approved, decide_proposal  # noqa: PLC0415
    from memfw.policies import PolicyName  # noqa: PLC0415

    module_path = Path(memfw.__file__).resolve()
    if source_root.resolve() not in module_path.parents:
        raise RuntimeError("requested source root did not supply imported memfw")
    sequence = count(1)
    repository = Repository(
        repo_root, clock=lambda: NOW, id_factory=lambda: f"d5-op-{next(sequence):08d}"
    )
    config = repository.read_config()
    config.update(
        {
            "budget_mode": "total",
            "max_bytes": budget_bytes,
            "budget_scope": "all",
            "forget_policy": policy,
            "forget_action": "hard",
            "merge_fallback_policy": "fifo",
            "auto_enforce_on_write": False,
            "history_keep": 2,
            "log_keep_recent": 40,
            "keyness_enabled": True,
            "keyness_window": 20,
        }
    )
    repository.configure(config)
    physical_before = sum(_physical_breakdown(repo_root, repository).values())
    original_ids = {doc.id for doc in repository.list()}
    result = enforce_budget(
        repository, now=NOW, policy_override=PolicyName(policy), dry_run=dry_run, actor="mechanism"
    )
    review: dict[str, Any] = {
        "mode": "none",
        "proposed": 0,
        "simulated_approved": 0,
        "applied": 0,
        "fallback": None,
    }
    if not dry_run and result.pending_proposal_ids and simulate_approvals:
        # This is a deterministic harness action on an isolated local fixture.
        # The product API represents its reviewer role as `human`; no person or
        # model made these decisions.
        proposals = [
            proposal
            for proposal in repository.list_proposals(status="pending")
            if proposal.proposal_id in result.pending_proposal_ids
        ]
        chosen = []
        used_targets: set[str] = set()
        for proposal in proposals:
            if used_targets.isdisjoint(proposal.target_ids):
                chosen.append(proposal)
                used_targets.update(proposal.target_ids)
        for proposal in chosen:
            if hasattr(repository, "simulate_proposal_decision"):
                repository.simulate_proposal_decision(proposal.proposal_id)
            else:
                # Frozen Day5 source has no explicit synthetic-review actor.
                decide_proposal(
                    repository, proposal.proposal_id, "approve", payload_hash=proposal.payload_hash
                )
        for proposal in chosen:
            actor = (
                "simulated_approver"
                if hasattr(repository, "simulate_proposal_decision")
                else "human"
            )
            apply_approved(repository, proposal.proposal_id, actor=actor, enforce_fallback=False)
        review = {
            "mode": (
                "deterministic_simulated_approval_merge"
                if hasattr(repository, "simulate_proposal_decision")
                else "day5_legacy_human_role_simulated_approval_merge"
            ),
            "proposed": len(proposals),
            "simulated_approved": len(chosen),
            "applied": len(chosen),
            "fallback": "FIFO hard forget if still over target",
        }
        if sum(_physical_breakdown(repo_root, repository).values()) > budget_bytes:
            result = enforce_budget(
                repository,
                now=NOW,
                policy_override=PolicyName.FIFO,
                dry_run=False,
                actor="mechanism",
            )
            review["fallback_applied"] = True
    if not dry_run:
        repository.compact_log(keep_recent=40)
    physical_after = _physical_breakdown(repo_root, repository)
    logical_after = _logical_breakdown(repo_root, repository)
    surviving = {doc.id for doc in repository.list()}
    labels = {
        row["memory_id"]: row["importance"]
        for row in (
            json.loads(line) for line in labels_path.read_text(encoding="utf-8").splitlines()
        )
    }
    important_ids = {identifier for identifier, value in labels.items() if value == "important"}
    retained_important = len(surviving & important_ids)
    important_total = len(important_ids)
    random_baseline = hypergeometric_random_retention(
        population_size=len(labels),
        important_count=important_total,
        retained_count=len(surviving),
        observed_important=retained_important,
    )
    return {
        "policy": policy,
        "budget_bytes": budget_bytes,
        "dry_run": dry_run,
        "budget_active": physical_before > budget_bytes,
        "physical_before_bytes": physical_before,
        "physical_after_bytes": sum(physical_after.values()),
        "logical_after_breakdown": logical_after,
        "physical_after_breakdown": physical_after,
        "retained_count": len(surviving),
        "forgotten_count": len(original_ids - surviving),
        "budget_satisfied": sum(physical_after.values()) <= budget_bytes,
        "policy_result_satisfied": result.satisfied_after_apply,
        "unsatisfied_reason": result.unsatisfied_reason,
        "important_retained": retained_important,
        "important_total": important_total,
        "important_recall": retained_important / important_total if important_total else None,
        "important_precision": retained_important / len(surviving) if surviving else None,
        "random_retention_baseline": random_baseline,
        "review_simulation": review,
        "surviving_ids": sorted(surviving),
        "module_source_verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("build", "budget"), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--accesses", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--policy", default="fifo")
    parser.add_argument("--budget-bytes", type=int, default=1_000_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.mode == "build":
        if args.fixture is None or args.accesses is None:
            parser.error("build mode requires --fixture and --accesses")
        result = build_base(args.repo_root, args.fixture, args.accesses, args.source_root)
    else:
        if args.labels is None:
            parser.error("budget mode requires --labels")
        result = run_budget(
            args.repo_root,
            source_root=args.source_root,
            policy=args.policy,
            budget_bytes=args.budget_bytes,
            labels_path=args.labels,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
