"""``memfw.runtime`` (A3a, A3b): the composition root owns the recall wiring and the opener."""

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
    RECALL_POLICIES,
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
                repository,
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
                repository,
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


def _recall(runtime: Runtime, policy_name: str) -> RecallOutcome:
    return runtime.recall(
        Repository(runtime.root),
        "backup",
        limit=5,
        policy_name=policy_name,
        source="",
        include_history=False,
        as_of=None,
        scope="",
    )


@pytest.mark.parametrize("name", ["", "bogus", "STRICT", "compat-fts", None])
def test_recall_fails_closed_on_an_unknown_policy(tmp_path: Path, name: Any) -> None:
    repository = _vault(tmp_path)
    runtime = build_runtime(repository.root)
    calls: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            Runtime,
            "_two_stage_providers",
            lambda self, *a, **k: calls.append("built") or ([], {}),
        )
        with pytest.raises(ValueError, match="unknown policy"):
            _recall(runtime, name)
    assert calls == []  # rejected before any provider is built
    assert RECALL_POLICIES == ("compat_fts", "strict")
    for good in RECALL_POLICIES:
        assert isinstance(_recall(runtime, good), RecallOutcome)


def test_mcp_keeps_its_policy_messages(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False, allowed_sources=())
    with pytest.raises(ValueError, match="^unknown policy$"):
        gateway.search("backup", mode="two_stage", policy="bogus")
    with pytest.raises(ValueError, match="^policy only applies to mode=two_stage$"):
        gateway.search("backup", mode="fts", policy="strict")


def test_runtime_root_is_the_configured_path(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repository.root, target_is_directory=True)
    for root in (repository.root, link, tmp_path / "vault" / ".." / "vault"):
        gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=())
        # Configured, not resolved: the vault the caller opens per call decides.
        assert gateway.runtime.root == gateway.root == Path(root)
        assert gateway._repo().root == repository.root


def _two_vaults(tmp_path: Path) -> tuple[Path, Path]:
    for name in ("a", "b"):
        other = Repository(tmp_path / name)
        other.init()
        other.add(f"{name} backup vault", f"backup body {name}", memory_id=f"m-{name}")
    link = tmp_path / "current"
    link.symlink_to(tmp_path / "a", target_is_directory=True)
    return link, tmp_path / "b"


def test_symlink_retarget_fts_and_two_stage_agree(tmp_path: Path) -> None:
    link, target_b = _two_vaults(tmp_path)
    gateway = Gateway(link, tenant="", entity_enabled=False, allowed_sources=())

    def ids(mode: str) -> list[str]:
        return [row["id"] for row in gateway.search("backup", mode=mode)["results"]]

    assert ids("fts") == ids("two_stage") == ["m-a"]
    link.unlink()
    link.symlink_to(target_b, target_is_directory=True)
    assert ids("fts") == ids("two_stage") == ["m-b"]
    # The runtime itself follows the caller's repository, not a pinned path.
    outcome = _recall(gateway.runtime, "compat_fts")
    assert outcome.result.ids == ["m-b"]


def test_ensure_index_refuses_a_missing_vault(tmp_path: Path) -> None:
    for root in (tmp_path / "missing", tmp_path / "empty"):
        if root.name == "empty":
            root.mkdir()  # a directory without vault.yaml is not a vault
        runtime = build_runtime(root)
        with pytest.raises(ValueError, match="^vault root does not exist$"):
            runtime.ensure_index()
        assert not (root / "vault.yaml").exists()
        assert not (root / "index.sqlite3").exists()
    assert not (tmp_path / "missing").exists()


def test_timeouts_are_runtime_parameters(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    default = build_runtime(repository.root)
    assert (default.provider_timeout, default.total_timeout) == (3.0, 4.0)
    tuned = build_runtime(repository.root, provider_timeout=0.5, total_timeout=1.25)
    assert (tuned.provider_timeout, tuned.total_timeout) == (0.5, 1.25)
    budgets: list[float] = []
    totals: list[float] = []
    original = Coordinator.__init__

    def record(self: Coordinator, *args: Any, **kwargs: Any) -> None:
        budgets.extend(c.budget.timeout_seconds for c in kwargs["configs"].values())
        totals.append(kwargs["total_timeout"])
        original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Coordinator, "__init__", record)
        _recall(tuned, "compat_fts")
        _recall(default, "compat_fts")
    assert budgets == [0.5, 3.0] and totals == [1.25, 4.0]


