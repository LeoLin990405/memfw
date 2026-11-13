"""``memfw history``: read-only transcript index. Mount ``app`` as ``memfw history``."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

import typer

from ..core.errors import MemfwError
from ..core.repository import Repository
from .conversations import ORIGINS, ConversationStore, OptOut
from .index import (
    HistoryError,
    HistoryLimits,
    SourceRoot,
    TranscriptIndex,
    default_db,
    dumps,
    parse_source_option,
    sources_from_config,
)
from .redact import redact_path
from .sources import ALIASES, READERS, StoreRoot, UnknownAgent, get_reader, store_roots
from .store_location import check_store_path, store_path_from_config

app = typer.Typer(help="Read-only index over agent session transcripts", no_args_is_help=True)

RootOption = Annotated[
    Path, typer.Option("--root", "-r", help="memfw vault root (holds config and the index)")
]
SourceOption = Annotated[
    list[str] | None,
    typer.Option("--source", help="NAME=PATH transcript root; replaces vault.yaml sources"),
]
DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="Derived index path (default <root>/.memfw-history.sqlite)"),
]
RefreshOption = Annotated[
    bool, typer.Option("--refresh/--no-refresh", help="Incrementally re-index before querying")
]


def open_index(root: Path, sources: list[str] | None, db: Path | None) -> TranscriptIndex:
    """Build an index from CLI sources, else from ``history_sources`` in vault.yaml."""

    roots: list[SourceRoot]
    repository = Repository(root)
    config: dict[str, Any] = repository.read_config() if repository.config_path.exists() else {}
    if sources:
        roots = [parse_source_option(item) for item in sources]
    else:
        roots = sources_from_config(config)
    if not roots:
        raise HistoryError("no history sources: set history_sources in vault.yaml or pass --source")
    return TranscriptIndex(
        db or default_db(Path(root).expanduser().resolve()),
        roots,
        limits=HistoryLimits.from_config(config),
    )


def _run(action: Any) -> None:
    try:
        typer.echo(dumps(action()))
    except (HistoryError, MemfwError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("index")
def history_index(
    root: RootOption = Path("."), source: SourceOption = None, db: DbOption = None
) -> None:
    """Incrementally index transcripts (unchanged size+mtime files are skipped)."""

    def action() -> dict[str, Any]:
        index = open_index(root, source, db)
        return {"counts": index.refresh(), "stats": index.stats()}

    _run(action)


@app.command("list")
def history_list(
    root: RootOption = Path("."),
    source: SourceOption = None,
    db: DbOption = None,
    source_name: Annotated[str | None, typer.Option("--source-name")] = None,
    session: Annotated[str | None, typer.Option("--session", help="List one session")] = None,
    since: Annotated[str | None, typer.Option("--since", help="ISO timestamp lower bound")] = None,
    until: Annotated[str | None, typer.Option("--until", help="ISO timestamp upper bound")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    refresh: RefreshOption = True,
) -> None:
    """List sessions with lineage, or one session's windows and entries."""

    def action() -> dict[str, Any]:
        index = open_index(root, source, db)
        if refresh:
            index.refresh()
        return index.list(
            source=source_name, session=session, since=since, until=until, limit=limit
        )

    _run(action)


@app.command("search")
def history_search(
    query: Annotated[str, typer.Argument()],
    root: RootOption = Path("."),
    source: SourceOption = None,
    db: DbOption = None,
    source_name: Annotated[str | None, typer.Option("--source-name")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    refresh: RefreshOption = True,
) -> None:
    """Search redacted snippets; results carry a ref for `memfw history read`."""

    def action() -> dict[str, Any]:
        index = open_index(root, source, db)
        if refresh:
            index.refresh()
        return index.search(
            query, source=source_name, session=session, since=since, until=until, limit=limit
        )

    _run(action)


@app.command("read")
def history_read(
    ref: Annotated[str, typer.Argument(help="SOURCE:FILE@OFFSET from list or search")],
    root: RootOption = Path("."),
    source: SourceOption = None,
    db: DbOption = None,
    before: Annotated[int, typer.Option("--before")] = 2,
    after: Annotated[int, typer.Option("--after")] = 2,
    max_bytes: Annotated[int, typer.Option("--max-bytes")] = 8192,
) -> None:
    """Read the redacted, size-capped window at a stored location."""

    def action() -> dict[str, Any]:
        index = open_index(root, source, db)
        return index.read(ref, before=before, after=after, max_bytes=max_bytes)

    _run(action)


# -- agent conversation readers ----------------------------------------------------

ConversationDbOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        help="Conversation store (default: vault.yaml conversation_store.path, else"
        " ~/.local/share/memfw/conversations.sqlite)",
    ),
]


def _display(path: Path, home: Path) -> str:
    """A store path as ``~/...`` so the user's home never echoes back."""

    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return redact_path(str(path))


def open_conversation_store(root: Path, db: Path | None) -> ConversationStore:
    """The local conversation store, after the location checks (vault, git, synced)."""

    vault = Path(root).expanduser().resolve()
    repository = Repository(vault)
    config: dict[str, Any] = repository.read_config() if repository.config_path.exists() else {}
    path = db if db is not None else store_path_from_config(config)
    return ConversationStore(
        check_store_path(path, vault_root=vault), optout=OptOut.from_config(config)
    )


@app.command("agents")
def history_agents() -> None:
    """List the registered agent readers and which stores exist here (home and Worker homes)."""

    def action() -> dict[str, Any]:
        home = Path.home()
        return {
            "agents": [
                {
                    "agent": name,
                    "parser_version": reader.parser_version,
                    "default_roots": ["~/" + root for root in reader.default_roots],
                    "found": [
                        {"origin": item.origin, "path": _display(item.path, home)}
                        for item in store_roots(reader, home)
                    ],
                }
                for name, reader in READERS.items()
            ],
            "aliases": ALIASES,
        }

    _run(action)


@app.command("ingest")
def history_ingest(
    agents: Annotated[
        list[str] | None, typer.Argument(help="Agents to ingest (default: every registered one)")
    ] = None,
    root: RootOption = Path("."),
    db: ConversationDbOption = None,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Read this store root instead of the defaults (one agent)"),
    ] = None,
) -> None:
    """Incrementally ingest agent conversations into the local, redacted conversation store."""

    def action() -> dict[str, Any]:
        try:
            readers = [get_reader(name) for name in agents] if agents else list(READERS.values())
        except UnknownAgent as exc:
            raise HistoryError(f"unknown agent: {exc.args[0]}") from exc
        if path is not None and len(readers) != 1:
            raise HistoryError("--path needs exactly one agent")
        roots_override: list[StoreRoot] | None = None
        if path is not None:
            explicit = path.expanduser()
            if explicit.is_symlink() or not explicit.is_dir():
                raise HistoryError("--path must be a real directory")
            roots_override = [StoreRoot(explicit.resolve())]
        store = open_conversation_store(root, db)
        try:
            store.stats()  # opens (and creates) the store once, before any reader runs
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"cannot open conversation store: {exc}") from exc
        results: dict[str, Any] = {}
        for reader in readers:
            roots = roots_override if roots_override is not None else store_roots(reader)
            try:
                counts = store.ingest(reader, roots).as_dict()
            except Exception as exc:  # one broken reader must not stop the others
                counts = {"error": type(exc).__name__}
            results[reader.agent] = counts | {
                "roots": {o: sum(1 for r in roots if r.origin == o) for o in ORIGINS}
            }
        try:
            stats = store.stats()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"cannot read conversation store: {exc}") from exc
        return {"ingested": results, "stats": stats}

    _run(action)
