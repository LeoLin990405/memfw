"""Human confirmation for staged full-capture candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from ..core.errors import IntegrityError
from ..core.models import EventKind, Layer, Tier
from ..core.repository import Repository
from . import CaptureCandidate, CaptureSession, _ensure_raw_stage_locked, _read_stage_records

Decision = Literal["keep", "discard", "anchor"]
app = typer.Typer(help="Review staged capture candidates")


def _repository(root: str | Path) -> Repository:
    from memfw.cli import _repo

    return _repo(Path(root))


def _read_pending_candidates(repository: Repository) -> dict[str, CaptureCandidate]:
    """Read staged items without CaptureSession cleanup side effects."""

    staging = repository.root / ".staging" / "capture-candidates.jsonl"
    if not staging.exists() and not staging.with_name("capture-candidates.jsonl.zst").exists():
        return {}
    recorded: dict[str, str] = {}
    decided: set[str] = set()
    for event in repository.events():
        candidate_id = event.details.get("candidate_id") or event.details.get("proposal_id")
        if not isinstance(candidate_id, str):
            continue
        source_hash = event.details.get("source_hash")
        if event.kind == EventKind.CAPTURE_RECORD and isinstance(source_hash, str):
            recorded[candidate_id] = source_hash
        if (
            event.kind == EventKind.HUMAN_DECISION
            and event.actor == "human"
            and event.details.get("decision") in {"keep", "discard", "anchor"}
        ):
            decided.add(candidate_id)
    result: dict[str, CaptureCandidate] = {}
    try:
        records = _read_stage_records(staging)
    except ValueError as exc:
        raise IntegrityError(str(exc)) from exc
    for candidate, raw in records:
        candidate_id = candidate.candidate_id
        if candidate_id in decided:
            continue
        if hashlib.sha256(raw).hexdigest() != recorded.get(candidate_id):
            raise IntegrityError(f"Staged candidate integrity check failed: {candidate_id}")
        result[candidate_id] = candidate
    return result


def confirm_candidates(
    repository: Repository,
    decisions: Sequence[str] = (),
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """List staged full candidates and apply explicit per-candidate human choices.

    Each decision is ``candidate_id=keep|discard|anchor``. The capture module owns
    staging persistence and enforces that only a human can confirm candidates.
    """

    session = None if dry_run else CaptureSession.from_repository(repository, session_id="*")
    if session is None:
        pending = _read_pending_candidates(repository)
    else:
        pending = {item.candidate_id: item for item in session.items()}
    parsed: list[tuple[str, Decision]] = []
    seen: set[str] = set()
    for raw in decisions:
        if "=" not in raw:
            raise IntegrityError("Each capture decision must be candidate_id=keep|discard|anchor")
        candidate_id, decision = raw.split("=", 1)
        if not candidate_id or decision not in {"keep", "discard", "anchor"}:
            raise IntegrityError("Capture choice must be keep, discard, or anchor")
        if candidate_id not in pending:
            raise IntegrityError(f"No pending capture candidate: {candidate_id}")
        if candidate_id in seen:
            raise IntegrityError(f"Candidate appears more than once: {candidate_id}")
        seen.add(candidate_id)
        parsed.append((candidate_id, cast(Decision, decision)))

    if dry_run:
        return {
            "confirmed": [],
            "would_confirm": [
                {"candidate_id": candidate_id, "decision": decision}
                for candidate_id, decision in parsed
            ],
            "pending": [
                {
                    "candidate_id": item.candidate_id,
                    "session_id": item.session_id,
                    "timestamp": item.timestamp.isoformat(),
                    "source": item.source,
                    "source_id": item.source_id,
                    "text": item.text,
                }
                for item in pending.values()
            ],
            "formal_memory_ids_created": [],
        }

    assert session is not None
    applied = []
    formal_memory_ids: list[str] = []
    with repository._locked():
        for candidate_id, decision in parsed:
            candidate = pending[candidate_id]
            memory_id = candidate.source_id or candidate.candidate_id
            staged_path = repository.root / ".staging" / "capture-candidates.jsonl"
            _ensure_raw_stage_locked(repository, staged_path)
            stage_records = _read_stage_records(staged_path)
            staged_bytes = next(
                (raw for item, raw in stage_records if item.candidate_id == candidate_id), b""
            )
            expected_hash = session._stage_hashes.get(candidate_id)
            event_hash = next(
                (
                    event.details.get("source_hash")
                    for event in reversed(repository.events())
                    if event.kind == EventKind.CAPTURE_RECORD
                    and event.details.get("candidate_id", event.details.get("proposal_id"))
                    == candidate_id
                ),
                None,
            )
            expected_hash = expected_hash or event_hash
            if not staged_bytes or hashlib.sha256(staged_bytes).hexdigest() != expected_hash:
                raise IntegrityError(
                    f"Staged candidate changed before confirmation: {candidate_id}"
                )
            if decision in {"keep", "anchor"}:
                existing_ids = {doc.id for doc in repository.list()}
                is_anchor = decision == "anchor"
                if memory_id not in existing_ids:
                    title = candidate.text.splitlines()[0].strip()[:120] or "Captured memory"
                    repository.add(
                        title,
                        candidate.text,
                        memory_id=memory_id,
                        layer=Layer.ANCHOR if is_anchor else Layer.CURRENT,
                        tier=Tier.HOT,
                        source=f"capture:{candidate.source}",
                        provenance=(
                            f"capture-session:{candidate.session_id};"
                            f"candidate:{candidate.candidate_id};"
                            f"source_id:{candidate.source_id or ''};"
                            f"source_sha256:{hashlib.sha256(candidate.text.encode()).hexdigest()}"
                        ),
                        anchor=f"capture:{candidate.candidate_id}" if is_anchor else None,
                        tags=["capture", candidate.source],
                        actor="human",
                    )
                else:
                    existing = repository.get(memory_id)
                    marker = f"candidate:{candidate.candidate_id};"
                    if (
                        marker not in existing.provenance
                        or existing.body != candidate.text
                        or (existing.layer == Layer.ANCHOR) != is_anchor
                        or (existing.anchor is not None) != is_anchor
                    ):
                        raise IntegrityError(f"Capture memory id already exists: {memory_id}")
                formal_memory_ids.append(memory_id)
            applied.append(session.confirm_candidate(candidate_id, decision, actor="human"))
    remaining = session.items()
    return {
        "confirmed": [
            {
                "candidate_id": item.candidate_id,
                "session_id": item.session_id,
                "decision": item.decision,
                "actor": item.actor,
                "memory_id": (
                    pending[item.candidate_id].source_id or item.candidate_id
                    if item.decision in {"keep", "anchor"}
                    else None
                ),
            }
            for item in applied
        ],
        "pending": [
            {
                "candidate_id": item.candidate_id,
                "session_id": item.session_id,
                "timestamp": item.timestamp.isoformat(),
                "source": item.source,
                "source_id": item.source_id,
                "text": item.text,
            }
            for item in remaining
        ],
        "formal_memory_ids_created": formal_memory_ids,
    }


@app.command("confirm")
def confirm_command(
    decision: Annotated[
        list[str] | None,
        typer.Option("--decision", help="candidate_id=keep|discard|anchor; repeat per item"),
    ] = None,
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Show pending full-capture candidates or apply explicit human choices."""

    try:
        result = confirm_candidates(_repository(root), decision or (), dry_run=dry_run)
    except (IntegrityError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


__all__ = ["app", "confirm_command", "confirm_candidates"]
