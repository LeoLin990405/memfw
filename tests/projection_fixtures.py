"""Synthetic vault + meta-index for the projection / resolve / join tests.

Everything here is invented: no real memories, paths, names or hosts. The vault
is written through the real :class:`memfw.core.repository.Repository` so the
event log is genuine; a few documents are then patched on disk to carry fields
``add`` cannot set (``valid_to``, ``superseded_by``, ``is_stub``, ``status``,
``summary``, ``about``), exactly like an imported vault that a newer writer
touched. ``build_search_vault`` is the clean variant used for the
``index search`` byte-identity baseline (no on-disk patches).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from memfw.core.models import Layer, Tier
from memfw.core.repository import Repository

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projection"
CLOCK = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
META_GENERATED_AT = "2026-11-10T00:05:00Z"


def stable_id(rel: str) -> str:
    return "f" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def _doc(rel: str | None, title: str, body: str, **extra: Any) -> dict[str, Any]:
    memory_id = extra.pop("memory_id", None) or (stable_id(rel) if rel else None)
    tags = list(extra.pop("tags", []))
    if rel is not None and not extra.pop("no_fleet_tag", False):
        tags.insert(0, f"fleet_path:{rel}")
    return {"rel": rel, "id": memory_id, "title": title, "body": body, "tags": tags, **extra}


#: name -> document spec. ``meta`` is the meta-index item (None = no item).
DOCS: dict[str, dict[str, Any]] = {
    "active": _doc(
        "example_gen8_cron.md",
        "Example: pause the cron before a disk migration",
        "Synthetic body about pausing cron jobs before migrating disks.",
        tags=["nas1", "cron", "migration"],
        memory_type="fact",
        patch={"summary": "Pause cron first.", "about": ["hd-migration"], "applies_to": ["nas1"]},
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "retired": _doc(
        "incident_retired_example.md",
        "Retired incident example",
        "Synthetic retired incident.",
        meta={"lifecycle": "retired", "privacy": None, "scope": "fleet"},
    ),
    "change_event": _doc(
        "feedback-change-event-20261101-0001.md",
        "change event 0001",
        '{"kind": "update", "path": "example_gen8_cron.md"}',
        memory_type="change-event",
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "private": _doc(
        "private_example.md",
        "[private]",
        "Synthetic private body.",
        tags=["sensitivity:private"],
        meta={"lifecycle": "active", "privacy": "local-private", "scope": "personal"},
    ),
    "privacy_mismatch": _doc(
        "privacy_mismatch_example.md",
        "Privacy mismatch example",
        "Meta-index says local-private, vault tag missing.",
        meta={"lifecycle": "active", "privacy": "local-private", "scope": "fleet"},
    ),
    "privacy_unknown": _doc(
        "privacy_unknown_example.md",
        "Privacy unknown example",
        "Meta-index privacy has an unexpected value.",
        meta={"lifecycle": "active", "privacy": "team-secret", "scope": "fleet"},
    ),
    "native_no_tag": _doc(
        None,
        "Native memory without fleet_path",
        "Written natively, never imported.",
        memory_id="native-note-0001",
        meta=None,
    ),
    "orphan": _doc(
        "orphan_not_in_meta.md",
        "Orphan: tag present, meta-index item absent",
        "Synthetic orphan.",
        meta=None,
    ),
    "id_mismatch": _doc(
        "id_mismatch_example.md",
        "Id mismatch example",
        "The f+12hex id does not match sha256(fleet_path).",
        memory_id="f000000000000",
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "two_tags": _doc(
        "two_tags_a.md",
        "Two fleet_path tags",
        "Ambiguous join.",
        tags=["fleet_path:two_tags_b.md"],
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "expired_vault": _doc(
        "expired_vault_example.md",
        "Expired via vault valid_to",
        "Synthetic expired.",
        patch={"valid_to": "2026-01-01T00:00:00Z"},
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "expired_meta": _doc(
        "expired_meta_example.md",
        "Expired via meta-index valid_until",
        "Synthetic expired.",
        meta={
            "lifecycle": "active",
            "privacy": None,
            "scope": "fleet",
            "valid_until": "2026-02-01",
        },
    ),
    "expiry_unparsable": _doc(
        "expiry_unparsable_example.md",
        "Unparsable expiry",
        "Synthetic.",
        meta={
            "lifecycle": "active",
            "privacy": None,
            "scope": "fleet",
            "valid_until": "next tuesday",
        },
    ),
    "superseded_vault": _doc(
        "superseded_vault_example.md",
        "Superseded via vault superseded_by",
        "Synthetic superseded.",
        patch={"superseded_by": stable_id("example_gen8_cron.md")},
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "superseded_meta": _doc(
        "superseded_meta_example.md",
        "Superseded via meta-index lifecycle",
        "Synthetic superseded.",
        meta={"lifecycle": "superseded", "privacy": None, "scope": "fleet"},
    ),
    "cold": _doc(
        "cold_example.md",
        "Cold tier example",
        "Synthetic cold.",
        tier=Tier.COLD,
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "stub": _doc(
        "stub_example.md",
        "Stub example",
        "Synthetic stub summary.",
        patch={"is_stub": True},
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "tenant_other": _doc(
        "tenant_other_example.md",
        "Other tenant example",
        "Synthetic tenant.",
        tenant="other-team",
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "disputed": _doc(
        "disputed_example.md",
        "Disputed example",
        "Synthetic disputed.",
        patch={"status": "disputed"},
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "anchor": _doc(
        "anchor_example.md",
        "Anchor example",
        "Synthetic anchor.",
        layer=Layer.ANCHOR,
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "r3_hypothesis": _doc(
        "r3_hypothesis_example.md",
        "R3 causal hypothesis example",
        "Synthetic causal hypothesis.",
        epistemic="inference",
        causal="hypothesis",
        causal_record={
            "treatment": "pause cron",
            "outcome": "migration not interrupted",
            "estimand": "ATE",
            "identification": {"assumptions": ["no confounding"], "untested": ["no confounding"]},
        },
        limitations=["single observation"],
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "eval_native": _doc(
        "zeval/zeval-0001.md",
        "Eval probe (non f-id)",
        "Synthetic eval probe.",
        memory_id="zeval-0001",
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
}


def meta_index(docs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    items = []
    for spec in (docs or DOCS).values():
        meta = spec.get("meta")
        if meta is None or spec["rel"] is None:
            continue
        item = {
            "path": spec["rel"],
            "lifecycle": meta.get("lifecycle"),
            "privacy": meta.get("privacy"),
            "valid_until": meta.get("valid_until"),
            "scope": meta.get("scope"),
            "sensitivity": "fleet",
        }
        if "superseded_by" in meta:
            item["superseded_by"] = meta["superseded_by"]
        items.append(item)
    return {
        "generated_at": META_GENERATED_AT,
        "root": "/synthetic/files",
        "stats": {"items": len(items)},
        "items": items,
    }


def _repository(root: Path) -> Repository:
    counter = iter(range(10_000))
    return Repository(
        root,
        clock=lambda: CLOCK,
        id_factory=lambda: hashlib.sha256(f"seed:{next(counter)}".encode()).hexdigest()[:32],
    )


def _add(repository: Repository, spec: dict[str, Any]) -> None:
    repository.add(
        spec["title"],
        spec["body"],
        memory_id=spec["id"],
        layer=spec.get("layer", Layer.CURRENT),
        tier=spec.get("tier", Tier.HOT),
        memory_type=spec.get("memory_type"),
        tenant=spec.get("tenant", ""),
        tags=spec["tags"],
        epistemic=spec.get("epistemic"),
        causal=spec.get("causal", "none"),
        causal_record=spec.get("causal_record"),
        limitations=spec.get("limitations"),
        source="fleet-import",
        provenance="imported",
    )


def _patch(path: Path, changes: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    _, raw, body = text.split("---\n", 2)
    metadata = yaml.safe_load(raw)
    metadata.update(changes)
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body,
        encoding="utf-8",
    )


def build_vault(tmp: Path, docs: dict[str, dict[str, Any]] | None = None) -> tuple[Path, Path]:
    """Write the synthetic vault and meta-index; return ``(vault_root, meta_index_path)``."""

    docs = docs or DOCS
    root = tmp / "vault"
    repository = _repository(root)
    repository.init()
    for spec in docs.values():
        _add(repository, spec)
    for spec in docs.values():
        if spec.get("patch"):
            _patch(root / "memories" / f"{spec['id']}.md", spec["patch"])
    meta_path = tmp / "status" / "memory-meta-index.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(meta_index(docs), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root, meta_path


SEARCH_DOCS: list[tuple[str, str, str]] = [
    ("f1a2b3c4d5e60", "Pause cron before disk migration", "cron jobs must stop before migrating"),
    ("f1a2b3c4d5e61", "Disk migration checklist", "migration steps: backup, verify, cut over"),
    ("f1a2b3c4d5e62", "迁移前先暂停定时任务", "迁移磁盘之前暂停 cron，避免任务中断"),
    ("f1a2b3c4d5e63", "Unrelated note", "nothing about the topic here"),
]


def build_search_vault(tmp: Path) -> Path:
    """Clean vault (no on-disk patches) for the ``index search`` baseline."""

    root = tmp / "search-vault"
    repository = _repository(root)
    repository.init()
    for memory_id, title, body in SEARCH_DOCS:
        repository.add(title, body, memory_id=memory_id, tags=[f"fleet_path:{memory_id}.md"])
    return root


SEARCH_QUERIES: list[str] = ["cron", "migration", "迁移", "absent-term-xyz"]


def tree_digest(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root`` (for read-only assertions)."""

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


#: Extra documents for resolve tests that need shapes ``DOCS`` does not have;
#: kept separate so the export counts on ``DOCS`` stay fixed.
EXTRA_DOCS: dict[str, dict[str, Any]] = {
    "multiline": _doc(
        "multiline_example.md",
        "Multi-line body example",
        "line one\nline two\nline three\nline four\nline five",
        meta={"lifecycle": "active", "privacy": None, "scope": "fleet"},
    ),
    "superseded_meta_by": _doc(
        "superseded_meta_by_example.md",
        "Superseded via meta-index superseded_by",
        "Synthetic superseded.",
        meta={
            "lifecycle": "active",
            "privacy": None,
            "scope": "fleet",
            "superseded_by": stable_id("example_gen8_cron.md"),
        },
    ),
}


def patch_frontmatter(path: Path, changes: dict[str, Any]) -> None:
    """Public alias of the on-disk frontmatter patch (tests mutate tmp vaults)."""

    _patch(path, changes)


def write_meta(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
