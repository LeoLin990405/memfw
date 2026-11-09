"""Load a synthetic class into a temporary vault and search it with shipped code."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import perf_counter
from typing import Any

import scripts.memfw_readonly_service as service
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
    "mcp_default",
    "http_search",
    "notes_fts",
)

_READ_TOKEN = "eval-read-token"
_WRITE_TOKEN = "eval-write-token"
_SERVER: ThreadingHTTPServer | None = None
_SERVER_LOCK = threading.Lock()

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
    stage_calls: int = 0
    notes_index_calls: int = 0
    repository_adds: int = 0
    last_mode: str | None = None
    last_degraded: bool | None = None
    last_entry: str = ""


def _note_markdown(document: Document) -> str:
    return (
        f"---\ntitle: {document.title}\nsource: {document.source}\n---\n\n"
        f"# {document.title}\n\n{document.body}\n"
    )


class _QuietHandler(service.Handler):
    """The eval client is not a person reading an access log."""

    def log_message(self, format: str, *args: Any) -> None:
        return


def _ensure_server() -> ThreadingHTTPServer:
    """One loopback server. Each request points it at that call's vault."""

    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _SERVER = server
        return _SERVER


def _http(root: Path, path: str, payload: dict[str, Any], *, write: bool) -> dict[str, Any]:
    """POST to the shipped readonly service on an ephemeral loopback port."""

    server = _ensure_server()
    host, port = server.server_address
    body = json.dumps(payload).encode("utf-8")
    keys = (service.ROOT_ENV, service.READ_TOKEN_ENV, service.WRITE_TOKEN_ENV)
    previous = {key: os.environ.get(key) for key in keys}
    os.environ[service.ROOT_ENV] = str(root.resolve())
    os.environ[service.READ_TOKEN_ENV] = _READ_TOKEN
    os.environ[service.WRITE_TOKEN_ENV] = _WRITE_TOKEN
    connection = HTTPConnection(str(host), int(port), timeout=30)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                service.TOKEN_HEADER: _WRITE_TOKEN if write else _READ_TOKEN,
            },
        )
        response = connection.getresponse()
        raw = response.read()
        status = int(response.status)
    finally:
        connection.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path} returned non-JSON status {status}") from exc
    if status != 200 or not isinstance(decoded, dict):
        raise RuntimeError(f"{path} failed with status {status}: {decoded}")
    return decoded


def load_corpus(corpus: ClassCorpus, root: Path) -> BuiltCorpus:
    """Write the class into a new vault and rebuild the disposable indexes.

    Authority memories use ``Repository.add``. A ``stage`` document is posted
    to loopback ``/stage`` and stays an unapproved candidate. A ``notes``
    document is written only into the notes tree and indexed by ``NotesIndex``.
    """

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
    stage_calls = 0
    repository_adds = 0
    for document in corpus.documents:
        if document.source == "stage":
            staged = _http(
                vault,
                "/stage",
                {
                    "proposal_id": document.doc_id,
                    "title": document.title,
                    "content": document.body,
                    "tenant": document.tenant,
                    "source": "stage",
                    "tags": [],
                    "related": [],
                },
                write=True,
            )
            if staged.get("authority") is not False or staged.get("staged") is not True:
                raise RuntimeError("stage did not keep the candidate unapproved")
            stage_calls += 1
            continue
        if document.source == "notes":
            continue
        repository.add(
            document.title,
            document.body,
            memory_id=document.doc_id,
            tenant=document.tenant,
            source=document.source,
            provenance=document.provenance,
        )
        repository_adds += 1
    for document in corpus.documents:
        if document.source in {"stage", "notes"}:
            continue
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
        if document.source == "stage":
            continue
        note_path = notes_root / f"{document.doc_id}.md"
        note_path.write_text(_note_markdown(document), encoding="utf-8")
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
        stage_calls=stage_calls,
        notes_index_calls=1,
        repository_adds=repository_adds,
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
    built.last_mode = None
    built.last_degraded = None
    built.last_entry = path
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
        built.last_mode = str(result["mode"])
        built.last_degraded = bool(result["degraded"])
        return [str(item["id"]) for item in result["results"]]
    if path in {"mcp_fts", "mcp_default"}:
        if question.tenant != built.gateway.tenant:
            raise ValueError("question tenant does not match the gateway tenant")
        if path == "mcp_fts":
            result = built.gateway.search(query, limit=limit, mode="fts")
        else:
            result = built.gateway.search(query, limit=limit)
        built.last_mode = str(result["mode"])
        built.last_degraded = bool(result["degraded"])
        return [str(item["id"]) for item in result["results"]]
    if path == "http_search":
        result = _http(
            built.repository.root,
            "/search",
            {"query": query, "limit": limit, "tenant": question.tenant},
            write=False,
        )
        built.last_mode = str(result.get("mode") or "")
        built.last_degraded = bool(result.get("degraded"))
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
) -> tuple[list[str], float, dict[str, Any]]:
    started = perf_counter()
    identifiers = search_path(path, built, question, limit=limit)
    elapsed = max(0.0, perf_counter() - started)
    return (
        identifiers,
        elapsed,
        {
            "mode": built.last_mode,
            "degraded": built.last_degraded,
            "entry": built.last_entry,
        },
    )


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
