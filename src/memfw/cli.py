"""Typer command-line interface for deterministic local memory operations."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from . import __version__
from .attachments.cli import app as attach_app
from .budget import BYTE_BUDGETS, BudgetLimits, BudgetProfile
from .capture.cli import app as capture_app
from .capture.confirm import confirm_command
from .capture.review import Candidate, list_staged_candidates
from .code_retrieval import CodeIndex, index_code
from .conversations.cli import app as conversations_app
from .conversations.policy import OPERATOR, SharePolicy
from .core import invocation
from .core.epistemic import (
    OPEN_QUESTION_TYPE,
    validate_epistemic_write,
    validate_open_question_references,
)
from .core.errors import IntegrityError, MemfwError, NotFoundError
from .core.evidence_refs import is_attachment_reference
from .core.local_data import vault_identity
from .core.models import Layer, Tier, foreign_event_kind_counts
from .core.repository import Repository, open_vault
from .corroboration import (
    CorroborationError,
    OtherVaultError,
    corroboration_for,
    legacy_path,
    projection_path,
)
from .corroboration import check_projection as check_corroboration
from .corroboration import rebuild as rebuild_corroboration
from .curation.engine import enforce_budget, plan_forget, run_forget
from .curation.review_cli import review_command as run_review
from .dream.cli import app as dream_app
from .experience.cli import experience_app
from .handoff.cli import handoff as handoff_command
from .history.cli import app as history_app
from .index import MemoryIndex
from .notes import (
    FeedbackStore,
    GoldenAnnotator,
    HttpEmbeddingProvider,
    HttpRerankerProvider,
    NotesIndex,
    NotesRetriever,
    RetrievalConfig,
    build_manifest,
    check_pairs,
    compare_indexes,
    create_batch,
    evaluate_retriever,
    export_pairs,
    import_batch,
    is_finish,
    load_batch_queries,
    load_cases,
    normalize_kind,
    path_search_term,
    path_selection,
    read_manifest,
    verify_manifest,
    write_batch,
    write_manifest,
    write_pairs,
)
from .notes.mcp import NotesGateway
from .notes.mcp import serve as serve_notes_mcp
from .notes.retrieval import Mode
from .okf import export_okf, validate_okf_bundle
from .ontology.cli import app as concepts_app
from .policies import PolicyName
from .ports.privacy import (
    LEGACY_PRIVACY_KEY,
    NEW_PRIVACY_KEY,
    clearance_is_valid,
    privacy_value,
)
from .projection import ProjectionError, export_projection, load_policy, read_hits
from .projection import resolve as resolve_hits
from .projections import ObsidianProjector
from .related import RelatedIndex
from .retrieval import VaultRetriever
from .stats import measure_runtime
from .storage.evidence import gate_evidence_classes
from .storage.operations import lookup_current
from .tiers import BudgetAwareTierPolicy

app = typer.Typer(help="Local-first Markdown memory vault", no_args_is_help=True)
index_app = typer.Typer(help="Manage the disposable trigram FTS5 index")
log_app = typer.Typer(help="Inspect the hash-chained append-only event log")
budget_app = typer.Typer(help="Inspect and enforce deterministic memory budgets")
forget_app = typer.Typer(help="Plan and apply policy-driven forgetting")
tier_app = typer.Typer(help="Inspect and move hot/cold memory tiers")
tier_alias_app = typer.Typer(help="Tier placement commands")
layer_alias_app = typer.Typer(help="Layer classification commands")
okf_app = typer.Typer(help="Export and validate an OKF v0.2 knowledge bundle")
privacy_app = typer.Typer(help="Privacy levels of memories and the vault's legacy default")
notes_app = typer.Typer(help="Index and search a Markdown notes tree (derived, rebuildable)")
code_app = typer.Typer(help="Local code retrieval: lexical + optional semantic + structure")
eval_app = typer.Typer(help="Local evaluation harnesses (no network, no model calls)")
projection_app = typer.Typer(
    help="Read-only recall projection: export files for an external recall backend"
)
corroboration_app = typer.Typer(
    help="Corroboration projection: which apps/sessions independently mention a memory"
)
project_app = typer.Typer(help="Read-only human-facing projections of the vault (rebuildable)")
graph_app = typer.Typer(
    help="Typed-edge projection (K3) and bounded path patterns over it (K4); read only"
)
app.add_typer(index_app, name="index")
app.add_typer(corroboration_app, name="corroboration")
app.add_typer(okf_app, name="okf")
app.add_typer(privacy_app, name="privacy")
app.add_typer(notes_app, name="notes")
app.add_typer(code_app, name="code")
app.add_typer(log_app, name="log")
app.add_typer(budget_app, name="budget")
app.add_typer(forget_app, name="forget")
app.add_typer(tier_app, name="tiers")
app.add_typer(tier_alias_app, name="tier")
app.add_typer(layer_alias_app, name="layer")
app.add_typer(capture_app, name="capture")
app.add_typer(concepts_app, name="concepts")
app.add_typer(history_app, name="history")
app.add_typer(dream_app, name="dream")
app.add_typer(experience_app, name="experience")
app.add_typer(eval_app, name="eval")
app.add_typer(projection_app, name="projection")
app.add_typer(conversations_app, name="conversations")
app.add_typer(project_app, name="project")
app.add_typer(attach_app, name="attach")
app.add_typer(graph_app, name="graph")
app.command("handoff")(handoff_command)
capture_app.command(name="confirm")(confirm_command)


def _notes_embedder(
    endpoint: str | None,
    model: str,
    dimension: int,
    batch_size: int,
) -> HttpEmbeddingProvider | None:
    if not endpoint:
        return None
    return HttpEmbeddingProvider(
        endpoint,
        model=model,
        dimension=dimension,
        batch_size=batch_size,
    )


def _semantic_admit(
    embedder: Any,
    allow_remote: bool,
    *,
    repository: Repository | None = None,
    notes_db: Path | None = None,
) -> Callable[[str, str, str], bool] | None:
    """The ``build_semantic`` gate: ``None`` (embed all) unless the embedder is off-host.

    Off-host and not opted in (``--allow-remote`` / ``MEMFW_EMBEDDING_ALLOW_REMOTE``):
    only a ``public``/``shared`` memory (read from ``repository``) or note
    (its indexed frontmatter in ``notes_db``) is embedded; anything else,
    including an item whose privacy cannot be read, is not.
    """

    from .notes.http import embedder_sends_off_host
    from .notes.index import remote_embedding_allowed
    from .related import shareable_item

    if allow_remote or remote_embedding_allowed() or not embedder_sends_off_host(embedder):
        return None
    return shareable_item(repository=repository, notes_db=notes_db)


@notes_app.command("index")
def notes_index(
    root: Annotated[Path, typer.Option("--root", "-r", help="Markdown notes root")] = Path("."),
    db: Annotated[Path, typer.Option("--db", help="Derived SQLite index path")] = Path(
        ".memfw-notes.sqlite"
    ),
    endpoint: Annotated[
        str | None, typer.Option("--endpoint", help="OpenAI-compatible embeddings URL")
    ] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    batch_size: Annotated[int, typer.Option("--batch-size")] = 16,
    max_files: Annotated[int | None, typer.Option("--max-files")] = None,
    commit_every: Annotated[int, typer.Option("--commit-every")] = 100,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help="Let a non-loopback embedder receive local-private text and queries "
            "(also MEMFW_EMBEDDING_ALLOW_REMOTE=1)",
        ),
    ] = False,
) -> None:
    """Build or incrementally update a section-level notes index.

    A non-loopback ``--endpoint`` embeds only public/shared notes unless
    ``--allow-remote`` (or ``MEMFW_EMBEDDING_ALLOW_REMOTE=1``) is given.
    """

    index = NotesIndex(
        db,
        embedder=_notes_embedder(endpoint, model, dimension, batch_size),
        allow_remote=True if allow_remote else None,
    )
    counts = index.index(root, max_files=max_files, commit_every=commit_every)
    typer.echo(
        json.dumps({"counts": counts, "status": index.status()}, ensure_ascii=False, indent=2)
    )


@notes_app.command("search")
def notes_search(
    query: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="Derived SQLite index path")] = Path(
        ".memfw-notes.sqlite"
    ),
    endpoint: Annotated[
        str | None, typer.Option("--endpoint", help="OpenAI-compatible embeddings URL")
    ] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    path_prefix: Annotated[str | None, typer.Option("--path-prefix")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    course: Annotated[str | None, typer.Option("--course")] = None,
    mode: Annotated[str, typer.Option("--mode", help="fts, vector or hybrid")] = "hybrid",
    candidates: Annotated[int, typer.Option("--candidates")] = 50,
    rrf_k: Annotated[int, typer.Option("--rrf-k")] = 60,
    rerank_endpoint: Annotated[
        str | None, typer.Option("--rerank-endpoint", help="llama.cpp /v1/rerank URL")
    ] = None,
    rerank_model: Annotated[str, typer.Option("--rerank-model")] = "reranker",
    rerank_candidates: Annotated[int, typer.Option("--rerank-candidates")] = 0,
    rerank_timeout: Annotated[float, typer.Option("--rerank-timeout")] = 60.0,
    meta: Annotated[
        list[str] | None, typer.Option("--meta", help="key=value metadata filter")
    ] = None,
    since: Annotated[
        float | None, typer.Option("--since", help="mtime lower bound, epoch seconds")
    ] = None,
    until: Annotated[
        float | None, typer.Option("--until", help="mtime upper bound, epoch seconds")
    ] = None,
    min_query_coverage: Annotated[float | None, typer.Option("--min-query-coverage")] = None,
    feedback_db: Annotated[Path | None, typer.Option("--feedback-db")] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    tenant: Annotated[str, typer.Option("--tenant")] = "",
    source: Annotated[str, typer.Option("--source-name")] = "",
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help="Let a non-loopback embedder receive local-private text and queries "
            "(also MEMFW_EMBEDDING_ALLOW_REMOTE=1)",
        ),
    ] = False,
) -> None:
    """Search the notes index; automatically degrades to FTS without an embedder.

    A non-loopback ``--endpoint`` never receives the query unless
    ``--allow-remote`` (or ``MEMFW_EMBEDDING_ALLOW_REMOTE=1``): the search is
    FTS, with ``semantic_query_withheld`` in ``errors``.
    """

    index = NotesIndex(
        db,
        embedder=_notes_embedder(endpoint, model, dimension, 16),
        allow_remote=True if allow_remote else None,
    )
    reranker = None
    if rerank_endpoint:
        reranker = HttpRerankerProvider(
            rerank_endpoint,
            model=rerank_model,
            timeout=rerank_timeout,
        )
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(
            mode=mode,  # type: ignore[arg-type]
            candidates=candidates,
            final_k=limit,
            rrf_k=rrf_k,
            rerank_candidates=rerank_candidates,
            min_query_coverage=min_query_coverage,
        ),
        reranker=reranker,
        trace_store=FeedbackStore(feedback_db) if feedback_db else None,
    )
    metadata: dict[str, str] = {}
    for item in meta or []:
        if "=" not in item:
            raise typer.BadParameter("--meta must be key=value")
        key, value = item.split("=", 1)
        if not key:
            raise typer.BadParameter("--meta key must not be empty")
        metadata[key] = value
    result = retriever.search(
        query,
        limit=limit,
        path_prefix=path_prefix,
        tags=tag or (),
        course=course,
        metadata=metadata or None,
        since=since,
        until=until,
        task_id=task_id,
        tenant=tenant,
        source=source,
    )
    typer.echo(
        json.dumps(
            {
                "mode": result.mode_used,
                "degraded": result.degraded,
                "rewrites": result.rewrites,
                "errors": result.errors,
                "trace_id": result.trace_id,
                "task_id": result.task_id,
                "abstained": result.abstained,
                "abstain_reason": result.abstain_reason,
                "results": [hit.as_dict() for hit in result.hits],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@notes_app.command("serve")
def notes_serve(
    root: Annotated[Path, typer.Option("--root", "-r", help="Markdown notes root")] = Path("."),
    db: Annotated[Path, typer.Option("--db", help="Derived SQLite index path")] = Path(
        ".memfw-notes.sqlite"
    ),
    endpoint: Annotated[
        str | None, typer.Option("--endpoint", help="OpenAI-compatible embeddings URL")
    ] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    related_db: Annotated[Path | None, typer.Option("--related-db")] = None,
    clearance: Annotated[
        str | None,
        typer.Option(
            "--clearance",
            envvar="MEMFW_NOTES_CLEARANCE",
            help="Privacy levels to return, comma separated (public, shared, "
            "local-private). Unset: no privacy filtering",
        ),
    ] = None,
    legacy_privacy_default: Annotated[
        str,
        typer.Option("--legacy-privacy-default", help="Level of notes without a privacy field"),
    ] = "local-private",
) -> None:
    """Serve read-only notes tools over stdio MCP."""

    if not clearance_is_valid(clearance):
        raise typer.BadParameter("--clearance must list only public, shared, local-private")
    embedder = _notes_embedder(endpoint, model, dimension, 16)
    serve_notes_mcp(
        NotesGateway(
            root,
            db,
            embedder=embedder,
            related_db=related_db,
            clearance=clearance,
            legacy_privacy_default=legacy_privacy_default,
        )
    )


@notes_app.command("annotate-batch")
def notes_annotate_batch(
    queries: Annotated[Path, typer.Option("--queries", help="query JSONL/JSON/YAML")],
    db: Annotated[Path, typer.Option("--db")],
    out: Annotated[Path, typer.Option("--out", help="CSV annotation packet")],
    markdown: Annotated[Path | None, typer.Option("--markdown")] = None,
    meta: Annotated[Path | None, typer.Option("--meta")] = None,
    annotator: Annotated[str, typer.Option("--annotator")] = "",
    candidates: Annotated[int, typer.Option("--candidates")] = 8,
    mode: Annotated[str, typer.Option("--mode")] = "fts",
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
) -> None:
    """Generate a batch annotation packet for manual review.

    The CSV is the source of truth. Fill relevant=1 and answerable=0/1, then run
    `memfw notes annotate-import`.
    """
    parsed = load_batch_queries(queries)
    index = NotesIndex(db, embedder=_notes_embedder(endpoint, model, dimension, 16))
    rows = create_batch(index, parsed, candidates=candidates, mode=mode)
    info = write_batch(
        rows,
        csv_path=out,
        md_path=markdown,
        meta_path=meta,
        annotator=annotator,
    )
    typer.echo(json.dumps(info, ensure_ascii=False, indent=2))


@notes_app.command("annotate-import")
def notes_annotate_import(
    csv_path: Annotated[Path, typer.Option("--csv")],
    db: Annotated[Path, typer.Option("--db")],
    out: Annotated[Path, typer.Option("--out")],
    annotator: Annotated[str, typer.Option("--annotator")] = "",
) -> None:
    """Import a filled batch CSV into golden YAML with path validation."""
    import os

    entries = import_batch(
        csv_path,
        db=db,
        out=out,
        annotator=annotator or os.environ.get("USER") or "human",
    )
    typer.echo(json.dumps({"out": str(out), "entries": len(entries)}, ensure_ascii=False))


@notes_app.command("annotate")
def notes_annotate(
    db: Annotated[Path, typer.Option("--db", help="notes index used to validate paths")],
    out: Annotated[Path, typer.Option("--out")] = Path("golden-human.yml"),
    annotator: Annotated[str, typer.Option("--annotator")] = "",
) -> None:
    """Record one golden query at a time; OS dictation can fill each prompt.

    macOS: focus this terminal and press fn-fn before speaking. The tool only
    receives text; it never records or uploads audio.
    """
    import os

    owner = annotator or os.environ.get("USER") or "human"
    try:
        session = GoldenAnnotator(db, out, annotator=owner)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"golden file: {out} (existing entries: {len(session.entries)})")
    typer.echo("Voice: use OS dictation; each prompt remains editable before save.")
    typer.echo("Type ?keyword at a path prompt to search note paths.")
    while True:
        query = typer.prompt("Query (empty or 结束 to finish)", default="", show_default=False)
        if not query.strip() or is_finish(query):
            break
        raw_kind = typer.prompt("Kind", default="answerable")
        try:
            kind = normalize_kind(raw_kind)
        except ValueError as exc:
            typer.echo(f"not saved: {exc}")
            continue
        paths: list[str] = []
        hints: list[str] = []
        if kind != "no_answer":
            while True:
                raw = typer.prompt(
                    "Expected path (empty=done, 搜索 关键词=find, 选择 N=pick)",
                    default="",
                    show_default=False,
                ).strip()
                if not raw or is_finish(raw):
                    break
                term = path_search_term(raw)
                if term is not None:
                    hints = session.suggest_paths(term)
                    if not hints:
                        typer.echo("no matching note paths")
                        continue
                    for index, hint in enumerate(hints, start=1):
                        typer.echo(f"  {index}. {hint}")
                    typer.echo("say 选择 1 or 选择 2 ...")
                    continue
                selected = path_selection(raw)
                if selected is not None:
                    if 1 <= selected <= len(hints):
                        paths.append(hints[selected - 1])
                        typer.echo(f"added {hints[selected - 1]}")
                    else:
                        typer.echo("no such suggestion; use 搜索 关键词 first")
                    continue
                paths.append(raw)
        evidence = typer.prompt("Evidence/notes (optional)", default="", show_default=False)
        try:
            entry = session.add(
                query,
                paths,
                kind=kind,
                notes=evidence,
            )
        except ValueError as exc:
            typer.echo(f"not saved: {exc}")
            continue
        typer.echo(f"saved {entry.query_id}: {entry.query}")
    typer.echo(f"wrote {len(session.entries)} entries to {out}")


@notes_app.command("eval")
def notes_eval(
    queries: Annotated[Path, typer.Option("--queries", help="golden JSONL/JSON/YAML")],
    db: Annotated[Path, typer.Option("--db")] = Path(".memfw-notes.sqlite"),
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    mode: Annotated[str, typer.Option("--mode")] = "hybrid",
    candidates: Annotated[int, typer.Option("--candidates")] = 50,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Evaluate a golden query file against one notes index."""
    cases = load_cases(queries)
    index = NotesIndex(db, embedder=_notes_embedder(endpoint, model, dimension, 16))
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(
            mode=mode,  # type: ignore[arg-type]
            candidates=candidates,
            final_k=limit,
        ),
    )
    report = evaluate_retriever(retriever, cases, name=mode, limit=limit)
    typer.echo(json.dumps(report.as_dict(limit), ensure_ascii=False, indent=2))


