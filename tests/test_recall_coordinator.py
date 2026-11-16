"""Coordinator: timeouts, degraded providers, fail-closed resolve/filter, labels, metrics."""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.ports.types import Availability, Candidate, ProviderUnavailable, RecallBudget
from memfw.recall import (
    Coordinator,
    FilterPolicy,
    MemoryMeta,
    MemoryPathResolver,
    ProviderConfig,
    RecallMetrics,
    Resolution,
    Resolver,
)
from memfw.recall.builtin import VaultIdResolver, default_resolvers

SECRET_QUERY = "what is the private launch codename zebra-7"


class FakeProvider:
    def __init__(
        self,
        name: str,
        refs: Sequence[str] = (),
        *,
        ref_kind: str = "memory_id",
        lexical: bool = False,
        delay: float = 0.0,
        block: threading.Event | None = None,
        error: BaseException | None = None,
        availability: Availability | None = None,
        result: object | None = None,
        projection_version: str = "",
    ) -> None:
        self.name = name
        self.refs = list(refs)
        self.ref_kind = ref_kind
        self.lexical = lexical
        self.delay = delay
        self.block = block
        self.error = error
        self.availability = availability or Availability.ok()
        self.result = result
        self.projection_version = projection_version
        self.calls: list[dict[str, object]] = []

    def available(self) -> Availability:
        return self.availability

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        self.calls.append({"limit": limit, "tenant": tenant, "as_of": as_of, "budget": budget})
        if self.block is not None:
            self.block.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result  # type: ignore[return-value]
        return [
            Candidate(
                ref=ref,
                ref_kind=self.ref_kind,  # type: ignore[arg-type]
                score=1.0 / (index + 1),
                provider=self.name,
                rank=index + 1,
                extra={"why": "fake"},
            )
            for index, ref in enumerate(self.refs[:limit])
        ]


class MetaResolver:
    """Test double: resolves ids from a fixed metadata map."""

    def __init__(self, metas: Mapping[str, MemoryMeta | None]) -> None:
        self.metas = dict(metas)
        self.calls: list[list[str]] = []

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        self.calls.append(list(refs))
        return {ref: Resolution(ref, self.metas[ref]) for ref in refs if ref in self.metas}


class MappingPathResolver:
    """Test double for the R6 PR-1 path resolver (``files/<rel>`` → id)."""

    def __init__(self, paths: Mapping[str, str], ids: Resolver) -> None:
        self.paths = dict(paths)
        self.ids = ids

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        wanted = {ref: self.paths[ref] for ref in refs if ref in self.paths}
        found = self.ids.resolve(sorted(set(wanted.values())))
        return {ref: found[mid] for ref, mid in wanted.items() if mid in found}


class RaisingResolver:
    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        raise RuntimeError(SECRET_QUERY)


def _meta(memory_id: str, **changes: object) -> MemoryMeta:
    values: dict[str, object] = {"memory_id": memory_id, "tenant": "", "source": "manual"}
    values.update(changes)
    return MemoryMeta(**values)  # type: ignore[arg-type]


def _metas(*ids: str) -> dict[str, MemoryMeta | None]:
    return {memory_id: _meta(memory_id) for memory_id in ids}


def _coordinator(
    providers: Sequence[FakeProvider],
    metas: Mapping[str, MemoryMeta | None] | None = None,
    **kwargs: object,
) -> Coordinator:
    resolver = MetaResolver(metas if metas is not None else _metas(*"abcdefghij"))
    resolvers = kwargs.pop("resolvers", {"memory_id": resolver})
    return Coordinator(providers, resolvers=resolvers, **kwargs)  # type: ignore[arg-type]


POLICY = FilterPolicy()


def test_fuses_two_providers_and_labels_them() -> None:
    coordinator = _coordinator(
        [FakeProvider("fts", ["a", "b"], lexical=True), FakeProvider("qmd", ["b", "c"])]
    )
    result = coordinator.search("memory design notes overall", limit=10, policy=POLICY)
    assert result.ids == ["b", "a", "c"]
    assert result.labels.providers == ("fts", "qmd")
    assert result.labels.degraded == {}
    assert not result.labels.fail_closed
    assert result.hits[0].providers == ("fts", "qmd")
    assert result.hits[0].provider_scores == {"fts": 0.5, "qmd": 1.0}
    assert result.hits[0].extra == {"why": "fake"}
    assert result.metrics.returned == 3


