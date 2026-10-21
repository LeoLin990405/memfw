#!/usr/bin/env python3
"""Run real-vault Day 3 policy and capture experiments from a fixed corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock, Timeout
from gen_synthetic import SEED, SyntheticRecord, generate_dataset, write_dataset

from memfw.budget import BYTE_BUDGETS, BudgetProfile
from memfw.capture import CaptureSession, TimedSignal
from memfw.core.models import CaptureState, EventKind, Layer
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.curation.review import apply_approved, decide_proposal
from memfw.policies import PolicyName
from memfw.stats import measure_runtime

NOW = datetime(2026, 10, 18, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "reports" / "day3_experiment.json"
REPORT_MARKDOWN = ROOT / "reports" / "day3_experiment.md"
CAPTURE_THRESHOLD = 0.65


def _seed_base(records: Iterable[SyntheticRecord], path: Path) -> Repository:
    rows = tuple(records)
    point = [datetime.fromisoformat(rows[0].timestamp)]
    event_counter = [0]
    access_events: list[tuple[datetime, str, int]] = []

    def clock() -> datetime:
        return point[0]

    def event_id() -> str:
        event_counter[0] += 1
        return f"d3-event-{event_counter[0]:08d}"

    repository = Repository(path, clock=clock, id_factory=event_id)
    repository.init()
    for index, row in enumerate(rows):
        point[0] = datetime.fromisoformat(row.timestamp)
        is_anchor = index % 100 == 0
        repository.add(
            f"{row.theme} {row.memory_id}",
            row.text,
            memory_id=row.memory_id,
            layer=Layer.ANCHOR if is_anchor else Layer.CURRENT,
            anchor=f"synthetic-anchor:{row.memory_id}" if is_anchor else None,
            source="synthetic-day3",
            provenance=f"synthetic-source:{row.memory_id}",
            tags=[row.importance, row.theme],
        )
        repository.update(
            row.memory_id,
            strength=row.topic_strength,
            reference_count=(index * 7 + 3) % 12,
            last_access_interval_days=float((index * 11) % 46),
            manually_marked=index % 97 == 0,
        )
        activity_count = 1 if index % 10 == 0 else 0
        created_at = datetime.fromisoformat(row.timestamp)
        for access_index in range(activity_count):
            access_time = created_at + timedelta(
                seconds=(NOW - created_at).total_seconds()
                * (access_index + 1)
                / (activity_count + 1)
            )
            access_events.append((access_time, row.memory_id, access_index + 1))
    for access_time, memory_id, access_count in sorted(access_events):
        repository.capture_event(
            EventKind.MEMORY_ACCESS,
            actor="mechanism",
            timestamp=access_time,
            details={"memory_id": memory_id, "access_count": access_count},
        )
    point[0] = NOW
    return repository


def _write_config(repository: Repository, *, policy: PolicyName, profile: BudgetProfile) -> None:
    config = repository.read_config()
    config.update(
        {
            "budget_level": profile.value,
            "budget_scope": "all",
            "budget_mode": "total",
            "forget_policy": policy.value,
            "forget_action": "hard",
            "auto_enforce_on_write": False,
        }
    )
    repository._atomic_write(
        repository.config_path,
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )


def _run_one(
    base: Path,
    run_root: Path,
    records: tuple[SyntheticRecord, ...],
    policy: PolicyName,
    profile: BudgetProfile,
) -> dict[str, Any]:
    if run_root.exists():
        shutil.rmtree(run_root)
    shutil.copytree(base, run_root)
    event_sequence = [0]

    def event_id() -> str:
        event_sequence[0] += 1
        return f"day3-run-{policy.value}-{profile.value}-{event_sequence[0]:08d}"

    repository = Repository(run_root, clock=lambda: NOW, id_factory=event_id)
    _write_config(repository, policy=policy, profile=profile)
    initial_stats = measure_runtime(repository, now=NOW).as_dict()
    initial_docs = repository.list()
    truth = {row.memory_id: row.importance for row in records}
    initial_important = sum(value == "important" for value in truth.values())
    initial_noise = sum(value == "noise" for value in truth.values())
    if policy == PolicyName.CONSOLIDATE_THEN_FORGET:
        _apply_synthetic_merges(repository, profile)
        _write_config(repository, policy=PolicyName.FIFO, profile=profile)
        result = enforce_budget(
            repository,
            policy_override=PolicyName.FIFO,
            now=NOW,
            dry_run=False,
            actor="mechanism",
        )
        approval_mode = "synthetic-human-approved"
    else:
        result = enforce_budget(
            repository,
            policy_override=policy,
            now=NOW,
            dry_run=False,
            actor="mechanism",
        )
        approval_mode = "not-applicable"
    final_docs = repository.list()
    final_stats = measure_runtime(repository, now=NOW).as_dict()
    retained_ids = _retained_source_ids(final_docs)
    anchor_ids = {document.id for document in initial_docs if document.anchor}
    final_document_ids = {document.id for document in final_docs}
    retained_anchor_ids = anchor_ids & final_document_ids
    retained_important = sum(truth.get(memory_id) == "important" for memory_id in retained_ids)
    retained_noise = sum(truth.get(memory_id) == "noise" for memory_id in retained_ids)
    return {
        "policy": policy.value,
        "budget_profile": profile.value,
        "budget_mode": "total_physical_allocated_bytes",
        "initial_memory_count": len(initial_docs),
        "initial_body_bytes": initial_stats["body_bytes"],
        "initial_physical_disk_bytes": initial_stats["physical_disk_bytes"],
        "final_memory_count": len(final_docs),
        "final_body_bytes": final_stats["body_bytes"],
        "final_physical_disk_bytes": final_stats["physical_disk_bytes"],
        "important_recall": retained_important / initial_important if initial_important else 0.0,
        "important_retained": retained_important,
        "important_total": initial_important,
        "noise_retention": retained_noise / initial_noise if initial_noise else 0.0,
        "noise_retained": retained_noise,
        "noise_total": initial_noise,
        "forgotten_count": len(set(truth) - retained_ids),
        "anchor_count": len(anchor_ids),
        "anchors_forgotten": len(anchor_ids - retained_anchor_ids),
        "budget_satisfied": result.satisfied_after_apply,
        "unsatisfied_reason": result.unsatisfied_reason,
        "blocking_anchor_ids": list(result.blocking_anchor_ids),
        "approval_mode": approval_mode,
        "body_bytes_are_not_total_disk_budget": True,
    }


def _retained_source_ids(documents: Iterable[Any]) -> set[str]:
    retained: set[str] = set()
    for document in documents:
        retained.add(document.id)
        try:
            provenance = json.loads(document.provenance)
        except (ValueError, TypeError):
            provenance = []
        if isinstance(provenance, list):
            retained.update(
                item.split("@", 1)[0].removeprefix("memory:")
                for item in provenance
                if isinstance(item, str) and item.startswith("memory:")
            )
    return retained


def _signature(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verify_base(repository: Repository, rows: tuple[SyntheticRecord, ...]) -> None:
    documents = repository.list()
    expected = {row.memory_id: row for row in rows}
    if len(documents) != len(expected):
        raise RuntimeError("Existing benchmark base is partial or has a different record count")
    for document in documents:
        row = expected.get(document.id)
        if (
            row is None
            or document.body != row.text
            or document.title != f"{row.theme} {row.memory_id}"
        ):
            raise RuntimeError("Existing benchmark base does not match the requested dataset")


def _apply_synthetic_merges(repository: Repository, profile: BudgetProfile) -> None:
    """Use production judge proposals, explicit simulated approval, then fallback."""

    _write_config(repository, policy=PolicyName.CONSOLIDATE_THEN_FORGET, profile=profile)
    result = enforce_budget(
        repository,
        policy_override=PolicyName.CONSOLIDATE_THEN_FORGET,
        now=NOW,
        dry_run=False,
        actor="mechanism",
    )
    if result.unsatisfied_reason != "approval_required":
        return

    pending = [
        item
        for item in repository.list_proposals(status="pending")
        if item.proposal_id in result.pending_proposal_ids
    ]
    used_targets: set[str] = set()
    selected = []
    for proposal in pending:
        if used_targets.isdisjoint(proposal.target_ids):
            selected.append(proposal)
            used_targets.update(proposal.target_ids)
        else:
            decide_proposal(
                repository,
                proposal.proposal_id,
                "reject",
                payload_hash=proposal.payload_hash,
            )
    for proposal in selected:
        decide_proposal(
            repository,
            proposal.proposal_id,
            "approve",
            payload_hash=proposal.payload_hash,
        )
    for proposal in selected:
        apply_approved(
            repository,
            proposal.proposal_id,
            actor="human",
            enforce_fallback=False,
        )


def _capture_duty_seconds(session: CaptureSession, start: datetime, end: datetime) -> float:
    transitions = sorted(
        enumerate((event.timestamp, event.state) for event in session.events),
        key=lambda item: (item[1][0], item[0]),
    )
    state = CaptureState.DORMANT
    cursor = start
    full_seconds = 0.0
    for _index, (timestamp, next_state) in transitions:
        if timestamp < cursor:
            continue
        if state == CaptureState.FULL:
            full_seconds += (timestamp - cursor).total_seconds()
        state = next_state
        cursor = timestamp
    if state == CaptureState.FULL:
        full_seconds += (end - cursor).total_seconds()
    return full_seconds


def _capture_variant(rows: tuple[SyntheticRecord, ...], mode: str, root: Path) -> dict[str, Any]:
    sequence = [0]

    def event_id() -> str:
        sequence[0] += 1
        return f"capture-{mode}-{sequence[0]:08d}"

    repository = Repository(root, clock=lambda: NOW, id_factory=event_id)
    repository.init()
    before_bytes = measure_runtime(repository, now=NOW).physical_disk_bytes
    session = CaptureSession(
        repository=repository,
        session_id=f"day3-{mode}",
        initial_state=CaptureState.DORMANT,
        high_threshold=CAPTURE_THRESHOLD,
    )
    for row in rows:
        source = "human" if mode == "always_on" else "model"
        action = "remember" if mode == "always_on" else "content"
        intensity = 1.0 if mode == "always_on" else row.capture_intensity
        session.feed(
            TimedSignal(
                timestamp=datetime.fromisoformat(row.timestamp),
                source=source,
                action=action,
                intensity=intensity,
                text=row.text,
                source_id=row.memory_id,
            )
        )
    sample_step = timedelta(seconds=30 * 86400 / len(rows))
    start = datetime.fromisoformat(rows[0].timestamp)
    end = datetime.fromisoformat(rows[-1].timestamp) + sample_step
    session.feed(
        TimedSignal(
            timestamp=end,
            source="rule",
            action="sleep",
            intensity=0.0,
        )
    )
    candidates = session.items()
    captured_ids = {item.source_id for item in candidates if item.source_id is not None}
    important = {row.memory_id for row in rows if row.importance == "important"}
    noise = {row.memory_id for row in rows if row.importance == "noise"}
    quiet = {row.memory_id for row in rows if not row.awake}
    wake_rows = {row.timestamp: row for row in rows}
    full_wakes = [
        event
        for event in session.events
        if event.kind == EventKind.CAPTURE_WAKE
        and (mode != "always_on" and event.source == "model")
    ]
    false_wakes = sum(
        1
        for event in full_wakes
        if event.source == "model"
        and event.timestamp.isoformat() in wake_rows
        and wake_rows[event.timestamp.isoformat()].importance == "noise"
    )
    noise_captured = len(noise & captured_ids)
    quiet_noise_captured = len(noise & quiet & captured_ids)
    physical = measure_runtime(repository, now=NOW).physical_disk_bytes
    staged_bytes = sum(
        path.stat().st_size
        for path in (repository.root / ".staging" / "capture-candidates").glob("*.md")
    )
    total_seconds = max(1.0, (end - start).total_seconds())
    full_seconds = _capture_duty_seconds(session, start, end)
    return {
        "mode": mode,
        "captured_count": len(captured_ids),
        "captured_source_ids": sorted(captured_ids),
        "storage_bytes": physical,
        "physical_disk_bytes": physical,
        "physical_disk_delta_bytes": physical - before_bytes,
        "staged_markdown_bytes": staged_bytes,
        "duty_cycle": full_seconds / total_seconds,
        "full_capture_seconds": full_seconds,
        "elapsed_seconds": total_seconds,
        "missed_important_count": len(important - captured_ids),
        "important_recall": len(important & captured_ids) / len(important) if important else 0.0,
        "false_wake_count": false_wakes,
        "false_wake_rate": false_wakes / len(full_wakes) if full_wakes else None,
        "quiet_noise_captured_count": quiet_noise_captured,
        "quiet_noise_capture_rate": quiet_noise_captured / len(quiet) if quiet else 0.0,
        "noise_retention": noise_captured / len(noise) if noise else 0.0,
        "threshold": CAPTURE_THRESHOLD if mode == "intensity_awake" else None,
    }


def capture_results(
    records: Iterable[SyntheticRecord], *, work_root: Path | None = None
) -> list[dict[str, Any]]:
    """Run both capture modes through the real staged-vault path."""

    rows = tuple(records)
    temporary = (
        tempfile.TemporaryDirectory(prefix="memfw-day3-capture-") if work_root is None else None
    )
    if work_root is None:
        assert temporary is not None
        base = Path(temporary.name)
    else:
        base = work_root
    try:
        results = []
        for mode in ("always_on", "intensity_awake"):
            capture_root = base / f"capture-{mode.replace('_', '-')}"
            if capture_root.exists():
                shutil.rmtree(capture_root)
            results.append(_capture_variant(rows, mode, capture_root))
        return results
    finally:
        if temporary is not None:
            temporary.cleanup()


def _run_experiment(
    *,
    records: Iterable[SyntheticRecord] | None = None,
    work_root: Path | None = None,
    policies: Iterable[PolicyName] | None = None,
    profiles: Iterable[BudgetProfile] | None = None,
) -> dict[str, Any]:
    rows = tuple(records if records is not None else generate_dataset())
    temporary = tempfile.TemporaryDirectory(prefix="memfw-day3-") if work_root is None else None
    base_root = work_root or Path(temporary.name)  # type: ignore[union-attr]
    base_path = base_root / "base"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_signature = _signature({"records": [asdict(row) for row in rows], "now": NOW.isoformat()})
    base_manifest = base_root / "base-manifest.json"
    if (base_path / "events.jsonl").exists():
        base = Repository(base_path, clock=lambda: NOW)
        if base_manifest.exists():
            saved = json.loads(base_manifest.read_text(encoding="utf-8"))
            if saved != {"signature": base_signature, "record_count": len(rows)}:
                raise RuntimeError("Existing benchmark base manifest does not match this run")
        else:
            _verify_base(base, rows)
            base_manifest.write_text(
                json.dumps({"signature": base_signature, "record_count": len(rows)}, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
    else:
        if base_path.exists() and any(base_path.iterdir()):
            raise RuntimeError("Existing benchmark base is incomplete and has no event log")
        base = _seed_base(rows, base_path)
        base_manifest.write_text(
            json.dumps({"signature": base_signature, "record_count": len(rows)}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    initial = measure_runtime(base, now=NOW).as_dict()
    selected_policies = tuple(policies) if policies is not None else tuple(PolicyName)
    selected_profiles = tuple(profiles) if profiles is not None else tuple(BudgetProfile)
    checkpoint_path = base_root / "policy-results.json"
    results_signature = _signature(
        {
            "base_signature": base_signature,
            "policies": [policy.value for policy in selected_policies],
            "profiles": [profile.value for profile in selected_profiles],
        }
    )
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("signature") != results_signature:
            raise RuntimeError("Existing policy checkpoint belongs to a different run")
        results = checkpoint["results"]
    else:
        results = []
    completed = {(row["policy"], row["budget_profile"]) for row in results}
    for policy in selected_policies:
        for profile in selected_profiles:
            if (policy.value, profile.value) in completed:
                continue
            results.append(
                _run_one(
                    base_path,
                    base_root / f"{policy.value}-{profile.value}",
                    rows,
                    policy,
                    profile,
                )
            )
            checkpoint_path.write_text(
                json.dumps(
                    {"signature": results_signature, "results": results},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    captures = capture_results(rows, work_root=base_root)
    result = {
        "manifest": {
            "seed": SEED,
            "fixed_now_utc": NOW.isoformat(),
            "record_count": len(rows),
            "span_days": 30,
            "themes": sorted({row.theme for row in rows}),
            "policy_count": len(selected_policies),
            "budget_profiles": {
                profile.value: BYTE_BUDGETS[profile].max_bytes for profile in selected_profiles
            },
            "budget_metric": "physical allocated filesystem bytes from measure_runtime",
            "body_budget_note": (
                "UTF-8 body bytes are reported separately; they do not represent total disk use."
            ),
            "capture_metric_definitions": {
                "storage_bytes": (
                    "Allocated physical bytes per real capture vault, including event "
                    "logs and staged candidate Markdown."
                ),
                "duty_cycle": (
                    "Elapsed FULL-state seconds / elapsed stream seconds, from state "
                    "transition timestamps."
                ),
                "missed_important_count": (
                    "Important source IDs absent from staged capture candidates."
                ),
                "false_wake_count": (
                    "Model-caused FULL transitions at noise-labeled sample timestamps."
                ),
                "false_wake_rate": (
                    "false_wake_count / model-caused FULL transitions; null for always-on"
                ),
                "quiet_noise_capture_rate": (
                    "Noise IDs staged during quiet windows / all quiet-window IDs."
                ),
                "important_recall": "captured important source ids / all important source ids",
                "noise_retention": "retained noise source ids / all noise source ids",
            },
            "synthetic_data_limitations": (
                "Body sizes are synthetically padded with opaque random traces. Themes are "
                "assigned in contiguous time blocks, so topic and time are not independent. "
                "This measures runtime behavior, not natural conversational memory quality."
            ),
            "policy_feature_method": (
                "One hundred access events are timestamped across the corpus at a fixed "
                "one-in-ten index interval; reference counts, recency intervals, and sparse "
                "manual marks vary by index and are independent of ground-truth importance."
            ),
            "protected_anchor_count": 10,
            "capture_intensity_threshold": CAPTURE_THRESHOLD,
            "source_id_retention": (
                "Merged memories count their source IDs through recorded provenance. "
                "Anchor retention counts actual final anchor documents."
            ),
            "one_mb_physical_floor": (
                "The 1 MB total physical allocation profile remains unsatisfied when event "
                "logs, retained history, and protected anchors exceed the limit."
            ),
            "initial_vault_stats": initial,
            "llm_or_network_used": False,
        },
        "policy_results": results,
        "capture_results": captures,
    }
    if temporary is not None:
        temporary.cleanup()
    return result


def run_experiment(
    *,
    records: Iterable[SyntheticRecord] | None = None,
    work_root: Path | None = None,
    policies: Iterable[PolicyName] | None = None,
    profiles: Iterable[BudgetProfile] | None = None,
) -> dict[str, Any]:
    """Run one exclusive experiment against a persistent or temporary work root."""

    if work_root is None:
        return _run_experiment(records=records, policies=policies, profiles=profiles)
    work_root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(work_root / ".experiment.lock"), timeout=0)
    try:
        with lock:
            return _run_experiment(
                records=records,
                work_root=work_root,
                policies=policies,
                profiles=profiles,
            )
    except Timeout as exc:
        raise RuntimeError(f"Experiment work root is already in use: {work_root}") from exc


def markdown_report(result: dict[str, Any]) -> str:
    manifest = result["manifest"]
    lines = [
        "# Day 3 deterministic memory and capture experiment",
        "",
        "Policy runs use cloned real vaults and the production budget engine. Total-mode",
        "limits compare actual allocated filesystem bytes. Body bytes are shown separately.",
        "",
        "| Policy | Profile | Important recall | Noise retention | Final disk bytes | Satisfied |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for row in result["policy_results"]:
        lines.append(
            f"| {row['policy']} | {row['budget_profile']} | {row['important_recall']:.3f} | "
            f"{row['noise_retention']:.3f} | {row['final_physical_disk_bytes']:,} | "
            f"{row['budget_satisfied']} |"
        )
    lines.extend(
        [
            "",
            "## Capture comparison",
            "",
            "| Mode | Physical bytes | Duty cycle | Missed important | False wakes | "
            "Noise retention |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["capture_results"]:
        lines.append(
            f"| {row['mode']} | {row['physical_disk_bytes']:,} | {row['duty_cycle']:.3f} | "
            f"{row['missed_important_count']} | {row['false_wake_count']} | "
            f"{row['noise_retention']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Reproduction and metric formulas",
            "",
            f"- Seed `{manifest['seed']}`, fixed UTC time `{manifest['fixed_now_utc']}`.",
            f"- {manifest['record_count']} records across 30 days and "
            f"{len(manifest['themes'])} themes.",
            "- Important recall = retained important source IDs / all important source IDs.",
            "- Merged source IDs count as retained when recorded in the representative's "
            "provenance. Noise retention = retained noise source IDs / all noise source IDs.",
            "- Capture storage uses physical bytes from both real vaults. Both variants use "
            "the same session, log, and staging path.",
            "- Duty cycle = elapsed FULL-state seconds / elapsed stream seconds.",
            "- False wake count = model-caused FULL transitions at noise-labeled input "
            "timestamps. Rate divides by all model-caused FULL transitions; always-on has "
            "no automatic wake denominator and reports null.",
            "- Quiet noise capture rate = staged noise IDs during quiet windows / all quiet-"
            "window IDs.",
            f"- Intensity capture threshold = {manifest['capture_intensity_threshold']:.2f}.",
            "- Ten protected anchors are checked against actual final anchor documents.",
            "- The 1 MB total physical limit can remain unsatisfied because event logs, "
            "retained history, and protected anchors count toward allocated bytes.",
            "- Missed important = important source IDs absent from staged candidates.",
            "- Consolidation uses synthetic-only human-approved proposals, then applies "
            "the configured FIFO total-budget fallback. Proposals come from the production "
            "deterministic judge, not ground-truth duplicate labels.",
            f"- Synthetic data limitation: {manifest['synthetic_data_limitations']}",
            f"- Policy features: {manifest['policy_feature_method']}",
            "- No model or network calls are used.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(*, work_root: Path | None = None) -> dict[str, Any]:
    write_dataset()
    result = run_experiment(work_root=work_root)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    REPORT_MARKDOWN.write_text(markdown_report(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reports", action="store_true")
    parser.add_argument("--work-root", type=Path, default=None)
    args = parser.parse_args()
    result = (
        write_reports(work_root=args.work_root)
        if args.write_reports
        else run_experiment(work_root=args.work_root)
    )
    print(markdown_report(result))


if __name__ == "__main__":
    main()
