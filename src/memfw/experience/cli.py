"""``memfw experience``: draft and invalidate experience points (proposals only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from ..core.errors import IntegrityError, MemfwError
from ..core.repository import Repository
from .candidate import stage_candidate, stage_invalidation
from .github import draft_input_from_pull_request, fetch_pull_request
from .model import ExperienceDraftInput, build_draft, load_draft_input

experience_app = typer.Typer(
    help="Draft experience points after human review (proposals only, never the vault)",
    no_args_is_help=True,
)


def _repository(root: Path) -> Repository:
    repository = Repository(root)
    if not repository.config_path.exists() and not repository.events_path.exists():
        raise typer.BadParameter(f"Not a memfw vault: {root}; run `memfw init {root}` first")
    return repository


def _emit(payload: dict[str, object]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _stage(root: Path, draft_input: ExperienceDraftInput) -> dict[str, object]:
    draft = build_draft(draft_input)
    staged = stage_candidate(root, draft)
    return {
        "authority": False,
        "dry_run": False,
        "staged": staged.as_posix(),
        "title": draft.title,
        "type": draft.values["type"],
        "epistemic": draft.values["epistemic"],
        "causal": draft.values["causal"],
        "verdict": draft_input.review.verdict,
        "reviewer": draft_input.review.reviewer,
    }


def _preview(draft_input: ExperienceDraftInput) -> dict[str, object]:
    draft = build_draft(draft_input)
    return {
        "authority": False,
        "dry_run": True,
        "staged": None,
        "document": draft.payload,
    }


@experience_app.command("draft")
def draft(
    from_json: Annotated[
        Path, typer.Option("--from", help="Draft JSON file (schema: docs/experience-points.md)")
    ],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Stage the propose candidate under _candidates/experience/ (never the vault)",
        ),
    ] = False,
) -> None:
    """Validate a reviewed task into an experience-point proposal.

    The default is a dry run: the validated document is printed and nothing is
    written. ``--apply`` stages a candidate file for human review; it still does
    not write the authoritative vault.
    """

    try:
        draft_input = load_draft_input(from_json)
        _emit(_stage(root, draft_input) if apply else _preview(draft_input))
    except MemfwError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@experience_app.command("from-github")
def from_github(
    repo: Annotated[str, typer.Option("--repo", help="owner/name")],
    pr: Annotated[int, typer.Option("--pr", help="Merged pull request number")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Stage the propose candidate under _candidates/experience/ (never the vault)",
        ),
    ] = False,
) -> None:
    """Read one merged PR with ``gh`` and turn it into an experience-point draft."""

    try:
        data = fetch_pull_request(repo, pr)
        draft_input = ExperienceDraftInput.model_validate(
            draft_input_from_pull_request(data, repo=repo, pr=pr)
        )
        _emit(_stage(root, draft_input) if apply else _preview(draft_input))
    except MemfwError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@experience_app.command("invalidate")
def invalidate(
    memory_id: Annotated[str, typer.Argument(help="An experience point id")],
    reason: Annotated[str, typer.Option("--reason", help="What changed")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
) -> None:
    """Stage a pending-revision proposal for an experience point whose conditions changed."""

    try:
        repository = _repository(root)
        document = repository.get(memory_id)
        if document.type != "experience-point" or document.experience is None:
            raise IntegrityError(f"memory is not an experience point: {memory_id}")
        if not reason.strip():
            raise IntegrityError("--reason must not be empty")
        staged = stage_invalidation(
            root,
            memory_id=memory_id,
            title=document.title,
            reason=reason.strip(),
            experience=document.experience,
            now=datetime.now(UTC),
        )
        _emit(
            {
                "authority": False,
                "dry_run": False,
                "staged": staged.as_posix(),
                "experience_id": memory_id,
                "action": "revise",
                "reason": reason.strip(),
            }
        )
    except MemfwError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


__all__ = ["experience_app"]
