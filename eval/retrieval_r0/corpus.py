"""Load a synthetic class into a temporary vault and search it with shipped code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from eval.retrieval_r0.cases import CALLER_TENANT, ClassCorpus, Document, Question
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes.index import NotesIndex
from memfw.retrieval import VaultRetriever

_MODEL_ENV = (
    "MEMFW_EMBEDDING_URL",
    "MEMFW_RERANK_URL",
    "MEMFW_JUDGE_URL",
    "MEMFW_REWRITE_URL",
)

PATHS = (
    "memory_fts",
    "memory_bm25",
    "memory_count",
    "vault_bm25",
    "hybrid_fts",
    "mcp_fts",
    "notes_fts",
)

PATHS_NOT_RUN = (
    {
        "name": "notes_vector",
        "reason": "vector mode needs an embedding endpoint, and this run configures none",
    },
    {
        "name": "notes_hybrid",
        "reason": "hybrid mode calls the vector leg; notes_fts is the offline notes path",
    },
    {
        "name": "rerank",
        "reason": "rerank stays off unless server config names an endpoint",
    },
    {
        "name": "query_rewrite",
        "reason": "query rewrite stays off unless server config names an endpoint",
    },
    {
        "name": "code_retrieval",
        "reason": "code retrieval indexes source files, not this memory corpus",
    },
    {
        "name": "entity_route",
        "reason": "the entity route is opt-in and no entity dictionary is bundled",
    },
    {
        "name": "hosted_judge",
        "reason": "R0 does not call a judge",
    },
)


def assert_offline_env() -> None:
    """Refuse to construct a client when an endpoint is already in the environment."""

    present = [key for key in _MODEL_ENV if os.environ.get(key)]
    if present:
        raise RuntimeError(
            "model endpoint environment is set (" + ", ".join(present) + "); refusing to run"
        )


def guard_endpoint(endpoint: str | None) -> None:
    """The harness has no request field for an endpoint."""

    if endpoint is not None:
        raise ValueError("endpoint is not accepted from the request")


@dataclass(slots=True)
class BuiltCorpus:
    corpus: ClassCorpus
    repository: Repository
    index: MemoryIndex
    notes_index: NotesIndex
    by_id: dict[str, Document]
    hybrid: MemoryHybridRetriever
    gateway: Gateway


def load_corpus(corpus: ClassCorpus, root: Path) -> BuiltCorpus:
    """Write the class into a new vault and rebuild the disposable indexes."""

    assert_offline_env()
    root.mkdir(parents=True, exist_ok=True)
    vault = root / "vault"
    counter = iter(range(1_000_000))
    repository = Repository(
        vault,
        clock=lambda: datetime(2026, 4, 1, tzinfo=UTC),
        id_factory=lambda: f"gen{next(counter):08d}",
    )
    repository.init()
    for document in corpus.documents:
        repository.add(
            document.title,
            document.body,
            memory_id=document.doc_id,
            tenant=document.tenant,
            source=document.source,
            provenance=document.provenance,
        )
    for document in corpus.documents:
        if document.valid_to is None and document.superseded_by is None:
            continue
        changes: dict[str, object] = {}
        if document.valid_to is not None:
            changes["valid_to"] = datetime.fromisoformat(document.valid_to)
        if document.superseded_by is not None:
            changes["superseded_by"] = document.superseded_by
        repository.update(document.doc_id, **changes)
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    notes_root = root / "notes"
    notes_root.mkdir()
    for document in corpus.documents:
        note_path = notes_root / f"{document.doc_id}.md"
        note_path.write_text(
            f"---\ntitle: {document.title}\nsource: {document.source}\n---\n\n"
            f"# {document.title}\n\n{document.body}\n",
            encoding="utf-8",
        )
    notes_index = NotesIndex(root / "notes.sqlite")
    notes_index.index(notes_root)
    return BuiltCorpus(
        corpus=corpus,
        repository=repository,
        index=index,
        notes_index=notes_index,
        by_id={document.doc_id: document for document in corpus.documents},
        hybrid=MemoryHybridRetriever(repository, tenant=CALLER_TENANT, entity_enabled=False),
        gateway=Gateway(vault, tenant=CALLER_TENANT, entity_enabled=False),
    )


def search_path(
    path: str,
    built: BuiltCorpus,
    question: Question,
    *,
    limit: int,
    endpoint: str | None = None,
) -> list[str]:
    """Run one shipped path. ``question.question`` is the only text passed in."""

    guard_endpoint(endpoint)
    query = question.question
    if path == "memory_fts":
        return [item[0] for item in built.index.search(query, limit=limit)]
    if path == "memory_bm25":
        return [item[0] for item in built.index.ranked_search(query, limit=limit, ranking="bm25")]
    if path == "memory_count":
        return [item[0] for item in built.index.ranked_search(query, limit=limit, ranking="count")]
    if path == "vault_bm25":
        retriever = VaultRetriever(built.repository, built.index, ranking="bm25")
        return retriever.retrieve(query, limit=limit)
    if path == "hybrid_fts":
        result = built.hybrid.search(query, limit=limit, mode="fts", tenant=question.tenant)
        return [str(item["id"]) for item in result["results"]]
    if path == "mcp_fts":
        if question.tenant != built.gateway.tenant:
            raise ValueError("question tenant does not match the gateway tenant")
        result = built.gateway.search(query, limit=limit, mode="fts")
        return [str(item["id"]) for item in result["results"]]
    if path == "notes_fts":
        hits = built.notes_index.search(query, limit=limit, mode="fts")
        return [Path(hit.path).stem for hit in hits]
    raise ValueError(f"unknown path: {path}")


def timed_search(
    path: str,
    built: BuiltCorpus,
    question: Question,
    *,
    limit: int,
) -> tuple[list[str], float]:
    started = perf_counter()
    identifiers = search_path(path, built, question, limit=limit)
    elapsed = max(0.0, perf_counter() - started)
    return identifiers, elapsed


__all__ = [
    "PATHS",
    "PATHS_NOT_RUN",
    "BuiltCorpus",
    "assert_offline_env",
    "guard_endpoint",
    "load_corpus",
    "search_path",
    "timed_search",
]
