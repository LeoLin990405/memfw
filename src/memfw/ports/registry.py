"""Adapter registry: discovery, enablement and graceful absence.

Adapters come from two places: an explicit ``register()`` call, and the
``memfw.adapters`` entry-point group of installed distributions. Every
adapter is off until the config lists it under ``enabled``. An adapter whose
optional dependency is missing reports itself unavailable instead of raising,
so a missing extra can never break the kernel.

Discovery reads entry-point metadata only. An entry point is imported the
first time ``load`` is asked for it while it is enabled; any failure while
importing or building it (``SystemExit`` included, ``KeyboardInterrupt``
excepted) becomes a status reason.

Settings (endpoints, ports, collection names) come only from the config
mapping handed to ``load``; a request body never reaches a factory. That is
the SSRF rule of the compat framework.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from .protocols import Deriver, Ingestor, Projector, RecallProvider
from .types import Availability, check_name

ENTRY_POINT_GROUP = "memfw.adapters"

AdapterKind = Literal["recall", "projector", "ingestor", "deriver"]
ADAPTER_KINDS: tuple[str, ...] = get_args(AdapterKind)

_PROTOCOLS: dict[str, type] = {
    "recall": RecallProvider,
    "projector": Projector,
    "ingestor": Ingestor,
    "deriver": Deriver,
}

_log = logging.getLogger(__name__)

Factory = Callable[[Mapping[str, Any]], object]


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """How to build one adapter.

    ``requires`` lists importable module names the adapter needs (its
    optional extra). ``source`` and ``license`` record where the external
    project lives and under which licence it is used.
    """

    name: str
    kind: AdapterKind
    factory: Factory
    requires: tuple[str, ...] = ()
    version: str = ""
    source: str = ""
    license: str = ""

    def __post_init__(self) -> None:
        check_name(self.name, what="adapter name")
        if self.kind not in ADAPTER_KINDS:
            raise ValueError(f"adapter kind must be one of {ADAPTER_KINDS}")
        if not callable(self.factory):
            raise ValueError("factory must be callable")


@dataclass(frozen=True, slots=True)
class AdaptersConfig:
    """Which adapters are enabled, and each one's settings."""

    enabled: frozenset[str] = frozenset()
    settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> AdaptersConfig:
        """Read ``{"enabled": [...], "<name>": {...}}``. Anything malformed → all off."""

        if not data or not isinstance(data, Mapping):
            return cls()
        raw_enabled = data.get("enabled", ())
        if not isinstance(raw_enabled, list | tuple | set | frozenset):
            return cls()
        if not all(isinstance(item, str) for item in raw_enabled):
            return cls()
        enabled = frozenset(item.strip() for item in raw_enabled if item.strip())
        settings = {
            str(key): dict(value)
            for key, value in data.items()
            if key != "enabled" and isinstance(value, Mapping)
        }
        return cls(enabled=enabled, settings=settings)

    def is_enabled(self, name: str) -> bool:
        return name in self.enabled


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """Outcome of loading one adapter. ``adapter`` is set only when usable."""

    name: str
    kind: str
    enabled: bool
    available: bool
    reason: str = ""
    adapter: object | None = None


def missing_modules(modules: Iterable[str]) -> tuple[str, ...]:
    """Modules from ``modules`` that cannot be imported here."""

    missing: list[str] = []
    for module in modules:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(module)
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """What is known about an adapter without importing it."""

    name: str
    origin: Literal["registered", "entry_point"]
    value: str = ""
    distribution: str = ""
    kind: str = ""


def _guarded(call: Callable[[], Any]) -> tuple[Any, str]:
    """Run third-party code. Any failure, even ``SystemExit``, becomes a code.

    ``KeyboardInterrupt`` is re-raised: an operator's Ctrl-C must still work.
    """

    try:
        return call(), ""
    except KeyboardInterrupt:
        raise
    except ImportError:
        return None, "missing_dependency"
    except BaseException as exc:  # noqa: BLE001 - adapter code boundary
        _log.warning("adapter code failed: %s", type(exc).__name__)
        return None, "load_error"


