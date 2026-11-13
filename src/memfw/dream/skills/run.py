"""``memfw dream skills``: stage skill candidates distilled from transcripts.

Reads the transcript index (redacted snippets only), groups entries into task
episodes, keeps procedures repeated in at least ``min_sessions`` sessions and
stages one proposal per procedure under ``_candidates/skills/<candidate_id>/``:

* ``SKILL.md`` -- the draft (``name``, ``description``, when to use, steps);
* ``evidence.json`` -- sessions, agents, per-step refs and counts;
* ``candidate.json`` -- the proposal record (``status: proposed``,
  ``approved: false``).

Nothing is installed into any skills directory and nothing is approved. The
same evidence gives the same candidate id, so a rerun stages nothing new.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from memfw.core.repository import Repository
from memfw.history import HistoryError, has_secret

from ..run import _history
from ..stage import _DIR_FLAGS, DreamStageError, _child_dir, _exists, _write_new
from .distill import Procedure, distill
from .draft import (
    Draft,
    UnsafeText,
    apply_drafter,
    deterministic,
    load_drafter,
    render_skill,
)
from .episodes import Episode, segment

SKILLS_DIR = Path("_candidates") / "skills"
MAX_SESSIONS = 200
MAX_ENTRIES = 500
"""Entries read per session (the history index caps one listing at 500)."""
_HOME_PATH = ("/Users/", "/home/", "C:\\Users\\")


@dataclass
class SkillOptions:
    min_sessions: int = 3
    min_steps: int = 2
    similarity: float = 0.4
    max_sessions: int = MAX_SESSIONS
    source: str | None = None
    since: str | None = None
    until: str | None = None
    refresh: bool = True
    drafter: str | None = None

    def check(self) -> None:
        if self.min_sessions < 2:
            raise ValueError("min_sessions must be at least 2")
        if self.min_steps < 1:
            raise ValueError("min_steps must be at least 1")
        if not 0.0 < self.similarity <= 1.0:
            raise ValueError("similarity must be in (0, 1]")
        if self.max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")


@dataclass
class SkillCandidate:
    candidate_id: str
    procedure_key: str
    confidence: float
    skill_md: str
    evidence: dict[str, Any]
    record: dict[str, Any]
    status: str = "dry-run"
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "procedure_key": self.procedure_key,
            "name": self.record["name"],
            "confidence": self.confidence,
            "status": self.status,
            "path": self.path,
            "skill_md": self.skill_md,
            "evidence": self.evidence,
        }


@dataclass
class SkillResult:
    dry_run: bool
    candidates: list[SkillCandidate]
    report: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "authority": False,
            "report": self.report,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def collect(index: Any, options: SkillOptions) -> tuple[list[Episode], dict[str, int]]:
    """Episodes from the newest ``max_sessions`` sessions of the index."""

    listed = index.list(
        source=options.source,
        since=options.since,
        until=options.until,
        limit=min(options.max_sessions, 500),
    )["sessions"]
    episodes: list[Episode] = []
    counts = {"sessions_read": 0, "entries_read": 0}
    for session in sorted(listed, key=lambda item: (item["source"], item["session_id"])):
        try:
            entries = index.list(
                source=session["source"],
                session=session["session_id"],
                since=options.since,
                until=options.until,
                limit=MAX_ENTRIES,
            )["entries"]
        except HistoryError:
            continue
        counts["sessions_read"] += 1
        counts["entries_read"] += len(entries)
        episodes.extend(segment(session["source"], session["session_id"], entries))
    return episodes, counts


def _safe(text: str) -> bool:
    return not has_secret(text) and not any(marker in text for marker in _HOME_PATH)


def build(
    procedure: Procedure, options: SkillOptions, drafter: Any, created_at: str
) -> SkillCandidate | None:
    """One candidate, or None when its text cannot be made safe."""

    try:
        draft: Draft = deterministic(procedure)
    except UnsafeText:
        return None
    if drafter is not None:
        draft = apply_drafter(
            procedure,
            draft,
            drafter,
            min_steps=options.min_steps,
            min_sessions=options.min_sessions,
        )
    candidate_id = procedure.candidate_id
    confidence = procedure.confidence(options.min_sessions)
    skill_md = render_skill(candidate_id, draft, procedure, confidence)
    evidence = {
        "candidate_id": candidate_id,
        "procedure_key": procedure.procedure_key,
        "min_sessions": options.min_sessions,
        "sessions": procedure.sessions,
        "agents": procedure.agents,
        "episodes": len(procedure.episodes),
        "steps": [step.as_dict() for step in procedure.steps],
        "dropped_steps": list(procedure.dropped),
        "transcript_text_is_data": True,
    }
    record = {
        "candidate_id": candidate_id,
        "kind": "skill",
        "procedure_key": procedure.procedure_key,
        "name": draft.name,
        "status": "proposed",
        "approved": False,
        "authority": False,
        "installed": False,
        "confidence": confidence,
        "drafter": draft.drafter,
        "drafter_notes": list(draft.notes),
        "created_at": created_at,
    }
    blob = skill_md + json.dumps(evidence) + json.dumps(record)
    if not _safe(blob):
        return None
    return SkillCandidate(
        candidate_id=candidate_id,
        procedure_key=procedure.procedure_key,
        confidence=confidence,
        skill_md=skill_md,
        evidence=evidence,
        record=record,
    )


def _refuse_skill_home(path: Path) -> None:
    """Never stage into an agent's skills directory (``~/.<agent>/skills``)."""

    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == "skills" and parent.parent.name.startswith("."):
            raise DreamStageError(f"refusing to stage skill candidates inside {parent.name}/")


