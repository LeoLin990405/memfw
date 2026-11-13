"""``memfw conversations``: read the shared store, run the worker, print the schedule.

Every read command prints redacted text only, labelled as data, and obeys the
same per-app policy as the MCP tools (the caller is ``operator`` unless
``--as-app`` names an app). Nothing here writes to the store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ..core.errors import MemfwError
from .backends import load_backend
from .policy import OPERATOR
from .schedule import DEFAULT_INTERVAL, render
from .service import open_share, open_worker, read_config
from .share import render_markdown
from .store import ConversationError
from .worker import WorkState, state_path

app = typer.Typer(
    help="Shared, redacted conversation store: read, process, schedule", no_args_is_help=True
)

RootOption = Annotated[
    Path | None,
    typer.Option("--root", "-r", envvar="MEMFW_ROOT", help="memfw vault root (vault.yaml)"),
]
StoreOption = Annotated[
    Path | None, typer.Option("--store", help="Conversation store path (default from vault.yaml)")
]
AsAppOption = Annotated[
    str, typer.Option("--as-app", help="Read as this app (its visibility rules apply)")
]
AppOption = Annotated[str | None, typer.Option("--app", help="Only this app (claude, codex ...)")]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {str(exc)[:300]}", err=True)
    return typer.Exit(2)


@app.command("search")
def search(
    query: str,
    root: RootOption = None,
    store: StoreOption = None,
    as_app: AsAppOption = OPERATOR,
    app_name: AppOption = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 10,
    offset: Annotated[int, typer.Option("--offset")] = 0,
) -> None:
    """Redacted snippets of turns matching every query term."""

    try:
        share = open_share(root, as_app, store=store)
        typer.echo(
            _dump(share.search(query, app=app_name, since=since, limit=limit, offset=offset))
        )
    except (ConversationError, MemfwError) as exc:
        raise _fail(exc) from exc


@app.command("list")
def list_sessions(
    root: RootOption = None,
    store: StoreOption = None,
    as_app: AsAppOption = OPERATOR,
    app_name: AppOption = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    offset: Annotated[int, typer.Option("--offset")] = 0,
) -> None:
    """Sessions, most recently updated first."""

    try:
        share = open_share(root, as_app, store=store)
        typer.echo(
            _dump(share.list_sessions(app=app_name, since=since, limit=limit, offset=offset))
        )
    except (ConversationError, MemfwError) as exc:
        raise _fail(exc) from exc


@app.command("get")
def get(
    session_id: str,
    root: RootOption = None,
    store: StoreOption = None,
    as_app: AsAppOption = OPERATOR,
    app_name: AppOption = None,
    start: Annotated[int, typer.Option("--start")] = 0,
    end: Annotated[int | None, typer.Option("--end")] = None,
    max_bytes: Annotated[int, typer.Option("--max-bytes")] = 16 * 1024,
) -> None:
    """A redacted, size-capped range of one session's turns."""

    try:
        share = open_share(root, as_app, store=store)
        typer.echo(
            _dump(share.get(session_id, app=app_name, start=start, end=end, max_bytes=max_bytes))
        )
    except (ConversationError, MemfwError) as exc:
        raise _fail(exc) from exc


@app.command("handoff")
def handoff(
    session_id: str,
    root: RootOption = None,
    store: StoreOption = None,
    as_app: AsAppOption = OPERATOR,
    app_name: AppOption = None,
    for_agent: Annotated[str | None, typer.Option("--for", help="App that continues")] = None,
    max_bytes: Annotated[int, typer.Option("--max-bytes")] = 16 * 1024,
    output: Annotated[str, typer.Option("--format", help="md or json")] = "md",
) -> None:
    """A continuation pack for another agent (printed; nothing is written)."""

    if output not in {"md", "json"}:
        raise _fail(ConversationError("--format must be md or json"))
    try:
        share = open_share(root, as_app, store=store)
        pack = share.handoff(session_id, app=app_name, for_agent=for_agent, max_bytes=max_bytes)
    except (ConversationError, MemfwError) as exc:
        raise _fail(exc) from exc
    typer.echo(_dump(pack) if output == "json" else render_markdown(pack))


@app.command("worker")
def worker(
    root: RootOption = None,
    store: StoreOption = None,
    state: Annotated[Path | None, typer.Option("--state", help="Worker state path")] = None,
    backend: Annotated[
        str | None, typer.Option("--backend", help="'extractive' (default) or module:factory")
    ] = None,
    allow_cloud: Annotated[
        bool, typer.Option("--allow-cloud", help="Permit a non-local backend")
    ] = False,
    judge: Annotated[
        str | None, typer.Option("--judge", help="Claim judge module:function (verify-first)")
    ] = None,
    ingest: Annotated[bool, typer.Option("--ingest/--no-ingest")] = True,
    max_sessions: Annotated[int | None, typer.Option("--max-sessions")] = None,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens")] = None,
    max_seconds: Annotated[float | None, typer.Option("--max-seconds")] = None,
    idle_minutes: Annotated[float | None, typer.Option("--idle-minutes")] = None,
) -> None:
    """One idempotent, locked, budgeted processing run; prints counts only."""

    from memfw.dream.run import load_judge

    try:
        model = load_backend(backend, allow_cloud=allow_cloud)
        runner = open_worker(
            root,
            backend=model,
            store=store,
            state=state,
            ingest=ingest,
            judge=load_judge(judge),
            max_sessions=max_sessions,
            max_tokens=max_tokens,
            max_seconds=max_seconds,
            idle_minutes=idle_minutes,
        )
        report = runner.run()
    except (ConversationError, MemfwError, ValueError) as exc:
        raise _fail(exc) from exc
    typer.echo(_dump(report.as_dict()))


@app.command("status")
def status(root: RootOption = None) -> None:
    """Worker job and episode counts (no text)."""

    try:
        config, vault = read_config(root)
        typer.echo(_dump(WorkState(state_path(config, vault=vault)).status()))
    except (ConversationError, MemfwError) as exc:
        raise _fail(exc) from exc


@app.command("schedule")
def schedule(
    print_plist: Annotated[bool, typer.Option("--print", help="Print the launchd plist")] = False,
    root: RootOption = None,
    interval: Annotated[int, typer.Option("--interval")] = DEFAULT_INTERVAL,
    memfw_bin: Annotated[str | None, typer.Option("--bin", help="memfw executable")] = None,
) -> None:
    """Print a launchd plist for the worker. Installing it is left to you."""

    if not print_plist:
        raise _fail(ConversationError("only --print is supported; install the plist yourself"))
    if root is None:
        raise _fail(ConversationError("--root (or MEMFW_ROOT) is required"))
    try:
        typer.echo(render(vault=root, memfw_bin=memfw_bin, interval=interval), nl=False)
    except ConversationError as exc:
        raise _fail(exc) from exc
