"""C3: the ``memory_context`` cache (ids and versions only) and ``if_version``."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.context import if_version_request
from memfw.core.models import EventKind, Layer
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.runtime import build_runtime
from memfw.runtime import context_cache as cache_module
from memfw.runtime.context_cache import CachedIds, CacheKey, ContextCache, vault_fingerprint
from tests.conversation_fixtures import (
    isolated_home,  # noqa: F401  (fixture)
    standard_rows,
    write_store,
)

pytestmark = pytest.mark.usefixtures("isolated_home")

NOW = datetime(2026, 11, 16, tzinfo=UTC)
PRIVATE_BODY = "a body that becomes private after it was cached"
SHARE = {"enabled": True, "apps": {"grok": {"can_read": False}}}


def _vault(tmp_path: Path, share: dict[str, Any] | None = None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Anchor B", "Always cite sources.", memory_id="a-b", layer=Layer.ANCHOR)
    repository.add("Anchor A", "Prefer local tools.", memory_id="a-a", anchor="tools")
    repository.add("Proc", "1. Search 2. Read 3. Cite", memory_id="p-1", memory_type="procedural")
    repository.add("Semantic", "not resident", memory_id="s-1", memory_type="semantic")
    repository.add("Later private", PRIVATE_BODY, memory_id="a-later", layer=Layer.ANCHOR)
    repository.add(
        "Other tenant", "other tenant body", memory_id="a-other", layer=Layer.ANCHOR, tenant="beta"
    )
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    config = repository.read_config()
    config["conversation_store"] = {"path": str(store)}
    if share is not None:
        config["conversation_share"] = share
    repository.configure(config)
    return repository


def _rpc(gateway: Gateway, method: str, params: dict[str, Any]) -> dict[str, Any]:
    sent: list[Any] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle({"jsonrpc": "2.0", "id": 9, "method": method, "params": params})
    response: dict[str, Any] = sent[0]
    return response


def _call(gateway: Gateway, arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = _rpc(
        gateway, "tools/call", {"name": "memory_context", "arguments": arguments}
    )["result"]
    return result


def _ok(gateway: Gateway, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _call(gateway, arguments)
    assert not result["isError"], result
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def _text(gateway: Gateway, arguments: dict[str, Any]) -> str:
    result = _call(gateway, arguments)
    text: str = result["content"][0]["text"]
    return text


def _masked(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "generated_at": ""}


def _ids(payload: dict[str, Any], slot: str = "core") -> list[str]:
    found = next(item for item in payload["slots"] if item["name"] == slot)
    return [entry["id"] for entry in found["entries"]]


def _uncached(root: Path, **kwargs: Any) -> Gateway:
    gateway = Gateway(root, **kwargs)
    gateway.runtime.context_cache = None
    return gateway


class _ListCounter:
    """Counts ``Repository.list`` calls (a miss reads the whole vault, a hit does not)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = Repository.list

        def counting(repository: Repository) -> list[Any]:
            self.calls += 1
            return original(repository)

        monkeypatch.setattr(Repository, "list", counting)


# --- hit / miss ------------------------------------------------------------------


def test_second_call_is_a_hit_and_returns_the_same_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    counter = _ListCounter(monkeypatch)
    first = _ok(gateway, {"v": 2})
    second = _ok(gateway, {"v": 2})
    assert _masked(first) == _masked(second)
    assert gateway.runtime.context_cache is not None
    assert gateway.runtime.context_cache.stats() == {"hits": 1, "misses": 1, "size": 1}
    assert counter.calls == 1  # the hit never listed the vault
    assert _masked(first) == _masked(_ok(_uncached(repository.root), {"v": 2}))


@pytest.mark.parametrize(
    "arguments",
    [
        {"v": 2},
        {"v": 2, "policy": "strict"},
        {"v": 2, "limit_tokens": 7},
        {"v": 2, "limit_tokens": 12, "policy": "strict"},
        {"v": 2, "slots": ["core", "topics"]},
        {"v": 2, "slots": ["handoff", "core"], "policy": "strict"},
    ],
)
def test_cached_pack_equals_the_uncached_pack(tmp_path: Path, arguments: dict[str, Any]) -> None:
    repository = _vault(tmp_path, SHARE)
    cached = Gateway(repository.root, app="claude")
    plain = _uncached(repository.root, app="claude")
    expected = _masked(_ok(plain, arguments))
    for _ in range(3):
        assert _masked(_ok(cached, arguments)) == expected


