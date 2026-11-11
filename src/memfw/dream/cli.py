"""``memfw dream``: verify-first offline memory consolidation (proposals only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from ..core.errors import MemfwError
from ..history import HistoryError
from .run import MAX_SESSIONS, DreamOptions, run_dream
from .stage import DreamExistsError

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
        dream_id=dream_id,
        judge=judge,
        allow_config_judge=allow_config_judge,
    )
    try:
        result = run_dream(root, now=_now(now), dry_run=dry_run, options=options)
    except (DreamExistsError, HistoryError, MemfwError, ValueError, ImportError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
