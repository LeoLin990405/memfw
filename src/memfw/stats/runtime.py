"""Vault storage accounting, separate from deterministic policy body-byte budgets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from memfw.core.models import Tier
from memfw.core.repository import Repository


@dataclass(frozen=True, slots=True)
class RuntimeStats:
    memory_count: int
    active_logical_bytes: int
    hot_logical_bytes: int
    cold_logical_bytes: int
    physical_vault_bytes: int
    history_bytes: int
    archive_bytes: int
    index_bytes: int
    events_bytes: int = 0
    memories_bytes: int = 0
    anchors_bytes: int = 0
    other_bytes: int = 0
    logical_vault_bytes: int = 0
    logical_history_bytes: int = 0
    logical_archive_bytes: int = 0
    logical_index_bytes: int = 0
    logical_events_bytes: int = 0
    logical_memories_bytes: int = 0
    logical_anchors_bytes: int = 0
    logical_other_bytes: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def measure_vault(repository: Repository) -> RuntimeStats:
    documents = repository.list()
    breakdown = measure_physical_breakdown(repository)
    logical = measure_logical_breakdown(repository)
    physical = sum(breakdown.values())
    history = _directory_size(repository.history_dir)
    archives = _directory_size(repository.archives_dir)
    index = repository.index_path.stat().st_size if repository.index_path.exists() else 0
    active = sum(len(doc.body.encode("utf-8")) for doc in documents if not doc.is_stub)
    hot = sum(
        len(doc.body.encode("utf-8"))
        for doc in documents
        if not doc.is_stub and doc.tier == Tier.HOT
    )
    cold = sum(
        len(doc.body.encode("utf-8"))
        for doc in documents
        if not doc.is_stub and doc.tier == Tier.COLD
    )
    return RuntimeStats(
        len(documents),
        active,
        hot,
        cold,
        physical,
        history,
        archives,
        index,
        breakdown["events"],
        breakdown["memories"],
        breakdown["anchors"],
        breakdown["other"],
        sum(logical.values()),
        logical["history"],
        logical["archives"],
        logical["index"],
        logical["events"],
        logical["memories"],
        logical["anchors"],
        logical["other"],
    )


def measure_physical_breakdown(repository: Repository) -> dict[str, int]:
    """Return non-overlapping allocated-byte categories that sum to the vault total."""
    return _measure_breakdown(repository, allocated=True)


def measure_logical_breakdown(repository: Repository) -> dict[str, int]:
    """Return non-overlapping logical file sizes; allocated bytes are reported separately."""
    return _measure_breakdown(repository, allocated=False)


def _measure_breakdown(repository: Repository, *, allocated: bool) -> dict[str, int]:
    anchor_ids = {
        document.id
        for document in repository.list()
        if document.layer.value == "anchor" or bool(document.anchor)
    }
    totals = {
        name: 0
        for name in ("events", "history", "memories", "anchors", "archives", "index", "other")
    }
    paths = repository.root.rglob("*") if repository.root.exists() else ()
    for path in paths:
        if (
            not path.is_file()
            or path.name in {".memfw.lock", ".pending.json"}
            or path.name.endswith(".tmp")
        ):
            continue
        parts = path.relative_to(repository.root).parts
        if parts[0] in {"events.jsonl", "events.head", ".log"}:
            category = "events"
        elif parts[0] == ".history":
            category = "history"
        elif parts[0] == "memories":
            category = "anchors" if path.stem in anchor_ids else "memories"
        elif parts[0] == ".archives":
            category = "archives"
        elif path.name in {"index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm"}:
            category = "index"
        else:
            category = "other"
        stat = path.stat()
        totals[category] += (
            stat.st_blocks * 512 if allocated and hasattr(stat, "st_blocks") else stat.st_size
        )
    return totals


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
