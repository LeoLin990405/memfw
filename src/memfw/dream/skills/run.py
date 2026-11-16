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
from .boilerplate import BoilerplateFilter
from .distill import Procedure, VerifiedStep, distill
from .draft import (
    INTENT_CHARS,
    Draft,
    UnsafeText,
    apply_drafter,
    clean,
    deterministic,
    load_drafter,
    render_skill,
    step_headline,
)
from .episodes import Episode, segment
from .private import PrivateTerms
from .queue import EpisodeFeed, QueueBatch, collect_queue, settle

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
    from_queue: bool = False
    """Read the conversation worker's episode queue instead of the transcript index."""
    boilerplate_min_sessions: int = 5
    """Text repeated verbatim in this many sessions (and two agents, or three times as
    many sessions) is injected boilerplate; 0 keeps only the static harness markers."""

    def check(self) -> None:
        if self.min_sessions < 2:
            raise ValueError("min_sessions must be at least 2")
        if self.min_steps < 1:
            raise ValueError("min_steps must be at least 1")
        if not 0.0 < self.similarity <= 1.0:
            raise ValueError("similarity must be in (0, 1]")
        if self.max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        if self.boilerplate_min_sessions != 0 and self.boilerplate_min_sessions < 2:
            raise ValueError("boilerplate_min_sessions must be 0 (off) or at least 2")

    def boilerplate(self) -> BoilerplateFilter | None:
        if not self.boilerplate_min_sessions:
            return None
        return BoilerplateFilter(
            min_sessions=self.boilerplate_min_sessions,
            solo_sessions=3 * self.boilerplate_min_sessions,
        )


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
    error: str | None = None
    evidence_appended: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "procedure_key": self.procedure_key,
            "name": self.record["name"],
            "confidence": self.confidence,
            "status": self.status,
            "path": self.path,
            "error": self.error,
            "evidence_appended": self.evidence_appended,
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


def collect(
    index: Any, options: SkillOptions, boilerplate: BoilerplateFilter | None = None
) -> tuple[list[Episode], dict[str, int]]:
    """Episodes from the newest ``max_sessions`` sessions of the index.

    With ``boilerplate``, user and system texts of all read sessions are
    counted first, then every session is segmented with the repeated text
    stripped.
    """

    listed = index.list(
        source=options.source,
        since=options.since,
        until=options.until,
        limit=min(options.max_sessions, 500),
    )["sessions"]
    counts = {"sessions_read": 0, "entries_read": 0}
    read: list[tuple[str, str, list[dict[str, Any]]]] = []
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
        read.append((session["source"], session["session_id"], entries))
    if boilerplate is not None:
        for source, session_id, entries in read:
            for entry in entries:
                if str(entry.get("role") or "") in {"user", "system"}:
                    agent = str(entry.get("agent") or source)
                    boilerplate.observe(
                        (source, session_id), agent, str(entry.get("snippet") or "")
                    )
    episodes: list[Episode] = []
    for source, session_id, entries in read:
        episodes.extend(segment(source, session_id, entries, boilerplate))
    return episodes, counts


def _safe(text: str) -> bool:
    return not has_secret(text) and not any(marker in text for marker in _HOME_PATH)


