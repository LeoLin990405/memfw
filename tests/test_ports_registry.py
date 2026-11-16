"""Adapter registry: registration, config, load and entry-point discovery."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest

import memfw.ports.registry as registry_module
from memfw.ports import (
    ADAPTER_KINDS,
    ENTRY_POINT_GROUP,
    AdapterRegistry,
    AdaptersConfig,
    AdapterSpec,
    Availability,
    Candidate,
    RecallBudget,
    default_registry,
    missing_modules,
    register,
)

MISSING_MODULE = "memfw_nonexistent_mod_xyz"


class FakeRecall:
    def __init__(
        self,
        name: str = "fake",
        availability: Availability | None = None,
        settings: Mapping[str, Any] | None = None,
        raise_on_available: bool = False,
    ) -> None:
        self.name = name
        self._availability = availability or Availability.ok()
        self.settings = dict(settings or {})
        self._raise = raise_on_available

    def available(self) -> Availability:
        if self._raise:
            raise RuntimeError("boom")
        return self._availability

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        return []


class NotAnAdapter:
    name = "fake"


class CountingFactory:
    def __init__(self, build: Callable[[Mapping[str, Any]], object] | None = None) -> None:
        self.calls: list[Mapping[str, Any]] = []
        self._build = build or (lambda settings: FakeRecall(settings=settings))

    def __call__(self, settings: Mapping[str, Any]) -> object:
        self.calls.append(settings)
        return self._build(settings)


def _spec(
    name: str = "fake",
    *,
    kind: Any = "recall",
    factory: Callable[[Mapping[str, Any]], object] | None = None,
    requires: tuple[str, ...] = (),
) -> AdapterSpec:
    return AdapterSpec(
        name=name,
        kind=kind,
        factory=factory or CountingFactory(),
        requires=requires,
    )


def _enabled(*names: str, **settings: Mapping[str, Any]) -> AdaptersConfig:
    return AdaptersConfig(enabled=frozenset(names), settings=dict(settings))


# --- register --------------------------------------------------------------


def test_register_and_get() -> None:
    registry = AdapterRegistry()
    spec = _spec()
    registry.register(spec)
    assert registry.get("fake") is spec
    assert registry.get("other") is None
    assert registry.specs() == (spec,)


def test_specs_sorted() -> None:
    registry = AdapterRegistry()
    registry.register(_spec("zeta"))
    registry.register(_spec("alpha", factory=lambda settings: FakeRecall(name="alpha")))
    assert [spec.name for spec in registry.specs()] == ["alpha", "zeta"]


def test_register_duplicate_raises_and_replace() -> None:
    registry = AdapterRegistry()
    first = _spec()
    registry.register(first)
    second = _spec()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(second)
    assert registry.get("fake") is first
    registry.register(second, replace=True)
    assert registry.get("fake") is second


def test_module_register_uses_default_registry() -> None:
    name = "test-ports-registry-tmp"
    spec = _spec(name)
    registry = default_registry()
    assert registry.get(name) is None
    try:
        register(spec)
        assert registry.get(name) is spec
        with pytest.raises(ValueError):
            register(spec)
        register(spec, replace=True)
    finally:
        registry._specs.pop(name, None)
    assert registry.get(name) is None


# --- AdapterSpec -----------------------------------------------------------


@pytest.mark.parametrize("name", ["", "Bad", "1x", "has space"])
def test_adapter_spec_bad_name(name: str) -> None:
    with pytest.raises(ValueError, match="adapter name"):
        _spec(name)


@pytest.mark.parametrize("kind", ["", "recaller", "RECALL", None])
def test_adapter_spec_bad_kind(kind: Any) -> None:
    with pytest.raises(ValueError, match="kind"):
        _spec(kind=kind)


def test_adapter_spec_factory_must_be_callable() -> None:
    with pytest.raises(ValueError, match="callable"):
        AdapterSpec(name="fake", kind="recall", factory="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_adapter_spec_all_kinds(kind: Any) -> None:
    assert _spec(kind=kind).kind == kind


# --- AdaptersConfig --------------------------------------------------------


def test_config_from_none_and_empty() -> None:
    assert AdaptersConfig.from_mapping(None) == AdaptersConfig()
    assert AdaptersConfig.from_mapping({}) == AdaptersConfig()
    assert not AdaptersConfig.from_mapping(None).is_enabled("fake")


@pytest.mark.parametrize("enabled", ["fake", 5, None, 1.5])
def test_config_malformed_enabled_all_off(enabled: Any) -> None:
    config = AdaptersConfig.from_mapping({"enabled": enabled, "fake": {"url": "x"}})
    assert config.enabled == frozenset()
    assert dict(config.settings) == {}
    assert not config.is_enabled("fake")


def test_config_settings_picked_up() -> None:
    config = AdaptersConfig.from_mapping(
        {
            "enabled": ["fake", " other ", "", "  "],
            "fake": {"url": "http://127.0.0.1:1", "port": 1},
            "other": {"collection": "c"},
            "junk": "not-a-mapping",
        }
    )
    assert config.enabled == frozenset({"fake", "other"})
    assert config.is_enabled("fake")
    assert not config.is_enabled("junk")
    assert dict(config.settings) == {
        "fake": {"url": "http://127.0.0.1:1", "port": 1},
        "other": {"collection": "c"},
    }
    assert "enabled" not in config.settings


def test_config_without_enabled_key() -> None:
    config = AdaptersConfig.from_mapping({"fake": {"a": 1}})
    assert config.enabled == frozenset()
    assert dict(config.settings) == {"fake": {"a": 1}}


# --- missing_modules -------------------------------------------------------


def test_missing_modules() -> None:
    assert missing_modules(["json", "os.path"]) == ()
    assert missing_modules(["json", MISSING_MODULE, f"{MISSING_MODULE}.sub"]) == (
        MISSING_MODULE,
        f"{MISSING_MODULE}.sub",
    )
    assert missing_modules(["", ".relative"]) == ("", ".relative")


# --- load ------------------------------------------------------------------


def test_load_unknown_adapter() -> None:
    status = AdapterRegistry().load("nobody", _enabled("nobody"))
    assert status.name == "nobody"
    assert status.kind == ""
    assert status.enabled is True
    assert status.available is False
    assert status.reason == "unknown_adapter"
    assert status.adapter is None


def test_load_disabled_does_not_call_factory() -> None:
    factory = CountingFactory()
    registry = AdapterRegistry()
    registry.register(_spec(factory=factory))
    status = registry.load("fake", AdaptersConfig())
    assert (status.enabled, status.available, status.reason) == (False, False, "disabled")
    assert status.kind == "recall"
    assert status.adapter is None
    assert factory.calls == []


def test_load_missing_requires_does_not_call_factory() -> None:
    factory = CountingFactory()
    registry = AdapterRegistry()
    registry.register(_spec(factory=factory, requires=("json", MISSING_MODULE)))
    status = registry.load("fake", _enabled("fake"))
    assert status.available is False
    assert status.enabled is True
    assert status.reason == "missing_dependency"
    assert factory.calls == []


def _raiser(exc: BaseException) -> Callable[[Mapping[str, Any]], object]:
    def build(settings: Mapping[str, Any]) -> object:
        raise exc

    return build


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (ImportError("no extra"), "missing_dependency"),
        (ModuleNotFoundError("no extra"), "missing_dependency"),
        (RuntimeError("bad"), "factory_error"),
        (ValueError("bad"), "factory_error"),
    ],
)
def test_load_factory_errors(exc: BaseException, reason: str) -> None:
    registry = AdapterRegistry()
    registry.register(_spec(factory=_raiser(exc)))
    status = registry.load("fake", _enabled("fake"))
    assert (status.available, status.reason, status.adapter) == (False, reason, None)


def test_load_protocol_mismatch() -> None:
    registry = AdapterRegistry()
    registry.register(_spec(factory=lambda settings: NotAnAdapter()))
    assert registry.load("fake", _enabled("fake")).reason == "protocol_mismatch"


def test_load_protocol_mismatch_wrong_kind() -> None:
    registry = AdapterRegistry()
    registry.register(_spec(kind="projector", factory=lambda settings: FakeRecall()))
    assert registry.load("fake", _enabled("fake")).reason == "protocol_mismatch"


def test_load_name_mismatch() -> None:
    registry = AdapterRegistry()
    registry.register(_spec(factory=lambda settings: FakeRecall(name="other")))
    status = registry.load("fake", _enabled("fake"))
    assert (status.available, status.reason) == (False, "name_mismatch")


def test_load_adapter_unavailable_reason() -> None:
    registry = AdapterRegistry()
    registry.register(
        _spec(factory=lambda settings: FakeRecall(availability=Availability.missing("no_index")))
    )
    status = registry.load("fake", _enabled("fake"))
    assert (status.available, status.reason, status.adapter) == (False, "no_index", None)


def test_load_adapter_unavailable_without_reason() -> None:
    registry = AdapterRegistry()
    registry.register(
        _spec(factory=lambda settings: FakeRecall(availability=Availability(False, "")))
    )
    assert registry.load("fake", _enabled("fake")).reason == "unavailable"


def test_load_availability_raising() -> None:
    registry = AdapterRegistry()
    registry.register(_spec(factory=lambda settings: FakeRecall(raise_on_available=True)))
    status = registry.load("fake", _enabled("fake"))
    assert (status.available, status.reason) == (False, "availability_error")


def test_load_success_passes_config_settings_only() -> None:
    factory = CountingFactory()
    registry = AdapterRegistry()
    registry.register(_spec(factory=factory, requires=("json",)))
    config = _enabled("fake", fake={"url": "http://127.0.0.1:9"}, other={"secret": "x"})
    status = registry.load("fake", config)
    assert status.available is True
    assert status.enabled is True
    assert status.reason == ""
    assert isinstance(status.adapter, FakeRecall)
    assert status.adapter.settings == {"url": "http://127.0.0.1:9"}
    assert factory.calls == [{"url": "http://127.0.0.1:9"}]
    # The factory gets a copy; mutating it does not change the config.
    factory.calls[0]["url"] = "changed"  # type: ignore[index]
    assert dict(config.settings["fake"]) == {"url": "http://127.0.0.1:9"}


def test_load_success_without_settings_gets_empty_mapping() -> None:
    factory = CountingFactory()
    registry = AdapterRegistry()
    registry.register(_spec(factory=factory))
    assert registry.load("fake", _enabled("fake")).available is True
    assert factory.calls == [{}]


# --- load_enabled ----------------------------------------------------------


def test_load_enabled_with_kind_filter() -> None:
    disabled = CountingFactory()
    registry = AdapterRegistry()
    registry.register(_spec("alpha", factory=lambda settings: FakeRecall(name="alpha")))
    registry.register(_spec("beta", kind="projector", factory=lambda settings: object()))
    registry.register(_spec("gamma", factory=disabled))
    config = _enabled("alpha", "beta", "ghost")

    statuses = registry.load_enabled(config)
    assert [status.name for status in statuses] == ["alpha", "beta", "ghost"]
    assert disabled.calls == []

    recall = registry.load_enabled(config, kind="recall")
    assert [status.name for status in recall] == ["alpha", "ghost"]
    assert recall[0].available is True
    assert recall[1].reason == "unknown_adapter"

    projectors = registry.load_enabled(config, kind="projector")
    assert [status.name for status in projectors] == ["beta", "ghost"]
    assert projectors[0].reason == "protocol_mismatch"

    assert [s.name for s in registry.load_enabled(config, kind="deriver")] == ["ghost"]
    assert registry.load_enabled(AdaptersConfig()) == ()


# --- discover --------------------------------------------------------------


class FakeEntryPoint:
    def __init__(self, name: str, load: Callable[[], object]) -> None:
        self.name = name
        self._load = load

    def load(self) -> object:
        return self._load()


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, points: list[FakeEntryPoint]) -> list[str]:
    groups: list[str] = []

    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        groups.append(group)
        return points

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    return groups


def _raise(exc: BaseException) -> Callable[[], object]:
    def load() -> object:
        raise exc

    return load


class CountingEntryPoint(FakeEntryPoint):
    def __init__(self, name: str, load: Callable[[], object]) -> None:
        super().__init__(name, load)
        self.loads = 0
        self.value = f"pkg.{name.replace('-', '_')}:SPEC"

    def load(self) -> object:
        self.loads += 1
        return super().load()


def test_discover_records_names_without_importing(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [
        CountingEntryPoint("direct", lambda: _spec("direct")),
        CountingEntryPoint("exits", _raise(SystemExit(3))),
        CountingEntryPoint("Bad Name", lambda: _spec("x")),
    ]
    groups = _patch_entry_points(monkeypatch, points)  # type: ignore[arg-type]
    registry = AdapterRegistry()
    assert registry.discover() == ("direct", "exits")
    assert groups == [ENTRY_POINT_GROUP]
    assert all(point.loads == 0 for point in points)
    assert registry.discovery_errors == {"Bad Name": "bad_name"}
    listing = {info.name: info for info in registry.listing()}
    assert set(listing) == {"direct", "exits"}
    assert listing["exits"].origin == "entry_point"
    assert listing["exits"].value == "pkg.exits:SPEC"
    assert registry.specs() == ()
    # Disabled: still never imported.
    assert registry.load("exits", AdaptersConfig()).reason == "disabled"
    assert all(point.loads == 0 for point in points)


def test_enabled_entry_point_raising_system_exit_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = CountingEntryPoint("exits", _raise(SystemExit(3)))
    _patch_entry_points(monkeypatch, [point])  # type: ignore[list-item]
    registry = AdapterRegistry()
    registry.discover()
    status = registry.load("exits", _enabled("exits"))
    assert (status.available, status.reason) == (False, "load_error")
    assert point.loads == 1


def test_keyboard_interrupt_during_load_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, [FakeEntryPoint("ctrlc", _raise(KeyboardInterrupt()))])
    registry = AdapterRegistry()
    registry.discover()
    with pytest.raises(KeyboardInterrupt):
        registry.load("ctrlc", _enabled("ctrlc"))


def test_enabled_entry_point_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    direct_factory = CountingFactory(lambda settings: FakeRecall(name="direct"))
    via_factory = CountingFactory(lambda settings: FakeRecall(name="via-callable"))
    points = [
        FakeEntryPoint("direct", lambda: _spec("direct", factory=direct_factory)),
        FakeEntryPoint("via-callable", lambda: lambda: _spec("via-callable", factory=via_factory)),
        FakeEntryPoint("broken-import", _raise(ImportError("no extra"))),
        FakeEntryPoint("not-a-spec", lambda: lambda: 42),
        FakeEntryPoint("exploding", _raise(RuntimeError("boom"))),
        FakeEntryPoint("renamed", lambda: _spec("other-name")),
    ]
    _patch_entry_points(monkeypatch, points)
    registry = AdapterRegistry()
    registry.discover()
    config = _enabled(*(point.name for point in points))
    reasons = {point.name: registry.load(point.name, config).reason for point in points}
    assert reasons == {
        "direct": "",
        "via-callable": "",
        "broken-import": "missing_dependency",
        "not-a-spec": "not_a_spec",
        "exploding": "load_error",
        "renamed": "name_mismatch",
    }
    assert len(direct_factory.calls) == 1
    assert registry.get("direct") is not None


def test_discover_duplicate_of_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = AdapterRegistry()
    existing = _spec("fake")
    registry.register(existing)
    point = CountingEntryPoint("fake", lambda: _spec("fake"))
    _patch_entry_points(monkeypatch, [point])  # type: ignore[list-item]
    assert registry.discover() == ()
    assert registry.discovery_errors == {"fake": "duplicate"}
    assert registry.get("fake") is existing
    assert point.loads == 0


def test_factory_system_exit_is_factory_error() -> None:
    def factory(settings: Mapping[str, Any]) -> object:
        raise SystemExit(1)

    registry = AdapterRegistry()
    registry.register(_spec("fake", factory=factory))
    assert registry.load("fake", _enabled("fake")).reason == "factory_error"


def test_discover_via_registry_module_path(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec("fake")

    def fake_entry_points(*, group: str) -> list[FakeEntryPoint]:
        return [FakeEntryPoint("fake", lambda: spec)]

    monkeypatch.setattr(registry_module.importlib.metadata, "entry_points", fake_entry_points)
    registry = AdapterRegistry()
    assert registry.discover("custom.group") == ("fake",)


def test_discover_entry_points_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*, group: str) -> list[FakeEntryPoint]:
        raise OSError("bad metadata")

    monkeypatch.setattr(importlib.metadata, "entry_points", broken)
    registry = AdapterRegistry()
    assert registry.discover() == ()
    assert registry.discovery_errors == {"*": "OSError"}


def test_load_after_failed_import_keeps_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("needs-extra", _raise(ImportError("no extra"))),
            FakeEntryPoint("not-a-spec", lambda: lambda: "nope"),
        ],
    )
    registry = AdapterRegistry()
    registry.discover()
    config = _enabled("needs-extra", "not-a-spec")
    status = registry.load("needs-extra", config)
    assert (status.available, status.reason, status.enabled) == (
        False,
        "missing_dependency",
        True,
    )
    assert registry.load("not-a-spec", config).reason == "not_a_spec"
    assert registry.discovery_errors["not-a-spec"] == "not_a_spec"
    assert registry.load("needs-extra", AdaptersConfig()).enabled is False


def test_discovered_adapter_loads_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = CountingFactory()
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", lambda: _spec(factory=factory))])
    registry = AdapterRegistry()
    registry.discover()
    assert registry.load("fake", AdaptersConfig()).reason == "disabled"
    assert factory.calls == []
    assert registry.load("fake", _enabled("fake")).available is True
    assert len(factory.calls) == 1