def _staged_procedure(base: int, procedure_key: str) -> str | None:
    """Id of an already staged candidate for the same procedure, if any."""

    for name in sorted(os.listdir(base)):
        if name.startswith("."):
            continue
        try:
            directory = _child_dir(base, name, create=False)
        except DreamStageError:
            continue
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open("candidate.json", flags, dir_fd=directory)
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                record = json.loads(handle.read(1_000_000))
        except (OSError, ValueError):
            continue
        finally:
            os.close(directory)
        if isinstance(record, dict) and record.get("procedure_key") == procedure_key:
            return name
    return None


def stage_candidate(vault: Path, candidate: SkillCandidate) -> tuple[str, str]:
    """Publish one candidate directory with a single rename.

    Returns ``(status, candidate id)``: ``staged``; ``exists`` (same id, so
    the same evidence); or ``known`` (the same procedure is already staged
    under another id, from older evidence; it is left for the reviewer).
    """

    _refuse_skill_home(Path(vault))
    opened: list[int] = []
    try:
        root = os.open(Path(vault), _DIR_FLAGS & ~getattr(os, "O_NOFOLLOW", 0))
        opened.append(root)
        parent = _child_dir(root, SKILLS_DIR.parts[0], create=True)
        opened.append(parent)
        base = _child_dir(parent, SKILLS_DIR.parts[1], create=True)
        opened.append(base)
        name = candidate.candidate_id
        if _exists(base, name):
            return "exists", name
        known = _staged_procedure(base, candidate.procedure_key)
        if known is not None:
            return "known", known
        temporary = f".tmp-{name}-{os.getpid()}-{os.urandom(4).hex()}"
        os.mkdir(temporary, 0o700, dir_fd=base)
        work = _child_dir(base, temporary, create=False)
        opened.append(work)
        written: list[str] = []
        files = {
            "SKILL.md": candidate.skill_md,
            "evidence.json": json.dumps(candidate.evidence, indent=2, sort_keys=True) + "\n",
            "candidate.json": json.dumps(candidate.record, indent=2, sort_keys=True) + "\n",
        }
        try:
            for filename, text in files.items():
                _write_new(work, filename, text)
                written.append(filename)
            if _exists(base, name):
                raise FileExistsError(name)
            os.rename(temporary, name, src_dir_fd=base, dst_dir_fd=base)
        except BaseException as exc:
            for filename in written:
                try:
                    os.unlink(filename, dir_fd=work)
                except OSError:
                    pass
            try:
                os.rmdir(temporary, dir_fd=base)
            except OSError:
                pass
            if isinstance(exc, OSError) and _exists(base, name):
                return "exists", name
            raise
    except NotADirectoryError as exc:
        raise DreamStageError(f"refusing to stage skill candidates: {exc.strerror}") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    return "staged", name


def run_skills(
    root: Path,
    *,
    now: datetime,
    dry_run: bool = False,
    options: SkillOptions | None = None,
) -> SkillResult:
    options = options or SkillOptions()
    options.check()
    repository = Repository(root)
    if not repository.config_path.exists():
        raise ValueError(f"not a memfw vault: {root}")
    config = repository.read_config()
    drafter = load_drafter(options.drafter)
    created_at = now.isoformat().replace("+00:00", "Z")
    index, history_info = _history(repository, config, options.refresh, dry_run=dry_run)
    episodes: list[Episode] = []
    counts = {"sessions_read": 0, "entries_read": 0}
    if index is not None:
        episodes, counts = collect(index, options)
    procedures = distill(
        episodes,
        min_sessions=options.min_sessions,
        min_steps=options.min_steps,
        threshold=options.similarity,
    )
    candidates: list[SkillCandidate] = []
    unsafe = 0
    for procedure in procedures:
        candidate = build(procedure, options, drafter, created_at)
        if candidate is None:
            unsafe += 1
            continue
        candidates.append(candidate)
    if not dry_run:
        for candidate in candidates:
            candidate.status, staged_id = stage_candidate(repository.root, candidate)
            candidate.path = (SKILLS_DIR / staged_id).as_posix()
    report = {
        "created_at": created_at,
        "authority": False,
        "history": history_info,
        **counts,
        "episodes": len(episodes),
        "procedures": len(procedures),
        "candidates": len(candidates),
        "dropped_unsafe": unsafe,
        "min_sessions": options.min_sessions,
        "min_steps": options.min_steps,
        "similarity": options.similarity,
        "drafter": "local hook" if drafter is not None else "none (deterministic)",
        "staged_under": SKILLS_DIR.as_posix(),
        "installed": False,
        "transcript_text_is_data": True,
    }
    return SkillResult(dry_run=dry_run, candidates=candidates, report=report)