def build(
    procedure: Procedure,
    options: SkillOptions,
    drafter: Any,
    created_at: str,
    private: PrivateTerms | None = None,
) -> SkillCandidate | None:
    """One candidate, or None when its text cannot be made safe.

    Private terms are masked in every field: SKILL.md, evidence and record.
    The ids are computed before masking, so they do not depend on the list.
    """

    private = private or PrivateTerms()
    # A private keyword says nothing a reviewer may see: leave it out of the name.
    procedure.keywords = [word for word in procedure.keywords if not private.matches(word)]
    # Intents are the agents' own sentences: redacted and path-free, or left out.
    for step in procedure.steps:
        try:
            step.intent = private.mask(clean(step.intent, INTENT_CHARS)) if step.intent else ""
        except UnsafeText:
            step.intent = ""
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
            private=private,
        )
    if private:
        draft.name = private.mask_name(draft.name)
        draft.description = private.mask(draft.description)
        draft.when_to_use = private.mask(draft.when_to_use)
        draft.steps = [{**step, "text": private.mask(step["text"])} for step in draft.steps]
    candidate_id = procedure.candidate_id
    confidence = procedure.confidence(options.min_sessions)
    skill_md = private.mask(render_skill(candidate_id, draft, procedure, confidence))
    evidence = {
        "candidate_id": candidate_id,
        "procedure_key": procedure.procedure_key,
        "min_sessions": options.min_sessions,
        "sessions": procedure.sessions,
        "agents": procedure.agents,
        "episodes": len(procedure.episodes),
        "independent_sessions": procedure.independent,
        "days": len(procedure.days),
        "outcomes": procedure.outcomes,
        "preconditions": list(procedure.preconditions),
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
    evidence = private.mask_all(evidence)
    record = private.mask_all(record)
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


def generated_step(step: dict[str, Any]) -> str:
    """One evidence step as generated text: intent class plus command."""

    verified = VerifiedStep(
        key=str(step.get("id") or ""),
        signature=str(step.get("signature") or ""),
        sessions=int(step.get("sessions") or 0),
        refs=[],
        position=0.0,
        flags=list(step.get("flags") or []),
    )
    return step_headline(verified, shared_intent=False)


def quality_report(
    candidates: list[SkillCandidate],
    boilerplate: BoilerplateFilter | None,
    distill_stats: dict[str, int],
    episodes: int,
) -> dict[str, Any]:
    """How much was filtered and why, plus one example: for a reviewer, not a gate."""

    filtered = boilerplate.report() if boilerplate is not None else {"corpus_filter": 0}
    best = max(candidates, key=lambda item: (item.confidence, item.candidate_id), default=None)
    example = None
    if best is not None:
        example = {
            "candidate_id": best.candidate_id,
            "name": best.record["name"],
            "confidence": best.confidence,
            "independent_sessions": best.evidence.get("independent_sessions"),
            "agents": sorted(best.evidence.get("agents") or {}),
            # Generated text only: the command and its intent class, never an intent.
            "steps": [
                f"{number}. {generated_step(step)}"
                for number, step in enumerate(best.evidence.get("steps") or [], start=1)
            ][:5],
        }
    return {
        "candidates": len(candidates),
        "episodes": episodes,
        "clusters": distill_stats.get("clusters", 0),
        "boilerplate_filtered": filtered,
        "rejected": {
            key.removeprefix("rejected_"): value
            for key, value in sorted(distill_stats.items())
            if key.startswith("rejected_")
        },
        "example": example,
    }


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


def _later_evidence(candidate: SkillCandidate) -> dict[str, Any]:
    evidence = candidate.evidence
    return {
        "candidate_id": candidate.candidate_id,
        "observed_at": candidate.record.get("created_at"),
        "confidence": candidate.confidence,
        "sessions": evidence["sessions"],
        "agents": evidence["agents"],
        "independent_sessions": evidence.get("independent_sessions"),
        "steps": [
            {"signature": step["signature"], "sessions": step["sessions"]}
            for step in evidence["steps"]
        ],
    }


def _append_evidence(base: int, staged_id: str, candidate: SkillCandidate) -> bool:
    """Record newer support in a staged candidate's ``evidence.json``.

    SKILL.md and candidate.json are left alone (a reviewer may be reading
    them). The same newer evidence is recorded once. The file is replaced
    atomically; returns whether anything was added.
    """

    directory = _child_dir(base, staged_id, create=False)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open("evidence.json", flags, dir_fd=directory)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            data = json.loads(handle.read(10_000_000))
        if not isinstance(data, dict):
            raise DreamStageError(f"{staged_id}/evidence.json is not an object")
        later = data.get("later_evidence")
        later = later if isinstance(later, list) else []
        if any(
            isinstance(item, dict) and item.get("candidate_id") == candidate.candidate_id
            for item in later
        ):
            return False
        data["later_evidence"] = [*later, _later_evidence(candidate)]
        temporary = f".tmp-evidence-{os.getpid()}-{os.urandom(4).hex()}.json"
        _write_new(directory, temporary, json.dumps(data, indent=2, sort_keys=True) + "\n")
        try:
            os.replace(temporary, "evidence.json", src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            os.unlink(temporary, dir_fd=directory)
            raise
        return True
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DreamStageError(f"{staged_id}/evidence.json is not valid JSON") from exc
    finally:
        os.close(directory)


def stage_candidate(vault: Path, candidate: SkillCandidate) -> tuple[str, str]:
    """Publish one candidate directory with a single rename.

    Returns ``(status, candidate id)``: ``staged``; ``exists`` (same id, so
    the same evidence); or ``known`` (the same procedure is already staged
    under another id, from older evidence; its SKILL.md is left for the
    reviewer and the newer support is appended to its ``evidence.json``).
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
            candidate.evidence_appended = _append_evidence(base, known, candidate)
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


def _feed(feed: EpisodeFeed | None) -> EpisodeFeed:
    # Only reached with a batch, i.e. after --from-queue checked the feed.
    assert feed is not None
    return feed


def run_skills(
    root: Path,
    *,
    now: datetime,
    dry_run: bool = False,
    options: SkillOptions | None = None,
    queue_parts: dict[str, Any] | None = None,
    episode_feed: EpisodeFeed | None = None,
) -> SkillResult:
    """``episode_feed``: the conversation worker's queue for ``--from-queue``, injected
    by the caller (``memfw dream skills`` passes
    :class:`memfw.conversations.feed.WorkerEpisodeFeed`). ``queue_parts`` (tests):
    ``state`` and ``source`` overrides for ``--from-queue``."""

    queue_parts = dict(queue_parts or {})
    options = options or SkillOptions()
    options.check()
    repository = Repository(root)
    if not repository.config_path.exists():
        raise ValueError(f"not a memfw vault: {root}")
    config = repository.read_config()
    private = PrivateTerms.from_config(config, repository.root)
    drafter = load_drafter(options.drafter)
    created_at = now.isoformat().replace("+00:00", "Z")
    episodes: list[Episode] = []
    counts: dict[str, int] = {"sessions_read": 0, "entries_read": 0}
    batch: QueueBatch | None = None
    boilerplate = options.boilerplate()
    if options.from_queue:
        # The conversation worker's episode queue instead of the transcript index.
        if episode_feed is None:
            raise ValueError("--from-queue needs the conversation worker's episode feed")
        batch = collect_queue(
            config,
            repository.root,
            feed=episode_feed,
            dry_run=dry_run,
            limit=options.max_sessions,
            boilerplate=boilerplate,
            **queue_parts,
        )
        episodes, counts, history_info = batch.episodes, batch.counts, batch.info
    else:
        index, history_info = _history(repository, config, options.refresh, dry_run=dry_run)
        if index is not None:
            episodes, counts = collect(index, options, boilerplate)
    state = queue_parts.get("state")
    distill_stats: dict[str, int] = {}
    try:
        procedures = distill(
            episodes,
            min_sessions=options.min_sessions,
            min_steps=options.min_steps,
            threshold=options.similarity,
            stats=distill_stats,
        )
        candidates: list[SkillCandidate] = []
        unsafe = 0
        for procedure in procedures:
            candidate = build(procedure, options, drafter, created_at, private)
            if candidate is None:
                unsafe += 1
                continue
            candidates.append(candidate)
        errors = 0
        if not dry_run:
            for candidate in candidates:
                # One failure must not hide what was already staged: report each.
                try:
                    candidate.status, staged_id = stage_candidate(repository.root, candidate)
                except (DreamStageError, OSError) as exc:
                    candidate.status = "error"
                    candidate.error = str(exc)
                    errors += 1
                    continue
                candidate.path = (SKILLS_DIR / staged_id).as_posix()
    except BaseException:
        if batch is not None:
            settle(
                batch,
                ok=False,
                config=config,
                vault=repository.root,
                feed=_feed(episode_feed),
                state=state,
            )
        raise
    if batch is not None:
        settled = settle(
            batch,
            ok=errors == 0,
            config=config,
            vault=repository.root,
            feed=_feed(episode_feed),
            state=state,
        )
        counts = {**counts, ("episodes_acked" if errors == 0 else "episodes_released"): settled}
    report = {
        "created_at": created_at,
        "authority": False,
        "history": history_info,
        **counts,
        "episodes": len(episodes),
        "procedures": len(procedures),
        "candidates": len(candidates),
        "dropped_unsafe": unsafe,
        "staging_errors": errors,
        "private_terms": len(private.terms),
        "min_sessions": options.min_sessions,
        "independence": "same request text counts once; >= 2 agents or >= 2 days",
        "boilerplate_min_sessions": options.boilerplate_min_sessions,
        "min_steps": options.min_steps,
        "similarity": options.similarity,
        "drafter": "local hook" if drafter is not None else "none (deterministic)",
        "staged_under": SKILLS_DIR.as_posix(),
        "installed": False,
        "transcript_text_is_data": True,
        "quality": quality_report(candidates, boilerplate, distill_stats, len(episodes)),
    }
    return SkillResult(dry_run=dry_run, candidates=candidates, report=report)
