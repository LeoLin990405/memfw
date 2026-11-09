"""Delete requests and soft delete. Hard delete is a plan, not an implementation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from memfw.core.repository import Repository
from memfw.index import MemoryIndex


def hard_delete_plan(memory_id: str, *, content_hash: str, previous_hash: str) -> dict[str, Any]:
    """Describe a tombstone. Nothing in this function erases a file."""

    return {
        "implemented": False,
        "memory_id": memory_id,
        "keep": ["content_hash", "previous_hash"],
        "content_hash": content_hash,
        "previous_hash": previous_hash,
        "blank_body": True,
        "tombstone_event": True,
        "verify": "link_only",
    }


def apply_soft_delete(
    repository: Repository,
    memory_id: str,
    *,
    tenant: str,
    now: datetime,
) -> None:
    """Close one memory in its own tenant and rebuild the disposable index."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    document = repository.get(memory_id)
    if document.tenant != tenant:
        raise ValueError("memory is not visible in this tenant")
    repository.update(memory_id, valid_to=now)
    MemoryIndex(repository.index_path).rebuild(repository)


def projection_residue(repository: Repository) -> list[str]:
    """Open index rows whose Markdown file is already gone."""

    path = repository.index_path
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT r.id, COALESCE(t.valid_to, '') "
            "FROM memory_rows r LEFT JOIN memory_temporal t ON t.id = r.id "
            "ORDER BY r.id"
        ).fetchall()
    residue: list[str] = []
    for memory_id, valid_to in rows:
        if valid_to:
            continue
        file_path = Path(repository.root) / "memories" / f"{memory_id}.md"
        if not file_path.is_file():
            residue.append(str(memory_id))
    return residue
