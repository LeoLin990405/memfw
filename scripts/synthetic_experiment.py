#!/usr/bin/env python3
"""Run a reproducible 500-memory, 30-day policy comparison without an LLM."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memfw.budget import BYTE_BUDGETS, BudgetProfile
from memfw.policies import PolicyCandidate, PolicyName, make_policy

SEED = 20261018
NOW = datetime(2026, 1, 31, tzinfo=UTC)
ITEM_COUNT = 500
SPAN_DAYS = 30
DUPLICATE_PAIR_COUNT = 170
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = REPOSITORY_ROOT / "reports" / "day2_policy_experiment.json"
REPORT_MARKDOWN = REPOSITORY_ROOT / "reports" / "day2_policy_experiment.md"


def fixture(seed: int = SEED) -> tuple[PolicyCandidate, ...]:
    """Create deterministic records with 15 MB of exact UTF-8 body content."""

    rng = random.Random(seed)
    created_start = NOW - timedelta(days=SPAN_DAYS)
    candidates: list[PolicyCandidate] = []
    for index in range(ITEM_COUNT):
        created_at = created_start + timedelta(seconds=SPAN_DAYS * 86400 * index / ITEM_COUNT)
        if index < DUPLICATE_PAIR_COUNT * 2:
            group_index = index // 2
            group = f"exact-pair-{group_index:03d}"
            payload_prefix = f"synthetic payload {group}\n"
            group_key = None
        else:
            group_index = index
            group = f"unique-{index:03d}"
            payload_prefix = f"synthetic payload unique-{index:03d}\n"
            group_key = None
        body_size = 10_000 + ((group_index * 7_919 + 11_337) % 40_001)
        body = payload_prefix + ("x" * (body_size - len(payload_prefix.encode("utf-8"))))

        # Explicit metadata values are seeded, bounded, and independent of system time.
        access_count = rng.randrange(0, 20)
        reference_count = rng.randrange(0, 8)
        strength = round(rng.random(), 6)
        interval_days = round(rng.uniform(0.0, 45.0), 3)
        access_age_days = rng.randrange(0, SPAN_DAYS + 1)
        last_accessed = max(created_at, NOW - timedelta(days=access_age_days))
        pinned = index % 100 == 0
        candidates.append(
            PolicyCandidate(
                id=f"m-{index:03d}",
                body=body,
                created_at=created_at,
                updated_at=created_at + timedelta(hours=index % 48),
                last_accessed_at=last_accessed,
                access_count=access_count,
                reference_count=reference_count,
                strength=strength,
                keyness=round(rng.random(), 6),
                manually_marked=(index % 37 == 0),
                last_access_interval_days=interval_days,
                group_key=group_key,
                layer="anchor" if pinned else "current",
                anchor=f"experiment-pin-{index}" if pinned else None,
                metadata={"synthetic_group": group},
            )
        )
    return tuple(candidates)


def run_experiment(seed: int = SEED) -> dict[str, Any]:
    candidates = fixture(seed)
    sizes = [item.byte_size for item in candidates]
    rows: list[dict[str, Any]] = []
    for strategy in PolicyName:
        for profile in BudgetProfile:
            plan = make_policy(strategy).plan(
                candidates,
                BYTE_BUDGETS[profile],
                now=NOW,
            )
            anchor_ids = {item.id for item in candidates if item.is_anchor}
            forgotten_anchor = len(anchor_ids.intersection(plan.forgotten_ids))
            rows.append(
                {
                    "strategy": strategy.value,
                    "budget": profile.value,
                    "input_count": len(candidates),
                    "input_bytes": sum(item.byte_size for item in candidates),
                    "retained_count": len(plan.retained_ids),
                    "retained_bytes": plan.usage.bytes,
                    "forgotten_count": len(plan.forgotten_ids),
                    "forgotten_anchor": forgotten_anchor,
                    "satisfied": plan.satisfied,
                    "unsatisfied_bytes": max(0, plan.usage.bytes - (plan.limits.max_bytes or 0)),
                    "consolidation_proposals": len(plan.consolidation_proposals),
                }
            )
    return {
        "manifest": {
            "seed": seed,
            "now_utc": NOW.isoformat(),
            "input_count": ITEM_COUNT,
            "span_days": SPAN_DAYS,
            "body_size_bytes_min": min(sizes),
            "body_size_bytes_max": max(sizes),
            "body_size_rule": (
                "10000 + ((group_index * 7919 + 11337) mod 40001); "
                "duplicate pair shares group_index"
            ),
            "exact_duplicate_pairs": DUPLICATE_PAIR_COUNT,
            "anchor_rule": "every 100th item; layer=anchor and anchor field set",
            "byte_definition": (
                "sum of UTF-8 encoded body bytes; excludes frontmatter, event log, "
                "index, archive and filesystem overhead"
            ),
            "budget_profiles": {
                profile.value: limits.max_bytes for profile, limits in BYTE_BUDGETS.items()
            },
            "metadata_rng": "Python random.Random(seed), fixed integer seed",
            "policy_path": "memfw.policies.make_policy(...).plan(..., now=NOW)",
            "llm_used": False,
        },
        "results": rows,
    }


def markdown_report(result: dict[str, Any]) -> str:
    manifest = result["manifest"]
    lines = [
        "# Day 2 policy experiment",
        "",
        "This report uses the production pure policy selector with a fixed synthetic input.",
        "Byte usage counts UTF-8 body bytes only; it excludes frontmatter, logs, "
        "indexes, archives, and filesystem overhead.",
        "",
        "| Strategy | Budget | Kept | Body bytes | Forgotten | Anchors lost | Fits | Proposals |",
        "|---|---:|---:|---:|---:|---:|:---:|---:|",
    ]
    for row in result["results"]:
        lines.append(
            f"| {row['strategy']} | {row['budget']} | {row['retained_count']} | "
            f"{row['retained_bytes']:,} | {row['forgotten_count']} | "
            f"{row['forgotten_anchor']} | {row['satisfied']} | "
            f"{row['consolidation_proposals']} |"
        )
    lines.extend(
        [
            "",
            "## Reproduction inputs",
            "",
            f"- Seed: `{manifest['seed']}`.",
            f"- Fixed now: `{manifest['now_utc']}`.",
            f"- Records: {manifest['input_count']} over {manifest['span_days']} days.",
            f"- Body size: {manifest['body_size_bytes_min']:,} to "
            f"{manifest['body_size_bytes_max']:,} UTF-8 bytes by deterministic rule; "
            f"exact duplicate pairs: {manifest['exact_duplicate_pairs']:,}.",
            f"- Total input: {result['results'][0]['input_bytes']:,} body bytes.",
            "- Budgets use decimal MB: 1 MB = 1,000,000 bytes.",
            "- No LLM or system clock is used. Consolidation uses exact body hashes only.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports() -> dict[str, Any]:
    result = run_experiment()
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    REPORT_MARKDOWN.write_text(markdown_report(result), encoding="utf-8")
    return result


if __name__ == "__main__":
    report = write_reports()
    print(markdown_report(report))
