"""``memfw attach proxy`` commands (MM2a): import, list, show, budget.

Like the rest of ``memfw attach`` this is the operator's local surface: it
shows every proxy whatever its privacy level. The import format is
``memfw.ports.proxies`` (``docs/attachment-proxies.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from memfw.core import invocation
from memfw.core.errors import MemfwError
from memfw.core.repository import Repository, open_vault

app = typer.Typer(
    help="Text proxies of attachments (OCR, transcripts, extracts): import, list, show, budget",
    no_args_is_help=True,
)

Root = Annotated[Path, typer.Option("--root", "-r", help="Vault directory")]


def _repo(root: Path) -> Repository:
    return open_vault(
        root,
        clock=invocation.clock(),
        id_factory=invocation.id_factory(root),
        error=typer.BadParameter,
    )


def _dump(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


@app.command("import")
def import_proxy(
    attachment_id: Annotated[str, typer.Argument(metavar="ATT-ID")],
    file: Annotated[Path, typer.Argument(help="Proxy file (JSON, format memfw.proxy v1)")],
    root: Root = Path("."),
    privacy: Annotated[
        str | None,
        typer.Option(help="public, shared or local-private (default local-private)"),
    ] = None,
    supersedes: Annotated[
        str | None, typer.Option(help="Active proxy (prx-...) of the same attachment it replaces")
    ] = None,
) -> None:
    """Validate FILE against attachment ATT-ID and store it as a proxy (one proxy_add event)."""
    repository = _repo(root)
    try:
        record = repository.import_proxy(
            attachment_id, file, privacy=privacy, supersedes=supersedes
        )
    except (MemfwError, ValueError, OSError) as exc:
        _fail(exc)
        return
    _dump(
        {
            "id": record.id,
            "attachment": record.attachment_id,
            "kind": record.kind,
            "segments": len(record.segments),
            "coverage": record.coverage.to_json(),
            "privacy": record.privacy_level,
            "supersedes": record.supersedes,
        }
    )


@app.command("list")
def list_proxies(
    attachment_id: Annotated[
        str | None, typer.Argument(metavar="[ATT-ID]", help="Default: every attachment")
    ] = None,
    root: Root = Path("."),
    include_superseded: Annotated[
        bool, typer.Option("--all", help="Include superseded proxies")
    ] = False,
) -> None:
    """List committed proxies as one JSON array (metadata, no text)."""
    repository = _repo(root)
    try:
        rows = repository.proxy_records(attachment_id, include_superseded=include_superseded)
    except MemfwError as exc:
        _fail(exc)
        return
    listed: list[dict[str, Any]] = []
    failed = False
    for proxy_id, record, superseded_by in rows:
        if isinstance(record, MemfwError):
            typer.echo(f"warning: {proxy_id}: {record}", err=True)
            failed = True
            continue
        item = record.summary()
        item["superseded_by"] = superseded_by
        listed.append(item)
    _dump(listed)
    if failed:
        raise typer.Exit(code=1)


@app.command("show")
def show(
    proxy_id: Annotated[str, typer.Argument(metavar="PRX-ID")],
    root: Root = Path("."),
) -> None:
    """Show one proxy with its segments and canonical locators."""
    repository = _repo(root)
    try:
        record = repository.get_proxy(proxy_id)
        superseded_by = repository.proxy_state().superseded_by.get(record.id)
    except MemfwError as exc:
        _fail(exc)
        return
    payload = record.to_json()
    payload["superseded_by"] = superseded_by
    _dump(payload)


@app.command("budget")
def budget(root: Root = Path(".")) -> None:
    """Bytes the proxy files take, against attachments_proxy_max_bytes (separate budget)."""
    _dump(_repo(root).proxy_usage())


__all__ = ["app"]
