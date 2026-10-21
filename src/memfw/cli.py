"""Typer command-line interface for deterministic local memory operations."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from .budget import BYTE_BUDGETS, BudgetLimits, BudgetProfile
from .capture.cli import app as capture_app
from .capture.confirm import confirm_command
from .core.errors import IntegrityError, MemfwError
from .core.models import Layer, Tier
from .core.repository import Repository
from .curation.engine import enforce_budget, run_forget
from .curation.review_cli import review_command as run_review
from .index import MemoryIndex
from .policies import PolicyName
from .retrieval import VaultRetriever
from .stats import measure_runtime
from .tiers import BudgetAwareTierPolicy

app = typer.Typer(help="Local-first Markdown memory vault", no_args_is_help=True)
index_app = typer.Typer(help="Manage the disposable trigram FTS5 index")
log_app = typer.Typer(help="Inspect the hash-chained append-only event log")
budget_app = typer.Typer(help="Inspect and enforce deterministic memory budgets")
forget_app = typer.Typer(help="Plan and apply policy-driven forgetting")
tier_app = typer.Typer(help="Inspect and move hot/cold memory tiers")
tier_alias_app = typer.Typer(help="Tier placement commands")
layer_alias_app = typer.Typer(help="Layer classification commands")
app.add_typer(index_app, name="index")
app.add_typer(log_app, name="log")
app.add_typer(budget_app, name="budget")
app.add_typer(forget_app, name="forget")
app.add_typer(tier_app, name="tiers")
app.add_typer(tier_alias_app, name="tier")
app.add_typer(layer_alias_app, name="layer")
app.add_typer(capture_app, name="capture")
capture_app.command(name="confirm")(confirm_command)


@app.command("review")
def review(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    approve_all: Annotated[bool, typer.Option("--approve-all")] = False,
    filter_type: Annotated[str | None, typer.Option("--filter")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    proposal_id: Annotated[str | None, typer.Option("--proposal-id")] = None,
    decision: Annotated[str | None, typer.Option("--decision")] = None,
    payload_hash: Annotated[str | None, typer.Option("--payload-hash")] = None,
    edited_output: Annotated[str | None, typer.Option("--edited-output")] = None,
    apply_proposal: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """List pending proposals or perform an explicit human review action."""
    output: dict[str, Any] | None = None
    if edited_output is not None:
        parsed: object = json.loads(edited_output)
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            raise typer.BadParameter("--edited-output must be a JSON object")
        output = parsed
    try:
        result = run_review(
            root,
            approve_all=approve_all,
            filter_type=filter_type,
            dry_run=dry_run,
            proposal_id=proposal_id,
            decision=decision,
            payload_hash=payload_hash,
            edited_output=output,
            apply=apply_proposal,
        )
    except IntegrityError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


_NOW: datetime | None = None
_ID_SEED: str | None = None


def _parse_now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("--now must be an ISO-8601 timestamp with timezone") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise typer.BadParameter("--now must include a timezone")
    return value.astimezone(UTC)


@app.callback()
def main(
    now: Annotated[str | None, typer.Option("--now", help="Inject an ISO-8601 UTC time")] = None,
    id_seed: Annotated[
        str | None, typer.Option("--id-seed", help="Inject a deterministic id seed")
    ] = None,
) -> None:
    """Manage a Markdown memory vault."""
    global _NOW, _ID_SEED
    _NOW = _parse_now(now)
    _ID_SEED = id_seed


def _repo(root: Path, *, allow_pending: bool = False) -> Repository:
    repository = Repository(
        root, clock=lambda: _NOW or datetime.now(UTC), id_factory=_id_factory(root)
    )
    if not repository.events_path.exists() and not repository.config_path.exists():
        raise typer.BadParameter(f"Not a memfw vault: {root}; run `memfw init {root}` first")
    if repository.pending_path.exists() and not allow_pending:
        raise typer.BadParameter("Vault has a pending transaction; run `memfw recover` first")
    return repository


def _id_factory(root: Path) -> Callable[[], str]:
    if _ID_SEED is None:
        import uuid

        return lambda: str(uuid.uuid4())
    sequence = 0
    event_log = root / "events.jsonl"
    if event_log.exists():
        try:
            sequence = len([line for line in event_log.read_bytes().splitlines() if line]) * 2
        except OSError:
            sequence = 0

    def next_id() -> str:
        nonlocal sequence
        sequence += 1
        return hashlib.sha256(f"{_ID_SEED}:{sequence}".encode()).hexdigest()[:32]

    return next_id


def _config(repository: Repository) -> dict[str, Any]:
    return repository.read_config()


def _limits(repository: Repository) -> BudgetLimits:
    config = _config(repository)
    try:
        name = BudgetProfile(config.get("budget_level", BudgetProfile.MB_10.value))
    except ValueError as exc:
        raise IntegrityError("budget_level must be 1MB, 10MB, or 100MB") from exc
    base = BYTE_BUDGETS[name]
    return BudgetLimits(
        max_bytes=config.get("max_bytes", base.max_bytes),
        max_count=config.get("max_count"),
    )


def _enforce_after_write(repository: Repository) -> None:
    if repository.read_config().get("auto_enforce_on_write") is True:
        result = enforce_budget(repository, now=repository._now(), dry_run=False, actor="mechanism")
        typer.echo(
            json.dumps(
                {
                    "auto_enforcement": True,
                    "applied_ids": list(result.applied_ids),
                    "satisfied_after_apply": result.satisfied_after_apply,
                },
                sort_keys=True,
            ),
            err=True,
        )


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Vault directory")],
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Initialize a vault."""
    if dry_run:
        typer.echo(json.dumps({"path": str(path), "would_initialize": not path.exists()}))
        return
    Repository(path, clock=lambda: _NOW or datetime.now(UTC), id_factory=_id_factory(path)).init()
    typer.echo(f"Initialized {path.expanduser().resolve()}")