class AdapterRegistry:
    """Known adapters by unique name.

    Explicitly registered specs and entry points are both listed by name
    only. An entry point is imported the first time ``load`` is asked for it
    while it is enabled; a disabled adapter's code never runs.
    """

    def __init__(self) -> None:
        self._specs: dict[str, AdapterSpec] = {}
        self._entry_points: dict[str, importlib.metadata.EntryPoint] = {}
        self.discovery_errors: dict[str, str] = {}

    def register(self, spec: AdapterSpec, *, replace: bool = False) -> None:
        if not isinstance(spec, AdapterSpec):
            raise TypeError("register() takes an AdapterSpec")
        if (spec.name in self._specs or spec.name in self._entry_points) and not replace:
            raise ValueError(f"adapter {spec.name!r} is already registered")
        self._entry_points.pop(spec.name, None)
        self._specs[spec.name] = spec

    def specs(self) -> tuple[AdapterSpec, ...]:
        """Specs already in memory (registered or loaded). Imports nothing."""

        return tuple(self._specs[name] for name in sorted(self._specs))

    def get(self, name: str) -> AdapterSpec | None:
        return self._specs.get(name)

    def listing(self) -> tuple[AdapterInfo, ...]:
        """Every known adapter, from metadata only. Imports nothing."""

        infos = [
            AdapterInfo(name, "registered", kind=spec.kind) for name, spec in self._specs.items()
        ]
        for name, point in self._entry_points.items():
            dist = getattr(point, "dist", None)
            infos.append(
                AdapterInfo(
                    name,
                    "entry_point",
                    value=str(getattr(point, "value", "")),
                    distribution=str(getattr(dist, "name", "") or ""),
                )
            )
        return tuple(sorted(infos, key=lambda info: info.name))

    def discover(self, group: str = ENTRY_POINT_GROUP) -> tuple[str, ...]:
        """Record installed entry points by name. Does not import any of them."""

        try:
            points = importlib.metadata.entry_points(group=group)
        except Exception as exc:  # noqa: BLE001 - metadata of a broken install
            self.discovery_errors["*"] = type(exc).__name__
            return ()
        added: list[str] = []
        for point in sorted(points, key=lambda item: str(item.name)):
            name = str(point.name)
            try:
                check_name(name, what="adapter name")
            except ValueError:
                self.discovery_errors[name] = "bad_name"
                continue
            if name in self._specs or name in self._entry_points:
                self.discovery_errors[name] = "duplicate"
                continue
            self._entry_points[name] = point
            added.append(name)
        return tuple(added)

    def _resolve(self, name: str) -> tuple[AdapterSpec | None, str]:
        spec = self._specs.get(name)
        if spec is not None:
            return spec, ""
        point = self._entry_points.get(name)
        if point is None:
            return None, self.discovery_errors.get(name, "unknown_adapter")

        def build() -> object:
            loaded = point.load()
            return loaded if isinstance(loaded, AdapterSpec) else loaded()

        produced, error = _guarded(build)
        if error:
            self.discovery_errors[name] = error
            return None, error
        if not isinstance(produced, AdapterSpec):
            self.discovery_errors[name] = "not_a_spec"
            return None, "not_a_spec"
        if produced.name != name:
            self.discovery_errors[name] = "name_mismatch"
            return None, "name_mismatch"
        del self._entry_points[name]
        self._specs[name] = produced
        return produced, ""

    def load(self, name: str, config: AdaptersConfig) -> AdapterStatus:
        """Build one adapter if it is enabled and its dependencies are present."""

        enabled = config.is_enabled(name)
        known = name in self._specs or name in self._entry_points
        if not enabled:
            reason = "disabled" if known else self.discovery_errors.get(name, "unknown_adapter")
            kind = self._specs[name].kind if name in self._specs else ""
            return AdapterStatus(name, kind, False, False, reason)
        spec, error = self._resolve(name)
        if spec is None:
            return AdapterStatus(name, "", True, False, error)
        if missing_modules(spec.requires):
            return AdapterStatus(name, spec.kind, True, False, "missing_dependency")
        settings = dict(config.settings.get(name, {}))
        adapter, error = _guarded(lambda: spec.factory(settings))
        if error:
            return AdapterStatus(
                name, spec.kind, True, False, "factory_error" if error == "load_error" else error
            )
        if not isinstance(adapter, _PROTOCOLS[spec.kind]):
            return AdapterStatus(name, spec.kind, True, False, "protocol_mismatch")
        if getattr(adapter, "name", None) != name:
            return AdapterStatus(name, spec.kind, True, False, "name_mismatch")
        availability, error = _guarded(adapter.available)  # type: ignore[attr-defined]
        if error or not isinstance(availability, Availability):
            return AdapterStatus(name, spec.kind, True, False, "availability_error")
        if not availability.available:
            return AdapterStatus(name, spec.kind, True, False, availability.reason or "unavailable")
        return AdapterStatus(name, spec.kind, True, True, "", adapter)

    def load_enabled(
        self, config: AdaptersConfig, *, kind: AdapterKind | None = None
    ) -> tuple[AdapterStatus, ...]:
        """Status of every enabled adapter (and enabled names nobody registered)."""

        statuses = [self.load(name, config) for name in sorted(config.enabled)]
        if kind is not None:
            statuses = [status for status in statuses if status.kind in {kind, ""}]
        return tuple(statuses)


_DEFAULT = AdapterRegistry()


def default_registry() -> AdapterRegistry:
    return _DEFAULT


def register(spec: AdapterSpec, *, replace: bool = False) -> None:
    """Register on the process-wide registry."""

    _DEFAULT.register(spec, replace=replace)


__all__ = [
    "ADAPTER_KINDS",
    "ENTRY_POINT_GROUP",
    "AdapterInfo",
    "AdapterKind",
    "AdapterRegistry",
    "AdapterSpec",
    "AdapterStatus",
    "AdaptersConfig",
    "default_registry",
    "missing_modules",
    "register",
]
