"""Orchestrate one dream: verify, curate, distill, budget check (④), stage.

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

from memfw.core.epistemic import (
    EPISTEMIC_FIELDS,
    validate_proposed_open_question,
    validate_proposed_write,
)
from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.history import (
    HistoryError,
    HistoryLimits,
    TranscriptIndex,
    default_db,
    sources_from_config,
)
from memfw.storage.evidence import gate_evidence_classes
from memfw.storage.operations import lookup_current

from .budget import budget_candidates, check_budget
from .curate import curate
from .distill import MIN_EVENTS, distill
from .model import Candidate
from .questions import ACTION as OPEN_QUESTION_ACTION
from .questions import (
    check_candidate,
    conflict_questions,
    lifecycle_source_from_config,
    load_lifecycle,
    rejections,
    staged_questions,
)
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
    read_only_history: bool = False
    """Read the transcript index read-only even when staging (never create or refresh it).

    The MCP tool sets this: a dream started by an agent writes only its
    candidates, never the derived index in the vault root."""
    stale_days: int = 180
    min_events: int = MIN_EVENTS
    """Independent events a distilled lesson needs (never fewer than 2)."""
    dream_id: str | None = None
    judge: str | None = None
    allow_config_judge: bool = False
    """Honour ``dream_judge`` from vault.yaml. Off: the vault must not choose code to run."""
    lifecycle_source: Path | None = None
    """Lifecycle meta-index for the open-question privacy check (default: the
    vault's ``status/memory-meta-index.json`` when present)."""
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
    repository: Repository, config: dict[str, Any], refresh: bool, *, dry_run: bool = False
) -> tuple[TranscriptIndex | None, dict[str, Any]]:
    try:
        sources = sources_from_config(config)
    except HistoryError as exc:
        return None, {"available": False, "reason": str(exc)}
    if not sources:
        return None, {"available": False, "reason": "no history_sources in vault.yaml"}
    db = default_db(repository.root)
    if dry_run:
        # A dry run writes nothing, not even the derived index: read an existing
        # index as an immutable snapshot, or report that there is none.
        if not db.is_file():
            return None, {
                "available": False,
                "reason": "no transcript index yet (dry runs and read-only dreams never "
                "build one); run `memfw history index` first",
            }
        index = TranscriptIndex(
            db, sources, limits=HistoryLimits.from_config(config), read_only=True
        )
        try:
            index.stats()
        except HistoryError as exc:
            return None, {"available": False, "reason": str(exc)}
        return index, {
            "available": True,
            "sources": sorted(item.name for item in sources),
            "read_only": True,
        }
    index = TranscriptIndex(db, sources, limits=HistoryLimits.from_config(config))
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
    if options.min_events < MIN_EVENTS:
        raise ValueError(f"min_events must be >= {MIN_EVENTS}")
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
    tenant_documents = [
        document for document in repository.list() if document.tenant == options.tenant
    ]
    documents = [document for document in tenant_documents if is_current(document)]
    to_verify: list[MemoryDocument] = (
        sorted(documents, key=lambda item: (-item.reference_count, item.id))
        if options.all_memories
        else cited(documents)
    )[: max(0, options.limit)]
    index, history_info = _history(
        repository,
        config,
        options.refresh and not options.read_only_history,
        dry_run=dry_run or options.read_only_history,
    )
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
    # A conflict curate found also becomes an open-question candidate (U3).
    # Without a conflict nothing below runs and the output is unchanged.
    questions: list[Candidate] = []
    question_stats = None
    if any(candidate.action == "supersede" for candidate in curated):
        questions, question_stats = conflict_questions(
            curated,
            tenant_documents,
            tenant=options.tenant,
            staged=staged_questions(repository.root, tenant=options.tenant),
            rejected=rejections(repository.root, tenant=options.tenant),
            # ``--lifecycle-source`` wins; else ``runtime.recall.lifecycle_source``.
            lifecycle=load_lifecycle(
                repository.root,
                options.lifecycle_source or lifecycle_source_from_config(config, repository.root),
            ),
            legacy_privacy=repository.legacy_privacy_default(),
        )
    distilled, distill_stats = distill(documents, verified, min_events=options.min_events)
    budget = check_budget(repository.root, config)
    promoted = budget_candidates(budget)
    candidates = verified + curated + questions + distilled + promoted
    candidates.sort(key=lambda item: (item.oid, item.operation, item.action, item.memory_ids))
    # A dream stages proposals, so it may propose at most ``causal=hypothesis``.
    # The shared gate refuses a staged ``causal=verified`` (and any structurally
    # incomplete causal claim) before a human can ever see it as a candidate.
    # One refused candidate is skipped and reported: it must never abort the run
    # (a memory that a human had already verified used to do exactly that).
    accepted: list[Candidate] = []
    skipped: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.action == OPEN_QUESTION_ACTION:
            # A proposed new memory: gated as a new open question (no claim
            # fields; conflicts exist in its tenant), never against the
            # memories it names, which it does not change.
            try:
                check_candidate(
                    candidate,
                    lookup=lambda ref: lookup_current(repository, ref),
                    actor="mechanism",
                )
            except MemfwError as exc:
                skipped.append(
                    {
                        "operation": candidate.operation,
                        "action": candidate.action,
                        "memory_ids": list(candidate.memory_ids),
                        "reason": str(exc),
                    }
                )
                continue
            accepted.append(candidate)
            continue
        proposed = {
            key: value for key, value in candidate.changes.items() if key in EPISTEMIC_FIELDS
        }
        if not proposed:
            accepted.append(candidate)
            continue
        try:
            for memory_id in candidate.memory_ids:
                try:
                    current: dict[str, Any] | None = repository.get(memory_id).model_dump()
                except (MemfwError, OSError):
                    current = None
                merged = dict(current or {})
                merged.update(proposed)
                # MM3: the keyword only when some pointer names an attachment,
                # so the default call is unchanged.
                classes = gate_evidence_classes(repository, merged, current)
                extra: dict[str, Any] = {"evidence_classes": classes} if classes else {}
                validate_proposed_write(current, proposed, actor="mechanism", **extra)
                validate_proposed_open_question(
                    current,
                    proposed,
                    lookup=lambda ref: lookup_current(repository, ref),
                    actor="mechanism",
                )
        except MemfwError as exc:
            skipped.append(
                {
                    "operation": candidate.operation,
                    "action": candidate.action,
                    "memory_ids": list(candidate.memory_ids),
                    "reason": str(exc),
                }
            )
            continue
        accepted.append(candidate)
    candidates = accepted
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
        "distill": distill_stats.as_dict(),
        "candidates": counts,
        "skipped_candidates": skipped,
        **({"open_questions": question_stats.as_dict()} if question_stats is not None else {}),
        "budget": [
            {"path": item.path, "lines": item.lines, "bytes": item.bytes, "over": item.over}
            for item in budget
        ],
        "operations": {
            "verify": "run",
            "curate": "run",
            "distill": f"run: lessons from >= {options.min_events} independent events "
            "(proposal-only; procedures: memfw dream skills)",
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
