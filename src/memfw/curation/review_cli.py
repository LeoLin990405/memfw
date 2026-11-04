"""Command-line entry point for human proposal review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from memfw.core.errors import IntegrityError
from memfw.core.repository import Repository

from .review import apply_approved, approve_batch, decide_proposal, list_pending

app = typer.Typer(help="Review and apply pending memory proposals")


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _repository(root: str | Path) -> Repository:
    # Runtime import avoids the CLI/review_cli import cycle and shares --now,
    # --id-seed, and pending-transaction validation with other root commands.
    from memfw.cli import _repo

    return _repo(Path(root))


def review_command(
    root: Path = Path("."),
    *,
    approve_all: bool = False,
    filter_type: str | None = None,
    dry_run: bool = False,
    proposal_id: str | None = None,
    decision: str | None = None,
    payload_hash: str | None = None,
    edited_output: dict[str, Any] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Root-command callback: list by default, or review through explicit options."""

    repository = _repository(root)
    if approve_all:
        if filter_type is None:
            raise IntegrityError("--approve-all requires --filter type=<kind>")
        return approve_batch(repository, filter_type=filter_type, dry_run=dry_run)
    if apply:
        if proposal_id is None:
            raise IntegrityError("--apply requires a proposal id")
        result = apply_approved(repository, proposal_id, dry_run=dry_run)
        if dry_run:
            if not isinstance(result, dict):
                raise IntegrityError("Dry-run apply did not return a preview")
            return {str(key): value for key, value in result.items()}
        return {
            "proposal_id": proposal_id,
            "applied": True,
            "memory_id": getattr(result, "id", None),
        }
    if decision is not None:
        if proposal_id is None or payload_hash is None:
            raise IntegrityError("A decision requires proposal id and --payload-hash")
        proposal = decide_proposal(
            repository,
            proposal_id,
            decision,
            payload_hash=payload_hash,
            edited_output=edited_output,
            dry_run=dry_run,
        )
        return {
            "proposal_id": proposal.proposal_id,
            "status": proposal.status,
            "dry_run": dry_run,
        }
    return {"pending": list_pending(repository), "dry_run": dry_run}


@app.callback(invoke_without_command=True)
def review_root(
    context: typer.Context,
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
    approve_all: Annotated[bool, typer.Option("--approve-all")] = False,
    filter_type: Annotated[str | None, typer.Option("--filter")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    proposal_id: Annotated[str | None, typer.Option("--proposal-id")] = None,
    decision: Annotated[str | None, typer.Option("--decision")] = None,
    payload_hash: Annotated[str | None, typer.Option("--payload-hash")] = None,
    edited_output: Annotated[str | None, typer.Option("--edited-output")] = None,
    apply_proposal: Annotated[bool, typer.Option("--apply-proposal")] = False,
) -> None:
    if context.invoked_subcommand is not None:
        return
    try:
        parsed = json.loads(edited_output) if edited_output is not None else None
        if parsed is not None and not isinstance(parsed, dict):
            raise IntegrityError("Edited output must be a JSON object")
        _emit(
            review_command(
                Path(root),
                approve_all=approve_all,
                filter_type=filter_type,
                dry_run=dry_run,
                proposal_id=proposal_id,
                decision=decision,
                payload_hash=payload_hash,
                edited_output=parsed,
                apply=apply_proposal,
            )
        )
    except (IntegrityError, ValueError, TypeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("list")
def list_command(
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
) -> None:
    _emit({"pending": list_pending(_repository(root))})


@app.command("decide")
def decide_command(
    proposal_id: Annotated[str, typer.Argument()],
    decision: Annotated[str, typer.Option("--decision")],
    payload_hash: Annotated[str, typer.Option("--payload-hash")],
    edited_output: Annotated[str | None, typer.Option("--edited-output")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
) -> None:
    try:
        output = json.loads(edited_output) if edited_output is not None else None
        if output is not None and not isinstance(output, dict):
            raise IntegrityError("Edited output must be a JSON object")
        proposal = decide_proposal(
            _repository(root),
            proposal_id,
            decision,
            payload_hash=payload_hash,
            edited_output=output,
            reason=reason,
        )
    except (IntegrityError, ValueError, TypeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit({"proposal_id": proposal.proposal_id, "status": proposal.status})


@app.command("apply")
def apply_command(
    proposal_id: Annotated[str, typer.Argument()],
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
    actor: Annotated[str, typer.Option("--actor")] = "human",
) -> None:
    try:
        result = apply_approved(_repository(root), proposal_id, actor=actor)
    except (IntegrityError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit(
        {
            "proposal_id": proposal_id,
            "applied": True,
            "memory_id": getattr(result, "id", None),
        }
    )


@app.command("batch")
def batch_command(
    filter_type: Annotated[str, typer.Option("--filter")],
    approve_all: Annotated[bool, typer.Option("--approve-all")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    root: Annotated[str, typer.Option("--root", "-r")] = ".",
) -> None:
    if not approve_all:
        raise typer.BadParameter("--approve-all is required for batch decisions")
    try:
        _emit(approve_batch(_repository(root), filter_type=filter_type, dry_run=dry_run))
    except IntegrityError as exc:
        raise typer.BadParameter(str(exc)) from exc


__all__ = [
    "app",
    "apply_command",
    "batch_command",
    "decide_command",
    "list_command",
    "review_command",
    "review_root",
]
