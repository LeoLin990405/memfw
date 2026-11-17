"""``memfw handoff AGENT SESSION``: print a continuation pack for another agent.

Stable interface for callers such as Conductor's quota handoff:

* ``--format json`` prints one ``memfw.handoff/v1`` object (see
  :mod:`memfw.handoff.schema`) on stdout, exit 0;
* on failure it prints ``{"schema", "status": "unavailable", "reason"}`` on
  stdout and exits with :data:`~.schema.EXIT_USAGE` (2, ``bad_request``),
  :data:`~.schema.EXIT_NOT_INDEXED` (3, ``session_not_indexed``),
  :data:`~.schema.EXIT_STORE_UNAVAILABLE` (4, ``store_unavailable``) or
  :data:`~.schema.EXIT_REFUSED` (5, ``location_refused`` / ``not_readable``).

Read-only towards the conversation store; the pack itself is saved under
``handoffs/`` next to the store (0600) unless ``--no-save``.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from memfw.conversations.adapter import checked_location, store_path
from memfw.conversations.backends import gated_loader
from memfw.conversations.policy import SharePolicy
from memfw.conversations.service import read_config
from memfw.conversations.store import ConversationError, check_id, check_private_location
from memfw.core.errors import MemfwError

from .ledger import Ledger, handoff_dir
from .pack import (
    HandoffError,
    build_pack,
    check_max_bytes,
    json_size,
    markdown_size,
    render_json,
    render_markdown,
)
from .schema import (
    DEFAULT_BYTES,
    DEFAULT_RECENT,
    EXIT_NOT_INDEXED,
    EXIT_REFUSED,
    EXIT_STORE_UNAVAILABLE,
    EXIT_USAGE,
    SCHEMA,
)
from .source import StoreUnavailable
from .summarize import SummarizeError
from .tools_map import canonical_agent

TASK_FILE_BYTES = 64 * 1024


def _read_task(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HandoffError(EXIT_USAGE, message="cannot read --task-file") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HandoffError(EXIT_USAGE, message="--task-file must be a regular file")
    with path.open("rb") as handle:
        data = handle.read(TASK_FILE_BYTES)
    return data.decode("utf-8", errors="replace")


def _policy_check(config: dict[str, Any], agent: str, for_agent: str | None) -> None:
    try:
        policy = SharePolicy.from_config(config)
    except ConversationError as exc:
        raise HandoffError(EXIT_USAGE, message=str(exc)) from exc
    if not policy.readable(agent):
        # Hidden answers exactly like missing.
        raise HandoffError(EXIT_NOT_INDEXED)
    if for_agent and policy.enabled:
        try:
            allowed = policy.allowed(for_agent, agent)
        except ConversationError:
            allowed = False
        if not allowed:
            raise HandoffError(EXIT_REFUSED, "not_readable", f"app {for_agent} may not read it")


def run(
    agent: str,
    session: str,
    *,
    for_agent: str | None = None,
    max_bytes: int = DEFAULT_BYTES,
    recent: int = DEFAULT_RECENT,
    fmt: str = "md",
    summarize_spec: str | None = None,
    allow_cloud: bool = False,
    task_file: Path | None = None,
    cwd: Path | None = None,
    git: bool = True,
    root: Path | None = None,
    store: Path | None = None,
    save: bool = True,
) -> tuple[dict[str, Any], str]:
    """``(pack, printed text)``; :class:`HandoffError` on failure."""

    if fmt not in {"md", "json"}:
        raise HandoffError(EXIT_USAGE, message="--format must be md or json")
    check_max_bytes(max_bytes)
    try:
        name = canonical_agent(agent)
        check_id(name, "agent")
        check_id(session)
        target = canonical_agent(for_agent) if for_agent else None
        if target:
            check_id(target, "agent")
    except ConversationError as exc:
        raise HandoffError(EXIT_USAGE, message=str(exc)) from exc
    try:
        config, vault = read_config(root)
        # A non-local --summarize backend needs --allow-cloud AND conversations.cloud.
        loader = gated_loader(config)
    except (ConversationError, MemfwError) as exc:
        raise HandoffError(EXIT_USAGE, message=str(exc)[:200]) from exc
    _policy_check(config, name, target)
    try:
        db = (
            checked_location(store, config, vault=vault)
            if store
            else store_path(config, vault=vault)
        )
    except ConversationError as exc:
        raise HandoffError(EXIT_REFUSED, message=str(exc)) from exc
    task_text = _read_task(task_file) if task_file is not None else None
    ledger = None
    if save:
        try:
            directory = check_private_location(handoff_dir(db), vault=vault)
        except ConversationError as exc:
            raise HandoffError(EXIT_REFUSED, message=str(exc)) from exc
        ledger = Ledger(directory)
    render = render_json if fmt == "json" else render_markdown
    measure = json_size if fmt == "json" else markdown_size
    try:
        previous = ledger.previous(name, session) if ledger else []
        reservation = ledger.reserve(name, session, target, fmt) if ledger else None
    except (OSError, ConversationError) as exc:
        raise HandoffError(EXIT_STORE_UNAVAILABLE, "handoff_dir_unwritable") from exc
    try:
        pack = build_pack(
            db,
            name,
            session,
            for_agent=target,
            max_bytes=max_bytes,
            recent=recent,
            task_text=task_text,
            cwd=cwd,
            git=git,
            previous=previous,
            summarize_spec=summarize_spec,
            allow_cloud=allow_cloud,
            measure=measure,
            saved_as=reservation.path.name if reservation else None,
            loader=loader,
        )
        body = render(pack)
        if ledger is not None and reservation is not None:
            ledger.commit(name, session, reservation, body)
    except BaseException:
        if ledger is not None and reservation is not None:
            ledger.release(name, session, reservation)
        raise
    return pack, body


def handoff(
    agent: Annotated[
        str, typer.Argument(help="Agent whose session to hand off (claude, codex ...)")
    ],
    session: Annotated[str, typer.Argument(help="Session id in the conversation store")],
    for_agent: Annotated[
        str | None, typer.Option("--for", help="Agent that will continue the work")
    ] = None,
    max_bytes: Annotated[
        int, typer.Option("--max-bytes", help="Size cap of the printed pack (2048..65536)")
    ] = DEFAULT_BYTES,
    recent: Annotated[
        int, typer.Option("--recent", help="User/assistant turns to include (1..50)")
    ] = DEFAULT_RECENT,
    fmt: Annotated[str, typer.Option("--format", help="md or json")] = "md",
    summarize_spec: Annotated[
        str | None,
        typer.Option("--summarize", help="Add a model summary with this backend (module:factory)"),
    ] = None,
    allow_cloud: Annotated[
        bool,
        typer.Option(
            "--allow-cloud",
            help="Allow a non-local --summarize backend (also needs conversations.cloud: true)",
        ),
    ] = False,
    task_file: Annotated[
        Path | None,
        typer.Option("--task-file", help="Task text to use as the goal (e.g. from Conductor)"),
    ] = None,
    cwd: Annotated[
        Path | None, typer.Option("--cwd", help="Work tree for read-only git status / diff --stat")
    ] = None,
    no_git: Annotated[bool, typer.Option("--no-git", help="Skip the git state")] = False,
    root: Annotated[
        Path | None,
        typer.Option("--root", "-r", envvar="MEMFW_ROOT", help="memfw vault root (vault.yaml)"),
    ] = None,
    store: Annotated[
        Path | None,
        typer.Option("--store", help="Conversation store path (default from vault.yaml)"),
    ] = None,
    no_save: Annotated[
        bool, typer.Option("--no-save", help="Print only; do not save the pack or record it")
    ] = False,
) -> None:
    """Print a continuation pack (data, not instructions) for another agent."""

    try:
        _pack, body = run(
            agent,
            session,
            for_agent=for_agent,
            max_bytes=max_bytes,
            recent=recent,
            fmt=fmt,
            summarize_spec=summarize_spec,
            allow_cloud=allow_cloud,
            task_file=task_file,
            cwd=cwd,
            git=not no_git,
            root=root,
            store=store,
            save=not no_save,
        )
    except SummarizeError as exc:
        _fail(fmt, HandoffError(EXIT_USAGE, message=str(exc)))
    except StoreUnavailable as exc:
        _fail(fmt, HandoffError(EXIT_STORE_UNAVAILABLE, message=str(exc)))
    except HandoffError as exc:
        _fail(fmt, exc)
    typer.echo(body, nl=False)


def _fail(fmt: str, error: HandoffError) -> NoReturn:
    if fmt == "json":
        typer.echo(json.dumps({"schema": SCHEMA, "status": "unavailable", "reason": error.reason}))
    typer.echo(f"error: {error.reason}: {str(error)[:300]}", err=True)
    raise typer.Exit(error.code)