def test_provider_receives_tenant_as_of_and_budget() -> None:
    provider = FakeProvider("fts", ["a"])
    budget = RecallBudget(timeout_seconds=0.7, max_candidates=5)
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    coordinator = _coordinator(
        [provider],
        {"a": _meta("a", tenant="alpha")},
        configs={"fts": ProviderConfig(budget=budget, overfetch=3)},
    )
    result = coordinator.search(
        "memory design", limit=4, policy=FilterPolicy(tenant="alpha", as_of=as_of)
    )
    assert result.ids == ["a"]
    call = provider.calls[0]
    assert call["tenant"] == "alpha"
    assert call["as_of"] == as_of
    assert call["budget"] == budget
    assert call["limit"] == 5  # min(4 * 3, max_candidates)


def test_max_candidates_truncates_provider_output() -> None:
    provider = FakeProvider("fts", list("abcdef"))
    provider.result = provider.search("x", limit=6, budget=RecallBudget(), tenant="", as_of=None)
    coordinator = _coordinator(
        [provider], configs={"fts": ProviderConfig(budget=RecallBudget(max_candidates=2))}
    )
    result = coordinator.search("memory design", limit=10, policy=POLICY)
    assert result.ids == ["a", "b"]
    assert result.metrics.providers[0].candidates == 6


def test_slow_provider_times_out_and_others_still_answer() -> None:
    release = threading.Event()
    slow = FakeProvider("qmd", ["c"], block=release)
    fast = FakeProvider("fts", ["a"])
    coordinator = _coordinator(
        [fast, slow],
        configs={"qmd": ProviderConfig(budget=RecallBudget(timeout_seconds=0.05))},
    )
    started = time.monotonic()
    try:
        result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    finally:
        release.set()
    assert time.monotonic() - started < 2.0
    assert result.ids == ["a"]
    assert result.labels.degraded == {"qmd": "timeout"}
    assert result.labels.providers == ("fts",)
    report = {item.name: item for item in result.metrics.providers}
    assert report["qmd"].status == "timeout"
    assert report["fts"].status == "ok"


def test_total_deadline_bounds_every_provider() -> None:
    release = threading.Event()
    slow = FakeProvider("qmd", ["c"], block=release)
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"]), slow],
        configs={"qmd": ProviderConfig(budget=RecallBudget(timeout_seconds=30))},
        total_timeout=0.1,
    )
    started = time.monotonic()
    try:
        result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    finally:
        release.set()
    assert time.monotonic() - started < 2.0
    assert result.labels.degraded == {"qmd": "timeout"}


def test_raising_provider_is_degraded_without_leaking_its_message() -> None:
    broken = FakeProvider("qmd", error=ValueError(f"bad query {SECRET_QUERY}"))
    coordinator = _coordinator([FakeProvider("fts", ["a"]), broken])
    result = coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert result.ids == ["a"]
    assert result.labels.degraded == {"qmd": "error:exception"}
    dumped = json.dumps([result.metrics.as_dict(), result.labels.as_dict()])
    assert "zebra" not in dumped
    assert "codename" not in dumped


def test_provider_unavailable_exception_and_availability() -> None:
    raising = FakeProvider("hybrid", error=ProviderUnavailable("fell_back_to_fts"))
    missing = FakeProvider("vector", availability=Availability.missing("no embedder!"))
    coordinator = _coordinator([FakeProvider("fts", ["a"]), raising, missing])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["a"]
    assert result.labels.degraded == {
        "hybrid": "unavailable",
        "vector": "unavailable",  # "no embedder!" is not a code: replaced
    }
    assert missing.calls == []


def test_availability_that_raises_is_unavailable() -> None:
    provider = FakeProvider("qmd", ["b"])

    def explode() -> Availability:
        raise RuntimeError("boom")

    provider.available = explode  # type: ignore[method-assign]
    coordinator = _coordinator([FakeProvider("fts", ["a"]), provider])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.labels.degraded == {"qmd": "unavailable:availability_error"}


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ("abc", "invalid:not_a_sequence"),
        (42, "invalid:not_a_sequence"),
        ([{"id": "a"}], "invalid:no_valid_candidates"),
        (
            [Candidate(ref="a", ref_kind="memory_id", score=1.0, provider="other", rank=1)],
            "invalid:no_valid_candidates",
        ),
    ],
)
def test_malformed_output_is_invalid(result: object, reason: str) -> None:
    coordinator = _coordinator([FakeProvider("fts", ["a"]), FakeProvider("qmd", result=result)])
    out = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert out.labels.degraded == {"qmd": reason}
    assert out.ids == ["a"]


