"""``memfw attach`` commands: add, list, show, verify (MM1a); detach, relink,
purge, scan (MM1b).

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

from .proxy_cli import app as proxy_app

app = typer.Typer(
    help=(
        "Attachments: originals kept as blobs outside the vault or referenced under "
        "configured roots (add, list, show, verify, detach, relink, purge, scan)"
    ),
    no_args_is_help=True,
)

app.add_typer(proxy_app, name="proxy")

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
    file: Annotated[
        Path,
        typer.Argument(
            help="Regular file to attach (not a symlink); with --reference also ROOT:PATH"
        ),
    ],
    root: Root = Path("."),
    embed: Annotated[
        bool,
        typer.Option(
            "--embed", help="Copy the bytes (audio and video outside a configured root need it)"
        ),
    ] = False,
    reference: Annotated[
        bool,
        typer.Option(
            "--reference",
            help="Record only a location under a configured root (attachments_roots); no copy",
        ),
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
    """Register FILE: hash it, copy it to the blob store (or reference it), record an event."""
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
            reference=reference,
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
            **({"location": record.location} if record.location is not None else {}),
        }
    )


@app.command("list")
def list_attachments(
    root: Root = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="One JSON array")] = False,
    show_all: Annotated[bool, typer.Option("--all", help="Include detached attachments")] = False,
) -> None:
    """List committed attachments (id, media type, size, privacy, name); detached hidden."""
    repository = _repo(root)
    rows: list[dict[str, Any]] = []
    failed = False
    try:
        loaded = repository.attachment_records()
        states = repository.attachment_state_map()
    except MemfwError as exc:
        _fail(exc)
        return
    for attachment_id, record in loaded:
        state = states[attachment_id].label
        if state == "detached" and not show_all:
            continue
        if isinstance(record, MemfwError):
            typer.echo(f"warning: {attachment_id}: {record}", err=True)
            failed = True
            continue
        row: dict[str, Any] = {
            "id": record.id,
            "media_type": record.media_type,
            "size": record.size,
            "privacy": record.privacy_level,
            "original_name": record.original_name,
            "recorded_at": record.recorded_at,
        }
        # Keys added in MM1b appear only when they say something.
        if record.storage != "embedded":
            row["storage"] = record.storage
            row["location"] = record.location
        if state != "active":
            row["state"] = state
        rows.append(row)
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
        record, log_state = repository.get_attachment_with_state(attachment_id)
        cited_by = repository.attachment_citations(attachment_id)
        state = log_state.label
    except MemfwError as exc:
        _fail(exc)
        return
    payload = record.to_json()
    payload["cited_by"] = cited_by
    if state != "active":
        payload["state"] = state
    _dump(payload)


@app.command("verify")
def verify(
    attachment_ids: Annotated[
        list[str] | None, typer.Argument(metavar="[ID]...", help="Default: every attachment")
    ] = None,
    root: Root = Path("."),
    record_missing: Annotated[
        bool,
        typer.Option(
            "--record-missing",
            help=(
                "Record state changes: attach_missing for originals found gone, "
                "attach_found for missing ones that are back"
            ),
        ),
    ] = False,
    no_candidates: Annotated[
        bool,
        typer.Option(
            "--no-candidates", help="Do not search the root for a moved original's sha256"
        ),
    ] = False,
) -> None:
    """Re-hash records, blobs and referenced originals; exit 1 on a missing or changed one.

    Writes nothing unless --record-missing. A missing referenced original lists
    candidates (same sha256 under the same root) for `attach relink`; nothing
    is relinked automatically.
    """
    repository = _repo(root)
    try:
        checked, problems = repository.verify_attachments(
            attachment_ids or None, candidates=not no_candidates
        )
        recorded: list[str] = []
        found: list[str] = []
        if record_missing:
            gone = [
                str(problem.attachment_id)
                for problem in problems
                if problem.status == "referenced_missing"
            ]
            recorded = repository.record_missing_attachments(gone) if gone else []
            found = repository.record_found_attachments()
    except MemfwError as exc:
        _fail(exc)
        return
    failing = [problem for problem in problems if problem.status not in _WARNINGS]
    failing_ids = {problem.attachment_id for problem in failing}
    payload: dict[str, Any] = {
        "checked": len(checked),
        "ok": len([item for item in checked if item not in failing_ids]),
        "problems": [problem.to_json() for problem in problems],
    }
    if record_missing:
        payload["recorded_missing"] = recorded
        payload["recorded_found"] = found
    _dump(payload)
    if failing:
        raise typer.Exit(code=1)


@app.command("detach")
def detach(
    attachment_id: Annotated[str, typer.Argument(metavar="ID")],
    root: Root = Path("."),
) -> None:
    """Soft-delete ID (one detach event); record and blob stay until `attach purge`."""
    repository = _repo(root)
    try:
        record = repository.detach_attachment(attachment_id)
    except MemfwError as exc:
        _fail(exc)
        return
    _dump({"id": record.id, "state": "detached"})


@app.command("relink")
def relink(
    attachment_id: Annotated[str, typer.Argument(metavar="ID")],
    target: Annotated[str, typer.Argument(metavar="PATH", help="File in a root, or ROOT:PATH")],
    root: Root = Path("."),
) -> None:
    """Point a referenced ID at PATH, which must hold the same bytes (sha256 and size)."""
    repository = _repo(root)
    try:
        record = repository.relink_attachment(attachment_id, target)
    except (MemfwError, ValueError, OSError) as exc:
        _fail(exc)
        return
    _dump({"id": record.id, "location": record.location})


@app.command("purge")
def purge(
    attachment_id: Annotated[
        str | None, typer.Argument(metavar="[ID]", help="Attachment to hard-delete")
    ] = None,
    root: Root = Path("."),
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm the hard delete (without it: plan only)")
    ] = False,
    orphans: Annotated[
        bool,
        typer.Option(
            "--orphans", help="Instead: blobs no record names and this vault's stale stages"
        ),
    ] = False,
) -> None:
    """Hard-delete ID: record, blob (unless shared), stale stages, the index; cannot be undone.

    Without --yes only the plan is printed (memories citing ID, a shared blob,
    copies a purge cannot reach) and nothing is written.
    """
    if orphans == (attachment_id is not None):
        _fail(ValueError("give either an attachment ID or --orphans"))
        return
    repository = _repo(root)
    try:
        if orphans:
            if yes:
                _dump({**repository.purge_orphan_blobs(confirm=True).to_json(), "purged": True})
            else:
                _dump({**repository.attachment_orphan_plan().to_json(), "purged": False})
            return
        assert attachment_id is not None
        if yes:
            _dump(repository.purge_attachment(attachment_id, confirm=True).to_json())
            return
        plan = repository.attachment_purge_plan(attachment_id).to_json()
        plan["purged"] = False
        plan["confirm"] = "rerun with --yes to hard-delete (cannot be undone)"
        _dump(plan)
    except (MemfwError, OSError) as exc:
        _fail(exc)


@app.command("reown")
def reown(
    root: Root = Path("."),
    new_vault_id: Annotated[
        bool,
        typer.Option(
            "--new-vault-id",
            help="For a copy of a vault: new vault_id, blobs copied into its own directory",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm")] = False,
) -> None:
    """Make the vault's blob directory its own (after a move), or split a copy off (--new-vault-id).

    A copy of a vault (cp -r, a snapshot, an eval vault) shares its vault_id and
    so its blob directory; run `attach reown --new-vault-id --yes` in the copy
    before any attachment write. Without --yes nothing is written.
    """
    repository = _repo(root)
    if not yes:
        store = repository.attachment_blob_store()
        _dump(
            {
                "owner": store.owner_status() if store is not None else "absent",
                "changed": False,
                "confirm": "rerun with --yes",
            }
        )
        return
    try:
        result = repository.reown_blob_store(new_vault_id=new_vault_id, confirm=True)
    except (MemfwError, OSError) as exc:
        _fail(exc)
        return
    _dump({**result, "changed": True})


@app.command("scan")
def scan(
    target: Annotated[
        str, typer.Argument(metavar="ROOT", help="ROOT: or ROOT:DIR, or a directory in a root")
    ],
    root: Root = Path("."),
    hash_files: Annotated[
        bool, typer.Option("--hash", help="Also count duplicate sha256 (reads same-size files)")
    ] = False,
) -> None:
    """Dry-run inventory of a configured root (counts, sizes, types, likely private).

    Writes nothing: no record, no event, no blob, no vault_id.
    """
    repository = _repo(root)
    try:
        report = repository.scan_attachment_root(target, hash_files=hash_files)
    except (MemfwError, OSError) as exc:
        _fail(exc)
        return
    _dump(report)


#: Reported but not failing: nothing an attachment needs is missing or changed.
_WARNINGS = frozenset(
    {
        "blob_orphan",
        "blob_mode_loose",
        "blob_stage_stale",
        "blob_stage_foreign",
        # The root is not mounted or not configured on this host: nothing is known to be lost.
        "referenced_unavailable",
    }
)


__all__ = ["app"]
