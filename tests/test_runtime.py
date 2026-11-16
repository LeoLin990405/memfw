"""``memfw.runtime`` (A3a): the composition root owns the two_stage recall wiring."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.recall import Coordinator, FilterPolicy, InflightRegistry, ProviderConfig
from memfw.recall.builtin import FtsProvider, HybridProvider, default_resolvers
from memfw.runtime import (
    TWO_STAGE_PROVIDER_TIMEOUT,
    TWO_STAGE_TOTAL_TIMEOUT,
    RecallOutcome,
    Runtime,
    build_runtime,
)
from tests.architecture import layers, scan

QUERIES = ("homelab backup", "backup", "notes", "迁移 备份", "release v0.2.0")


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("Backup notes", "backup notes and restore drills", memory_id="m-notes", source="nas")
    add("Ops backup", "ops backup runbook notes", memory_id="m-ops", source="ops")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("迁移计划", "迁移之前先暂停定时任务，再做备份", memory_id="m-zh")
    add("Private backup", "private backup keys", memory_id="m-priv", tags=["sensitivity:private"])
    add("Alpha backup", "tenant alpha backup notes", memory_id="m-alpha", tenant="alpha")
    add("Old backup plan", "old homelab backup plan notes", memory_id="m-old")
    add("Expired backup", "expired backup window notes", memory_id="m-exp")
    repository.update("m-old", superseded_by="m-backup")
    repository.update("m-exp", valid_to=datetime(2026, 1, 1, tzinfo=UTC))
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _old_path(
    repository: Repository,
    query: str,
    *,
    tenant: str,
    allowed: tuple[str, ...],
    embedder: Any,
    policy: str,
    source: str,
    include_history: bool,
    as_of: datetime | None,
    limit: int,
) -> tuple[list[str], dict[str, Any]]:
    """The wiring MCP built inline before A3a, rebuilt by hand."""

    from memfw.ports import RecallBudget

    budget = RecallBudget(timeout_seconds=3.0)
    providers: list[Any] = [
        FtsProvider(
            repository.root, source=source, include_history=include_history, allowed_sources=allowed
        )
    ]
    configs: dict[str, Any] = {"fts": ProviderConfig(budget=budget)}
    if embedder:
        providers.append(
            HybridProvider(
                repository.root,
                embedder=embedder,
                source=source,
                include_history=include_history,
                allowed_sources=allowed,
            )
        )
        configs["hybrid"] = ProviderConfig(budget=budget)
    kwargs: dict[str, Any] = {
        "tenant": tenant,
        "allowed_sources": allowed,
        "source": source,
        "as_of": as_of,
        "include_history": include_history,
    }
    filter_policy = (
        FilterPolicy(**kwargs) if policy == "strict" else FilterPolicy.compat_fts(**kwargs)
    )
    result = Coordinator(
        providers,
        resolvers=default_resolvers(repository.root),
        configs=configs,
        inflight=InflightRegistry(),
        total_timeout=4.0,
    ).search(query, limit=limit, policy=filter_policy)
    labels = result.labels.as_dict()
    return result.ids, labels


def test_build_runtime_takes_values_as_given(tmp_path: Path) -> None:
    embedder = HashEmbeddingProvider(dimension=64)
    runtime = build_runtime(
        tmp_path, tenant="alpha", allowed_sources=("nas",), embedder=embedder, entity_enabled=True
    )
    assert isinstance(runtime, Runtime)
    assert runtime.root == tmp_path
    assert (runtime.tenant, runtime.allowed_sources) == ("alpha", ("nas",))
    assert runtime.embedder is embedder and runtime.entity_enabled is True
    assert isinstance(runtime.recall_inflight, InflightRegistry)
    default = build_runtime(tmp_path)
    assert (default.tenant, default.allowed_sources, default.embedder) == ("", (), None)
    assert default.entity_enabled is False
    assert (TWO_STAGE_PROVIDER_TIMEOUT, TWO_STAGE_TOTAL_TIMEOUT) == (3.0, 4.0)


@pytest.mark.parametrize("policy", ["compat_fts", "strict"])
@pytest.mark.parametrize("with_embedder", [False, True])
def test_recall_matches_the_old_inline_wiring(
    tmp_path: Path, policy: str, with_embedder: bool
) -> None:
    repository = _vault(tmp_path)
    embedder = HashEmbeddingProvider(dimension=64) if with_embedder else None
    cases: list[dict[str, Any]] = [
        {"tenant": "", "allowed": ()},
        {"tenant": "", "allowed": ("manual",)},
        {"tenant": "alpha", "allowed": ()},
        {"tenant": "", "allowed": (), "include_history": True},
        {"tenant": "", "allowed": (), "source": "nas"},
        {"tenant": "", "allowed": (), "as_of": datetime(2025, 12, 31, tzinfo=UTC)},
    ]
    for case in cases:
        runtime = build_runtime(
            repository.root,
            tenant=case["tenant"],
            allowed_sources=case["allowed"],
            embedder=embedder,
        )
        for query in QUERIES:
            outcome = runtime.recall(
                repository.root,
                query,
                limit=10,
                policy_name=policy,
                source=case.get("source", ""),
                include_history=case.get("include_history", False),
                as_of=case.get("as_of"),
                scope="",
            )
            assert isinstance(outcome, RecallOutcome)
            ids, labels = _old_path(
                repository,
                query,
                tenant=case["tenant"],
                allowed=case["allowed"],
                embedder=embedder,
                policy=policy,
                source=case.get("source", ""),
                include_history=case.get("include_history", False),
                as_of=case.get("as_of"),
                limit=10,
            )
            assert outcome.result.ids == ids, (case, query)
            assert outcome.result.labels.as_dict() == labels, (case, query)
            # Observed rows only for providers that answered.
            assert set(outcome.observed) <= set(outcome.result.labels.providers)
            if outcome.result.ids:
                assert "fts" in outcome.observed or "hybrid" in outcome.observed


def test_gateway_gets_recall_from_its_runtime(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False, allowed_sources=("manual",))
    runtime = gateway.runtime
    assert isinstance(runtime, Runtime)
    assert runtime.allowed_sources == ("manual",) and runtime.tenant == ""
    calls: list[str] = []
    original = Runtime.recall

    def spy(self: Runtime, *args: Any, **kwargs: Any) -> RecallOutcome:
        calls.append(kwargs["policy_name"])
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Runtime, "recall", spy)
        gateway.search("homelab backup", mode="two_stage", policy="strict")
        gateway.search("homelab backup", mode="fts")
    assert calls == ["strict"]  # only two_stage goes through the runtime


def test_inflight_registry_is_shared_per_runtime(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    seen: list[InflightRegistry] = []
    original = Coordinator.__init__

    def record(self: Coordinator, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs["inflight"])
        original(self, *args, **kwargs)

    first = build_runtime(repository.root)
    second = build_runtime(repository.root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Coordinator, "__init__", record)
        for runtime in (first, first, second):
            runtime.recall(
                repository.root,
                "backup",
                limit=5,
                policy_name="compat_fts",
                source="",
                include_history=False,
                as_of=None,
                scope="",
            )
    assert seen[0] is seen[1] is first.recall_inflight
    assert seen[2] is second.recall_inflight
    assert first.recall_inflight is not second.recall_inflight
    gateway = Gateway(repository.root, tenant="", entity_enabled=False, allowed_sources=())
    assert gateway.runtime.recall_inflight is not first.recall_inflight


def test_runtime_is_l4_and_imports_no_interface() -> None:
    assert layers.UNITS["memfw.runtime"] == "L4-runtime"
    assert "memfw.runtime" in layers.RESTRICTED["memfw.mcp"]
    interfaces = {unit for unit, layer in layers.UNITS.items() if layer == "L5-interface"}
    found = [
        item
        for item in scan.scan_tree()
        if scan.unit_of(item.source, layers.UNITS) == "memfw.runtime"
        and item.target.startswith("memfw")
    ]
    assert found, "the scan saw no imports in memfw.runtime"
    for item in found:
        target = scan.unit_of(item.target, layers.UNITS)
        assert target not in interfaces, item
        assert target is not None
        assert layers.RANK[layers.UNITS[target]] <= layers.RANK["L4-runtime"], item


def test_importing_runtime_does_not_load_builtin_providers() -> None:
    code = (
        "import sys, memfw.runtime; "
        "print('memfw.recall.builtin' in sys.modules, 'memfw.mcp' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["False", "False"]
