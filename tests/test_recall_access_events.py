"""``memory_access`` events: recall providers are read-only; access follows the filter.

``mode=two_stage`` logs one access per memory it returns, after the
coordinator's ``FilterPolicy``; a hit the policy drops (privacy, tenant) is
never logged. The direct ``hybrid``/``vector``/``fts`` paths keep logging what
they return, exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider
from memfw.ports import RecallBudget
from memfw.recall.builtin import HybridProvider

pytest.importorskip("sqlite_vec")

QUERY = "private backup keys"


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("Backup notes", "backup notes and restore drills", memory_id="m-notes")
    add(
        "Private backup",
        "private backup keys location",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    add("Alpha backup", "tenant alpha private backup keys", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _embedder() -> HashEmbeddingProvider:
    return HashEmbeddingProvider(dimension=64)


def _gateway(root: Path, **kwargs: Any) -> Gateway:
    kwargs.setdefault("tenant", "")
    kwargs.setdefault("entity_enabled", False)
    kwargs.setdefault("allowed_sources", ())
    return Gateway(root, **kwargs)


def _accesses(repository: Repository) -> list[tuple[str, str]]:
    return [
        (event.memory_id, str(event.details.get("operation") or ""))
        for event in repository.events()
        if event.kind == EventKind.MEMORY_ACCESS
    ]


def _log_bytes(repository: Repository) -> tuple[bytes, bytes]:
    root = repository.root
    state = root / "events.state.json"
    return (
        (root / "events.jsonl").read_bytes(),
        state.read_bytes() if state.exists() else b"",
    )


def _provider(repository: Repository) -> HybridProvider:
    return HybridProvider(repository.root, embedder=_embedder())


def test_hybrid_provider_search_appends_no_event(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _log_bytes(repository)
    # First search builds the disposable hybrid index: a projection, no event.
    hits = _provider(repository).search(
        QUERY, limit=10, budget=RecallBudget(timeout_seconds=5.0), tenant="", as_of=None
    )
    assert "m-priv" in [hit.ref for hit in hits]
    assert (repository.root / ".memory-hybrid.sqlite").exists()
    assert _log_bytes(repository) == before
    # And again on a warm index.
    _provider(repository).search(
        QUERY, limit=10, budget=RecallBudget(timeout_seconds=5.0), tenant="", as_of=None
    )
    assert _log_bytes(repository) == before


def test_retriever_record_access_false_covers_the_fts_fallback(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _log_bytes(repository)
    retriever = MemoryHybridRetriever(repository, record_access=False)
    # No embedder: the retriever answers from FTS, which logged access too.
    result = retriever.search(QUERY, limit=10, mode="hybrid")
    assert result["mode"] == "fts"
    assert result["results"]
    assert _log_bytes(repository) == before


def test_two_stage_strict_logs_only_returned_ids(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    payload = gateway.search(QUERY, limit=10, mode="two_stage", policy="strict")
    assert "hybrid" in payload["recall"]["providers"]
    returned = [row["id"] for row in payload["results"]]
    assert "m-priv" not in returned
    assert payload["recall_metrics"]["dropped"].get("privacy") == 1
    accesses = _accesses(repository)
    assert [memory_id for memory_id, _ in accesses] == returned
    assert {operation for _, operation in accesses} <= {"two_stage_retrieve"}


def test_two_stage_compat_logs_the_private_hit_it_returns(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    payload = gateway.search(QUERY, limit=10, mode="two_stage")
    returned = [row["id"] for row in payload["results"]]
    assert "m-priv" in returned
    assert [memory_id for memory_id, _ in _accesses(repository)] == returned


@pytest.mark.parametrize("policy", ["compat_fts", "strict"])
def test_two_stage_never_logs_another_tenant(tmp_path: Path, policy: str) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    payload = gateway.search(
        "tenant alpha private backup keys", limit=50, mode="two_stage", policy=policy
    )
    returned = [row["id"] for row in payload["results"]]
    assert "m-alpha" not in returned
    ids = [memory_id for memory_id, _ in _accesses(repository)]
    assert "m-alpha" not in ids
    assert ids == returned


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_direct_modes_still_log_what_they_return(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    payload = gateway.search(QUERY, limit=10, mode=mode)
    assert payload["mode"] == mode
    returned = [row["id"] for row in payload["results"]]
    assert returned
    assert _accesses(repository) == [(memory_id, "hybrid_retrieve") for memory_id in returned]
