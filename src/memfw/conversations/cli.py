"""``memfw conversations``: read the shared store, run the worker, print the schedule.

Every read command prints redacted text only, labelled as data, and obeys the
same per-app policy as the MCP tools (the caller is ``operator`` unless
``--as-app`` names an app). Nothing here writes to the store except
``worker`` (through the store's own ingest) and ``reredact --apply``
(:mod:`memfw.history.reredact`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ..core.errors import MemfwError
from .adapter import checked_location, store_path
from .backends import backend_status, gated_loader
from .policy import OPERATOR
from .schedule import DEFAULT_INTERVAL, default_log_dir, render
from .service import open_share, open_worker, read_config
from .share import render_markdown
from .store import ConversationError
from .worker import LOCK_NAME, WorkState, run_lock, state_path

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
ArchiveStateOption = Annotated[
    bool,
    typer.Option("--with-archive-state", help="Add archived: true/false (settled ingest)"),
]
ArchivedOnlyOption = Annotated[
    bool, typer.Option("--archived-only", help="Only sessions archived by settled ingest")
]


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
    with_archive_state: ArchiveStateOption = False,
    archived_only: ArchivedOnlyOption = False,
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            help="'default' (turn snippets) or 'two_stage' (ranked sessions: summary, tags,"
            " turn windows)",
        ),
    ] = None,
    task_type: Annotated[
        str | None, typer.Option("--task-type", help="Tag filter: dev, review, debug, plan ...")
    ] = None,
    outcome: Annotated[
        str | None, typer.Option("--outcome", help="Tag filter: done, blocked, ongoing ...")
    ] = None,
    project: Annotated[str | None, typer.Option("--project", help="Tag filter")] = None,
    component: Annotated[str | None, typer.Option("--component", help="Tag filter")] = None,
    intent_like: Annotated[
        str | None, typer.Option("--intent-like", help="Tag filter: text inside the intent")
    ] = None,
    windows: Annotated[
        int | None, typer.Option("--windows", help="two_stage: turn windows per session")
    ] = None,
    window_turns: Annotated[
        int | None, typer.Option("--window-turns", help="two_stage: turns each side of a hit")
    ] = None,
    max_bytes: Annotated[
        int | None, typer.Option("--max-bytes", help="two_stage: output size cap")
    ] = None,
) -> None:
    """Redacted snippets of turns matching every query term (QUERY may be '' with
    --mode two_stage and a tag filter)."""

    try:
        embedder = None
        if mode == "two_stage":
            import os

            from memfw.runtime.settings import embedder_from_env

            embedder = embedder_from_env(os.environ)
        share = open_share(root, as_app, store=store, embedder=embedder)
        typer.echo(
            _dump(
                share.search(
                    query,
                    app=app_name,
                    since=since,
                    limit=limit,
                    offset=offset,
                    with_archive_state=with_archive_state,
                    archived_only=archived_only,
                    mode=mode,
                    task_type=task_type,
                    outcome=outcome,
                    project=project,
                    component=component,
                    intent_like=intent_like,
                    windows=windows,
                    window_turns=window_turns,
                    max_bytes=max_bytes,
                )
            )
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
    with_archive_state: ArchiveStateOption = False,
    archived_only: ArchivedOnlyOption = False,
) -> None:
    """Sessions, most recently updated first."""

    try:
        share = open_share(root, as_app, store=store)
        typer.echo(
            _dump(
                share.list_sessions(
                    app=app_name,
                    since=since,
                    limit=limit,
                    offset=offset,
                    with_archive_state=with_archive_state,
                    archived_only=archived_only,
                )
            )
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
    with_archive_state: ArchiveStateOption = False,
    archived_only: ArchivedOnlyOption = False,
) -> None:
    """A redacted, size-capped range of one session's turns."""

    try:
        share = open_share(root, as_app, store=store)
        typer.echo(
            _dump(
                share.get(
                    session_id,
                    app=app_name,
                    start=start,
                    end=end,
                    max_bytes=max_bytes,
                    with_archive_state=with_archive_state,
                    archived_only=archived_only,
                )
            )
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
        str | None,
        typer.Option(
            "--backend",
            help="'extractive' or module:factory (default: vault.yaml conversations.backend,"
            " else extractive)",
        ),
    ] = None,
    allow_cloud: Annotated[
        bool,
        typer.Option(
            "--allow-cloud",
            help="Permit a non-local --backend (also needs conversations.cloud: true)",
        ),
    ] = False,
    backfill_days: Annotated[
        float | None,
        typer.Option(
            "--backfill-days",
            help="Under a new prompt version, re-plan unchanged sessions this many days back"
            " (default: vault.yaml conversations.backfill_days, else 30)",
        ),
    ] = None,
    judge: Annotated[
        str | None, typer.Option("--judge", help="Claim judge module:function (verify-first)")
    ] = None,
    ingest: Annotated[bool, typer.Option("--ingest/--no-ingest")] = True,
    max_sessions: Annotated[int | None, typer.Option("--max-sessions")] = None,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens")] = None,
    max_seconds: Annotated[float | None, typer.Option("--max-seconds")] = None,
    idle_minutes: Annotated[float | None, typer.Option("--idle-minutes")] = None,
    settled_minutes: Annotated[
        float | None,
        typer.Option(
            "--settled-minutes", min=0, help="Settled ingest: active sessions stay out of the store"
        ),
    ] = None,
) -> None:
    """One idempotent, locked, budgeted processing run; prints counts only."""

    from memfw.dream.claims import DreamClaimStager, DreamClaimVerifier
    from memfw.dream.run import load_judge

    try:
        # No --backend: the one vault.yaml names (gated on conversations.cloud).
        # With --backend, a non-local one needs --allow-cloud AND conversations.cloud.
        model = None
        if backend:
            config, _vault = read_config(root)
            model = gated_loader(config)(backend, allow_cloud=allow_cloud)
        runner = open_worker(
            root,
            backend=model,
            store=store,
            state=state,
            ingest=ingest,
            judge=load_judge(judge),
            verifier=DreamClaimVerifier(),
            stager=DreamClaimStager(),
            max_sessions=max_sessions,
            max_tokens=max_tokens,
            max_seconds=max_seconds,
            idle_minutes=idle_minutes,
            settled_minutes=settled_minutes,
            backfill_days=backfill_days,
        )
        report = runner.run()
    except (ConversationError, MemfwError, ValueError) as exc:
        raise _fail(exc) from exc
    typer.echo(_dump(report.as_dict()))