@app.command()
def add(
    title: Annotated[str, typer.Option("--title")],
    body: Annotated[str, typer.Option("--body")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    memory_id: Annotated[str | None, typer.Option("--id")] = None,
    layer: Annotated[Layer, typer.Option()] = Layer.CURRENT,
    tier: Annotated[Tier, typer.Option()] = Tier.HOT,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Add a Markdown memory."""
    try:
        if dry_run:
            if root.exists() and (root / "vault.yaml").exists():
                repository = _repo(root)
                if memory_id is not None:
                    repository._check_id(memory_id)
                    if any(event.memory_id == memory_id for event in repository.events()):
                        raise IntegrityError(f"Memory id already used: {memory_id}")
            typer.echo(
                json.dumps(
                    {
                        "valid": True,
                        "memory_id": memory_id,
                        "layer": layer.value,
                        "tier": tier.value,
                    }
                )
            )
            return
        repository = _repo(root)
        doc = repository.add(title, body, memory_id=memory_id, layer=layer, tier=tier)
        _enforce_after_write(repository)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(doc.id)


@app.command("list")
def list_memories(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """List current memories and show integrity warnings."""
    repository = _repo(root)
    for document in repository.list():
        suffix = "\tstub" if document.is_stub else ""
        typer.echo(
            f"{document.id}\t{document.tier.value}\t{document.layer.value}\t{document.title}{suffix}"
        )
    for warning in repository.list_warnings():
        typer.echo(f"warning: {warning}", err=True)


@app.command()
def show(
    memory_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
) -> None:
    """Show one memory as Markdown."""
    try:
        repository = _repo(root)
        repository.get(memory_id)
        typer.echo(repository._current_path(memory_id).read_bytes().decode("utf-8"), nl=False)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def update(
    memory_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    title: Annotated[str | None, typer.Option("--title")] = None,
    body: Annotated[str | None, typer.Option("--body")] = None,
    layer: Annotated[Layer | None, typer.Option()] = None,
    tier: Annotated[Tier | None, typer.Option()] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Update selected memory fields; --dry-run validates without writing."""
    changes = {
        key: value
        for key, value in {"title": title, "body": body, "layer": layer, "tier": tier}.items()
        if value is not None
    }
    try:
        repository = _repo(root)
        if dry_run:
            old = repository.get(memory_id)
            typer.echo(
                json.dumps(
                    {
                        "memory_id": memory_id,
                        "changes": {k: str(v) for k, v in changes.items()},
                        "valid": bool(changes),
                        "version": old.version + 1,
                    },
                    ensure_ascii=False,
                )
            )
            return
        repository.update(memory_id, **changes)
        _enforce_after_write(repository)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Updated {memory_id}")


@app.command()
def delete(
    memory_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Hard delete content and retain a hash-only tombstone event."""
    try:
        repository = _repo(root)
        if dry_run:
            current = repository.get(memory_id)
            typer.echo(
                json.dumps(
                    {
                        "memory_id": memory_id,
                        "action": "hard_delete",
                        "body_bytes": len(current.body.encode()),
                    },
                    ensure_ascii=False,
                )
            )
            return
        repository.delete(memory_id)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Deleted {memory_id}; rollback is unavailable")


@app.command()
def rollback(
    event_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Append a new version that restores a retained prior Markdown state."""
    try:
        repository = _repo(root)
        if dry_run:
            event = next((item for item in repository.events() if item.event_id == event_id), None)
            typer.echo(
                json.dumps(
                    {
                        "event_id": event_id,
                        "available": bool(
                            event
                            and event.before_hash
                            and repository._has_version(event.memory_id, event.before_hash)
                        ),
                    }
                )
            )
            return
        doc = repository.rollback(event_id)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Restored {doc.id} as a new event")


@index_app.command("rebuild")
def index_rebuild(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Recreate FTS5 from current Markdown and cold stubs."""
    repository = _repo(root)
    if dry_run:
        typer.echo(json.dumps({"would_index": len(repository.list()), "writes": False}))
        return
    count = MemoryIndex(repository.index_path).rebuild(repository)
    typer.echo(f"Indexed {count} current memories/stubs")


@index_app.command("search")
def index_search(
    query: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Search current Markdown and cold summary stubs; hits may trigger measured autowarm."""
    repository = _repo(root)
    index = MemoryIndex(repository.index_path)
    if dry_run:
        if not repository.index_path.exists():
            typer.echo(json.dumps({"would_rebuild": True, "writes": False, "results": []}))
            return
        matches = index.search(query)
        typer.echo(json.dumps({"writes": False, "results": matches}, ensure_ascii=False))
        return
    retriever = VaultRetriever(repository, index)
    identifiers = retriever.retrieve(query)
    for memory_id in identifiers:
        try:
            doc = repository.get(memory_id)
            typer.echo(f"{memory_id}\t{doc.title}")
        except MemfwError as exc:
            typer.echo(f"warning: {memory_id}: {exc}", err=True)
    typer.echo(json.dumps(retriever.last_metrics, sort_keys=True))


@app.command()
def fsck(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Check committed state, quarantine invalid Markdown, and register external changes."""
    repository = _repo(root)
    if dry_run:
        typer.echo(json.dumps({"would_check": len(repository.list()), "writes": False}))
        return
    issues = repository.fsck()
    typer.echo(json.dumps(issues, ensure_ascii=False, indent=2))


@app.command()
def recover(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Explicitly recover or clean interrupted file-replacement transactions."""
    repository = _repo(root, allow_pending=True)
    if dry_run:
        typer.echo(json.dumps({"pending": repository.pending_path.exists(), "writes": False}))
        return
    changed = repository.recover_pending()
    typer.echo(json.dumps({"reconciled_memory_ids": changed}, ensure_ascii=False))


@log_app.command("replay")
def log_replay(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """Read-only terminal projection; does not materialize or repair files."""
    typer.echo(json.dumps(_repo(root).replay(), ensure_ascii=False, sort_keys=True, indent=2))


@log_app.command("verify")
def log_verify(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """Verify active payloads and report whether archived payloads or summaries remain."""
    repository = _repo(root)
    events = repository.events()
    _, segments = repository._log_segments()
    summary_count = sum(int(item.get("schema_version", 1)) == 2 for item in segments)
    legacy_count = len(segments) - summary_count
    if not segments:
        verification_level = "full_event_payloads"
    elif summary_count and legacy_count:
        verification_level = "active_payloads_and_mixed_archives"
    elif summary_count:
        verification_level = "active_payloads_and_archived_summaries"
    else:
        verification_level = "active_and_archived_full_event_payloads"
    typer.echo(
        json.dumps(
            {
                "valid": True,
                "event_count": len(events),
                "head": events[-1].event_hash if events else "0" * 64,
                "segment_count": len(segments),
                "archived_event_count": sum(int(segment["event_count"]) for segment in segments),
                "summary_segment_count": summary_count,
                "legacy_payload_segment_count": legacy_count,
                "verification_level": verification_level,
                "original_archived_payloads_available": legacy_count > 0,
                "active_tail_payloads_verified": True,
            }
        )
    )


@log_app.command("compact")
def log_compact(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    keep_recent: Annotated[int | None, typer.Option("--keep-recent", min=1)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Compact old metadata events into deterministic compressed audit segments."""
    repository = _repo(root)
    if dry_run:
        events = repository.events()
        archived, _ = repository._log_segments()
        keep = keep_recent or int(repository.read_config().get("log_keep_recent", 100))
        typer.echo(
            json.dumps(
                {
                    "would_archive_events": max(0, len(events) - len(archived) - keep),
                    "keep_recent": keep,
                    "writes": False,
                },
                sort_keys=True,
            )
        )
        return
    typer.echo(json.dumps(repository.compact_log(keep_recent), sort_keys=True))


@app.command("cold-archive")
def cold_archive(
    archive_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Write a deterministic tar.zst cold archive and replace full files with stubs."""
    repository = _repo(root)
    if dry_run:
        ids = [doc.id for doc in repository.list() if doc.tier == Tier.COLD and not doc.is_stub]
        typer.echo(json.dumps({"archive_id": archive_id, "memory_ids": ids}))
        return
    typer.echo(str(repository.cold_archive(archive_id)))


@app.command("cold-restore")
def cold_restore(
    memory_id: Annotated[str, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    autowarm: Annotated[
        bool,
        typer.Option(
            "--autowarm", help="Restore exact bytes then separately move cold tier to hot"
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Restore exact archived Markdown bytes; optionally append a separate hot-tier change."""
    try:
        repository = _repo(root)
        if dry_run:
            doc = repository.get(memory_id)
            typer.echo(
                json.dumps({"memory_id": memory_id, "is_stub": doc.is_stub, "autowarm": autowarm})
            )
            return
        if autowarm:
            started = time.perf_counter()
            doc = repository.autowarm(memory_id, actor="human")
            elapsed = time.perf_counter() - started
            typer.echo(f"Restored and warmed {doc.id}; cold_restore_seconds={elapsed:.9f}")
        else:
            doc = repository.restore_cold(memory_id)
            typer.echo(f"Restored exact archived Markdown bytes for {doc.id}")
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("record-proposal")
def record_proposal(
    memory_id: Annotated[str, typer.Argument()],
    operation: Annotated[str, typer.Option("--operation")],
    rationale: Annotated[str, typer.Option("--rationale")],
    payload: Annotated[
        str, typer.Option("--payload", help="JSON object with memory_id, operation, changes")
    ],
    agent_id: Annotated[str, typer.Option("--agent-id")],
    signature: Annotated[str, typer.Option("--signature")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Append an agent proposal with a hash bound to its exact payload."""
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise typer.BadParameter("--payload must be valid JSON") from exc
    if parsed.get("memory_id") != memory_id or parsed.get("operation") != operation:
        raise typer.BadParameter("Payload memory_id/operation must match the command options")
    if dry_run:
        typer.echo(
            json.dumps(
                {"valid": True, "memory_id": memory_id, "operation": operation}, ensure_ascii=False
            )
        )
        return
    changes = parsed.get("changes", {})
    event = _repo(root).record_proposal(
        memory_id=memory_id,
        operation=operation,
        changes=changes,
        rationale=rationale,
        agent_id=agent_id,
        signature=signature,
    )
    typer.echo(event.event_id)


@app.command("approve-proposal")
def approve_proposal(
    proposal_id: Annotated[str, typer.Argument()],
    payload: Annotated[
        str, typer.Option("--payload", help="Exact JSON payload approved by a human")
    ],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Record human approval bound to an exact, current proposal payload."""
    try:
        data = json.loads(payload)
        repository = _repo(root)
        if dry_run:
            typer.echo(json.dumps({"proposal_id": proposal_id, "payload_valid": True}))
            return
        result = repository.decide_proposal(proposal_id, payload=data)
    except (ValueError, MemfwError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "approved": proposal_id,
                "memory_id": data.get("memory_id"),
                "deleted": result is None,
            },
            ensure_ascii=False,
        )
    )


@budget_app.command("show")
def budget_show(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """Show body-byte/count limits separately from physical vault bytes."""
    repository = _repo(root)
    config = _config(repository)
    limits = _limits(repository)
    stats = measure_runtime(repository, now=repository._now()).as_dict()
    typer.echo(
        json.dumps(
            {
                "profile": config.get("budget_level", "10MB"),
                "scope": "hot" if config.get("budget_scope", "hot") == "hot" else "all",
                "limits": {"max_bytes": limits.max_bytes, "max_count": limits.max_count},
                "physical_and_logical": stats,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


@budget_app.command("configure")
def budget_configure(
    level: Annotated[BudgetProfile | None, typer.Option("--level")] = None,
    max_count: Annotated[int | None, typer.Option("--max-count")] = None,
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Set the byte profile and optional record count limit."""
    repository = _repo(root)
    config = _config(repository)
    if level is not None:
        config["budget_level"] = level.value
        config.pop("max_bytes", None)
    if max_count is not None:
        if max_count < 0:
            raise typer.BadParameter("--max-count must be >= 0")
        config["max_count"] = max_count
    if dry_run:
        typer.echo(json.dumps(config, ensure_ascii=False, sort_keys=True))
        return
    repository.configure(config)
    typer.echo("Updated budget configuration")


@forget_app.command("run")
def forget_run(
    policy: Annotated[PolicyName, typer.Option("--policy")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    action: Annotated[
        str, typer.Option("--action", help="soft or hard (cold/delete aliases accepted)")
    ] = "soft",
    scope: Annotated[str, typer.Option("--scope", help="hot or all")] = "hot",
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Plan policy-driven forgetting; applying soft or hard action is explicit."""
    repository = _repo(root)
    action = {"cold": "soft", "delete": "hard"}.get(action, action)
    try:
        result = run_forget(
            repository,
            policy,
            _limits(repository),
            now=repository._now(),
            scope=scope,
            action=action,
            dry_run=dry_run,
            actor="human",
        )
    except (IntegrityError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))


@tier_app.command("status")
def tier_status(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """Report hot/cold memory counts and bytes."""
    repository = _repo(root)
    docs = repository.list()
    stats = measure_runtime(repository, now=repository._now()).as_dict()
    typer.echo(
        json.dumps(
            {
                "hot_count": sum(d.tier == Tier.HOT for d in docs),
                "cold_count": sum(d.tier == Tier.COLD for d in docs),
                "hot_body_bytes": stats["hot_body_bytes"],
                "cold_body_bytes": stats["cold_body_bytes"],
                "physical_disk_bytes": stats["physical_disk_bytes"],
            },
            sort_keys=True,
            indent=2,
        )
    )


tier_alias_app.command("status")(tier_status)


@tier_app.command("run")
def tier_run(
    idle_days: Annotated[float, typer.Option("--idle-days")] = 30,
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Cool idle, non-anchor hot memories. Decisions use recorded access timestamps."""
    if idle_days < 0:
        raise typer.BadParameter("--idle-days must be >= 0")
    repository = _repo(root)
    policy = BudgetAwareTierPolicy(idle_days=idle_days)
    plan = policy.plan(repository, now=repository._now(), limits=_limits(repository))
    policy.apply(repository, plan, dry_run=dry_run)
    typer.echo(
        json.dumps(
            {
                "dry_run": dry_run,
                "idle_days": idle_days,
                "cooled_ids": list(plan.cooled_ids),
                "hot_usage_before": plan.hot_usage_before,
                "hot_usage_after": plan.hot_usage_after,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("layer-set")
def layer_set(
    memory_id: Annotated[str, typer.Argument()],
    layer: Annotated[Layer, typer.Argument()],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Move one memory across the current/consolidated/anchor layer axis."""
    repository = _repo(root)
    current = repository.get(memory_id)
    if dry_run:
        typer.echo(
            json.dumps(
                {"memory_id": memory_id, "before": current.layer.value, "after": layer.value}
            )
        )
        return
    repository.update(memory_id, layer=layer)
    typer.echo(f"Moved {memory_id} to {layer.value}")


layer_alias_app.command("set")(layer_set)


@app.command("stats")
def stats(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """Emit JSON physical and logical vault statistics."""
    repository = _repo(root)
    typer.echo(
        json.dumps(
            measure_runtime(repository, now=repository._now()).as_dict(), sort_keys=True, indent=2
        )
    )
