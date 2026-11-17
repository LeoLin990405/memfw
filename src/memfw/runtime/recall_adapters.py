"""Operator-configured recall adapters for ``mode=two_stage`` (``runtime.recall.adapters``).

Off by default: without the key nothing is read beyond ``vault.yaml`` and the
two_stage providers are exactly FTS (plus hybrid with an embedder), as before.
With it, each entry names an adapter's ``module:factory`` and its options::

    runtime:
      recall:
        adapters:
          - factory: "memfw_adapter_bm25s:factory"
            options: {clearance: "public,shared"}
            weight: 1.0            # RRF weight (default 1.0)
            timeout_seconds: 3.0   # per-provider wait (default: the runtime's)

The factory is called with one mapping: the entry's ``options`` plus two keys
the runtime fills in, ``vault`` (the vault root, a string) and ``embedder``
(the runtime's embedding provider, the ``MEMFW_EMBEDDING_*`` one, or
``None``). ``options`` may not set them. It must return a ``RecallProvider``
whose name is not taken. A factory should be cheap (check settings, build
objects): it runs on the first ``two_stage`` call, possibly more than once
when calls race, and network or heavy I/O belongs in ``available()`` and
``search()``, which the coordinator bounds by the provider's budget. The
kernel never imports an adapter by name: the module comes only from this
operator setting, never from a request.

Fails closed: a malformed section or an adapter that cannot be loaded raises
``RecallAdapterError`` (a ``ValueError`` with a fixed ``code``), so
``two_stage`` errors instead of silently running without it. An adapter that
loads but cannot serve (its service is down) is the coordinator's business:
it is reported as degraded and the other providers answer (T7).
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from memfw.ports.protocols import RecallProvider

#: The keys the runtime fills in; ``options`` may not set them.
INJECTED_KEYS: Final = ("vault", "embedder")
#: At most this many adapters (each holds a worker thread while it runs).
MAX_ADAPTERS: Final = 8
#: Longest accepted ``timeout_seconds``.
MAX_TIMEOUT_SECONDS: Final = 30.0
_SPEC = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
_ENTRY_KEYS: Final = frozenset({"factory", "options", "weight", "timeout_seconds"})


class RecallAdapterError(ValueError):
    """A recall adapter setting that cannot be used; ``code`` is a fixed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(f"recall adapter: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class AdapterEntry:
    """One checked ``runtime.recall.adapters`` entry."""

    factory: str
    options: Mapping[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LoadedAdapter:
    """A built provider and the coordinator settings its entry asked for."""

    provider: RecallProvider
    weight: float
    timeout_seconds: float | None


def _number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecallAdapterError(code)
    number = float(value)
    if not math.isfinite(number):
        raise RecallAdapterError(code)
    return number


def parse_entries(config: Mapping[str, Any]) -> tuple[AdapterEntry, ...]:
    """The checked entries of a parsed ``vault.yaml``; none without the key."""

    section = config.get("runtime")
    if section is None:
        return ()
    if not isinstance(section, Mapping):
        raise RecallAdapterError("bad_config")
    recall = section.get("recall")
    if recall is None:
        return ()
    if not isinstance(recall, Mapping):
        raise RecallAdapterError("bad_config")
    raw = recall.get("adapters")
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_ADAPTERS:
        raise RecallAdapterError("bad_config")
    entries: list[AdapterEntry] = []
    for item in raw:
        if not isinstance(item, Mapping) or not set(item) <= _ENTRY_KEYS:
            raise RecallAdapterError("bad_entry")
        spec = item.get("factory")
        if not isinstance(spec, str) or not _SPEC.fullmatch(spec):
            raise RecallAdapterError("bad_factory")
        options = item.get("options", {})
        if options is None:
            options = {}
        if not isinstance(options, Mapping) or not all(isinstance(k, str) for k in options):
            raise RecallAdapterError("bad_options")
        if any(key in options for key in INJECTED_KEYS):
            raise RecallAdapterError("reserved_option")
        weight = _number(item.get("weight", 1.0), "bad_weight")
        if weight < 0:
            raise RecallAdapterError("bad_weight")
        timeout: float | None = None
        if item.get("timeout_seconds") is not None:
            timeout = _number(item["timeout_seconds"], "bad_timeout")
            if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
                raise RecallAdapterError("bad_timeout")
        entries.append(AdapterEntry(spec, dict(options), weight, timeout))
    return tuple(entries)


def load_adapters(
    entries: Sequence[AdapterEntry],
    *,
    root: Path,
    embedder: Any,
    taken: Sequence[str] = (),
) -> tuple[LoadedAdapter, ...]:
    """Import and build each entry's provider; ``taken`` are names already in use."""

    names = set(taken)
    loaded: list[LoadedAdapter] = []
    for entry in entries:
        module_name, _, attribute = entry.factory.partition(":")
        try:
            factory = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError):
            raise RecallAdapterError("not_importable") from None
        if not callable(factory):
            raise RecallAdapterError("not_importable")
        settings = {**entry.options, "vault": str(root), "embedder": embedder}
        try:
            provider = factory(settings)
        except Exception:  # noqa: BLE001 - an adapter's message never reaches the caller
            raise RecallAdapterError("factory_error") from None
        if not isinstance(provider, RecallProvider):
            raise RecallAdapterError("not_a_provider")
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or name in names:
            raise RecallAdapterError("duplicate_name")
        names.add(name)
        loaded.append(LoadedAdapter(provider, entry.weight, entry.timeout_seconds))
    return tuple(loaded)


__all__ = [
    "INJECTED_KEYS",
    "MAX_ADAPTERS",
    "AdapterEntry",
    "LoadedAdapter",
    "RecallAdapterError",
    "load_adapters",
    "parse_entries",
]
