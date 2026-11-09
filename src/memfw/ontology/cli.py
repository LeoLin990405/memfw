"""``memfw concepts`` commands. Dry-run is the default for every write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .model import (
    OntologyError,
    import_seed,
    lint_concepts,
    load_concepts,
    merge_concepts,
    propose_concept,
)

app = typer.Typer(help="Concept anchors and the alias projection", no_args_is_help=True)

Vault = Annotated[Path, typer.Option("--vault", help="Vault directory")]
Apply = Annotated[bool, typer.Option("--apply", help="Write the change")]
Dry = Annotated[bool, typer.Option("--dry-run", help="Show the change and write nothing")]


def _writes(apply: bool, dry_run: bool) -> bool:
    if apply and dry_run:
        typer.echo("pass only one of --apply and --dry-run", err=True)
        raise typer.Exit(code=2)
    return apply


def _dump(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _fail(exc: OntologyError) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc


@app.command("list")
def list_concepts(vault: Vault) -> None:
    """List concept anchors and the lint review list."""
    concepts = load_concepts(vault)
    rows = [
        {
            "class": concept.class_name,
            "merged_into": concept.merged_into,
            "oid": concept.oid,
            "pref_label": concept.pref_label,
            "status": concept.status,
        }
        for concept in concepts.values()
    ]
    _dump({"concepts": rows, "review": lint_concepts(concepts)})


@app.command("show")
def show_concept(oid: Annotated[str, typer.Argument()], vault: Vault) -> None:
    """Show one concept anchor."""
    concept = load_concepts(vault).get(oid)
    if concept is None:
        typer.echo(f"unknown concept {oid}", err=True)
        raise typer.Exit(code=1)
    _dump(
        {
            "alt_labels": [label.text for label in concept.alt_labels],
            "class": concept.class_name,
            "merged_into": concept.merged_into,
            "oid": concept.oid,
            "pref_label": concept.pref_label,
            "same_as": list(concept.same_as),
            "status": concept.status,
        }
    )


@app.command("propose")
def propose(
    vault: Vault,
    oid: Annotated[str, typer.Option("--oid")],
    class_name: Annotated[str, typer.Option("--class")],
    pref_label: Annotated[str, typer.Option("--pref-label")],
    alt_label: Annotated[list[str] | None, typer.Option("--alt-label")] = None,
    definition: Annotated[str, typer.Option("--definition")] = "",
    apply: Apply = False,
    dry_run: Dry = False,
) -> None:
    """Propose a candidate concept. Nothing is written unless ``--apply`` is set."""
    try:
        result = propose_concept(
            vault,
            oid=oid,
            class_name=class_name,
            pref_label=pref_label,
            alt_labels=tuple(alt_label or ()),
            definition=definition,
            apply=_writes(apply, dry_run),
        )
    except OntologyError as exc:
        _fail(exc)
    _dump(result)


@app.command("merge")
def merge(
    vault: Vault,
    source: Annotated[str, typer.Argument()],
    target: Annotated[str, typer.Argument()],
    apply: Apply = False,
    dry_run: Dry = False,
) -> None:
    """Point one concept at another. The source file stays on disk."""
    try:
        result = merge_concepts(vault, source, target, apply=_writes(apply, dry_run))
    except OntologyError as exc:
        _fail(exc)
    _dump(result)


@app.command("import-seed")
def import_seed_command(
    seed: Annotated[Path, typer.Option("--seed", help="Approved seed YAML")],
    vault: Annotated[Path | None, typer.Option("--vault")] = None,
    apply: Apply = False,
    dry_run: Dry = False,
) -> None:
    """Import a seed into a temporary vault. A live vault is refused."""
    try:
        result = import_seed(seed, vault, apply=_writes(apply, dry_run))
    except OntologyError as exc:
        _fail(exc)
    _dump(result)