def test_partly_malformed_output_keeps_the_valid_candidates() -> None:
    good = Candidate(ref="b", ref_kind="memory_id", score=1.0, provider="qmd", rank=1)
    coordinator = _coordinator([FakeProvider("qmd", result=[{"id": "x"}, good])])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["b"]
    assert result.metrics.providers[0].invalid == 1


def test_empty_list_is_ok_not_degraded() -> None:
    coordinator = _coordinator([FakeProvider("fts", [])])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == []
    assert not result.labels.fail_closed
    assert result.labels.degraded == {}


def test_all_providers_failing_fails_closed() -> None:
    coordinator = _coordinator(
        [
            FakeProvider("fts", error=RuntimeError("x")),
            FakeProvider("qmd", availability=Availability.missing("down")),
        ]
    )
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.hits == ()
    assert result.labels.fail_closed
    assert result.labels.fallback_reason == "all_providers_failed"


def test_exact_route_falls_back_when_lexical_fails() -> None:
    fts = FakeProvider("fts", error=RuntimeError("index gone"), lexical=True)
    qmd = FakeProvider("qmd", ["b"])
    coordinator = _coordinator([fts, qmd])
    result = coordinator.search('"exact phrase"', limit=5, policy=POLICY)
    assert result.labels.route == "exact"
    assert result.ids == ["b"]
    assert result.labels.fallback_reason == "routed_providers_failed"
    assert result.labels.degraded == {"fts": "error:exception"}


def test_exact_route_uses_only_lexical_providers() -> None:
    fts = FakeProvider("fts", ["a"], lexical=True)
    qmd = FakeProvider("qmd", ["b"])
    coordinator = _coordinator([qmd, fts])
    result = coordinator.search('"exact phrase"', limit=5, policy=POLICY)
    assert result.ids == ["a"]
    assert qmd.calls == []
    # Config can mark a provider lexical even if the class does not.
    coordinator = _coordinator([qmd, fts], configs={"qmd": ProviderConfig(lexical=True)})
    assert coordinator.search('"exact phrase"', limit=5, policy=POLICY).ids == ["b", "a"]


def test_unresolved_refs_are_dropped_and_counted() -> None:
    coordinator = _coordinator([FakeProvider("fts", ["a", "ghost", "b"])], _metas("a", "b"))
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["a", "b"]
    assert result.metrics.dropped == {"unresolved": 1}


def test_ref_kind_without_resolver_is_unresolved() -> None:
    provider = FakeProvider("qmd", ["memories/a.md"], ref_kind="path")
    coordinator = _coordinator([provider])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == []
    assert result.metrics.dropped == {"unresolved": 1}
    assert not result.labels.fail_closed  # the provider answered; nothing passed


def test_raising_resolver_fails_closed_for_its_refs() -> None:
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"])], resolvers={"memory_id": RaisingResolver()}
    )
    result = coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert result.ids == []
    assert result.metrics.resolver_errors == 1
    assert result.metrics.dropped == {"unresolved": 1}
    assert "zebra" not in json.dumps(result.metrics.as_dict())


def test_path_resolver_double_joins_paths_to_ids() -> None:
    ids = MetaResolver(_metas("a", "b"))
    paths = MappingPathResolver({"files/notes/a.md": "a", "files/b.md": "b"}, ids)
    refs = ["files/b.md", "files/unknown.md", "files/notes/a.md"]
    qmd = FakeProvider("qmd", refs, ref_kind="path")
    fts = FakeProvider("fts", ["a"])
    coordinator = Coordinator([fts, qmd], resolvers={"memory_id": ids, "path": paths})
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["a", "b"]  # a: both providers
    assert result.hits[0].providers == ("fts", "qmd")
    assert result.metrics.dropped == {"unresolved": 1}


def test_filter_drops_are_counted_by_reason() -> None:
    metas: dict[str, MemoryMeta | None] = {
        "a": _meta("a"),
        "b": _meta("b", tenant="other"),
        "c": _meta("c", sensitivity_private=True),
        "d": None,
        "e": _meta("e", superseded_by="a"),
        "f": _meta("f", valid_to="garbage"),
        "g": _meta("g", status="disputed"),
    }
    coordinator = _coordinator([FakeProvider("fts", list("abcdefg"))], metas)
    result = coordinator.search("memory design notes", limit=10, policy=POLICY)
    assert result.ids == ["a", "g"]
    assert result.metrics.dropped == {
        "tenant": 1,
        "privacy": 1,
        "missing_meta": 1,
        "superseded": 1,
        "expiry_unparsable": 1,
    }
    assert result.hits[1].labels == ("disputed",)


