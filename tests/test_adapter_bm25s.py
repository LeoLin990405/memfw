"""memfw-adapter-bm25s: in-process bm25s recall, wired through ``runtime.recall.adapters``.

The adapter lives in ``packages/adapters/memfw-adapter-bm25s`` and imports only
``memfw.ports``; memfw reaches it only through the operator's ``module:factory``
setting. Everything here runs offline on temporary vaults under a temporary
HOME (the index's default location).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.ports import (
    AdapterRegistry,
    AdaptersConfig,
    Availability,
    RecallBudget,
    RecallProvider,
)
from memfw.ports.testing import check_recall_provider, tree_digest
from memfw.recall import Coordinator, FilterPolicy
from memfw.recall.builtin import FtsProvider, default_resolvers
from memfw.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_SRC = ROOT / "packages" / "adapters" / "memfw-adapter-bm25s" / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member; CI installs only memfw[dev]
    sys.path.insert(0, str(ADAPTER_SRC))
# Its third-party dependencies are not part of a default install: skip without them.
pytest.importorskip("bm25s")
pytest.importorskip("jieba")

from memfw_adapter_bm25s import SPEC, Bm25sProvider, factory, store  # noqa: E402

ALL_LEVELS = "public,shared,local-private"
QUERIES = ("homelab backup", "HAL9000", "迁移 备份", "release notes")


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    return home


def _vault(tmp_path: Path, *, legacy: str | None = "public") -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    if legacy is not None:
        path = repository.root / "vault.yaml"
        path.write_text(
            path.read_text(encoding="utf-8") + f"legacy_privacy_default: {legacy}\n",
            encoding="utf-8",
        )
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("迁移计划", "迁移之前先暂停定时任务，再做备份", memory_id="m-zh")
    add(
        "Private backup",
        "private backup keys zebrafish",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    add(
        "Shared backup",
        "shared fleet backup schedule",
        memory_id="m-shared",
        tags=["privacy:shared"],
    )
    add("Odd backup", "odd backup marker", memory_id="m-odd", tags=["privacy:whatever"])
    add("Alpha backup", "tenant alpha homelab backup notes", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _search(provider: Bm25sProvider, query: str, tenant: str = "", limit: int = 10) -> list[str]:
    found = provider.search(query, limit=limit, budget=RecallBudget(), tenant=tenant, as_of=None)
    return [candidate.ref for candidate in found]


# --- the contract -----------------------------------------------------------------


def test_contract_kit_passes_and_the_vault_is_untouched(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    tenants = {doc.id: doc.tenant for doc in repository.list()}
    provider = factory({"vault": str(repository.root)})
    assert isinstance(provider, RecallProvider)
    assert provider.lexical is True and provider.name == "bm25s"
    seen = check_recall_provider(
        provider,
        queries=QUERIES,
        limit=5,
        vault=repository.root,
        vault_exclude=(".memfw.lock",),
        tenant_of=lambda candidate: tenants.get(candidate.ref),
        error_queries=(),
    )
    assert seen["HAL9000"][0].ref == "m-hal"
    assert [c.ref for c in seen["迁移 备份"]] == ["m-zh"]  # jieba segments the Chinese text
    assert all(c.extra == {"why": "lexical"} for hits in seen.values() for c in hits)
    alpha = check_recall_provider(
        provider,
        queries=["homelab backup"],
        tenant="alpha",
        tenant_of=lambda candidate: tenants.get(candidate.ref),
    )
    assert [c.ref for c in alpha["homelab backup"]] == ["m-alpha"]
    check_recall_provider(provider, queries=["a"], error_queries=["x" * 600])


def test_tenant_is_exact_and_applied_before_the_page_is_cut(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = factory({"vault": str(repository.root)})
    assert "m-alpha" not in _search(provider, "tenant alpha homelab backup notes")
    assert _search(provider, "tenant alpha homelab backup notes", tenant="alpha") == ["m-alpha"]
    assert _search(provider, "homelab", tenant="alph") == []
    # limit 1 in tenant alpha still finds alpha's memory, though others score higher.
    assert _search(provider, "homelab backup", tenant="alpha", limit=1) == ["m-alpha"]


def test_privacy_default_indexes_public_and_shared_only(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = factory({"vault": str(repository.root)})
    status = provider.status()
    # m-priv (sensitivity:private) and m-odd (unknown privacy tag) are never indexed.
    assert status["skipped"] == {"unreadable": 0, "privacy": 2}
    assert status["documents"] == 6
    assert _search(provider, "zebrafish") == []
    assert "m-odd" not in _search(provider, "odd backup marker")
    assert "m-shared" in _search(provider, "shared fleet backup")
    wider = factory({"vault": str(repository.root), "clearance": ALL_LEVELS})
    assert _search(wider, "zebrafish") == ["m-priv"]
    assert "m-odd" not in _search(wider, "odd backup marker")  # unknown: never admitted


def test_legacy_privacy_default_decides_memories_without_a_privacy_field(tmp_path: Path) -> None:
    repository = _vault(tmp_path, legacy=None)  # unset: local-private
    provider = factory({"vault": str(repository.root)})
    assert _search(provider, "homelab backup") == []
    # A tag can only make a memory stricter: privacy:shared on a local-private one stays out.
    assert _search(provider, "shared fleet backup") == []
    assert provider.status()["documents"] == 0


@pytest.mark.parametrize(
    "lines",
    [
        "legacy_privacy_default: public\n",
        "legacy_privacy_default: 'shared'\n",
        "legacy_privacy_default: public\n  local-private\n",  # a multi-line plain scalar
        "legacy_privacy_default: public\nlegacy_privacy_default: local-private\n",
        "x: [a,\nlegacy_privacy_default: public]\n",  # inside a flow sequence
        "legacy_privacy_default: |\n  public\n",
        "legacy_privacy_default: public # note\n",
        "legacy_privacy_default: PUBLIC\n",
        "legacy_privacy_default: [public]\n",
        "legacy_privacy_default: !!str public\n",
        "legacy_privacy_default: public\n---\nother: 1\n",
        "{legacy_privacy_default: public}\n",
        "",
    ],
)
def test_legacy_default_is_read_exactly_as_memfw_reads_it(tmp_path: Path, lines: str) -> None:
    from memfw.index.readonly import vault_legacy_privacy

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "vault.yaml").write_text(lines, encoding="utf-8")
    assert store.legacy_privacy(vault) == vault_legacy_privacy(vault), lines
    if "local-private" in lines or "[" in lines or "---" in lines:
        assert store.legacy_privacy(vault) == "local-private", lines


def test_index_lives_outside_the_vault_privately_and_holds_no_words(
    tmp_path: Path, _home: Path
) -> None:
    repository = _vault(tmp_path)
    before = tree_digest(repository.root, exclude=(".memfw.lock",))
    provider = factory({"vault": str(repository.root), "clearance": ALL_LEVELS})
    assert _search(provider, "homelab backup")
    assert tree_digest(repository.root, exclude=(".memfw.lock",)) == before
    directory = provider.index_dir()
    assert directory.parent == _home / ".local" / "share" / "memfw" / "adapters" / "bm25s"
    assert directory.name == store.vault_key(repository.root)
    builds = list(directory.glob("idx-*"))
    assert len(builds) == 1
    for path in [directory, *builds]:
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700, path
    # Words of the bodies (ids such as m-backup are expected in docs.json).
    words = ("homelab", "zebrafish", "hal9000", "迁移", "备份", "nightly", "rack")
    for path in builds[0].iterdir():
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600, path
        raw = path.read_bytes()
        for word in words:
            assert word.encode("utf-8") not in raw.lower(), (path.name, word)
    assert not (builds[0] / "corpus.jsonl").exists()


def test_index_location_follows_memfws_local_data_rule(tmp_path: Path, _home: Path) -> None:
    """The adapter mirrors ``memfw.core.local_data`` (it may import only the ports)."""

    from memfw.core import local_data

    repository = _vault(tmp_path)
    assert store.vault_key(repository.root) == local_data.vault_key(repository.root)
    assert store.default_data_dir() == local_data.projection_dir("adapters") / "bm25s"


def test_vault_changes_rebuild_and_a_fresh_provider_loads_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    provider = factory({"vault": str(repository.root)})
    assert _search(provider, "quokka") == []
    first = provider.status()["fingerprint"]
    repository.add("Quokka", "the quokka migration plan", memory_id="m-quokka")
    assert _search(provider, "quokka") == ["m-quokka"]
    second = provider.status()["fingerprint"]
    assert second != first
    assert [p.name for p in provider.index_dir().glob("idx-*")] == [f"idx-{second}"]

    def no_rebuild(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a saved build must be loaded, not rebuilt")

    monkeypatch.setattr(store, "read_corpus", no_rebuild)
    fresh = factory({"vault": str(repository.root)})
    assert _search(fresh, "quokka") == ["m-quokka"]
    assert fresh.projection_version == f"bm25s-{second[:16]}"


def test_settings_are_checked(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = str(repository.root)
    for bad, message in (
        ({}, "needs a vault"),
        ({"vault": root, "colour": "x"}, "unknown bm25s settings"),
        ({"vault": root, "clearance": "public,secret"}, "clearance"),
        ({"vault": root, "clearance": ""}, "clearance"),
        ({"vault": root, "clearance": ["public"]}, "clearance"),
        ({"vault": root, "k1": "1"}, "k1"),
        ({"vault": root, "b": 2}, "b must be"),
        ({"vault": root, "name": "Bad Name"}, "provider"),
    ):
        with pytest.raises(ValueError, match=message):
            factory(bad)
    assert factory({"vault": root, "embedder": object()}).name == "bm25s"  # injected, ignored


def test_unavailable_without_a_vault_and_refuses_a_symlinked_data_dir(tmp_path: Path) -> None:
    assert factory({"vault": str(tmp_path / "nowhere")}).available() == Availability.missing(
        "no_vault"
    )
    repository = _vault(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / store.vault_key(repository.root)).symlink_to(target)
    provider = factory({"vault": str(repository.root), "data_dir": str(data)})
    with pytest.raises(store.StoreError):
        provider.search("homelab", limit=5, budget=RecallBudget(), tenant="", as_of=None)
    assert list(target.iterdir()) == []


def test_registry_spec_is_off_until_enabled(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    registry = AdapterRegistry()
    registry.register(SPEC)
    assert SPEC.license == "MIT" and SPEC.version == "0.3.16"
    assert registry.load("bm25s", AdaptersConfig()).reason == "disabled"
    config = AdaptersConfig.from_mapping(
        {"enabled": ["bm25s"], "bm25s": {"vault": str(repository.root)}}
    )
    status = registry.load("bm25s", config)
    assert status.available and isinstance(status.adapter, Bm25sProvider)


# --- the coordinator and the runtime -------------------------------------------------


def test_coordinator_with_bm25s_in_place_of_fts(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    resolvers = default_resolvers(root)
    swapped = Coordinator(
        [factory({"vault": str(root), "clearance": ALL_LEVELS})], resolvers=resolvers
    )
    policy = FilterPolicy()
    backup = swapped.search("homelab backup", limit=10, policy=policy)
    assert backup.labels.providers == ("bm25s",) and not backup.labels.degraded
    assert backup.ids[0] == "m-backup"
    assert "m-alpha" not in backup.ids and "m-priv" not in backup.ids
    assert "m-priv" not in swapped.search("zebrafish", limit=10, policy=policy).ids
    fused = Coordinator([FtsProvider(root), factory({"vault": str(root)})], resolvers=resolvers)
    result = fused.search("homelab backup power", limit=10, policy=policy)
    assert result.labels.providers == ("fts", "bm25s")
    assert any(hit.providers == ("fts", "bm25s") for hit in result.hits)


def _configure(repository: Repository, adapters: Any) -> None:
    path = repository.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "runtime:\n  recall:\n    adapters: "
        + json.dumps(adapters, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _two_stage(runtime: Any, query: str = "homelab backup") -> dict[str, Any]:
    return runtime.search(query, limit=10, mode="two_stage")  # type: ignore[no-any-return]


def test_runtime_wires_bm25s_through_module_factory(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _configure(
        repository,
        [{"factory": "memfw_adapter_bm25s:factory", "weight": 0.5, "timeout_seconds": 5}],
    )
    runtime = build_runtime(repository.root, record_access=False)
    payload = _two_stage(runtime)
    assert payload["recall"]["providers"] == ["fts", "bm25s"]
    assert not payload["degraded"]
    ids = [row["id"] for row in payload["results"]]
    assert ids[0] == "m-backup" and "m-alpha" not in ids
    assert any(row["providers"] == ["fts", "bm25s"] for row in payload["results"])
    loaded = runtime.recall_adapters(repository)
    assert [adapter.provider.name for adapter in loaded] == ["bm25s"]
    assert runtime.recall_adapters(repository) is loaded  # built once per Runtime
    # The loader itself (codes, degrade, defaults) is covered without bm25s in
    # tests/test_runtime_recall_adapters.py.