def test_ensure_index_rebuilds_a_missing_index(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    runtime = build_runtime(repository.root)
    repository.index_path.unlink()
    runtime.ensure_index()  # opens Repository(runtime.root)
    assert repository.index_path.is_file()
    before = repository.index_path.stat().st_mtime_ns
    runtime.ensure_index(repository)  # fresh: no rebuild
    assert repository.index_path.stat().st_mtime_ns == before


def test_mcp_checks_the_index_through_the_runtime_before_recall(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False, allowed_sources=())
    order: list[str] = []
    ensure, recall = Runtime.ensure_index, Runtime.recall
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            Runtime, "ensure_index", lambda self, *a: order.append("index") or ensure(self, *a)
        )
        patch.setattr(
            Runtime, "recall", lambda self, *a, **k: order.append("recall") or recall(self, *a, **k)
        )
        gateway.search("homelab backup", mode="two_stage")
    assert order == ["index", "recall"]


# --- A3b: settings and the vault opener come from the Runtime ----------------


def test_resolve_settings_reads_the_environment_unless_given() -> None:
    from memfw.notes import HttpEmbeddingProvider
    from memfw.runtime import resolve_settings

    env = {
        "MEMFW_APP": " kimi ",
        "MEMFW_TENANT": "alpha",
        "MEMFW_ENTITY_ENABLED": "Yes",
        "MEMFW_ALLOWED_SOURCES": "nas, ,manual",
        "MEMFW_EMBEDDING_URL": "http://127.0.0.1:9/v1/embeddings",
        "MEMFW_EMBEDDING_DIMENSION": "8",
    }
    settings = resolve_settings(environ=env)
    assert (settings.app, settings.tenant, settings.entity_enabled) == ("kimi", "alpha", True)
    assert settings.allowed_sources == ("nas", "manual")
    assert isinstance(settings.embedder, HttpEmbeddingProvider)
    explicit = resolve_settings(
        tenant="", entity_enabled=False, allowed_sources=(), app="", environ=env, embedder="e"
    )
    assert (explicit.app, explicit.tenant, explicit.entity_enabled) == ("", "", False)
    assert (explicit.allowed_sources, explicit.embedder) == ((), "e")
    assert resolve_settings(environ={}).embedder is None


def test_open_repository_refuses_a_missing_vault_and_never_creates_one(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path / "missing")
    with pytest.raises(ValueError, match="^vault root does not exist$"):
        runtime.open_repository()
    assert not (tmp_path / "missing").exists()
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="^vault root does not exist$"):
        build_runtime(tmp_path / "empty").open_repository()
    assert list((tmp_path / "empty").iterdir()) == []


def test_open_repository_follows_a_moved_symlink_per_call(tmp_path: Path) -> None:
    link, target_b = _two_vaults(tmp_path)
    runtime = build_runtime(link)
    assert runtime.open_repository().root == (tmp_path / "a").resolve()
    link.unlink()
    link.symlink_to(target_b, target_is_directory=True)
    assert runtime.open_repository().root == target_b.resolve()
    assert runtime.root == link


def test_runtime_search_checks_arguments_before_opening_the_vault(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path / "missing", allowed_sources=("nas",))
    cases: list[tuple[dict[str, Any], str]] = [
        ({"query": "  "}, "query required"),
        ({"query": "x", "policy": "bogus"}, "unknown policy"),
        (
            {"query": "x", "mode": "fts", "policy": "strict"},
            "policy only applies to mode=two_stage",
        ),
        ({"query": "x", "scope": 3}, "scope must be a string"),
        ({"query": "x", "scope": "s" * 513}, "scope too long"),
        ({"query": "x", "source": "manual"}, "source is not allowed for this account"),
        ({"query": "x", "policy": None, "scope": None}, "vault root does not exist"),
    ]
    for arguments, message in cases:
        with pytest.raises(ValueError, match=f"^{message}$"):
            runtime.search(**arguments)


def test_gateway_settings_are_read_only_views_of_the_runtime(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, tenant="t", entity_enabled=True, allowed_sources=("a",))
    runtime = gateway.runtime
    assert (gateway.tenant, gateway.allowed_sources) == (runtime.tenant, runtime.allowed_sources)
    assert (gateway.embedder, gateway.entity_enabled) == (runtime.embedder, True)
    with pytest.raises(AttributeError):
        gateway.tenant = "other"  # type: ignore[misc]
