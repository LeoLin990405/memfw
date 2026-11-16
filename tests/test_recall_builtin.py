"""Built-in providers: the FTS equivalence guard, hybrid/multihop wrapping, contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.epistemic import CAUSAL_VALUES as CORE_CAUSAL
from memfw.core.epistemic import EPISTEMIC_VALUES as CORE_EPISTEMIC
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.ports.testing import check_recall_provider
from memfw.ports.types import CAUSAL_VALUES, EPISTEMIC_VALUES, Candidate, RecallBudget
from memfw.recall import Coordinator, FilterPolicy, ProviderConfig
from memfw.recall.builtin import (
    FtsProvider,
    HybridProvider,
    MultihopProvider,
    default_resolvers,
)
from memfw.recall.coordinator import MAX_LIMIT
from memfw.retrieval.multihop import search_multihop
from memfw.runtime import ensure_search_index

QUERIES = (
    "homelab backup",
    "HAL9000",
    "cron pause before disk migration",
    "backup",
    "notes",
    "迁移 备份",
    "a",
    "release v0.2.0",
)


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("NAS1 migration", "pause cron before the disk migration on NAS1", memory_id="m-nas1")
    add("Backup notes", "backup notes and restore drills", memory_id="m-notes", source="nas")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("迁移计划", "迁移之前先暂停定时任务，再做备份", memory_id="m-zh")
    add(
        "Private backup",
        "private backup keys location",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    add("Alpha backup", "tenant alpha backup notes", memory_id="m-alpha", tenant="alpha")
    add("Old backup plan", "old homelab backup plan notes", memory_id="m-old")
    add("Expired backup", "expired backup window notes", memory_id="m-exp")
    repository.update("m-old", superseded_by="m-backup")
    repository.update("m-exp", valid_to=datetime(2026, 1, 1, tzinfo=UTC))
    for index in range(60):
        add(
            f"Filler {index}",
            f"backup filler entry number {index} notes",
            memory_id=f"m-f{index:02d}",
        )
    # Hand-edited records: a valid_until key and an unparsable valid_from.
    # The vault's integrity check rejects edited files, so both paths must
    # agree on leaving them out.
    add("Until record", "backup until record", memory_id="m-until")
    add("Bad date record", "backup bad date record", memory_id="m-baddate")
    _edit_frontmatter(repository, "m-until", "valid_until: 2020-01-01")
    _edit_frontmatter(repository, "m-baddate", "valid_from: not-a-date")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _edit_frontmatter(repository: Repository, memory_id: str, line: str) -> None:
    path = repository.memories_dir / f"{memory_id}.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\n---\n", f"\n{line}\n---\n", 1), encoding="utf-8")


def _fts_coordinator(
    root: Path,
    *,
    source: str = "",
    include_history: bool = False,
    allowed: tuple[str, ...] = (),
    scope: str = "",
) -> Coordinator:
    provider = FtsProvider(
        root,
        source=source,
        include_history=include_history,
        scope=scope,
        allowed_sources=allowed,
        prepare=ensure_search_index,
    )
    return Coordinator([provider], resolvers=default_resolvers(root))


@pytest.mark.parametrize("limit", [1, 3, 10, 50, 51, 100, MAX_LIMIT])
@pytest.mark.parametrize("tenant", ["", "alpha"])
def test_fts_only_coordinator_matches_mcp_fts_ids_and_order(
    tmp_path: Path, limit: int, tenant: str
) -> None:
    """The "default byte-identical" guard for the wiring PR."""

    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant=tenant, entity_enabled=True, allowed_sources=())
    coordinator = _fts_coordinator(repository.root)
    for query in QUERIES:
        expected = [
            item["id"] for item in gateway.search(query, limit=limit, mode="fts")["results"]
        ]
        result = coordinator.search(
            query, limit=limit, policy=FilterPolicy.compat_fts(tenant=tenant)
        )
        assert result.ids == expected, query
        assert result.metrics.dropped == {}, query
        assert not result.labels.degraded


def test_fts_equivalence_with_source_history_as_of_and_allowed(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    cases: list[dict[str, object]] = [
        {"source": "nas"},
        {"include_history": True},
        {"as_of": "2025-06-01T00:00:00+00:00"},
        {"allowed": ("manual",)},
        {"allowed": ("manual", "nas")},
        {"allowed": ("nas", "manual", "nas")},
        {"source": "nas", "allowed": ("nas", "manual")},
    ]
    for case in cases:
        source = str(case.get("source", ""))
        include_history = bool(case.get("include_history", False))
        as_of_text = case.get("as_of")
        allowed = tuple(case.get("allowed", ()))  # type: ignore[arg-type]
        gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=allowed)
        coordinator = _fts_coordinator(
            root, source=source, include_history=include_history, allowed=allowed
        )
        as_of = datetime.fromisoformat(as_of_text) if isinstance(as_of_text, str) else None
        policy = FilterPolicy.compat_fts(
            tenant="",
            allowed_sources=allowed,
            source=source,
            as_of=as_of,
            include_history=include_history,
        )
        for query in QUERIES:
            payload = gateway.search(
                query,
                limit=10,
                mode="fts",
                source=source,
                include_history=include_history,
                as_of=as_of_text if isinstance(as_of_text, str) else None,
            )
            expected = [item["id"] for item in payload["results"]]
            assert coordinator.search(query, limit=10, policy=policy).ids == expected, (case, query)


def test_fts_equivalence_includes_the_hidden_rows(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    coordinator = _fts_coordinator(repository.root)
    ids = coordinator.search("backup", limit=50, policy=FilterPolicy.compat_fts(tenant="")).ids
    assert "m-priv" in ids  # compat applies no privacy rule, like today
    assert "m-old" not in ids and "m-exp" not in ids and "m-alpha" not in ids


def test_strict_policy_on_the_same_provider_drops_private(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    coordinator = _fts_coordinator(repository.root)
    result = coordinator.search("private backup keys", limit=10, policy=FilterPolicy())
    assert "m-priv" not in result.ids
    assert result.metrics.dropped.get("privacy") == 1


def test_fts_provider_availability_and_contract(tmp_path: Path) -> None:
    assert FtsProvider(tmp_path / "missing").available().reason == "no_vault"
    bare = Repository(tmp_path / "bare")
    bare.init()
    assert FtsProvider(bare.root).available().reason == "no_index"
    assert FtsProvider(bare.root, prepare=ensure_search_index).available().available
    repository = _vault(tmp_path)
    provider = FtsProvider(repository.root)
    seen = check_recall_provider(
        provider, queries=QUERIES, limit=5, vault=repository.root, vault_exclude=(".memfw.lock",)
    )
    assert seen["HAL9000"][0].ref == "m-hal"
    assert all(c.ref_kind == "memory_id" for items in seen.values() for c in items)


def test_fts_provider_bad_query_degrades_in_coordinator(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    coordinator = _fts_coordinator(repository.root)
    result = coordinator.search("x" * 600, limit=5, policy=FilterPolicy.compat_fts(tenant=""))
    assert result.labels.fail_closed
    assert result.labels.degraded["fts"].startswith("error:")


def test_hybrid_provider_unavailable_without_embedder(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = HybridProvider(repository.root, embedder=None)
    assert provider.available().reason == "no_embedder"
    assert check_recall_provider(provider, queries=["x"], require_available=False) == {}
    fts = FtsProvider(repository.root)
    coordinator = Coordinator([provider, fts], resolvers=default_resolvers(repository.root))
    result = coordinator.search("homelab backup plans", limit=5, policy=FilterPolicy())
    assert result.labels.degraded == {"hybrid": "unavailable:no_embedder"}
    assert result.ids  # FTS answered


def test_hybrid_provider_reports_missing_sqlite_vec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    import importlib.util as util

    real = util.find_spec
    monkeypatch.setattr(
        util, "find_spec", lambda name, *a: None if name == "sqlite_vec" else real(name, *a)
    )
    provider = HybridProvider(repository.root, embedder=HashEmbeddingProvider(8))
    assert provider.available().reason == "sqlite_vec_missing"


def test_hybrid_provider_wraps_the_retriever(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    repository = _vault(tmp_path)
    embedder = HashEmbeddingProvider(8)
    provider = HybridProvider(repository.root, embedder=embedder)
    seen = check_recall_provider(provider, queries=["homelab backup"], limit=5)
    gateway = Gateway(repository.root, embedder=embedder, entity_enabled=False)
    expected = [
        item["id"] for item in gateway.search("homelab backup", limit=5, mode="hybrid")["results"]
    ]
    assert [c.ref for c in seen["homelab backup"]] == expected
    assert provider.projection_version == str(getattr(embedder, "fingerprint", "") or "")


def test_hybrid_provider_fallback_to_fts_is_unavailable(tmp_path: Path) -> None:
    repository = _vault(tmp_path)

    class BrokenEmbedder(HashEmbeddingProvider):
        def embed(self, texts: list[str]) -> list[list[float]]:  # type: ignore[override]
            raise RuntimeError("embedding service down")

    provider = HybridProvider(repository.root, embedder=BrokenEmbedder(8))
    fts = FtsProvider(repository.root)
    coordinator = Coordinator([provider, fts], resolvers=default_resolvers(repository.root))
    result = coordinator.search("homelab backup plans", limit=5, policy=FilterPolicy())
    assert result.labels.degraded.get("hybrid", "").startswith("unavailable")
    assert result.labels.providers == ("fts",)


def test_multihop_provider_matches_search_multihop(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = MultihopProvider(repository.root)
    seen = check_recall_provider(provider, queries=QUERIES[:4], limit=10)
    for query in QUERIES[:4]:
        expected = [item["id"] for item in search_multihop(repository, query, limit=10)["results"]]
        assert [c.ref for c in seen[query]] == expected
    bare = Repository(tmp_path / "bare")
    bare.init()
    assert MultihopProvider(bare.root).available().reason == "no_index"


def test_all_builtins_together_are_deterministic(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    providers = [FtsProvider(root), MultihopProvider(root), HybridProvider(root, embedder=None)]
    coordinator = Coordinator(
        providers,
        resolvers=default_resolvers(root),
        configs={"multihop": ProviderConfig(weight=0.5, budget=RecallBudget(timeout_seconds=3))},
    )
    policy = FilterPolicy()
    first = coordinator.search("homelab backup plans overall", limit=10, policy=policy)
    second = coordinator.search("homelab backup plans overall", limit=10, policy=policy)
    assert first.ids == second.ids
    assert set(first.labels.providers) == {"fts", "multihop"}
    assert "m-priv" not in first.ids


def test_ports_epistemic_constants_mirror_core() -> None:
    assert EPISTEMIC_VALUES == CORE_EPISTEMIC
    assert CAUSAL_VALUES == CORE_CAUSAL


@pytest.mark.parametrize("entity_enabled", [False, True])
@pytest.mark.parametrize("scope", ["", "homelab", "nas backup", "no-such-scope"])
def test_fts_equivalence_with_scope_and_entities(
    tmp_path: Path, scope: str, entity_enabled: bool
) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=entity_enabled)
    coordinator = _fts_coordinator(repository.root, scope=scope)
    for query in QUERIES:
        for limit in (5, 50, MAX_LIMIT):
            payload = gateway.search(query, limit=limit, mode="fts", scope=scope)
            expected = [item["id"] for item in payload["results"]]
            got = coordinator.search(query, limit=limit, policy=FilterPolicy.compat_fts(tenant=""))
            assert got.ids == expected, (scope, query, limit)


def test_fts_equivalence_large_pages_are_capped_like_mcp(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False)
    coordinator = _fts_coordinator(repository.root)
    expected = [item["id"] for item in gateway.search("backup", limit=200, mode="fts")["results"]]
    got = coordinator.search("backup", limit=200, policy=FilterPolicy.compat_fts(tenant="")).ids
    assert len(expected) == 50
    assert got == expected


def test_edited_records_are_left_out_by_both_paths(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False)
    coordinator = _fts_coordinator(repository.root)
    for query in ("until record", "bad date record"):
        expected = [item["id"] for item in gateway.search(query, limit=10, mode="fts")["results"]]
        got = coordinator.search(query, limit=10, policy=FilterPolicy.compat_fts(tenant="")).ids
        assert got == expected
        assert "m-until" not in got and "m-baddate" not in got


def test_builtin_providers_pass_the_full_contract_for_each_tenant(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root

    def tenant_of(candidate: Candidate) -> str | None:
        try:
            return repository.get(candidate.ref).tenant
        except Exception:  # noqa: BLE001 - unknown ref
            return None

    for tenant in ("", "alpha"):
        for provider in (FtsProvider(root), MultihopProvider(root)):
            check_recall_provider(
                provider,
                queries=QUERIES,
                limit=10,
                tenant=tenant,
                tenant_of=tenant_of,
                error_queries=["x" * 600, "a\x00b"],
                budget=RecallBudget(timeout_seconds=5),
            )


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ([], "none"),
        (["sensitivity:private"], "local-private"),
        (["Sensitivity:Private"], "local-private"),
        (["SENSITIVITY:PRIVATE "], "local-private"),
        (["sensitivity:local-private"], "unknown"),
        (["sensitivity:secret"], "unknown"),
        (["sensitivity:private", "sensitivity:secret"], "unknown"),
        (["Privacy:local-private"], "local-private"),
        (["privacy:none", "sensitivity:private"], "local-private"),
        # Several privacy tags: the strictest wins regardless of case or order.
        (["Privacy:none", "privacy:local-private"], "local-private"),
        (["privacy:none", "privacy:local-private"], "local-private"),
        (["privacy:local-private", "privacy:none"], "local-private"),
        (["PRIVACY:none", "privacy:secret"], "unknown"),
    ],
)
def test_meta_from_document_classes_sensitivity_like_the_projection(
    tmp_path: Path, tags: list[str], expected: str
) -> None:
    from memfw.projection import tag_privacy_class
    from memfw.recall.builtin import meta_from_document
    from memfw.recall.policy import FilterPolicy, evaluate

    repository = Repository(tmp_path / "vault")
    repository.init()
    document = repository.add("t", "body", tags=tags)
    meta = meta_from_document(document)
    assert meta.privacy_class()[0] == expected
    if not any(tag.lower().startswith("privacy:") for tag in tags):
        assert tag_privacy_class(tags) == expected
    verdict = evaluate(meta, FilterPolicy(), now=datetime.now(UTC))
    assert (verdict.reason == "privacy") == (expected != "none")


@pytest.mark.parametrize(
    ("tags", "reason"),
    [
        (["Lifecycle:active", "lifecycle:retired"], "retired"),
        (["lifecycle:retired", "lifecycle:active"], "retired"),
        (["LIFECYCLE:Active", "lifecycle:superseded"], "superseded"),
        (["lifecycle:active", "lifecycle:draft"], "lifecycle"),
        (["Lifecycle:Retired"], "retired"),
        (["lifecycle:active"], None),
    ],
)
def test_lifecycle_tags_strongest_wins(tmp_path: Path, tags: list[str], reason: str | None) -> None:
    from memfw.recall.builtin import meta_from_document
    from memfw.recall.policy import FilterPolicy, evaluate

    repository = Repository(tmp_path / "vault")
    repository.init()
    document = repository.add("t", "body", tags=tags)
    verdict = evaluate(meta_from_document(document), FilterPolicy(), now=datetime.now(UTC))
    assert verdict.reason == reason