def test_default_and_v1_output_is_byte_identical_with_and_without_cache(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    cached = Gateway(repository.root)
    plain = _uncached(repository.root)
    for arguments in ({}, {"limit_tokens": 40}, {"v": 1}):
        expected = _text(plain, arguments)
        assert _text(cached, arguments) == expected
        assert _text(cached, arguments) == expected
    assert "unchanged" not in _ok(cached, {"v": 2})


def test_v1_never_uses_the_cache(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _ok(gateway, {})
    _ok(gateway, {"v": 1})
    assert gateway.runtime.context_cache is not None
    assert gateway.runtime.context_cache.stats() == {"hits": 0, "misses": 0, "size": 0}


def test_cache_can_be_disabled_or_injected(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    assert build_runtime(repository.root, context_cache=False).context_cache is None
    assert build_runtime(repository.root, context_cache=None).context_cache is None
    shared = ContextCache(max_entries=4)
    assert build_runtime(repository.root, context_cache=shared).context_cache is shared
    assert isinstance(build_runtime(repository.root).context_cache, ContextCache)
    with pytest.raises(TypeError):
        build_runtime(repository.root, context_cache="yes")  # type: ignore[arg-type]
    runtime = build_runtime(repository.root, context_cache=False)
    pack = runtime.context_pack(repository, policy_name="compat_fts", limit_tokens=2000, now=NOW)
    assert [entry.id for entry in pack.slot("core").entries] == ["a-a", "a-b", "a-later", "p-1"]


# --- invalidation ----------------------------------------------------------------


def _warm(gateway: Gateway, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _ok(gateway, arguments or {"v": 2})
    assert _ok(gateway, arguments or {"v": 2})["pack_version"] == payload["pack_version"]
    return payload


def _stats(gateway: Gateway) -> dict[str, int]:
    assert gateway.runtime.context_cache is not None
    return gateway.runtime.context_cache.stats()


def _assert_fresh(gateway: Gateway, root: Path, arguments: dict[str, Any] | None = None) -> None:
    arguments = arguments or {"v": 2}
    assert _masked(_ok(gateway, arguments)) == _masked(_ok(_uncached(root), arguments))


def test_a_vault_write_invalidates(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    before = _warm(gateway)
    repository.add("New anchor", "new resident text", memory_id="a-new", layer=Layer.ANCHOR)
    after = _ok(gateway, {"v": 2})
    assert after["pack_version"] != before["pack_version"]
    assert "a-new" in _ids(after)
    assert _stats(gateway)["misses"] == 2
    _assert_fresh(gateway, repository.root)


def test_an_update_and_a_delete_invalidate(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    before = _warm(gateway)
    repository.update("a-b", body="Always cite two sources.")
    updated = _ok(gateway, {"v": 2})
    assert updated["pack_version"] != before["pack_version"]
    assert "two sources" in json.dumps(updated)
    repository.delete("p-1")
    deleted = _ok(gateway, {"v": 2})
    assert "p-1" not in _ids(deleted)
    _assert_fresh(gateway, repository.root)


def test_a_hand_placed_file_invalidates(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    text = (repository.memories_dir / "a-a.md").read_text(encoding="utf-8")
    (repository.memories_dir / "a-hand.md").write_text(
        text.replace("id: a-a", "id: a-hand"), encoding="utf-8"
    )
    assert "a-hand" in _ids(_ok(gateway, {"v": 2}))
    _assert_fresh(gateway, repository.root)


def test_an_in_place_edit_with_the_old_mtime_still_invalidates(tmp_path: Path) -> None:
    """A non-member edited by hand into an anchor, its mtime set back: ctime still moves."""

    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    path = repository.memories_dir / "s-1.md"
    stat = path.stat()
    text = path.read_text(encoding="utf-8")
    edited = text.replace("layer: current", "layer: anchor ")
    assert edited != text and len(edited) == len(text)
    path.write_text(edited, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert "s-1" in _ids(_ok(gateway, {"v": 2}))
    _assert_fresh(gateway, repository.root)


def test_an_event_alone_invalidates(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    before = vault_fingerprint(repository)
    repository.capture_event(
        EventKind.MEMORY_ACCESS, actor="mechanism", details={"memory_id": "a-a", "operation": "x"}
    )
    assert vault_fingerprint(repository) != before
    _ok(gateway, {"v": 2})
    assert _stats(gateway) == {"hits": 1, "misses": 2, "size": 1}


def test_a_config_change_invalidates(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    config = repository.read_config()
    config["conversation_share"] = SHARE
    repository.configure(config)
    _ok(gateway, {"v": 2})
    assert _stats(gateway)["misses"] == 2


def test_a_projection_rebuild_never_serves_a_stale_pack(tmp_path: Path) -> None:
    """The core slot reads memory files, not projections: a rebuild leaves it as a fresh build."""

    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    before = _warm(gateway)
    MemoryIndex(repository.index_path).rebuild(repository)
    after = _ok(gateway, {"v": 2})
    assert after["pack_version"] == before["pack_version"]
    _assert_fresh(gateway, repository.root)


def test_topics_and_handoff_are_never_cached(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    for _ in range(2):
        _ok(gateway, {"v": 2, "slots": ["core", "handoff", "topics"]})
    assert gateway.runtime.context_cache is not None
    assert {key.slot for key in gateway.runtime.context_cache.keys()} == {"core"}


def test_a_conversation_share_change_changes_the_handoff_version(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    arguments = {"v": 2, "slots": ["handoff"]}
    shared = _ok(gateway, arguments)
    assert _ids(shared, "handoff")
    unchanged = _ok(gateway, {**arguments, "if_version": shared["pack_version"]})
    assert unchanged["unchanged"] is True
    config = repository.read_config()
    config["conversation_share"] = {"enabled": True, "apps": {"claude": {"can_read": False}}}
    repository.configure(config)
    closed = _ok(gateway, {**arguments, "if_version": shared["pack_version"]})
    assert closed["unchanged"] is False
    assert _ids(closed, "handoff") == []
    assert closed["pack_version"] != shared["pack_version"]


def test_a_concept_change_changes_the_topics_version(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.update("s-1", about=["concept:backup"])
    concept = repository.root / "concepts" / "Concept" / "backup.md"
    concept.parent.mkdir(parents=True)
    concept.write_text(
        "---\noid: concept:backup\nclass: Concept\npref_label: Backup\nstatus: active\n---\n",
        encoding="utf-8",
    )
    gateway = Gateway(repository.root)
    arguments = {"v": 2, "slots": ["topics"], "policy": "strict"}
    first = _ok(gateway, arguments)
    concept.write_text(concept.read_text(encoding="utf-8").replace("Backup", "Backups"))
    second = _ok(gateway, {**arguments, "if_version": first["pack_version"]})
    assert second["unchanged"] is False and second["pack_version"] != first["pack_version"]


# --- re-filter on hit ----------------------------------------------------------------


def test_a_memory_made_private_after_caching_is_not_returned(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    assert "a-later" in _ids(_warm(gateway, {"v": 2, "policy": "strict"}))
    repository.update("a-later", tags=["privacy:local-private"])
    payload = _ok(gateway, {"v": 2, "policy": "strict"})
    assert "a-later" not in _ids(payload)
    assert PRIVATE_BODY not in json.dumps(payload)


def test_a_hit_rereads_and_refilters_even_if_the_fingerprint_missed_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memfw stays authoritative: the entry gives ids only, the vault decides what is shown."""

    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    monkeypatch.setattr(cache_module, "vault_fingerprint", lambda repository: "f" * 64)
    _warm(gateway, {"v": 2, "policy": "strict"})
    # Hand edits that keep each file's version, so the entry still matches.
    later = repository.memories_dir / "a-later.md"
    text = later.read_text(encoding="utf-8")
    assert "tags: []" in text
    later.write_text(text.replace("tags: []", "tags:\n- privacy:local-private"), encoding="utf-8")
    cite = repository.memories_dir / "a-b.md"
    text = cite.read_text(encoding="utf-8")
    cite.write_text(text.replace("Always cite sources.", "Cite primary sources."), encoding="utf-8")
    payload = _ok(gateway, {"v": 2, "policy": "strict"})
    assert _stats(gateway) == {"hits": 2, "misses": 1, "size": 1}
    assert "a-later" not in _ids(payload)
    assert PRIVATE_BODY not in json.dumps(payload)
    assert "Cite primary sources." in json.dumps(payload)
    assert _masked(payload) == _masked(
        _ok(_uncached(repository.root), {"v": 2, "policy": "strict"})
    )


def test_a_hit_with_a_changed_version_falls_back_to_a_full_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    monkeypatch.setattr(cache_module, "vault_fingerprint", lambda repository: "f" * 64)
    _warm(gateway)
    repository.update("a-b", body="Cite primary sources.")  # version 1 -> 2
    counter = _ListCounter(monkeypatch)
    _ok(gateway, {"v": 2})
    assert counter.calls == 1


def test_strict_is_judged_at_the_calls_own_time_on_a_hit(tmp_path: Path) -> None:
    """A memory valid only from a later date enters the slot on a hit once that date is reached."""

    repository = _vault(tmp_path)
    repository.update("a-later", valid_from="2027-01-01T00:00:00+00:00")
    runtime = build_runtime(repository.root)
    early = runtime.context_pack(repository, policy_name="strict", limit_tokens=2000, now=NOW)
    later = runtime.context_pack(
        repository,
        policy_name="strict",
        limit_tokens=2000,
        now=datetime(2027, 2, 1, tzinfo=UTC),
    )
    assert runtime.context_cache is not None
    assert runtime.context_cache.stats()["hits"] == 1
    assert "a-later" not in [entry.id for entry in early.slot("core").entries]
    assert "a-later" in [entry.id for entry in later.slot("core").entries]


def test_a_pending_transaction_still_raises_with_a_warm_cache(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    repository.pending_path.write_text("{}", encoding="utf-8")
    result = _call(gateway, {"v": 2})
    assert result["isError"]
    assert "memfw recover" in result["content"][0]["text"]


# --- tenant / policy separation -----------------------------------------------------


def test_tenants_and_policies_have_separate_entries(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    shared = ContextCache()
    alpha = build_runtime(repository.root, context_cache=shared)
    beta = build_runtime(repository.root, tenant="beta", context_cache=shared)
    for _ in range(2):
        a = alpha.context_pack(repository, policy_name="compat_fts", limit_tokens=2000, now=NOW)
        b = beta.context_pack(repository, policy_name="compat_fts", limit_tokens=2000, now=NOW)
        s = alpha.context_pack(repository, policy_name="strict", limit_tokens=2000, now=NOW)
        assert [entry.id for entry in a.slot("core").entries] == ["a-a", "a-b", "a-later", "p-1"]
        assert [entry.id for entry in b.slot("core").entries] == ["a-other"]
        assert s.pack_version != a.pack_version
    keys = shared.keys()
    assert {(key.tenant, key.policy, key.slot) for key in keys} == {
        ("", "compat_fts", "core"),
        ("beta", "compat_fts", "core"),
        ("", "strict", "core"),
    }
    by_tenant = {key.tenant: shared.get(key) for key in keys if key.policy == "compat_fts"}
    assert by_tenant[""] is not None and "a-other" not in by_tenant[""].ids
    assert by_tenant["beta"] is not None and by_tenant["beta"].ids == ("a-other",)
    assert shared.stats() == {"hits": 3, "misses": 3, "size": 3}


def test_allowed_sources_have_separate_entries(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add("Nas anchor", "nas text", memory_id="a-nas", layer=Layer.ANCHOR, source="nas")
    shared = ContextCache()
    every = build_runtime(repository.root, context_cache=shared)
    manual = build_runtime(repository.root, allowed_sources=("manual",), context_cache=shared)
    for _ in range(2):
        assert "a-nas" in [
            e.id
            for e in every.context_pack(
                repository, policy_name="compat_fts", limit_tokens=2000, now=NOW
            )
            .slot("core")
            .entries
        ]
        assert "a-nas" not in [
            e.id
            for e in manual.context_pack(
                repository, policy_name="compat_fts", limit_tokens=2000, now=NOW
            )
            .slot("core")
            .entries
        ]
    assert shared.stats()["size"] == 2


def test_an_entry_holds_ids_and_versions_only(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    _warm(gateway)
    cache = gateway.runtime.context_cache
    assert cache is not None
    (key,) = cache.keys()
    entry = cache.get(key)
    assert entry is not None
    assert entry.ids == ("a-a", "a-b", "a-later", "p-1")
    assert entry.versions == (1, 1, 1, 1)
    text = repr((key, entry))
    for document in repository.list():
        assert document.body not in text
        assert document.title not in text


# --- if_version --------------------------------------------------------------------


def test_if_version_unchanged_returns_no_slots(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    full = _ok(gateway, {"v": 2})
    same = _ok(gateway, {"v": 2, "if_version": full["pack_version"]})
    assert same == {
        "v": 2,
        "limit_tokens": 2000,
        "pack_version": full["pack_version"],
        "policy": "compat_fts",
        "generated_at": same["generated_at"],
        "unchanged": True,
    }
    other = _ok(gateway, {"v": 2, "if_version": "0" * 64})
    assert other["unchanged"] is False
    assert _masked({k: v for k, v in other.items() if k != "unchanged"}) == _masked(full)


def test_if_version_after_a_change_returns_the_new_pack(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    full = _ok(gateway, {"v": 2, "policy": "strict"})
    repository.update("a-later", tags=["privacy:local-private"])
    changed = _ok(gateway, {"v": 2, "policy": "strict", "if_version": full["pack_version"]})
    assert changed["unchanged"] is False
    assert changed["pack_version"] != full["pack_version"]
    assert "a-later" not in _ids(changed)


def test_if_version_is_validated_before_the_vault_is_read(tmp_path: Path) -> None:
    gateway = Gateway(tmp_path / "missing")
    for arguments, message in (
        ({"v": 1, "if_version": "0" * 64}, "if_version only applies to v=2"),
        ({"if_version": "0" * 64}, "if_version only applies to v=2"),
        ({"v": 2, "if_version": "ABC"}, "if_version must be a pack_version"),
        ({"v": 2, "if_version": "0" * 63}, "if_version must be a pack_version"),
        ({"v": 2, "if_version": 7}, "if_version must be a pack_version"),
    ):
        result = _call(gateway, arguments)
        assert result["isError"], arguments
        assert message in result["content"][0]["text"]
    assert if_version_request(2, None) is None
    assert if_version_request(2, "") is None
    assert if_version_request(1, None) is None


def test_resource_if_version(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)

    def read(uri: str) -> dict[str, Any]:
        response = _rpc(gateway, "resources/read", {"uri": uri})
        payload: dict[str, Any] = json.loads(response["result"]["contents"][0]["text"])
        return payload

    full = read("memfw://context/core")
    version = full["pack_version"]
    assert read(f"memfw://context/core?if_version={version}")["unchanged"] is True
    strict = read("memfw://context/core?policy=strict")
    both = read(f"memfw://context/core?policy=strict&if_version={strict['pack_version']}")
    assert both["unchanged"] is True and both["policy"] == "strict"
    swapped = read(f"memfw://context/core?if_version={strict['pack_version']}&policy=strict")
    assert swapped["unchanged"] is True
    stale = read("memfw://context/core?if_version=" + "0" * 64)
    assert stale["unchanged"] is False and stale["slots"]
    for query in (
        "if_version=",
        "if_version=xyz",
        f"if_version={version}&if_version={version}",
        "policy=strict&policy=strict",
        f"if_version={version}&other=1",
        "policy=loose",
        f"if_version={version}&",
    ):
        response = _rpc(gateway, "resources/read", {"uri": f"memfw://context/core?{query}"})
        assert response["error"] == {"code": -32602, "message": "invalid params"}, query


# --- the cache itself -----------------------------------------------------------------


def _key(n: int) -> CacheKey:
    return CacheKey(tenant="", policy="compat_fts", slot="core", scope=(f"/v{n}", ()))


def test_the_cache_is_bounded_lru() -> None:
    cache = ContextCache(max_entries=2)
    entry = CachedIds(fingerprint="f", ids=("a",), versions=(1,))
    cache.put(_key(1), entry)
    cache.put(_key(2), entry)
    assert cache.get(_key(1)) is entry  # 1 is now the most recent
    cache.put(_key(3), entry)
    assert cache.get(_key(2)) is None
    assert cache.get(_key(1)) is entry and cache.get(_key(3)) is entry
    assert cache.stats()["size"] == 2
    cache.discard(_key(1))
    cache.clear()
    assert cache.stats()["size"] == 0
    for bad in (0, -1, 1.5):
        with pytest.raises(ValueError):
            ContextCache(max_entries=bad)  # type: ignore[arg-type]


def test_concurrent_calls_agree(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    runtime = build_runtime(repository.root, context_cache=ContextCache(max_entries=1))
    expected = runtime.context_pack(
        repository, policy_name="compat_fts", limit_tokens=2000, now=NOW
    ).pack_version
    seen: list[str] = []
    errors: list[BaseException] = []

    def work(policy: str) -> None:
        try:
            for _ in range(20):
                pack = runtime.context_pack(
                    Repository(repository.root), policy_name=policy, limit_tokens=2000, now=NOW
                )
                if policy == "compat_fts":
                    seen.append(pack.pack_version)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=("compat_fts" if n % 2 else "strict",)) for n in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert set(seen) == {expected}
    assert runtime.context_cache is not None and runtime.context_cache.stats()["size"] == 1


def test_a_vault_without_memories_dir(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    runtime = build_runtime(repository.root)
    (repository.memories_dir).rename(tmp_path / "moved")
    assert vault_fingerprint(repository) is not None  # absent is a state, not an error
    pack = runtime.context_pack(repository, policy_name="compat_fts", limit_tokens=2000, now=NOW)
    assert pack.slot("core").entries == ()
