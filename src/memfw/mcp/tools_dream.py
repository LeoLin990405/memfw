"""``dream_run`` and ``dream_list``: dry runs and staged dreams under _candidates/dream/."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from memfw.dream.run import DreamOptions, run_dream
from memfw.dream.stage import DREAM_DIR
from memfw.mcp.base import GatewayBase

DREAM_MAX_LIMIT = 1000
DREAM_MAX_SESSIONS = 100
"""The dreams reference reads 1 to 100 sessions; memfw starts small (20)."""
DREAM_MAX_CANDIDATES = 200
"""Candidates returned by one dream_run call; the rest are counted, and staged in full."""
DREAM_LIST_MAX = 200
DREAM_MAX_STAGED = 50
"""Staged dreams one tenant may have waiting for review before dream_run refuses to stage."""
DREAM_STAGE_INTERVAL_SECONDS = 60.0
"""Least time between two staged dream_run calls of one server and tenant."""


_REPORT_MAX_BYTES = 1024 * 1024


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


class DreamTools(GatewayBase):
    """Dream proposals for this vault and tenant (never the vault itself)."""

    def dream_run(
        self,
        *,
        dry_run: bool = True,
        focus: str = "",
        all_memories: bool = False,
        limit: int = 200,
        max_sessions: int = 20,
        source: str = "",
        since: str = "",
        until: str = "",
        stale_days: int = 180,
        min_events: int = 2,
    ) -> dict[str, Any]:
        """One dream over this vault and tenant. Never loads a judge (no code from callers)."""

        if not 0 <= limit <= DREAM_MAX_LIMIT:
            raise ValueError(f"limit must be between 0 and {DREAM_MAX_LIMIT}")
        if not 1 <= max_sessions <= DREAM_MAX_SESSIONS:
            raise ValueError(f"max_sessions must be between 1 and {DREAM_MAX_SESSIONS}")
        if stale_days < 0:
            raise ValueError("stale_days must be >= 0")
        repository = self._repo()
        if not dry_run:
            self._check_stage_allowed()
        options = DreamOptions(
            tenant=self.tenant,
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
            # Never build or refresh the transcript index from MCP: staging writes
            # only the dream's candidates (the index is the operator's CLI job).
            refresh=False,
            read_only_history=True,
        )
        result = run_dream(
            repository.root, now=self.dream_clock(), dry_run=dry_run, options=options
        ).as_dict()
        if not dry_run:
            self._last_staged = time.monotonic()
        candidates = result["candidates"]
        result["candidates"] = candidates[:DREAM_MAX_CANDIDATES]
        result["candidates_truncated"] = max(0, len(candidates) - DREAM_MAX_CANDIDATES)
        return result

    def _check_stage_allowed(self) -> None:
        last = self._last_staged
        if last is not None and time.monotonic() - last < DREAM_STAGE_INTERVAL_SECONDS:
            raise ValueError(
                f"a dream was staged less than {DREAM_STAGE_INTERVAL_SECONDS:g}s ago; "
                "wait, or use dry_run"
            )
        staged, _skipped = self._staged_dreams()
        if len(staged) >= DREAM_MAX_STAGED:
            raise ValueError(
                f"{len(staged)} staged dreams are waiting for review (at most "
                f"{DREAM_MAX_STAGED}); review or remove some, or use dry_run"
            )

    def _staged_dreams(self) -> tuple[list[dict[str, Any]], list[str]]:
        """This tenant's staged dreams (newest first) and the entries skipped."""

        base = self._repo().root
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
            if str(report.get("tenant") or "") != self.tenant:
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

    def dream_list(self, limit: int = 20) -> dict[str, Any]:
        """Staged dreams of this tenant, newest first. Symlinks are skipped, never followed."""

        if not 1 <= limit <= DREAM_LIST_MAX:
            raise ValueError(f"limit must be between 1 and {DREAM_LIST_MAX}")
        dreams, skipped = self._staged_dreams()
        return {"dreams": dreams[:limit], "skipped": skipped, "authority": False}