def test_disputed_weight_reorders() -> None:
    metas = {"a": _meta("a", status="disputed"), "b": _meta("b")}
    coordinator = _coordinator([FakeProvider("fts", ["a", "b"])], metas)
    assert coordinator.search("memory design", limit=5, policy=POLICY).ids == ["b", "a"]


def test_privacy_mismatch_is_counted() -> None:
    metas = {"a": _meta("a", privacy="none", sensitivity_private=True)}
    coordinator = _coordinator([FakeProvider("fts", ["a"])], metas)
    result = coordinator.search("memory design", limit=5, policy=POLICY)
    assert result.metrics.privacy_source_mismatch == 1
    assert result.metrics.dropped == {"privacy": 1}


def test_projection_version_label() -> None:
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"]), FakeProvider("qmd", ["b"], projection_version="tree-ab12cd34")]
    )
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.labels.projection_versions == {"qmd": "tree-ab12cd34"}


def test_metrics_sink_gets_no_query_and_failures_are_swallowed() -> None:
    seen: list[RecallMetrics] = []
    coordinator = _coordinator([FakeProvider("fts", ["a"])], metrics_sink=seen.append)
    coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert len(seen) == 1
    dumped = json.dumps(seen[0].as_dict())
    for word in SECRET_QUERY.split():
        if len(word) > 3:
            assert word not in dumped
    assert set(seen[0].as_dict()) == {
        "total_ms",
        "route",
        "providers",
        "resolved",
        "returned",
        "dropped",
        "privacy_source_mismatch",
        "resolver_errors",
    }

    def broken(_: RecallMetrics) -> None:
        raise RuntimeError("sink down")

    coordinator = _coordinator([FakeProvider("fts", ["a"])], metrics_sink=broken)
    assert coordinator.search("memory design", limit=5, policy=POLICY).ids == ["a"]
    assert coordinator.last_metrics is not None


def test_order_is_deterministic_under_random_finish_order() -> None:
    rng = random.Random(3)
    baseline: list[str] | None = None
    for _ in range(8):
        providers = [
            FakeProvider("fts", ["a", "b", "c"], delay=rng.random() / 100),
            FakeProvider("qmd", ["c", "d", "a"], delay=rng.random() / 100),
            FakeProvider("hybrid", ["d", "e"], delay=rng.random() / 100),
        ]
        ids = _coordinator(providers).search("memory design notes", limit=10, policy=POLICY).ids
        baseline = baseline or ids
        assert ids == baseline


def test_constructor_and_argument_validation() -> None:
    with pytest.raises(ValueError):
        _coordinator([FakeProvider("fts"), FakeProvider("fts")])
    with pytest.raises(ValueError):
        _coordinator([FakeProvider("fts")], configs={"nope": ProviderConfig()})
    with pytest.raises(ValueError):
        _coordinator([FakeProvider("fts")], total_timeout=0)
    with pytest.raises(ValueError):
        ProviderConfig(weight=-1)
    with pytest.raises(ValueError):
        ProviderConfig(overfetch=0)
    coordinator = _coordinator([FakeProvider("fts")])
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            coordinator.search("x", limit=bad, policy=POLICY)
    with pytest.raises(ValueError):
        coordinator.search("  ", limit=1, policy=POLICY)


# -- built-in resolvers on a real vault ------------------------------------


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Public", "public body", memory_id="pub")
    repository.add("Private", "private body", memory_id="priv", tags=["sensitivity:private"])
    repository.add("Declared", "x", memory_id="decl", tags=["privacy:team-only"])
    repository.add("Retired", "x", memory_id="old", tags=["lifecycle:retired"])
    repository.add("Alpha", "x", memory_id="alpha", tenant="alpha")
    return repository