@notes_app.command("shadow")
def notes_shadow(
    queries: Annotated[Path, typer.Option("--queries", help="golden JSONL/JSON/YAML")],
    baseline_db: Annotated[Path, typer.Option("--baseline-db")],
    candidate_db: Annotated[Path, typer.Option("--candidate-db")],
    endpoint: Annotated[str | None, typer.Option("--endpoint")] = None,
    model: Annotated[str, typer.Option("--model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    mode: Annotated[str, typer.Option("--mode")] = "hybrid",
    candidates: Annotated[int, typer.Option("--candidates")] = 50,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Compare two index generations read-only; never switches production."""
    cases = load_cases(queries)
    baseline = NotesIndex(baseline_db, embedder=_notes_embedder(endpoint, model, dimension, 16))
    candidate = NotesIndex(candidate_db, embedder=_notes_embedder(endpoint, model, dimension, 16))
    from typing import cast

    report = compare_indexes(
        baseline,
        candidate,
        cases,
        mode=cast(Mode, mode),
        candidates=candidates,
        limit=limit,
    )
    typer.echo(json.dumps(report.as_dict(limit), ensure_ascii=False, indent=2))


@notes_app.command("feedback-export")
def notes_feedback_export(
    feedback_db: Annotated[Path, typer.Option("--db")],
    out: Annotated[Path, typer.Option("--out")],
    min_confidence: Annotated[float, typer.Option("--min-confidence")] = 0.0,
) -> None:
    """Export explicit feedback pairs; never invents labels or trains."""
    report = export_pairs(FeedbackStore(feedback_db), min_confidence=min_confidence)
    write_pairs(report, out)
    typer.echo(
        json.dumps(
            {
                "out": str(out),
                "pairs": len(report.pairs),
                "skipped_unknown": report.skipped_unknown,
                "skipped_missing": report.skipped_missing,
                "skipped_duplicate": report.skipped_duplicate,
                "quality": check_pairs(report.pairs),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@notes_app.command("manifest-write")
def notes_manifest_write(
    db: Annotated[Path, typer.Option("--db")],
    out: Annotated[Path, typer.Option("--out")],
) -> None:
    """Write an index manifest with model/chunker/count identity."""
    write_manifest(out, build_manifest(NotesIndex(db)))
    typer.echo(json.dumps({"manifest": str(out)}, ensure_ascii=False))


@notes_app.command("manifest-verify")
def notes_manifest_verify(
    db: Annotated[Path, typer.Option("--db")],
    manifest: Annotated[Path, typer.Option("--manifest")],
) -> None:
    """Verify model/chunker/manifest compatibility without switching anything."""
    verify_manifest(NotesIndex(db), read_manifest(manifest))
    typer.echo(json.dumps({"ok": True}, ensure_ascii=False))


@notes_app.command("related-build")
def notes_related_build(
    notes_db: Annotated[Path, typer.Option("--notes-db")],
    related_db: Annotated[Path, typer.Option("--related-db")] = Path(".memfw-related.sqlite"),
    endpoint: Annotated[str | None, typer.Option("--embedding-url")] = None,
    model: Annotated[str, typer.Option("--embedding-model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help="Let a non-loopback embedder receive local-private text and queries "
            "(also MEMFW_EMBEDDING_ALLOW_REMOTE=1)",
        ),
    ] = False,
) -> None:
    """Rebuild note-to-note relatedness edges, optionally with semantic edges.

    A non-loopback ``--embedding-url`` embeds only public/shared notes unless
    ``--allow-remote`` (or ``MEMFW_EMBEDDING_ALLOW_REMOTE=1``).
    """
    index = RelatedIndex(related_db)
    count = index.rebuild_notes(notes_db)
    semantic = 0
    if endpoint:
        embedder = HttpEmbeddingProvider(endpoint, model=model, dimension=dimension)
        semantic = index.build_semantic(
            embedder, admit=_semantic_admit(embedder, allow_remote, notes_db=notes_db)
        )
    typer.echo(
        json.dumps(
            {"related_db": str(related_db), "edges": count, "semantic_edges": semantic},
            ensure_ascii=False,
        )
    )


@notes_app.command("related-sections-build")
def notes_related_sections_build(
    notes_db: Annotated[Path, typer.Option("--notes-db")],
    related_db: Annotated[Path, typer.Option("--related-db")] = Path(".memfw-related.sqlite"),
    max_sections: Annotated[int, typer.Option("--max-sections")] = 2000,
) -> None:
    """Build bounded section-level relatedness edges."""
    count = RelatedIndex(related_db).rebuild_sections(notes_db, max_sections=max(1, max_sections))
    typer.echo(json.dumps({"related_db": str(related_db), "edges": count}, ensure_ascii=False))


@notes_app.command("related")
def notes_related(
    path: Annotated[str, typer.Argument()],
    related_db: Annotated[Path, typer.Option("--related-db")] = Path(".memfw-related.sqlite"),
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Read related notes from the derived edge projection."""
    typer.echo(
        json.dumps(
            {"path": path, "related": RelatedIndex(related_db).related("notes", path, limit=limit)},
            ensure_ascii=False,
            indent=2,
        )
    )


@notes_app.command("status")
def notes_status(
    db: Annotated[Path, typer.Option("--db", help="Derived SQLite index path")] = Path(
        ".memfw-notes.sqlite"
    ),
) -> None:
    """Show notes index counts, tokenizer and vector state."""
    typer.echo(json.dumps(NotesIndex(db).status(), ensure_ascii=False, indent=2))


@code_app.command("index")
def code_index(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    db: Annotated[Path, typer.Option("--db")] = Path(".memfw-code.sqlite"),
    endpoint: Annotated[str | None, typer.Option("--embedding-url")] = None,
    model: Annotated[str, typer.Option("--embedding-model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
) -> None:
    """Build a disposable code index from Python symbols/files."""
    embedder = _notes_embedder(endpoint, model, dimension, 16)
    typer.echo(json.dumps(index_code(root, db, embedder=embedder), ensure_ascii=False))


@code_app.command("search")
def code_search(
    query: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db")] = Path(".memfw-code.sqlite"),
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    endpoint: Annotated[str | None, typer.Option("--embedding-url")] = None,
    model: Annotated[str, typer.Option("--embedding-model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
) -> None:
    """Search code by exact symbol, natural language, or structural question."""
    embedder = _notes_embedder(endpoint, model, dimension, 16)
    results = CodeIndex(db, embedder=embedder).search(query, limit=limit)
    typer.echo(json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2))


@code_app.command("map")
def code_map(
    db: Annotated[Path, typer.Option("--db")] = Path(".memfw-code.sqlite"),
) -> None:
    """Emit entry files, directory summary, and recent files."""
    typer.echo(json.dumps(CodeIndex(db).repo_map(), ensure_ascii=False, indent=2))


def _reject_missing_proposal_id(details: dict[str, object]) -> None:
    """Refuse a candidate that cannot be safely recovered after a retry.

    A legacy candidate without a proposal_id has no identity the recovery path
    can match on, so a failed first write followed by a retry would write a
    second, duplicate memory. Refusing before the first write is the only way
    to keep that from happening, and re-staging through ``/stage`` is how an
    operator gets a proposal_id onto the candidate.
    """
    typer.echo(
        json.dumps(
            {
                **details,
                "error": "missing_proposal_id",
                "status": "missing_proposal_id",
                "message": (
                    "this candidate has no proposal_id, so a retry could not find "
                    "the memory it already wrote; re-stage it with /stage to get one"
                ),
            },
            ensure_ascii=False,
        )
    )
    raise typer.Exit(code=1)


def _candidate_error(details: dict[str, object], error: str, message: str) -> NoReturn:
    """Print a candidate-apply refusal as JSON and exit 1, like the other refusals."""
    typer.echo(
        json.dumps(
            {**details, "error": error, "status": error, "message": message},
            ensure_ascii=False,
        )
    )
    raise typer.Exit(code=1)


def _read_staged_candidate(candidate: Path) -> Candidate:
    from .capture.lifecycle import read_candidate
    from .capture.review import InvalidProposalIdError

    try:
        return read_candidate(candidate)
    except InvalidProposalIdError as exc:
        _candidate_error(
            {"candidate": str(candidate)},
            "invalid_proposal_id",
            f"{exc}; proposal_id must be a non-blank string, re-stage the candidate with /stage",
        )
    except (MemfwError, OSError) as exc:
        _candidate_error({"candidate": str(candidate)}, "invalid_candidate", str(exc))


def _require_proposal_id(staged: Candidate, details: dict[str, object]) -> None:
    """Refuse a candidate without a usable proposal_id.

    ``read_candidate`` already turns ``""``/whitespace into ``None`` and refuses
    a non-string; the strip here keeps a hand-built :class:`Candidate` honest.
    """
    if staged.proposal_id is None or not staged.proposal_id.strip():
        _reject_missing_proposal_id(details)


def _require_related(repository: Repository, staged: Candidate, details: dict[str, object]) -> None:
    for related_id in staged.related:
        try:
            repository.get(related_id)
        except MemfwError as exc:
            _candidate_error(details, "related_not_found", str(exc))


@app.command("candidate-apply")
def candidate_apply(
    candidate: Annotated[Path, typer.Argument(help="staged candidate Markdown")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Human-confirm a staged candidate and write it to the authoritative vault."""
    from .capture.lifecycle import (
        candidate_dict,
        candidate_provenance,
        find_applied_memory_id,
        mark_candidate_applied,
        validate_candidate_hash,
    )

    repository = _repo(root, allow_pending=not dry_run)
    if dry_run:
        staged = _read_staged_candidate(candidate)
        details = candidate_dict(staged)
        if staged.content_hash is None or staged.content_hash == "":
            typer.echo(
                json.dumps(
                    {
                        **details,
                        "error": "missing_content_hash",
                        "status": "missing_content_hash",
                    },
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1)
        _require_proposal_id(staged, details)
        if staged.content_hash:
            try:
                validate_candidate_hash(staged)
            except IntegrityError as exc:
                typer.echo(
                    json.dumps(
                        {
                            **details,
                            "error": "hash_mismatch",
                            "status": "hash_mismatch",
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                    )
                )
                raise typer.Exit(code=1) from exc
        _require_related(repository, staged, details)
        typer.echo(
            json.dumps(
                {**details, "dry_run": True, "writes": False, "authority": False},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    with repository._locked():
        if repository.pending_path.exists():
            raise typer.BadParameter("Vault has a pending transaction; run `memfw recover` first")
        staged = _read_staged_candidate(candidate)
        details = candidate_dict(staged)
        if staged.content_hash is None or staged.content_hash == "":
            typer.echo(
                json.dumps(
                    {
                        **details,
                        "error": "missing_content_hash",
                        "status": "missing_content_hash",
                    },
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1)
        _require_proposal_id(staged, details)
        if staged.status == "applied" or staged.applied_memory_id is not None:
            memory_id = staged.applied_memory_id
            typer.echo(
                json.dumps(
                    {
                        **details,
                        "error": "already_applied",
                        "status": "already_applied",
                        "memory_id": memory_id,
                        "applied_memory_id": memory_id,
                    },
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1)

        if staged.content_hash:
            try:
                validate_candidate_hash(staged)
            except IntegrityError as exc:
                typer.echo(
                    json.dumps(
                        {
                            **details,
                            "error": "hash_mismatch",
                            "status": "hash_mismatch",
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                    )
                )
                raise typer.Exit(code=1) from exc

        recovered_id = find_applied_memory_id(repository, staged)
        if recovered_id is not None:
            try:
                mark_candidate_applied(staged, recovered_id)
            except Exception as exc:
                typer.echo(
                    json.dumps(
                        {
                            **details,
                            "error": "mark_failed",
                            "status": "error",
                            "message": str(exc),
                            "memory_id": recovered_id,
                            "applied_memory_id": recovered_id,
                        },
                        ensure_ascii=False,
                    )
                )
                raise typer.Exit(code=1) from exc
            typer.echo(
                json.dumps(
                    {
                        **details,
                        "error": "already_applied",
                        "status": "already_applied",
                        "memory_id": recovered_id,
                        "applied_memory_id": recovered_id,
                    },
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1)

        _require_related(repository, staged, details)

        document = repository.add(
            staged.title,
            staged.body,
            tags=staged.tags,
            tenant=staged.tenant,
            related=staged.related,
            source=staged.source,
            provenance=candidate_provenance(staged),
            actor="human",
            privacy=staged.privacy,
        )
        try:
            mark_candidate_applied(staged, document.id)
        except Exception as exc:
            typer.echo(
                json.dumps(
                    {
                        **details,
                        "error": "mark_failed",
                        "status": "error",
                        "message": str(exc),
                        "memory_id": document.id,
                        "applied_memory_id": document.id,
                    },
                    ensure_ascii=False,
                )
            )
            raise typer.Exit(code=1) from exc
        typer.echo(
            json.dumps(
                {
                    "memory_id": document.id,
                    "tags": document.tags,
                    "proposal_id": staged.proposal_id,
                    "source": staged.source,
                    "candidate_id": staged.candidate_id,
                    "status": "applied",
                    "applied_memory_id": document.id,
                },
                ensure_ascii=False,
            )
        )


@app.command("candidates")
def candidates_list(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    include_expired: Annotated[
        bool,
        typer.Option("--include-expired", help="also list candidates past the 30-day TTL"),
    ] = False,
) -> None:
    """Show staged candidates with tags, related ids, and duplicate/conflict suggestions.

    Candidates not applied within 30 days of ``recorded_at`` are hidden.
    """
    skipped: list[str] = []
    candidates = list_staged_candidates(root, include_expired=include_expired, skipped=skipped)
    typer.echo(
        json.dumps(
            {"candidates": candidates, "skipped": skipped},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("candidates-expire")
def candidates_expire(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Mark staged candidates past the 30-day TTL as expired. Never deletes a file."""
    from .capture.lifecycle import mark_expired_candidates

    typer.echo(
        json.dumps(mark_expired_candidates(root, dry_run=dry_run), ensure_ascii=False, indent=2)
    )


@app.command("memory-related-build")
def memory_related_build(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    related_db: Annotated[Path, typer.Option("--related-db")] = Path(".memfw-related.sqlite"),
    endpoint: Annotated[str | None, typer.Option("--embedding-url")] = None,
    model: Annotated[str, typer.Option("--embedding-model")] = "embedding-model",
    dimension: Annotated[int, typer.Option("--dimension")] = 1024,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help="Let a non-loopback embedder receive local-private text and queries "
            "(also MEMFW_EMBEDDING_ALLOW_REMOTE=1)",
        ),
    ] = False,
) -> None:
    """Rebuild memory-to-memory relatedness edges, optionally with semantics.

    A non-loopback ``--embedding-url`` embeds only public/shared memories
    unless ``--allow-remote`` (or ``MEMFW_EMBEDDING_ALLOW_REMOTE=1``).
    """
    repository = _repo(root)
    index = RelatedIndex(related_db)
    count = index.rebuild_memory(repository)
    semantic = 0
    if endpoint:
        embedder = HttpEmbeddingProvider(endpoint, model=model, dimension=dimension)
        semantic = index.build_semantic(
            embedder, admit=_semantic_admit(embedder, allow_remote, repository=repository)
        )
    typer.echo(
        json.dumps(
            {"related_db": str(related_db), "edges": count, "semantic_edges": semantic},
            ensure_ascii=False,
        )
    )


@app.command("memory-related")
def memory_related(
    memory_id: Annotated[str, typer.Argument()],
    related_db: Annotated[Path, typer.Option("--related-db")] = Path(".memfw-related.sqlite"),
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Read related current memories from the derived edge projection."""
    typer.echo(
        json.dumps(
            {
                "memory_id": memory_id,
                "related": RelatedIndex(related_db).related("memory", memory_id, limit=limit),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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


def _json_option(raw: str | None, option: str) -> Any:
    """Parse an inline JSON object or a path to a JSON file for a CLI option."""

    if raw is None:
        return None
    text = raw
    if not raw.lstrip().startswith("{"):
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must be a JSON object or a JSON file") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{option} must be a JSON object")
    return parsed


def _open_question_block(
    current: dict[str, Any] | None,
    question: str | None,
    conflicts: list[str] | None,
    missing: str | None,
) -> dict[str, Any] | None:
    """The ``open_question`` block from ``--question/--conflicts/--missing``.

    ``None`` when none of the options is given (the write carries no block).
    ``current`` is the stored block of an update: given options replace its
    fields (``--conflicts`` replaces the whole list), the rest is kept. The
    write gates (type, no claim, conflicts exist in the same tenant) run in
    the repository; this only shapes the block.
    """

    if question is None and conflicts is None and missing is None:
        return None
    block: dict[str, Any] = dict(current or {})
    if question is not None:
        block["question"] = question
    if conflicts is not None:
        block["conflicts"] = [
            part.strip() for item in conflicts for part in item.split(",") if part.strip()
        ]
    if missing is not None:
        block["missing"] = missing
    if "question" not in block:
        raise typer.BadParameter("--conflicts and --missing need --question")
    block.setdefault("conflicts", [])
    block.setdefault("missing", "")
    return block


def _resolve_open_question(root: Path, memory_id: str, answer_id: str, *, dry_run: bool) -> None:
    """``memfw update QUESTION --resolved-by ANSWER``: a human closes an open question."""

    try:
        repository = _repo(root)
        if dry_run:
            question = repository.get(memory_id)
            if question.type != OPEN_QUESTION_TYPE or question.open_question is None:
                raise IntegrityError(f"Not an open question: {memory_id}")
            if question.open_question.resolved_by is not None:
                raise IntegrityError(
                    f"Open question is already resolved by {question.open_question.resolved_by}: "
                    + memory_id
                )
            if question.valid_to is not None:
                raise IntegrityError(f"Open question is no longer current: {memory_id}")
            values = question.model_dump()
            block = question.open_question.model_dump(mode="json")
            values.update(
                open_question={**block, "resolved_by": answer_id}, valid_to=repository._now()
            )
            validate_open_question_references(
                values,
                lookup=lambda ref: lookup_current(repository, ref),
                previous=question.model_dump(),
            )
            typer.echo(
                json.dumps(
                    {
                        "memory_id": memory_id,
                        "resolved_by": answer_id,
                        "valid": True,
                        "version": question.version + 1,
                    }
                )
            )
            return
        repository.resolve_open_question(memory_id, answer_id)
        _enforce_after_write(repository)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Resolved {memory_id} by {answer_id}")


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


def _show_version(value: bool) -> None:
    """Print the installed package version and exit (``memfw --version``)."""

    if value:
        typer.echo(f"memfw {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    now: Annotated[str | None, typer.Option("--now", help="Inject an ISO-8601 UTC time")] = None,
    id_seed: Annotated[
        str | None, typer.Option("--id-seed", help="Inject a deterministic id seed")
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the memfw version and exit",
        ),
    ] = False,
) -> None:
    """Manage a Markdown memory vault."""
    invocation.set_invocation(now=_parse_now(now), id_seed=id_seed)


def _repo(root: Path, *, allow_pending: bool = False) -> Repository:
    # The opener lives in core (A2b) so sub-apps share it without importing
    # this module; kept here under its old name for the root commands.
    return open_vault(
        root,
        allow_pending=allow_pending,
        clock=invocation.clock(),
        id_factory=invocation.id_factory(root),
        error=typer.BadParameter,
    )


def _id_factory(root: Path) -> Callable[[], str]:
    return invocation.id_factory(root)


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
        # A vault is initialized when vault.yaml exists; an existing but empty (or
        # partly created) directory is not, and ``init`` would set it up.
        target = path.expanduser()
        if target.exists() and not target.is_dir():
            raise typer.BadParameter(f"not a directory: {path}")
        initialized = (target / "vault.yaml").is_file()
        typer.echo(
            json.dumps(
                {
                    "path": str(path),
                    "exists": target.exists(),
                    "initialized": initialized,
                    "would_initialize": not initialized,
                }
            )
        )
        return
    Repository(
        path,
        clock=invocation.clock(),
        id_factory=_id_factory(path),
    ).init()
    typer.echo(f"Initialized {path.expanduser().resolve()}")


@app.command()
def add(
    title: Annotated[str, typer.Option("--title")],
    body: Annotated[str, typer.Option("--body")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    memory_id: Annotated[str | None, typer.Option("--id")] = None,
    layer: Annotated[Layer, typer.Option()] = Layer.CURRENT,
    tier: Annotated[Tier, typer.Option()] = Tier.HOT,
    memory_type: Annotated[
        str | None, typer.Option("--type", help="Free-form memory type, e.g. experience-point")
    ] = None,
    epistemic: Annotated[
        str | None,
        typer.Option("--epistemic", help="experience, fact, judgment, or inference"),
    ] = None,
    causal: Annotated[
        str, typer.Option("--causal", help="none, association, hypothesis, or verified")
    ] = "none",
    causal_record: Annotated[
        str | None, typer.Option("--causal-record", help="JSON object or JSON file")
    ] = None,
    limitation: Annotated[
        list[str] | None, typer.Option("--limitation", help="Known limitation (repeatable)")
    ] = None,
    evidence: Annotated[
        list[str] | None, typer.Option("--evidence", help="Raw-data/design pointer (repeatable)")
    ] = None,
    experience: Annotated[
        str | None,
        typer.Option(
            "--experience",
            help="Experience-point record as a JSON object or JSON file "
            "(requires --type experience-point --epistemic experience)",
        ),
    ] = None,
    privacy: Annotated[
        str | None,
        typer.Option(
            "--privacy",
            help="public, shared or local-private. Unset: the vault's legacy_privacy_default",
        ),
    ] = None,
    tenant: Annotated[
        str, typer.Option("--tenant", help='Tenant of the memory (default: "")')
    ] = "",
    question: Annotated[
        str | None,
        typer.Option(
            "--question",
            help="What is unknown (with --type open-question; such a memory takes no "
            "--epistemic, --causal, --causal-record or --experience)",
        ),
    ] = None,
    conflicts: Annotated[
        list[str] | None,
        typer.Option(
            "--conflicts",
            help="Id of a memory that contradicts another on the question (repeatable or "
            "comma-separated; each must exist in the same tenant)",
        ),
    ] = None,
    missing: Annotated[
        str | None, typer.Option("--missing", help="What evidence would answer the question")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Add a Markdown memory (--type open-question --question ... records an unknown)."""
    try:
        privacy_value(privacy)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    record = _json_option(causal_record, "--causal-record")
    experience_record = _json_option(experience, "--experience")
    block = _open_question_block(None, question, conflicts, missing)
    try:
        if dry_run:
            vault_exists = root.exists() and (root / "vault.yaml").exists()
            if vault_exists:
                repository = _repo(root)
                if memory_id is not None:
                    repository._check_id(memory_id)
                    if any(event.memory_id == memory_id for event in repository.events()):
                        raise IntegrityError(f"Memory id already used: {memory_id}")
            gate_values: dict[str, Any] = {
                "type": memory_type,
                "epistemic": epistemic,
                "causal": causal,
                "causal_record": record,
                "limitations": limitation or [],
                "experience": experience_record,
                "evidence": evidence or [],
                "body": body,
                "source": "manual",
                "provenance": "human-authored",
            }
            if block is not None:
                # Only with the open-question options: other dry runs are unchanged.
                gate_values.update(id=memory_id, tenant=tenant, open_question=block)
            if vault_exists:
                evidence_classes = gate_evidence_classes(repository, gate_values)
            elif any(is_attachment_reference(item) for item in evidence or []):
                # As for conflicts: the real write would check the attachment.
                raise IntegrityError(
                    "att: evidence can only be checked in an existing vault: " + str(root)
                )
            else:
                evidence_classes = {}
            validate_epistemic_write(gate_values, actor="human", evidence_classes=evidence_classes)
            if block is not None and block["conflicts"] and not vault_exists:
                # The conflicts cannot be checked without the vault, and the
                # real write would fail: do not report a dry run as valid.
                raise IntegrityError(
                    "open_question.conflicts can only be checked in an existing vault: " + str(root)
                )
            if block is not None and vault_exists:
                validate_open_question_references(
                    gate_values, lookup=lambda ref: lookup_current(repository, ref)
                )
            summary: dict[str, Any] = {
                "valid": True,
                "memory_id": memory_id,
                "layer": layer.value,
                "tier": tier.value,
                "epistemic": epistemic,
                "causal": causal,
            }
            if block is not None:
                summary["open_question"] = block
            typer.echo(json.dumps(summary))
            return
        repository = _repo(root)
        doc = repository.add(
            title,
            body,
            memory_id=memory_id,
            layer=layer,
            tier=tier,
            memory_type=memory_type,
            tenant=tenant,
            open_question=block,
            epistemic=epistemic,  # type: ignore[arg-type]
            causal=causal,  # type: ignore[arg-type]
            causal_record=record,
            limitations=limitation,
            experience=experience_record,
            evidence=evidence,
            privacy=privacy,
        )
        _enforce_after_write(repository)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(doc.id)


@app.command("list")
def list_memories(
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object instead")] = False,
) -> None:
    """List current memories and show integrity warnings."""
    repository = _repo(root)
    if as_json:
        documents = repository.list()
        warnings = repository.list_warnings()
        rows = [
            {
                "id": document.id,
                "tier": document.tier.value,
                "layer": document.layer.value,
                "title": document.title,
                "stub": document.is_stub,
            }
            for document in documents
        ]
        typer.echo(json.dumps({"memories": rows, "warnings": warnings}, ensure_ascii=False))
        for warning in warnings:
            typer.echo(f"warning: {warning}", err=True)
        return
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
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the parsed memory as one JSON object")
    ] = False,
) -> None:
    """Show one memory as Markdown."""
    try:
        repository = _repo(root)
        document = repository.get(memory_id)
        if as_json:
            typer.echo(json.dumps(document.model_dump(mode="json"), ensure_ascii=False))
            return
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
    status: Annotated[
        str | None, typer.Option("--status", help="confirmed, unconfirmed or disputed")
    ] = None,
    evidence: Annotated[
        list[str] | None, typer.Option("--evidence", help="history ref (repeatable)")
    ] = None,
    epistemic: Annotated[
        str | None,
        typer.Option("--epistemic", help="experience, fact, judgment, or inference"),
    ] = None,
    causal: Annotated[
        str | None, typer.Option("--causal", help="none, association, hypothesis, or verified")
    ] = None,
    causal_record: Annotated[
        str | None, typer.Option("--causal-record", help="JSON object or JSON file")
    ] = None,
    limitation: Annotated[
        list[str] | None, typer.Option("--limitation", help="Known limitation (repeatable)")
    ] = None,
    question: Annotated[
        str | None,
        typer.Option("--question", help="Open question only: reword what is unknown"),
    ] = None,
    conflicts: Annotated[
        list[str] | None,
        typer.Option(
            "--conflicts",
            help="Open question only: replace the conflicting memory ids (repeatable or "
            "comma-separated; same tenant)",
        ),
    ] = None,
    missing: Annotated[
        str | None,
        typer.Option("--missing", help="Open question only: what evidence would answer it"),
    ] = None,
    resolved_by: Annotated[
        str | None,
        typer.Option(
            "--resolved-by",
            help="Open question only: resolve it with this answer memory (sets valid_to; "
            "a human decision, not combinable with other changes)",
        ),
    ] = None,
    privacy: Annotated[
        str | None,
        typer.Option("--privacy", help="public, shared or local-private (a human decision)"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
) -> None:
    """Update selected memory fields; --dry-run validates without writing."""
    if status is not None and status not in {"confirmed", "unconfirmed", "disputed"}:
        raise typer.BadParameter("--status must be confirmed, unconfirmed or disputed")
    if privacy is not None:
        try:
            privacy = privacy_value(privacy)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if resolved_by is not None:
        others = (title, body, layer, tier, status, evidence, epistemic, causal, causal_record)
        extra = (limitation, question, conflicts, missing, privacy)
        if any(value is not None for value in (*others, *extra)):
            raise typer.BadParameter("--resolved-by cannot be combined with other changes")
        _resolve_open_question(root, memory_id, resolved_by, dry_run=dry_run)
        return
    record = _json_option(causal_record, "--causal-record")
    changes: dict[str, Any] = {
        key: value
        for key, value in {
            "title": title,
            "body": body,
            "layer": layer,
            "tier": tier,
            "status": status,
            "evidence": evidence,
            "epistemic": epistemic,
            "causal": causal,
            "causal_record": record,
            "limitations": limitation,
            "privacy": privacy,
        }.items()
        if value is not None
    }
    try:
        repository = _repo(root)
        if question is not None or conflicts is not None or missing is not None:
            stored = repository.get(memory_id).open_question
            current = stored.model_dump(mode="json") if stored is not None else None
            changes["open_question"] = _open_question_block(current, question, conflicts, missing)
        if dry_run:
            old = repository.get(memory_id)
            if set(changes) & {
                "epistemic",
                "causal",
                "causal_record",
                "limitations",
                "body",
                "open_question",
            }:
                values = old.model_dump()
                values.update(changes)
                validate_epistemic_write(
                    values,
                    actor="human",
                    previous_causal=old.causal,
                    evidence_classes=gate_evidence_classes(repository, values, old.model_dump()),
                )
            if changes and OPEN_QUESTION_TYPE in (old.type, changes.get("type")):
                # As the real update: every write to an open question re-checks
                # its references, whichever fields it changes.
                values = old.model_dump()
                values.update(changes)
                validate_open_question_references(
                    values,
                    lookup=lambda ref: lookup_current(repository, ref),
                    previous=old.model_dump(),
                )
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
            if event is None:
                # Fail the same way the real run does for an unknown event.
                raise NotFoundError(f"Event not found: {event_id}")
            typer.echo(
                json.dumps(
                    {
                        "event_id": event_id,
                        "available": bool(
                            not event.is_foreign_kind
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
    query: Annotated[str | None, typer.Argument()] = None,
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = False,
    query_stdin: Annotated[
        bool,
        typer.Option("--query-stdin", help="Read the query from stdin (keeps it out of argv)"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON object (results, warnings, metrics)"),
    ] = False,
) -> None:
    """Search current Markdown and cold summary stubs; hits may trigger measured autowarm."""
    if query_stdin:
        if query is not None:
            raise typer.BadParameter("give QUERY or --query-stdin, not both")
        query = typer.get_text_stream("stdin").read().strip()
        if not query:
            raise typer.BadParameter("--query-stdin read an empty query")
    elif query is None:
        raise typer.BadParameter("missing QUERY (or pass --query-stdin)")
    repository = _repo(root)
    index = MemoryIndex(repository.index_path)
    if dry_run:
        if not index.schema_is_current():
            typer.echo(json.dumps({"would_rebuild": True, "writes": False, "results": []}))
            return
        matches = index.search(query)
        typer.echo(json.dumps({"writes": False, "results": matches}, ensure_ascii=False))
        return
    retriever = VaultRetriever(repository, index)
    identifiers = retriever.retrieve(query)
    if as_json:
        results: list[dict[str, str]] = []
        warnings: list[str] = []
        for memory_id in identifiers:
            try:
                results.append({"id": memory_id, "title": repository.get(memory_id).title})
            except MemfwError as exc:
                warnings.append(f"{memory_id}: {exc}")
        payload = {"results": results, "warnings": warnings, "metrics": retriever.last_metrics}
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
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
    hash_referenced: Annotated[
        bool,
        typer.Option(
            "--hash-referenced",
            help="Re-hash referenced attachment originals (default: size check only)",
        ),
    ] = False,
) -> None:
    """Check committed state, quarantine invalid Markdown, and register external changes."""
    repository = _repo(root)
    if dry_run:
        typer.echo(json.dumps({"would_check": len(repository.list()), "writes": False}))
        return
    issues = repository.fsck(hash_referenced=hash_referenced)
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
    report: dict[str, object] = {
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
    # Marked events of kinds this version does not know (a newer memfw wrote
    # them): listed, not errors. The keys appear only when there are some.
    foreign = foreign_event_kind_counts(events)
    if foreign:
        report["unknown_non_memory_event_count"] = sum(foreign.values())
        report["unknown_non_memory_event_kinds"] = foreign
    typer.echo(json.dumps(report))


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
    try:
        archive = repository.cold_archive(archive_id)
    except (MemfwError, ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(str(archive))


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
    parsed = _json_object(payload, "--payload")
    if parsed.get("memory_id") != memory_id or parsed.get("operation") != operation:
        raise typer.BadParameter("Payload memory_id/operation must match the command options")
    changes = parsed.get("changes", {})
    if not isinstance(changes, dict):
        raise typer.BadParameter("Payload changes must be a JSON object")
    if dry_run:
        # The same checks the real run makes, minus the append; nothing stricter.
        try:
            _repo(root).check_proposal(
                memory_id=memory_id,
                operation=operation,
                changes=changes,
                agent_id=agent_id,
                signature=signature,
            )
        except (MemfwError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(
            json.dumps(
                {"valid": True, "memory_id": memory_id, "operation": operation}, ensure_ascii=False
            )
        )
        return
    try:
        event = _repo(root).record_proposal(
            memory_id=memory_id,
            operation=operation,
            changes=changes,
            rationale=rationale,
            agent_id=agent_id,
            signature=signature,
        )
    except (MemfwError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(event.event_id)


def _json_object(raw: str, option: str) -> dict[str, Any]:
    """Parse ``raw`` as a JSON object or fail with a clean usage error."""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{option} must be a JSON object")
    return parsed


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
    data = _json_object(payload, "--payload")
    try:
        repository = _repo(root)
        if dry_run:
            repository.check_proposal_approval(proposal_id, data)
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


@app.command("consolidate")
def consolidate(
    since: Annotated[str, typer.Option("--since", help="e.g. 7d, 24h, or ISO date")] = "7d",
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Scan recent memory changes and write a review-only consolidation packet.

    The command never writes memories or applies forgetting. Its output is an
    idempotent candidate/forget-plan draft under _candidates/consolidate/.
    """
    repository = _repo(root)
    now = repository._now()
    if since.endswith("d") and since[:-1].isdigit():
        cutoff = now - timedelta(days=int(since[:-1]))
    elif since.endswith("h") and since[:-1].isdigit():
        cutoff = now - timedelta(hours=int(since[:-1]))
    else:
        try:
            parsed = datetime.fromisoformat(since)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            cutoff = parsed.astimezone(UTC)
        except ValueError as exc:
            raise typer.BadParameter("--since must be N d/h or an ISO timestamp") from exc
    recent = [
        document
        for document in repository.list()
        if document.valid_to is None and document.updated_at >= cutoff
    ]
    plan = plan_forget(repository, PolicyName.FIFO, _limits(repository), now=now, scope="hot")
    payload = {
        "since": cutoff.isoformat(),
        "dry_run": dry_run,
        "candidates": [
            {
                "id": document.id,
                "title": document.title,
                "type": document.type,
                "updated_at": document.updated_at.isoformat(),
                "reason": "changed_since",
            }
            for document in recent
        ],
        "forget_plan": {
            "policy": plan.policy.value,
            "forgotten_ids": list(plan.forgotten_ids),
            "retained_count": len(plan.retained_ids),
            "satisfied": plan.satisfied,
            "usage": {"count": plan.usage.count, "bytes": plan.usage.bytes},
        },
        "authority": False,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    output = repository.root / "_candidates" / "consolidate" / f"consolidate-{digest}.json"
    if dry_run:
        payload["would_write"] = str(output)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")
    typer.echo(json.dumps({**payload, "written": str(output)}, ensure_ascii=False, indent=2))


@forget_app.command("run")
def forget_run(
    policy: Annotated[PolicyName, typer.Option("--policy")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    action: Annotated[
        str,
        typer.Option(
            "--action",
            help=(
                "soft or hard (cold/delete aliases accepted). hard never deletes a live "
                "supersede chain; use `memfw delete` to remove one explicitly"
            ),
        ),
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


@forget_app.command("residue")
def forget_residue(
    memory_id: Annotated[str, typer.Argument(help="The forgotten memory's id")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    related_db: Annotated[
        Path | None,
        typer.Option(
            "--related-db",
            help="Related projection to check (default <root>/.memfw-related.sqlite)",
        ),
    ] = None,
    entities_db: Annotated[
        Path | None, typer.Option("--entities-db", help="Entity projection to check too")
    ] = None,
) -> None:
    """Check that a forgotten memory left nothing in the index or projections.

    Read-only. Exit 0 when clean, 1 when a store still carries the memory
    (each one names its rebuild remedy), 2 on a usage error (bad id or root),
    3 when the memory is still live.
    """
    from .curation.residue import projection_residue

    try:
        report = projection_residue(root, memory_id, related_db=related_db, entities_db=entities_db)
    except (MemfwError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if report["clean"] is None:
        # Not 2: typer uses 2 for usage errors (BadParameter).
        raise typer.Exit(code=3)
    if not report["clean"]:
        raise typer.Exit(code=1)


@forget_app.command("requests")
def forget_requests(root: Annotated[Path, typer.Option("--root", "-r")] = Path(".")) -> None:
    """List deletion requests staged by HTTP ``POST /forget`` (memfw-http/3). Read-only."""
    from .curation.residue import list_forget_requests

    typer.echo(
        json.dumps(
            {"requests": list_forget_requests(root)}, ensure_ascii=False, sort_keys=True, indent=2
        )
    )


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


@app.command("supersede")
def supersede(
    memory_id: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Option("--title")],
    body: Annotated[str, typer.Option("--body")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Create a human-confirmed replacement and mark the old memory historical."""
    try:
        repository = _repo(root)
        old = repository.get(memory_id)
        if dry_run:
            typer.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "old_memory_id": old.id,
                        "new_title": title,
                        "would_set": {"valid_to": repository._now().isoformat()},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        new = repository.supersede(memory_id, title, body, actor="human")
        updated_old = repository.get(old.id)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "old_memory_id": old.id,
                "new_memory_id": new.id,
                "old_valid_to": updated_old.valid_to.isoformat() if updated_old.valid_to else None,
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
    try:
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
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
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


@eval_app.command("causal")
def eval_causal(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full report as JSON")
    ] = False,
    fixtures: Annotated[
        Path | None, typer.Option("--fixtures", help="Synthetic fixture directory")
    ] = None,
    workdir: Annotated[
        Path | None, typer.Option("--workdir", help="Scratch directory (temporary by default)")
    ] = None,
    multimodal: Annotated[
        bool,
        typer.Option(
            "--multimodal",
            help="Run the multimodal corpus (attachments as observation evidence, MM3); "
            "--fixtures then names a corpus JSON file",
        ),
    ] = False,
) -> None:
    """Measure whether associations are promoted to causal conclusions.

    Two layers, reported separately: the write gate (false accepts and false
    rejects over the synthetic materials) and retrieval (labels on hits, and
    whether the answer template states a hypothesis as a conclusion). It runs
    locally: no network and no model calls. ``--multimodal`` runs the
    attachment corpus instead: screenshots, recordings and reports offered as
    causal evidence, plus ``file:``/``sha256:`` bypass attempts.
    """

    from .memory_eval import run_causal_promotion_eval, run_multimodal_causal_eval

    if multimodal:
        try:
            multimodal_report = run_multimodal_causal_eval(fixtures, workdir=workdir)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        result = multimodal_report.as_dict()
        if json_output:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
            return
        for key in (
            "cases",
            "spurious_promotions",
            "fact_upgrades",
            "false_accepts",
            "false_rejects",
            "bypass_attempts",
            "bypasses_blocked",
        ):
            typer.echo(f"{key}: {result[key]}")
        return
    try:
        report = run_causal_promotion_eval(fixtures, workdir=workdir)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = report.as_dict()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(f"materials: {payload['materials']}")
    typer.echo(f"spurious_promotions: {payload['spurious_promotions']}")
    typer.echo(f"false_accepts: {payload['false_accepts']}")
    typer.echo(f"false_rejects: {payload['false_rejects']}")
    typer.echo(f"missing_labels: {payload['retrieval_layer']['missing_labels']}")
    typer.echo(
        f"hypothesis_as_conclusion: {payload['retrieval_layer']['hypothesis_as_conclusion']}"
    )


def _cap_list(value: str, what: str) -> tuple[int, ...]:
    try:
        caps = tuple(int(item) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise typer.BadParameter(f"{what} must be comma-separated integers") from exc
    if not caps:
        raise typer.BadParameter(f"{what} must name at least one cap")
    return caps


@eval_app.command("context")
def eval_context(
    snapshot: Annotated[
        Path, typer.Argument(help="A read-only vault snapshot (a copy; never written)")
    ],
    conversations: Annotated[
        Path | None,
        typer.Option("--conversations", help="Conversation store file for handoff (read-only)"),
    ] = None,
    usage: Annotated[
        Path | None,
        typer.Option("--usage", help="C5 usage projection file (memory_used; read-only)"),
    ] = None,
    state: Annotated[
        Path | None,
        typer.Option(
            "--state",
            help="Conversation worker state file for handoff summaries (read-only; default none)",
        ),
    ] = None,
    policy: Annotated[str, typer.Option("--policy", help="compat_fts or strict")] = "compat_fts",
    tenant: Annotated[
        str | None, typer.Option("--tenant", help="Tenant (default MEMFW_TENANT)")
    ] = None,
    sources: Annotated[
        list[str] | None,
        typer.Option("--source", help="Allowed source, repeatable (default MEMFW_ALLOWED_SOURCES)"),
    ] = None,
    clearance: Annotated[
        str | None,
        typer.Option("--clearance", help="Privacy levels, comma separated (MEMFW_MCP_CLEARANCE)"),
    ] = None,
    app_name: Annotated[
        str | None, typer.Option("--app", help="Calling app for handoff (default MEMFW_APP)")
    ] = None,
    slots: Annotated[
        str | None, typer.Option("--slots", help="Slots, comma separated, in pack order")
    ] = None,
    slot_caps: Annotated[
        str, typer.Option("--slot-caps", help="Token caps swept for every slot")
    ] = "500,1000,2000,4000",
    slot_cap: Annotated[
        list[str] | None,
        typer.Option("--slot-cap", help="NAME=CAP[,CAP...]: caps for one slot (repeatable)"),
    ] = None,
    resident_caps: Annotated[
        str, typer.Option("--resident-caps", help="Resident-prefix caps swept (tokens)")
    ] = "1000,2000,4000,6000",
    seed: Annotated[int, typer.Option("--seed")] = 17,
    sessions: Annotated[int, typer.Option("--sessions", help="Synthetic sessions")] = 20,
    queries: Annotated[int, typer.Option("--queries", help="Queries per synthetic session")] = 5,
    repeats: Annotated[int, typer.Option("--repeats", help="Timed builds per slot and cap")] = 5,
    session_gap: Annotated[
        float, typer.Option("--session-gap-minutes", help="Silence that splits access sessions")
    ] = 30.0,
    now: Annotated[
        str | None, typer.Option("--now", help="ISO instant (default: the snapshot's latest)")
    ] = None,
    current: Annotated[
        bool,
        typer.Option(
            "--current",
            help="Simulate runtime.context.current: true (default: the snapshot's vault.yaml)",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory for context-eval.json and context-eval.md"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the JSON report")] = False,
) -> None:
    """C4: coverage, waste, latency and token cost of the context pack (read-only).

    Runs the real slot providers on a private copy of SNAPSHOT for every slot
    cap and resident-prefix cap and prints a markdown table (``--json``: the
    JSON report). The report holds counts, ratios and timings only. No
    default changes; see docs/evaluation.md, "C4 context pack evaluation".
    """

    from datetime import datetime as _datetime

    from .runtime.context_eval import (
        EVAL_SLOTS,
        EvalOptions,
        SnapshotChanged,
        SnapshotRefused,
        evaluate_snapshot,
        render_markdown,
    )
    from .usage import UsageStoreError

    names = (
        tuple(item.strip() for item in slots.split(",") if item.strip()) if slots else EVAL_SLOTS
    )
    per_slot: dict[str, tuple[int, ...]] = {}
    for item in slot_cap or []:
        name, sep, caps = item.partition("=")
        if not sep or not name.strip():
            raise typer.BadParameter("--slot-cap must be NAME=CAP[,CAP...]")
        per_slot[name.strip()] = _cap_list(caps, "--slot-cap")
    moment = None
    if now:
        try:
            moment = _datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            raise typer.BadParameter("--now must be an ISO 8601 instant") from exc
        if moment.tzinfo is None:
            raise typer.BadParameter("--now must include a timezone")
    options = EvalOptions(
        policy=policy,
        tenant=tenant,
        allowed_sources=tuple(sources) if sources else None,
        clearance=clearance,
        app=app_name,
        slots=names,
        slot_caps=per_slot,
        default_slot_caps=_cap_list(slot_caps, "--slot-caps"),
        resident_caps=_cap_list(resident_caps, "--resident-caps"),
        seed=seed,
        sessions=sessions,
        queries=queries,
        repeats=repeats,
        session_gap_minutes=session_gap,
        now=moment,
        current=current,
    )
    outputs = [out / "context-eval.json", out / "context-eval.md"] if out else []
    try:
        report = evaluate_snapshot(
            snapshot,
            options=options,
            conversations=conversations,
            usage=usage,
            state=state,
            outputs=[out, *outputs] if out else [],
        )
    except (SnapshotRefused, SnapshotChanged, UsageStoreError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown = render_markdown(report)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        outputs[0].write_text(payload, encoding="utf-8")
        outputs[1].write_text(markdown, encoding="utf-8")
    typer.echo(payload if json_output else markdown, nl=False)


@okf_app.command("export")
def okf_export(
    out: Annotated[Path, typer.Argument(help="Output directory for the OKF bundle")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    force: Annotated[bool, typer.Option("--force")] = False,
    bundle_title: Annotated[str | None, typer.Option("--title")] = None,
    clearance: Annotated[
        str | None,
        typer.Option(
            "--clearance",
            help=(
                "Privacy levels to export, comma separated (public, shared, "
                "local-private). Unset exports every memory."
            ),
        ),
    ] = None,
) -> None:
    """Export the local vault as an OKF v0.2 bundle (derived, rebuildable)."""
    if not clearance_is_valid(clearance):
        raise typer.BadParameter("--clearance must list only public, shared, local-private")
    try:
        result = export_okf(root, out, force=force, bundle_title=bundle_title, clearance=clearance)
    except (FileExistsError, ValueError, MemfwError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        json.dumps(
            {
                "bundle": str(result.bundle),
                "concepts": result.concepts,
                "valid": result.valid,
                "manifest": str(result.manifest),
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _privacy_setting(root: Path, key: str, level: str | None, current: str | None) -> None:
    """Show (``level`` None) or set one privacy key of ``vault.yaml`` (one config_change)."""

    repository = _repo(root)
    if level is None:
        config = _config(repository)
        typer.echo(json.dumps({key: current, "configured": key in config}, sort_keys=True))
        return
    try:
        value = privacy_value(level)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    config = _config(repository)
    config[key] = value
    try:
        repository.configure(config)
    except MemfwError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({key: value, "configured": True}, sort_keys=True))


@privacy_app.command("legacy-default")
def privacy_legacy_default(
    level: Annotated[
        str | None,
        typer.Argument(help="public, shared or local-private; omit to show the current value"),
    ] = None,
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
) -> None:
    """Show or set legacy_privacy_default: the level of memories with no privacy field.

    Unset, it is local-private. Setting it rewrites vault.yaml once and records
    one config_change event; memory files are not touched.
    """
    current = _repo(root).legacy_privacy_default() if level is None else None
    _privacy_setting(root, LEGACY_PRIVACY_KEY, level, current)


@privacy_app.command("new-default")
def privacy_new_default(
    level: Annotated[
        str | None,
        typer.Argument(help="public, shared or local-private; omit to show the current value"),
    ] = None,
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
) -> None:
    """Show or set new_privacy_default: the level stamped on new memories that set none.

    Unset (null), new memories carry no privacy field, as before MM0. Setting it
    rewrites vault.yaml once and records one config_change event.
    """
    current = _repo(root).new_privacy_default() if level is None else None
    _privacy_setting(root, NEW_PRIVACY_KEY, level, current)


@okf_app.command("validate")
def okf_validate(path: Annotated[Path, typer.Argument(help="OKF bundle directory")]) -> None:
    """Validate an OKF bundle: frontmatter, required type, index/log, manifest count."""
    result = validate_okf_bundle(path)
    typer.echo(
        json.dumps(
            {
                "valid": result.valid,
                "concept_count": result.concept_count,
                "errors": result.errors,
                "warnings": result.warnings,
                "type_counts": result.type_counts,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not result.valid:
        raise typer.Exit(code=1)


@okf_app.command("import")
def okf_import(
    bundle: Annotated[Path, typer.Argument(help="OKF bundle directory (untrusted input)")],
    root: Annotated[Path, typer.Option("--root", "-r")] = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="Report only (default); --apply stages candidates under _candidates/remember/",
        ),
    ] = True,
    tenant: Annotated[str, typer.Option("--tenant")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full report as JSON")
    ] = False,
) -> None:
    """Stage an OKF bundle's concepts as candidates (never the authoritative vault).

    The bundle is path-checked and validated first; an unsafe or invalid bundle
    is refused whole. Concepts whose body is already in the vault are skipped.
    Apply each staged candidate with ``memfw candidate-apply --apply``.
    """
    from .okf.importer import OkfImportError, import_okf

    try:
        result = import_okf(bundle, root, dry_run=dry_run, tenant=tenant)
    except OkfImportError as exc:
        if json_output:
            # Same shape and exit code as ``okf validate`` on an invalid bundle.
            payload = {
                "valid": False,
                "error": str(exc),
                "errors": exc.errors,
                "warnings": [],
                "authority": False,
            }
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"error: {exc}", err=True)
            for line in exc.errors:
                typer.echo(f"  {line}", err=True)
        raise typer.Exit(code=1) from exc
    payload = result.as_dict()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    mode = "dry run" if dry_run else "applied"
    typer.echo(f"okf import ({mode}): {payload['concept_count']} concepts, source {result.source}")
    for item in result.items:
        detail = item.reason or item.proposal_id or ""
        typer.echo(f"  {item.status:<15} {item.concept}  {detail}")
    counts = ", ".join(f"{key}={value}" for key, value in result.counts.items())
    typer.echo(f"counts: {counts or 'none'}")
    for warning in result.warnings:
        typer.echo(f"warning: {warning}")


def _projection_fail(exc: Exception) -> None:
    """One stderr line and exit 2: projection callers must fail closed."""

    typer.echo(f"error: {' '.join(str(exc).split())}", err=True)
    raise typer.Exit(code=2)


@projection_app.command("export")
def projection_export(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")],
    out: Annotated[Path, typer.Option("--out", help="Directory that receives the generation")],
    policy: Annotated[
        Path | None, typer.Option("--policy", help="Projection policy JSON (default: v1)")
    ] = None,
    policy_version: Annotated[
        int | None, typer.Option("--policy-version", help="Override: 1, or 2 (adds R3 fields)")
    ] = None,
    lifecycle_source: Annotated[
        Path | None, typer.Option("--lifecycle-source", help="memory-meta-index.json")
    ] = None,
    previous: Annotated[
        Path | None, typer.Option("--previous", help="Previous generation dir or manifest")
    ] = None,
    skip_privacy: Annotated[
        str | None,
        typer.Option(
            "--skip-privacy",
            help="Comma list of privacy classes not to project, e.g. unknown,local-private",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON line (the default)")] = True,
) -> None:
    """Write a new projection generation (files + manifest); the vault is only read."""
    del as_json
    try:
        result = export_projection(
            root,
            out,
            policy=load_policy(
                policy,
                version=policy_version,
                skip_privacy=(
                    [item.strip() for item in skip_privacy.split(",") if item.strip()]
                    if skip_privacy is not None
                    else None
                ),
            ),
            lifecycle_source=lifecycle_source,
            previous=previous,
            now=invocation.injected_now(),
        )
    except (MemfwError, OSError) as exc:
        _projection_fail(exc)
        return
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("resolve")
def resolve_command(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")],
    ids_stdin: Annotated[
        bool, typer.Option("--ids-stdin", help="Read ids or memories/<id>.md hits from stdin")
    ] = False,
    lifecycle_source: Annotated[
        Path | None, typer.Option("--lifecycle-source", help="memory-meta-index.json")
    ] = None,
    policy: Annotated[Path | None, typer.Option("--policy")] = None,
    policy_version: Annotated[int | None, typer.Option("--policy-version")] = None,
    max_lines: Annotated[int, typer.Option("--max-lines", help="Body lines; 0 = no body")] = 200,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON line (the default)")] = True,
) -> None:
    """Batch read-only lookup of recall hits: authoritative lifecycle/validity/privacy."""
    del as_json
    if not ids_stdin:
        _projection_fail(ProjectionError("--ids-stdin is required (ids never go in argv)"))
        return
    try:
        hits = read_hits(typer.get_text_stream("stdin"))
        payload = resolve_hits(
            root,
            hits,
            lifecycle_source=lifecycle_source,
            policy=load_policy(policy, version=policy_version),
            max_lines=max_lines,
        )
    except (MemfwError, OSError) as exc:
        _projection_fail(exc)
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


def _share_rules(repository: Repository) -> SharePolicy:
    config = repository.read_config() if repository.config_path.exists() else {}
    try:
        return SharePolicy.from_config(config)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@corroboration_app.command("rebuild")
def corroboration_rebuild(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
) -> None:
    """Rebuild the corroboration projection (kept outside the vault; the vault is only read)."""
    repository = _repo(root)
    try:
        counts = rebuild_corroboration(repository, _share_rules(repository))
    except (MemfwError, CorroborationError, OSError) as exc:
        _projection_fail(exc)
        return
    payload: dict[str, Any] = {"path": str(projection_path(repository.root)), **counts}
    legacy = legacy_path(repository.root)
    if legacy.is_file() or legacy.is_symlink():
        payload["legacy_file"] = legacy.name
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if "legacy_file" in payload:
        typer.echo(
            f"note: {payload['legacy_file']} at the vault root is from an earlier release and is"
            " no longer used; you may delete it (memfw will not).",
            err=True,
        )


@project_app.command("obsidian")
def project_obsidian(
    out: Annotated[Path, typer.Option("--out", help="Board directory (outside the vault)")],
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
    clearance: Annotated[
        str | None,
        typer.Option(
            "--clearance",
            help="Comma list of public, shared, local-private (default: public,shared)",
        ),
    ] = None,
    tenant: Annotated[
        str, typer.Option("--tenant", help='The one tenant to project (default: "")')
    ] = "",
) -> None:
    """Write a read-only Obsidian board (pages, links, Bases views); the vault is only read."""
    try:
        projector = ObsidianProjector(clearance=clearance, tenant=tenant)
        corroboration_path = projection_path(root.expanduser())
        if corroboration_path.is_file():
            repository = _repo(root)
            rules = _share_rules(repository)
            identity = vault_identity(repository)
            try:
                check_corroboration(corroboration_path, identity)
            except OtherVaultError as exc:
                # Like a missing projection: the board is built without counts.
                typer.echo(f"warning: {exc}; corroboration counts left out", err=True)
            else:

                def lookup(ids: Any) -> dict[str, int]:
                    answer = corroboration_for(
                        ids,
                        caller_app=OPERATOR,
                        rules=rules,
                        path=corroboration_path,
                        identity=identity,
                    )
                    return {key: value.count for key, value in answer.items()}

                projector.corroboration = lookup
        result = projector.build(root, out)
    except (MemfwError, CorroborationError, OSError, ValueError) as exc:
        _projection_fail(exc)
        return
    for name in result.edited:
        typer.echo(f"warning: hand-edited generated file overwritten: {name}", err=True)
    typer.echo(json.dumps(result.as_dict(), ensure_ascii=False))


@corroboration_app.command("show")
def corroboration_show(
    memory_id: Annotated[str, typer.Argument(help="Memory id")],
    as_app: Annotated[
        str, typer.Option("--as-app", help="Caller app; output is trimmed to what it may read")
    ] = OPERATOR,
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
) -> None:
    """Corroboration of one memory as seen by --as-app (ids, names and counts only)."""
    repository = _repo(root)
    try:
        answer = corroboration_for(
            [memory_id],
            caller_app=as_app,
            rules=_share_rules(repository),
            path=projection_path(repository.root),
            identity=vault_identity(repository),
        )
    except (MemfwError, CorroborationError, OSError) as exc:
        _projection_fail(exc)
        return
    payload = {"id": memory_id, "corroboration": answer[memory_id].as_dict()}
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if not projection_path(repository.root).is_file():
        typer.echo("note: projection missing: run `memfw corroboration rebuild`", err=True)


@corroboration_app.command("queue")
def corroboration_queue(
    as_app: Annotated[
        str,
        typer.Option("--as-app", help="Caller app; corroboration is trimmed to what it may read"),
    ] = OPERATOR,
    tenant: Annotated[str, typer.Option("--tenant", help="Tenant of the questions")] = "",
    policy: Annotated[
        str,
        typer.Option(
            "--policy",
            help="strict (default: drops private, rejected, archived) or compat_fts",
        ),
    ] = "strict",
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
) -> None:
    """Open-question review queue: questions whose conflicts are corroborated come first.

    Order: more corroborated conflicts (>= 2 distinct visible apps), then more
    support, then older, then id. Without the projection the order is oldest
    first and ``corroboration.degraded`` says why. ``--policy strict`` (the
    default) lists, and counts support from, only memories the strict recall
    policy keeps. Never writes the vault.
    """
    from .runtime import build_runtime

    repository = _repo(root)
    runtime = build_runtime(repository.root, tenant=tenant)
    try:
        payload = runtime.open_question_queue(
            repository, app=as_app, policy_name=policy, allow_operator=True
        )
    except (MemfwError, OSError, ValueError) as exc:
        _projection_fail(exc)
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


# --- graph: K3 edge projection, K4 path patterns -------------------------------


@graph_app.command("rebuild")
def graph_rebuild(
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
) -> None:
    """Rebuild the typed-edge projection (kept outside the vault; the vault is only read)."""
    from .runtime.graph import rebuild_edge_projection

    repository = _repo(root)
    try:
        payload = rebuild_edge_projection(repository)
    except (MemfwError, OSError, ValueError) as exc:
        _projection_fail(exc)
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if "legacy_file" in payload:
        typer.echo(
            f"note: {payload['legacy_file']} at the vault root is from an earlier build and is"
            " no longer used; you may delete it (memfw will not).",
            err=True,
        )


@graph_app.command("match")
def graph_match_command(
    start: Annotated[
        str, typer.Option("--from", help="Start node: a memory id, concept oid or concept label")
    ],
    path: Annotated[
        str,
        typer.Option(
            "--path",
            help="Steps: NAME> forward, <NAME backward, NAME*M..N> M to N hops (at most 4 hops)",
        ),
    ],
    root: Annotated[Path, typer.Option("--root", "-r", help="memfw vault (read only)")] = Path("."),
    limit: Annotated[int, typer.Option("--limit", help="Paths returned (1..100)")] = 20,
    fanout: Annotated[
        int, typer.Option("--fanout", help="Neighbours expanded per node and step (1..200)")
    ] = 50,
    acyclic: Annotated[
        bool, typer.Option("--acyclic", help="Never visit a node twice on one path")
    ] = False,
    tenant: Annotated[
        str, typer.Option("--tenant", help='The one tenant to read (default: "")')
    ] = "",
    clearance: Annotated[
        str | None,
        typer.Option(
            "--clearance",
            help="Comma list of public, shared, local-private (default: public,shared)",
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Only memories from this source (repeatable)"),
    ] = None,
) -> None:
    """Paths from one node along a bounded typed-edge pattern, filtered like every exit."""
    from .runtime.graph import graph_match

    levels: list[str] | None = None
    if clearance is not None:
        if not clearance.strip() or not clearance_is_valid(clearance):
            raise typer.BadParameter("--clearance must list only public, shared, local-private")
        levels = [token.strip().lower() for token in clearance.split(",") if token.strip()]
    repository = _repo(root)
    try:
        payload = graph_match(
            repository,
            start=start,
            path=path,
            tenant=tenant,
            clearance=levels,
            allowed_sources=tuple(source or ()),
            limit=limit,
            fanout=fanout,
            acyclic=acyclic,
        )
    except (MemfwError, OSError, ValueError) as exc:
        _projection_fail(exc)
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


# --- grouped top-level help ----------------------------------------------------

PANEL_DAILY = "Daily"
PANEL_APPROVAL = "Approval"
PANEL_OPS = "Ops"
PANEL_EXPORT = "Export"

#: The ``memfw --help`` panel of every top-level command and group. Help layout
#: only: names, options and behaviour are unchanged. A new top-level command
#: must be added here (``tests/test_cli_tidy.py`` checks that none is missing).
HELP_PANELS: dict[str, str] = {
    # Day-to-day reading and writing of memories.
    "init": PANEL_DAILY,
    "add": PANEL_DAILY,
    "list": PANEL_DAILY,
    "show": PANEL_DAILY,
    "update": PANEL_DAILY,
    "delete": PANEL_DAILY,
    "rollback": PANEL_DAILY,
    "supersede": PANEL_DAILY,
    "layer-set": PANEL_DAILY,
    "layer": PANEL_DAILY,
    "stats": PANEL_DAILY,
    "concepts": PANEL_DAILY,
    "graph": PANEL_DAILY,
    "memory-related": PANEL_DAILY,
    "handoff": PANEL_DAILY,
    "notes": PANEL_DAILY,
    "attach": PANEL_DAILY,
    "code": PANEL_DAILY,
    # Human decisions: proposals, staged candidates, dream questions.
    "record-proposal": PANEL_APPROVAL,
    "approve-proposal": PANEL_APPROVAL,
    "review": PANEL_APPROVAL,
    "candidates": PANEL_APPROVAL,
    "candidate-apply": PANEL_APPROVAL,
    "candidates-expire": PANEL_APPROVAL,
    "capture": PANEL_APPROVAL,
    "consolidate": PANEL_APPROVAL,
    "dream": PANEL_APPROVAL,
    "experience": PANEL_APPROVAL,
    "corroboration": PANEL_APPROVAL,
    # Vault health, indexes, budgets, tiers, stores and settings.
    "fsck": PANEL_OPS,
    "recover": PANEL_OPS,
    "index": PANEL_OPS,
    "log": PANEL_OPS,
    "budget": PANEL_OPS,
    "forget": PANEL_OPS,
    "tiers": PANEL_OPS,
    "tier": PANEL_OPS,
    "cold-archive": PANEL_OPS,
    "cold-restore": PANEL_OPS,
    "memory-related-build": PANEL_OPS,
    "history": PANEL_OPS,
    "conversations": PANEL_OPS,
    "privacy": PANEL_OPS,
    "eval": PANEL_OPS,
    # Read-only exits to other tools.
    "okf": PANEL_EXPORT,
    "projection": PANEL_EXPORT,
    "project": PANEL_EXPORT,
    "resolve": PANEL_EXPORT,
}


def _assign_help_panels() -> None:
    for command in app.registered_commands:
        name = command.name or typer.main.get_command_name(command.callback.__name__)  # type: ignore[union-attr]
        command.rich_help_panel = HELP_PANELS.get(name)
    for group in app.registered_groups:
        group.rich_help_panel = HELP_PANELS.get(str(group.name))


_assign_help_panels()
