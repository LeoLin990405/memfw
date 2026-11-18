"""``memory_access`` events: recall providers are read-only; access follows the filter.

``mode=two_stage`` logs one access per memory it returns, after the
coordinator's ``FilterPolicy``; a hit the policy drops (privacy, tenant) is
never logged. The direct ``hybrid``/``vector``/``fts`` paths keep logging what
they return, exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from memfw.core.models import EventKind, Tier
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider
from memfw.ports import RecallBudget
from memfw.recall.builtin import HybridProvider
from memfw.retrieval import VaultRetriever

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


# --- warming (autowarm) follows the filter too ---------------------------------

WARM_QUERY = "zebra vault keys"
NOT_RETURNED = ("m-cold-alpha", "m-cold-priv", "m-stub-alpha")


def _cold_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Zebra hot", "zebra vault keys in the hot tier", memory_id="m-hot")
    add("Zebra own cold", "zebra vault keys own cold copy", memory_id="m-cold-own", tier=Tier.COLD)
    add(
        "Zebra alpha cold",
        "zebra vault keys alpha cold copy",
        memory_id="m-cold-alpha",
        tenant="alpha",
        tier=Tier.COLD,
    )
    add(
        "Zebra private cold",
        "zebra vault keys private cold copy",
        memory_id="m-cold-priv",
        tier=Tier.COLD,
        tags=["sensitivity:private"],
    )
    add(
        "Zebra alpha stub",
        "zebra vault keys alpha archived copy",
        memory_id="m-stub-alpha",
        tenant="alpha",
        tier=Tier.COLD,
    )
    repository.cold_archive("archive-zebra", memory_ids={"m-stub-alpha"})
    assert repository.get("m-stub-alpha").is_stub
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _warm_events(repository: Repository) -> list[tuple[str, str]]:
    kinds = {EventKind.TIER_CHANGE, EventKind.MEMORY_RESTORE}
    return [
        (event.memory_id, event.kind.value) for event in repository.events() if event.kind in kinds
    ]


def _files(repository: Repository, ids: tuple[str, ...]) -> dict[str, bytes]:
    return {
        memory_id: (repository.memories_dir / f"{memory_id}.md").read_bytes() for memory_id in ids
    }


def test_hybrid_provider_warms_nothing(tmp_path: Path) -> None:
    repository = _cold_vault(tmp_path)
    before_log = _log_bytes(repository)
    before = _files(repository, ("m-cold-own", *NOT_RETURNED))
    for tenant in ("", "alpha"):
        hits = _provider(repository).search(
            WARM_QUERY,
            limit=10,
            budget=RecallBudget(timeout_seconds=5.0),
            tenant=tenant,
            as_of=None,
        )
        assert hits
    assert _log_bytes(repository) == before_log
    assert _files(repository, ("m-cold-own", *NOT_RETURNED)) == before


def test_two_stage_strict_never_warms_what_it_drops(tmp_path: Path) -> None:
    repository = _cold_vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    before = _files(repository, NOT_RETURNED)
    payload = gateway.search(WARM_QUERY, limit=10, mode="two_stage", policy="strict")
    assert "hybrid" in payload["recall"]["providers"]
    returned = [row["id"] for row in payload["results"]]
    assert not set(returned) & set(NOT_RETURNED)
    warmed = {memory_id for memory_id, _ in _warm_events(repository)}
    assert not warmed & set(NOT_RETURNED)
    assert warmed <= set(returned)
    assert _files(repository, NOT_RETURNED) == before
    assert repository.get("m-stub-alpha").is_stub
    assert repository.get("m-cold-alpha").tier == Tier.COLD
    assert repository.get("m-cold-priv").tier == Tier.COLD


def test_two_stage_warms_the_cold_hits_it_returns(tmp_path: Path) -> None:
    repository = _cold_vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    payload = gateway.search(WARM_QUERY, limit=10, mode="two_stage")
    returned = [row["id"] for row in payload["results"]]
    assert "m-cold-own" in returned
    assert "m-cold-alpha" not in returned and "m-stub-alpha" not in returned
    assert repository.get("m-cold-own").tier == Tier.HOT
    assert repository.get("m-cold-alpha").tier == Tier.COLD
    assert repository.get("m-stub-alpha").is_stub
    assert {memory_id for memory_id, _ in _warm_events(repository)} <= set(returned)
    # Warm, then log, per returned hit, as the direct path does.
    assert [memory_id for memory_id, _ in _accesses(repository)] == returned


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_direct_modes_never_warm_another_tenant(tmp_path: Path, mode: str) -> None:
    repository = _cold_vault(tmp_path)
    gateway = _gateway(repository.root, embedder=_embedder())
    before = _files(repository, ("m-cold-alpha", "m-stub-alpha"))
    payload = gateway.search(WARM_QUERY, limit=10, mode=mode)
    assert payload["mode"] == mode
    returned = [row["id"] for row in payload["results"]]
    assert "m-cold-own" in returned
    # Same tenant: warmed as before.
    assert repository.get("m-cold-own").tier == Tier.HOT
    # Another tenant: neither warmed nor un-archived.
    assert _files(repository, ("m-cold-alpha", "m-stub-alpha")) == before
    warmed = {memory_id for memory_id, _ in _warm_events(repository)}
    assert not warmed & {"m-cold-alpha", "m-stub-alpha"}


def test_vault_retriever_admit_skips_warm_and_access(tmp_path: Path) -> None:
    repository = _cold_vault(tmp_path)
    before = _files(repository, ("m-cold-alpha", "m-stub-alpha"))
    ids = VaultRetriever(repository, admit=lambda document: document.tenant == "").retrieve(
        WARM_QUERY, limit=10
    )
    assert "m-cold-alpha" in ids  # still returned, for the caller to drop
    assert _files(repository, ("m-cold-alpha", "m-stub-alpha")) == before
    assert not {memory_id for memory_id, _ in _accesses(repository)} & {
        "m-cold-alpha",
        "m-stub-alpha",
    }
    assert repository.get("m-cold-own").tier == Tier.HOT


# --- the MM0 clearance applies before access and warming (direct modes) -------

REMOTE = frozenset({"public", "shared"})


@pytest.mark.parametrize("mode", ["hybrid", "vector", "fts_fallback"])
def test_direct_modes_never_log_or_warm_what_the_clearance_withholds(
    tmp_path: Path, mode: str
) -> None:
    """As in ``two_stage``: a withheld hit gets no ``memory_access`` and stays cold."""

    repository = _cold_vault(tmp_path)
    # Public notes need a level: the vault default makes unlabelled memories public.
    config = repository.root / "vault.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + "legacy_privacy_default: public\n", encoding="utf-8"
    )
    embedder = None if mode == "fts_fallback" else _embedder()
    gateway = _gateway(repository.root, embedder=embedder, clearance=REMOTE)
    before = _files(repository, ("m-cold-priv",))
    payload = gateway.search(WARM_QUERY, limit=10, mode="hybrid" if embedder is None else mode)
    returned = [row["id"] for row in payload["results"]]
    assert "m-cold-own" in returned and "m-cold-priv" not in returned
    logged = [memory_id for memory_id, _ in _accesses(repository)]
    assert "m-cold-priv" not in logged
    assert set(logged) == set(returned)
    assert repository.get("m-cold-priv").tier == Tier.COLD
    assert _files(repository, ("m-cold-priv",)) == before
    assert repository.get("m-cold-own").tier == Tier.HOT  # an admitted hit is still warmed


def test_hybrid_retriever_admit_skips_warm_and_access(tmp_path: Path) -> None:
    repository = _cold_vault(tmp_path)
    retriever = MemoryHybridRetriever(
        repository,
        embedder=_embedder(),
        admit=lambda document: "sensitivity:private" not in document.tags,
    )
    result = retriever.search(WARM_QUERY, limit=10, mode="hybrid")
    ids = [row["id"] for row in result["results"]]
    assert "m-cold-priv" in ids  # still returned, for the caller to drop
    assert "m-cold-priv" not in {memory_id for memory_id, _ in _accesses(repository)}
    assert repository.get("m-cold-priv").tier == Tier.COLD


# --- best effort: a read-only log never fails recall --------------------------


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_two_stage_with_a_read_only_log_keeps_its_hits(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    expected = [row["id"] for row in gateway.search("backup", limit=10, mode="fts")["results"]]
    log = repository.root / "events.jsonl"
    log.chmod(0o444)
    try:
        payload = gateway.search("backup", limit=10, mode="two_stage")
    finally:
        log.chmod(0o644)
    assert [row["id"] for row in payload["results"]] == expected
    assert payload["degraded"] is True
    assert "access_not_recorded" in payload["errors"]
