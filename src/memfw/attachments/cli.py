"""``memfw attach`` commands (MM1a): add, list, show, verify.

The CLI is the human operator's local surface, so it shows every attachment
whatever its privacy level (like ``memfw list``); no MCP tool or HTTP route
serves attachments yet (MM2b).
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
    help="Attachments: originals kept as blobs outside the vault (add, list, show, verify)",
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


@app.command("add")
def add(
    file: Annotated[Path, typer.Argument(help="Regular file to attach (not a symlink)")],
    root: Root = Path("."),
    embed: Annotated[
        bool, typer.Option("--embed", help="Copy the bytes (required for audio and video)")
    ] = False,
    privacy: Annotated[
        str | None,
        typer.Option(help="public, shared or local-private (default local-private)"),
    ] = None,
    media_type: Annotated[
        str | None, typer.Option("--media-type", help="Override the guessed media type")
    ] = None,
    origin: Annotated[str | None, typer.Option(help="Where the original came from")] = None,
    note: Annotated[str, typer.Option(help="Human note kept in the record body")] = "",
    captured_at: Annotated[
        str | None, typer.Option("--captured-at", help="ISO 8601 time with a timezone")
    ] = None,
    retention: Annotated[str, typer.Option(help="keep, review or until:YYYY-MM-DD")] = "keep",
    rights: Annotated[str | None, typer.Option(help="Copyright / usage note")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Tag (repeatable)")] = None,
) -> None:
    """Register FILE: hash it, copy it to the blob store, write a record and an event."""
    repository = _repo(root)
    try:
        result = repository.add_attachment(
            file,
            privacy=privacy,
            media_type=media_type,
            origin=origin,
            note=note,
            captured_at=captured_at,
            retention=retention,
            rights=rights,
            tags=tag or [],
            embed=embed,
        )
    except (MemfwError, ValueError, OSError) as exc:
        _fail(exc)
        return
    record = result.record
    _dump(
        {
            "id": record.id,
            "sha256": record.sha256,
            "size": record.size,
            "media_type": record.media_type,
            "storage": record.storage,
            "privacy": record.privacy_level,
            "deduplicated": result.deduplicated,
        }
    )


@app.command("list")
def list_attachments(
    root: Root = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="One JSON array")] = False,
) -> None:
    """List committed attachments (id, media type, size, privacy, name)."""
    repository = _repo(root)
    rows: list[dict[str, Any]] = []
    failed = False
    for attachment_id in repository.attachment_ids():
        try:
            record = repository.get_attachment(attachment_id)
        except MemfwError as exc:
            typer.echo(f"warning: {attachment_id}: {exc}", err=True)
            failed = True
            continue
        rows.append(
            {
                "id": record.id,
                "media_type": record.media_type,
                "size": record.size,
                "privacy": record.privacy_level,
                "original_name": record.original_name,
                "recorded_at": record.recorded_at,
            }
        )
    if as_json:
        _dump(rows)
    else:
        for row in rows:
            typer.echo(
                "\t".join(
                    str(row[key] if row[key] is not None else "")
                    for key in ("id", "media_type", "size", "privacy", "original_name")
                )
            )
    if failed:
        raise typer.Exit(code=1)


@app.command("show")
def show(
    attachment_id: Annotated[str, typer.Argument(metavar="ID")],
    root: Root = Path("."),
) -> None:
    """Show one record (frontmatter, note) and the memories that cite att:ID."""
    repository = _repo(root)
    try:
        record = repository.get_attachment(attachment_id)
        cited_by = repository.attachment_citations(attachment_id)
    except MemfwError as exc:
        _fail(exc)
        return
    payload = record.to_json()
    payload["cited_by"] = cited_by
    _dump(payload)


@app.command("verify")
def verify(
    attachment_ids: Annotated[
        list[str] | None, typer.Argument(metavar="[ID]...", help="Default: every attachment")
    ] = None,
    root: Root = Path("."),
) -> None:
    """Re-hash records and blobs; exit 1 on a missing or changed one. Writes nothing."""
    repository = _repo(root)
    try:
        checked, problems = repository.verify_attachments(attachment_ids or None)
    except MemfwError as exc:
        _fail(exc)
        return
    failing = [problem for problem in problems if problem.status not in _WARNINGS]
    failing_ids = {problem.attachment_id for problem in failing}
    _dump(
        {
            "checked": len(checked),
            "ok": len([item for item in checked if item not in failing_ids]),
            "problems": [problem.to_json() for problem in problems],
        }
    )
    if failing:
        raise typer.Exit(code=1)


#: Reported but not failing: nothing an attachment needs is missing or changed.
_WARNINGS = frozenset({"blob_orphan", "blob_mode_loose"})


__all__ = ["app"]