REREDACT_HELP = """Re-redact text already stored under older redaction rules (counts only).

Default: a dry run on a read-only connection; prints how many rows and columns
would change and which [REDACTED:kind] masks would be added. Never prints text.

--apply takes the store's writer lock and the worker's run lock (refuses when
either is held: stop the ingest/worker cycle first, or deploy the locked ingest:
while --apply runs, each ingest reader waits 60 s for the lock and then fails
that round, no data lost, the next round catches up). It writes a verified
backup (<store>.bak-reredact-<UTC>, 0600; it still holds every old value:
delete it once you are satisfied), then rewrites the changed rows in batches
(one BEGIN IMMEDIATE transaction each, guarded UPDATEs) with secure_delete on,
re-scans the rewritten rows (must be 0) and checks integrity. Interrupted? Run
it again: batches are idempotent.

Old bytes: secure_delete zeroes pages freed by this run; pages freed earlier
keep old values until --vacuum rewrites the file. The journal mode is detected:
in rollback-journal mode (the default) the -journal file briefly holds old
pages during each batch and is then deleted (its disk blocks are not wiped);
in WAL mode the WAL is checkpointed and truncated. The worker state
(conversations-work.sqlite: summaries, episodes) is scanned too and rewritten
with --worker-state. The history index (<vault>/.memfw-history.sqlite) is a
separate file rebuilt from transcripts: delete it, if it exists, and re-run
`memfw history index`.

Exit codes: 0 ok, 2 refused or failed before any write, 3 locked by another
writer, 4 verification failed, 5 failed after the backup (rows may have been
written; counts so far and the backup path are reported; run it again)."""


