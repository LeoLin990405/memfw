"""``dream_run`` and ``dream_list`` below the interface (A3b).

Moved from ``memfw.mcp.tools_dream``: the dream options an interface may
pass (never a judge, never a transcript index refresh) and the listing of
staged dreams under ``_candidates/dream/``. The ``DREAM_*`` limits stay with
the interface.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from memfw.core.repository import Repository
from memfw.dream.run import DreamOptions, run_dream
from memfw.dream.stage import DREAM_DIR

_REPORT_MAX_BYTES = 1024 * 1024


def run_interface_dream(
    repository: Repository,
    *,
    now: datetime,
    dry_run: bool,
    tenant: str,
    focus: str,
    all_memories: bool,
    limit: int,
    max_sessions: int,
    source: str,
    since: str,
    until: str,
    stale_days: int,
    min_events: int,
) -> dict[str, Any]:
    """One dream over this vault and tenant. Never loads a judge (no code from callers)."""

    options = DreamOptions(
        tenant=tenant,
        focus=focus,
        all_memories=all_memories,
        limit=limit,
        max_sessions=max_sessions,
        source=source or None,
        since=since or None,
        until=until or None,
        stale_days=stale_days,
        min_events=min_events,
        # The judge is code that reads transcripts: only the operator's CLI picks
        # one. vault.yaml's dream_judge is never honoured here either.
        judge=None,
        allow_config_judge=False,
        # Never build or refresh the transcript index from an interface: staging
        # writes only the dream's candidates (the index is the operator's CLI job).
        refresh=False,
        read_only_history=True,
    )
    result: dict[str, Any] = run_dream(
        repository.root, now=now, dry_run=dry_run, options=options
    ).as_dict()
    return result


def _read_dream_report(path: Path) -> dict[str, Any] | None:
    """A staged dream's report.json, or None (symlink, not a file, too big, not JSON)."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        raw = handle.read(_REPORT_MAX_BYTES + 1)
    if len(raw) > _REPORT_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def staged_dreams(repository: Repository, *, tenant: str) -> tuple[list[dict[str, Any]], list[str]]:
    """This tenant's staged dreams (newest first) and the entries skipped.

    Symlinks are skipped, never followed.
    """

    base = repository.root
    for part in DREAM_DIR.parts:
        base = base / part
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            return [], [str(DREAM_DIR)]
    if not base.is_dir():
        return [], []
    dreams: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in sorted(os.scandir(base), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue  # a dream being written (temporary directory)
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            skipped.append(entry.name)
            continue
        report = _read_dream_report(Path(entry.path) / "report.json")
        if report is None:
            skipped.append(entry.name)
            continue
        if str(report.get("tenant") or "") != tenant:
            continue
        counts = report.get("candidates")
        dreams.append(
            {
                "dream_id": str(report.get("dream_id") or entry.name),
                "created_at": str(report.get("created_at") or ""),
                "recorded_at": str(report.get("recorded_at") or ""),
                "candidates": counts if isinstance(counts, dict) else {},
                "path": (DREAM_DIR / entry.name).as_posix(),
            }
        )
    dreams.sort(key=lambda item: (item["created_at"], item["dream_id"]), reverse=True)
    return dreams, skipped


__all__ = ["run_interface_dream", "staged_dreams"]
