"""Orchestrate one dream: verify, curate, stub ③/④, budget check, stage.

The dream reads the vault and the transcript index and writes only
candidates under ``_candidates/dream/<dream_id>/``. With ``dry_run`` it writes
nothing at all (it may still refresh the derived transcript index).
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.history import (
    HistoryError,
    HistoryLimits,
    TranscriptIndex,
    default_db,
    sources_from_config,
)

from .budget import budget_candidates, check_budget
from .curate import curate
from .model import Candidate
from .stage import check_dream_id, stage
from .verify import Judge, cited, is_current, verify

MAX_SESSIONS = 20
"""Default cap on transcript sessions one dream reads (start small)."""


@dataclass
class DreamOptions:
    tenant: str = ""
    focus: str = ""
    all_memories: bool = False
    limit: int = 200
    max_sessions: int = MAX_SESSIONS
    source: str | None = None
    since: str | None = None
    until: str | None = None
    refresh: bool = True
    stale_days: int = 180
    dream_id: str | None = None
    judge: str | None = None
    allow_config_judge: bool = False
    """Honour ``dream_judge`` from vault.yaml. Off: the vault must not choose code to run."""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DreamResult:
    dream_id: str
    dry_run: bool
    candidates: list[Candidate]
    report: dict[str, Any]
    staged: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dream_id": self.dream_id,
            "dry_run": self.dry_run,
            "staged": self.staged,
            "authority": False,
            "report": self.report,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def load_judge(spec: str | None) -> Judge | None:
    """Load an optional local judge ``module:function``. Off unless named."""

    if not spec:
        return None
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError("judge must look like module:function")
    function = getattr(importlib.import_module(module_name), attribute)
    if not callable(function):
        raise ValueError("judge is not callable")
    return cast(Judge, function)


def _dream_id(repository: Repository, options: DreamOptions, now: datetime) -> str:
    if options.dream_id:
        return check_dream_id(options.dream_id)
    events = repository.events()
    head = events[-1].event_hash if events else "0" * 64
    seed = f"{head}|{options.tenant}|{options.focus}|{options.all_memories}|{options.limit}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return f"dream-{now.strftime('%Y%m%dT%H%M%SZ')}-{digest}"


def _history(
    repository: Repository, config: dict[str, Any], refresh: bool
) -> tuple[TranscriptIndex | None, dict[str, Any]]:
    try:
        sources = sources_from_config(config)
    except HistoryError as exc:
        return None, {"available": False, "reason": str(exc)}
    if not sources:
        return None, {"available": False, "reason": "no history_sources in vault.yaml"}
    index = TranscriptIndex(
        default_db(repository.root), sources, limits=HistoryLimits.from_config(config)
    )
    info: dict[str, Any] = {"available": True, "sources": sorted(item.name for item in sources)}
    if refresh:
        try:
            counts = index.refresh(blocking=False)
            info["refresh"] = {
                key: counts[key]
                for key in ("files_indexed", "files_resumed", "skipped_secret", "skipped_locked")
            }
        except HistoryError as exc:
            info["refresh_error"] = str(exc)
    return index, info


def run_dream(
    root: Path,
    *,
    now: datetime,
    dry_run: bool = False,
    options: DreamOptions | None = None,
) -> DreamResult:
    """Run one dream over the vault at ``root``."""

    options = options or DreamOptions()
    repository = Repository(root)
    if not repository.config_path.exists():
        raise ValueError(f"not a memfw vault: {root}")
    config = repository.read_config()
    # The judge is code that sees transcript text. Only the operator picks it
    # (--judge); vault.yaml, which also picks the transcript sources, may name
    # one only when the operator explicitly allows that.
    spec = options.judge
    configured = config.get("dream_judge")
    if spec is None and configured and options.allow_config_judge:
        spec = str(configured)
    judge = load_judge(spec)
    documents = [
        document
        for document in repository.list()
        if document.tenant == options.tenant and is_current(document)
    ]
    to_verify: list[MemoryDocument] = (
        sorted(documents, key=lambda item: (-item.reference_count, item.id))
        if options.all_memories
        else cited(documents)
    )[: max(0, options.limit)]
    index, history_info = _history(repository, config, options.refresh)
    verified, stats = verify(
        to_verify,
        index,
        focus=options.focus,
        source=options.source,
        since=options.since,
        until=options.until,
        max_sessions=options.max_sessions,
        judge=judge,
    )
    curated = curate(documents, now=now, stale_days=options.stale_days)
    budget = check_budget(repository.root, config)
    promoted = budget_candidates(budget)
    candidates = verified + curated + promoted
    candidates.sort(key=lambda item: (item.oid, item.operation, item.action, item.memory_ids))
    dream_id = _dream_id(repository, options, now)
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.action] = counts.get(candidate.action, 0) + 1
    report: dict[str, Any] = {
        "dream_id": dream_id,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "authority": False,
        "tenant": options.tenant,
        "focus": options.focus,
        "memories": {"current": len(documents), "verified": len(to_verify)},
        "history": history_info,
        "verify": stats.as_dict(),
        "candidates": counts,
        "budget": [
            {"path": item.path, "lines": item.lines, "bytes": item.bytes, "over": item.over}
            for item in budget
        ],
        "operations": {
            "verify": "run",
            "curate": "run",
            "distill": "stub: proposal-only, not implemented in v1 "
            "(needs two independent events per Lesson/Procedure)",
            "promote": "proposal-only: injected-layer budget demotions",
        },
        "judge": "local hook" if judge is not None else "none (deterministic rules)",
        "config_judge_ignored": bool(configured) and judge is None,
        "model_tokens": 0 if judge is None else None,
        "transcript_text_is_data": True,
    }
    result = DreamResult(dream_id=dream_id, dry_run=dry_run, candidates=candidates, report=report)
    if not dry_run:
        staged = stage(
            repository.root,
            dream_id,
            candidates,
            report,
            created_at=report["created_at"],
        )
        result.staged = staged.as_posix()
    return result