def test_vault_id_resolver_reads_metadata(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    resolver = VaultIdResolver(repository.root)
    found = resolver.resolve(["pub", "priv", "decl", "old", "alpha", "missing", "../etc", ""])
    assert set(found) == {"pub", "priv", "decl", "old", "alpha"}
    assert found["priv"].meta is not None and found["priv"].meta.sensitivity_private
    assert found["decl"].meta is not None and found["decl"].meta.privacy == "team-only"
    assert found["old"].meta is not None and found["old"].meta.lifecycle == "retired"
    assert found["alpha"].meta is not None and found["alpha"].meta.tenant == "alpha"


def test_vault_id_resolver_unreadable_file_has_no_meta(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    (repository.memories_dir / "broken.md").write_text("not frontmatter", encoding="utf-8")
    found = VaultIdResolver(repository.root).resolve(["broken"])
    assert found["broken"].meta is None


def test_vault_id_resolver_ignores_symlinks(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    (repository.memories_dir / "link.md").symlink_to(repository.memories_dir / "pub.md")
    assert VaultIdResolver(repository.root).resolve(["link"]) == {}


def test_memory_path_resolver_forms(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    paths = MemoryPathResolver(VaultIdResolver(repository.root))
    refs = [
        "memories/pub.md",
        "fleet-memory/memories/priv.md",
        "./memories/old.md",
        "files/pub.md",
        "memories/../pub.md",
        "memories/nope.md",
    ]
    found = paths.resolve(refs)
    assert {ref: res.memory_id for ref, res in found.items()} == {
        "memories/pub.md": "pub",
        "fleet-memory/memories/priv.md": "priv",
        "./memories/old.md": "old",
    }


def test_coordinator_with_default_resolvers_fails_closed_on_vault(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = FakeProvider("fts", ["pub", "priv", "decl", "old", "alpha", "gone"])
    paths = FakeProvider("qmd", ["memories/pub.md", "files/x.md"], ref_kind="path")
    coordinator = Coordinator([provider, paths], resolvers=default_resolvers(repository.root))
    result = coordinator.search("memory design notes", limit=10, policy=FilterPolicy())
    assert result.ids == ["pub"]
    assert result.hits[0].providers == ("fts", "qmd")
    assert result.metrics.dropped == {
        "unresolved": 2,
        "privacy": 2,
        "retired": 1,
        "tenant": 1,
    }
    cleared = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    assert coordinator.search("memory design notes", limit=10, policy=cleared).ids == [
        "pub",
        "priv",
    ]


# -- review fixes: bounded workers, hostile output, sanitised codes --------


def _live_threads(fragment: str) -> int:
    return sum(1 for thread in threading.enumerate() if fragment in thread.name)


def test_hung_provider_holds_at_most_one_thread() -> None:
    release = threading.Event()
    hang = FakeProvider("hang", ["a"], block=release)
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"]), hang],
        configs={"hang": ProviderConfig(budget=RecallBudget(timeout_seconds=0.02))},
    )
    try:
        reasons = []
        for _ in range(20):
            result = coordinator.search("memory design notes", limit=5, policy=POLICY)
            assert result.ids == ["a"]
            reasons.append(result.labels.degraded["hang"])
            assert _live_threads("provider:hang") <= 1
        assert reasons[0] == "timeout"
        assert set(reasons[1:]) == {"busy"}
        assert len(hang.calls) == 1
        assert coordinator.inflight("provider:hang") == 1
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while coordinator.inflight("provider:hang") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.inflight("provider:hang") == 0
    after = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert "hang" not in after.labels.degraded


def test_max_inflight_can_be_raised() -> None:
    release = threading.Event()
    hang = FakeProvider("hang", ["a"], block=release)
    coordinator = _coordinator(
        [hang],
        configs={"hang": ProviderConfig(budget=RecallBudget(timeout_seconds=0.02), max_inflight=3)},
    )
    try:
        for _ in range(5):
            coordinator.search("memory design notes", limit=5, policy=POLICY)
        assert len(hang.calls) == 3
        assert _live_threads("provider:hang") <= 3
    finally:
        release.set()


def test_hanging_available_is_bounded_by_total_deadline() -> None:
    release = threading.Event()
    provider = FakeProvider("slow", ["b"])

    def hanging() -> Availability:
        release.wait(5)
        return Availability.ok()

    provider.available = hanging  # type: ignore[method-assign]
    coordinator = _coordinator([FakeProvider("fts", ["a"]), provider], total_timeout=0.5)
    started = time.monotonic()
    try:
        result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    finally:
        release.set()
    assert time.monotonic() - started < 1.5
    assert result.ids == ["a"]
    assert result.labels.degraded == {"slow": "timeout"}


class SlowResolver:
    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        self.release.wait(5)
        return {ref: Resolution(ref, _meta(ref)) for ref in refs}


def test_slow_resolver_is_bounded_and_fails_closed() -> None:
    release = threading.Event()
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"])],
        resolvers={"memory_id": SlowResolver(release)},
        total_timeout=0.5,
    )
    started = time.monotonic()
    try:
        result = coordinator.search("memory design notes", limit=5, policy=POLICY)
        assert time.monotonic() - started < 1.5
        assert result.ids == []
        assert result.labels.resolvers_degraded == {"memory_id": "timeout"}
        assert result.metrics.resolver_errors == 1
        assert result.metrics.dropped == {"unresolved": 1}
        # Still running: the next call skips it instead of stacking threads.
        again = coordinator.search("memory design notes", limit=5, policy=POLICY)
        assert again.labels.resolvers_degraded == {"memory_id": "busy"}
        assert _live_threads("resolver:memory_id") <= 1
    finally:
        release.set()


class HostileList(list):  # type: ignore[type-arg]
    def __iter__(self):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"leaking {SECRET_QUERY}")


class HostileError(Exception):
    pass


HostileError.__name__ = "Leak_zebra_codename"


def test_hostile_sequence_is_invalid_and_never_propagates() -> None:
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"]), FakeProvider("evil", result=HostileList([1]))]
    )
    result = coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert result.ids == ["a"]
    assert result.labels.degraded == {"evil": "invalid:bad_output"}
    assert "zebra" not in json.dumps([result.labels.as_dict(), result.metrics.as_dict()])


def test_exception_type_names_never_reach_labels() -> None:
    coordinator = _coordinator([FakeProvider("evil", error=HostileError("x"))])
    result = coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert result.labels.degraded == {"evil": "error:exception"}
    assert "zebra" not in json.dumps([result.labels.as_dict(), result.metrics.as_dict()])


def test_base_exception_in_provider_is_contained() -> None:
    coordinator = _coordinator(
        [FakeProvider("fts", ["a"]), FakeProvider("exit", error=SystemExit(1))]
    )
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["a"]
    assert result.labels.degraded == {"exit": "error:exception"}


def test_provider_codes_are_sanitised() -> None:
    leaky = FakeProvider("leaky", availability=Availability.missing(f"down: {SECRET_QUERY}"))
    versioned = FakeProvider("ver", ["a"], projection_version=f"v1 {SECRET_QUERY}")
    good = FakeProvider("good", ["a"], projection_version="tree-ab12cd34")
    coordinator = _coordinator([leaky, versioned, good])
    result = coordinator.search(SECRET_QUERY, limit=5, policy=POLICY)
    assert result.labels.degraded == {"leaky": "unavailable"}
    assert result.labels.projection_versions == {"good": "tree-ab12cd34"}


def test_mutated_candidate_is_rejected_by_revalidation() -> None:
    sneaky = Candidate(ref="a", ref_kind="memory_id", score=1.0, provider="evil", rank=1)
    object.__setattr__(sneaky, "extra", {"body": "secret body text"})
    good = Candidate(ref="b", ref_kind="memory_id", score=1.0, provider="evil", rank=2)
    coordinator = _coordinator([FakeProvider("evil", result=[sneaky, good])])
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == ["b"]
    assert result.metrics.providers[0].invalid == 1
    assert all("body" not in hit.extra for hit in result.hits)


def test_resolution_meta_id_mismatch_is_dropped() -> None:
    class Mismatch:
        def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
            return {ref: Resolution(ref, _meta("someone-else")) for ref in refs}

    coordinator = _coordinator([FakeProvider("fts", ["a"])], resolvers={"memory_id": Mismatch()})
    result = coordinator.search("memory design notes", limit=5, policy=POLICY)
    assert result.ids == []
    assert result.metrics.dropped == {"id_mismatch": 1}


def test_malformed_resolver_output_fails_closed() -> None:
    class Weird:
        def resolve(self, refs: Sequence[str]) -> object:
            return {ref: ("not", "a", "resolution") for ref in refs}

    class NotMapping:
        def resolve(self, refs: Sequence[str]) -> object:
            return [1, 2]

    for resolver, code in ((Weird(), None), (NotMapping(), "invalid")):
        coordinator = _coordinator([FakeProvider("fts", ["a"])], resolvers={"memory_id": resolver})
        result = coordinator.search("memory design notes", limit=5, policy=POLICY)
        assert result.ids == []
        if code:
            assert result.labels.resolvers_degraded == {"memory_id": code}


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), -0.1, True])
def test_provider_weight_must_be_finite_and_non_negative(weight: float) -> None:
    with pytest.raises(ValueError, match="weight"):
        ProviderConfig(weight=weight)