def _reredact_lines(report: dict[str, Any]) -> list[str]:
    lines = [f"mode: {report.get('mode')}  status: {report.get('status')}"]
    for name in ("store", "worker_state"):
        part = report.get(name) or {}
        if not part or part.get("present") is False:
            lines.append(f"{name}: not present")
            continue
        lines.append(
            f"{name}: scanned {part.get('rows_scanned', 0)}, changed {part.get('rows_changed', 0)}"
            + (f", newly withheld {part['rows_withheld']}" if part.get("rows_withheld") else "")
            + (" (not included: pass --worker-state)" if part.get("included") is False else "")
        )
        for key in ("columns", "markers", "key_columns_flagged"):
            if part.get(key):
                pairs = ", ".join(f"{k}={v}" for k, v in part[key].items())
                lines.append(f"  {key}: {pairs}")
        if part.get("verify"):
            lines.append(f"  verify: {part['verify']}")
    for item in report.get("backups") or []:
        lines.append(f"backup: {item['path']} (integrity {item['integrity']})")
    return lines


@app.command("reredact", help=REREDACT_HELP)
def reredact(
    root: RootOption = None,
    store: StoreOption = None,
    state: Annotated[Path | None, typer.Option("--state", help="Worker state path")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Rewrite (default: dry run)")] = False,
    backup: Annotated[
        Path | None,
        typer.Option("--backup", help="Backup path (default <store>.bak-reredact-<UTC>)"),
    ] = None,
    batch: Annotated[int, typer.Option("--batch", min=1, help="Rows per transaction")] = 2000,
    vacuum: Annotated[
        bool, typer.Option("--vacuum", help="After --apply, VACUUM so free pages hold no old text")
    ] = False,
    worker_state: Annotated[
        bool, typer.Option("--worker-state", help="Also rewrite the worker state")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable summary")] = False,
) -> None:
    from memfw.history.reredact import (
        EXIT_ERROR,
        Options,
        ReredactError,
        ReredactFailedError,
        ReredactVerifyError,
        run,
    )

    try:
        config, vault = read_config(root)
        db = (
            checked_location(store, config, vault=vault)
            if store
            else store_path(config, vault=vault)
        )
        work = (
            checked_location(state, config, vault=vault)
            if state
            else state_path(config, vault=vault)
        )
        options = Options(
            store=db,
            apply=apply,
            batch=batch,
            backup=backup.expanduser() if backup is not None else None,
            vacuum=vacuum,
            worker_state=work,
            include_worker=worker_state,
            worker_lock=lambda: run_lock(work.with_name(LOCK_NAME)),
            check_backup=lambda path: checked_location(path, config, vault=vault),
        )
        report = run(options)
        code = 0
    except (ReredactVerifyError, ReredactFailedError) as exc:
        # Rows may have been written: report the counts so far and the backup path.
        report = exc.report or {
            "mode": "apply",
            "status": "verify_failed" if isinstance(exc, ReredactVerifyError) else "failed",
            "error": str(exc),
            "store": exc.counts,
        }
        code = exc.code
    except ReredactError as exc:
        report = {"mode": "apply" if apply else "dry_run", "status": "refused", "error": str(exc)}
        code = exc.code
    except (ConversationError, MemfwError) as exc:
        report = {"mode": "apply" if apply else "dry_run", "status": "refused", "error": str(exc)}
        code = EXIT_ERROR
    if as_json:
        typer.echo(_dump(report))
    else:
        if report.get("error"):
            typer.echo(f"error: {str(report['error'])[:300]}", err=True)
        if report.get("status") != "refused":
            typer.echo("\n".join(_reredact_lines(report)))
    if code:
        raise typer.Exit(code)


@app.command("status")
def status(root: RootOption = None) -> None:
    """Worker job and episode counts (no text)."""

    try:
        config, vault = read_config(root)
        result = WorkState(state_path(config, vault=vault)).status()
        configured = backend_status(config)
        if configured is not None:  # only when vault.yaml names a backend
            result["backend"] = configured
        typer.echo(_dump(result))
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
        typer.echo(
            f"# launchd does not create the log directory; before loading run: "
            f"mkdir -p {default_log_dir()}",
            err=True,
        )
    except ConversationError as exc:
        raise _fail(exc) from exc
