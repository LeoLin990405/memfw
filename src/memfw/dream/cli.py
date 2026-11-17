"""``memfw dream``: verify-first offline memory consolidation (proposals only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from ..core.errors import MemfwError
from ..history import HistoryError
from .questions import accept_question, reject_question
from .run import MAX_SESSIONS, DreamOptions, run_dream
from .skills.run import MAX_SESSIONS as SKILL_MAX_SESSIONS
from .skills.run import SkillOptions, run_skills
from .stage import DreamExistsError, DreamStageError

app = typer.Typer(
    help="Offline memory consolidation that only stages candidates", no_args_is_help=True
)


@app.callback()
def dream_root() -> None:
    """Verify memories against transcripts and stage candidates for human review."""


def _now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("--now must be an ISO-8601 timestamp with timezone") from exc
    if value.tzinfo is None:
        raise typer.BadParameter("--now must include a timezone")
    return value.astimezone(UTC)


@app.command("run")
def dream_run(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault root")] = Path("."),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the candidates; write nothing")
    ] = False,
    all_memories: Annotated[
        bool, typer.Option("--all", help="Verify every current memory, not only cited ones")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", help="Most memories to verify")] = 200,
    focus: Annotated[str, typer.Option("--focus", help="Steer the search direction")] = "",
    max_sessions: Annotated[
        int, typer.Option("--max-sessions", help="Most transcript sessions to read")
    ] = MAX_SESSIONS,
    source: Annotated[str | None, typer.Option("--source-name")] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    tenant: Annotated[str, typer.Option("--tenant")] = "",
    stale_days: Annotated[int, typer.Option("--stale-days")] = 180,
    min_events: Annotated[
        int,
        typer.Option("--min-events", help="Independent events a distilled lesson needs (>= 2)"),
    ] = 2,
    dream_id: Annotated[str | None, typer.Option("--dream-id")] = None,
    judge: Annotated[
        str | None,
        typer.Option("--judge", help="Optional local judge module:function (off by default)"),
    ] = None,
    allow_config_judge: Annotated[
        bool,
        typer.Option(
            "--allow-config-judge",
            help="Also honour dream_judge from vault.yaml (it names code to import)",
        ),
    ] = False,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh")] = True,
    now: Annotated[str | None, typer.Option("--now", help="Inject an ISO-8601 UTC time")] = None,
    lifecycle_source: Annotated[
        Path | None,
        typer.Option(
            "--lifecycle-source",
            help="memory-meta-index.json for the open-question privacy check",
        ),
    ] = None,
) -> None:
    """Run one dream. Stages candidates under _candidates/dream/<dream_id>/."""

    options = DreamOptions(
        tenant=tenant,
        focus=focus,
        all_memories=all_memories,
        limit=limit,
        max_sessions=max_sessions,
        source=source,
        since=since,
        until=until,
        refresh=refresh,
        stale_days=stale_days,
        min_events=min_events,
        dream_id=dream_id,
        judge=judge,
        allow_config_judge=allow_config_judge,
        lifecycle_source=lifecycle_source,
    )
    try:
        result = run_dream(root, now=_now(now), dry_run=dry_run, options=options)
    except (
        DreamExistsError,
        DreamStageError,
        HistoryError,
        MemfwError,
        ValueError,
        ImportError,
        OSError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))


@app.command("accept-question")
def dream_accept_question(
    dream_id: Annotated[str, typer.Argument(help="staged dream id")],
    memory_a: Annotated[str, typer.Argument(help="one conflicting memory id")],
    memory_b: Annotated[str, typer.Argument(help="the other conflicting memory id")],
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault root")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
    lifecycle_source: Annotated[
        Path | None,
        typer.Option("--lifecycle-source", help="memory-meta-index.json for the privacy check"),
    ] = None,
) -> None:
    """Human-accept a staged open-question candidate: write it to the vault.

    Dry run by default (prints the full memory); --apply writes the open
    question as the human. The question is rebuilt from the two memories; an
    edited or outdated staged candidate is refused.
    """

    from ..core.repository import Repository

    try:
        result = accept_question(
            Repository(root),
            dream_id,
            [memory_a, memory_b],
            actor="human",
            dry_run=dry_run,
            lifecycle_source=lifecycle_source,
        )
    except (MemfwError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("reject-question")
def dream_reject_question(
    dream_id: Annotated[str, typer.Argument(help="staged dream id")],
    memory_a: Annotated[str, typer.Argument(help="one conflicting memory id")],
    memory_b: Annotated[str, typer.Argument(help="the other conflicting memory id")],
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault root")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Reject a staged open-question candidate (never writes the vault).

    --apply removes only that candidate from its concept file and records the
    rejection, so later dreams do not re-propose it until a memory changes.
    """

    from ..core.repository import Repository

    try:
        result = reject_question(
            Repository(root), dream_id, [memory_a, memory_b], actor="human", dry_run=dry_run
        )
    except (MemfwError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("skills")
def dream_skills(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault root")] = Path("."),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the candidates; write nothing")
    ] = False,
    min_sessions: Annotated[
        int, typer.Option("--min-sessions", help="Distinct sessions a procedure must repeat in")
    ] = 3,
    min_steps: Annotated[int, typer.Option("--min-steps", help="Fewest verified steps")] = 2,
    similarity: Annotated[
        float, typer.Option("--similarity", help="Episode clustering threshold (0-1]")
    ] = 0.4,
    max_sessions: Annotated[
        int, typer.Option("--max-sessions", help="Most transcript sessions to read")
    ] = SKILL_MAX_SESSIONS,
    source: Annotated[str | None, typer.Option("--source-name")] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    drafter: Annotated[
        str | None,
        typer.Option("--drafter", help="Optional local drafter module:function (off by default)"),
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh")] = True,
    now: Annotated[str | None, typer.Option("--now", help="Inject an ISO-8601 UTC time")] = None,
    from_queue: Annotated[
        bool,
        typer.Option(
            "--from-queue",
            help="Read the conversation worker's episode queue instead of the transcript index",
        ),
    ] = False,
    boilerplate_min_sessions: Annotated[
        int,
        typer.Option(
            "--boilerplate-min-sessions",
            help="Text repeated in this many sessions is injected boilerplate (0: off)",
        ),
    ] = 5,
    summary: Annotated[
        bool, typer.Option("--summary", help="Print the report only, not the candidates")
    ] = False,
) -> None:
    """Stage skill candidates from repeated procedures under _candidates/skills/."""

    options = SkillOptions(
        min_sessions=min_sessions,
        min_steps=min_steps,
        similarity=similarity,
        max_sessions=max_sessions,
        source=source,
        since=since,
        until=until,
        refresh=refresh,
        drafter=drafter,
        from_queue=from_queue,
        boilerplate_min_sessions=boilerplate_min_sessions,
    )
    try:
        feed = None
        if from_queue:
            from memfw.conversations.feed import WorkerEpisodeFeed

            feed = WorkerEpisodeFeed()
        result = run_skills(
            root, now=_now(now), dry_run=dry_run, options=options, episode_feed=feed
        )
    except (DreamStageError, HistoryError, MemfwError, ValueError, ImportError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    output = result.as_dict()
    if summary:
        output = {key: value for key, value in output.items() if key != "candidates"}
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    failed = result.report.get("staging_errors", 0)
    if failed:
        typer.echo(f"error: {failed} candidate(s) could not be staged", err=True)
        raise typer.Exit(1)
