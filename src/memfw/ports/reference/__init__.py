"""Reference implementations of every port. Stdlib only; imports only ``memfw.ports``.

Each port has at least two implementations that pass the same
``memfw.ports.testing`` contract check (the A5 swap drills,
``tests/test_swap_drills.py``). This package holds the ones memfw did not
already have, small but real, for adapters to build on and tests to swap in:

============================  ======================================
Port                          Reference implementation(s)
============================  ======================================
``RecallProvider``            ``InMemoryRecallProvider`` (BM25-lite)
``Projector``                 ``JsonlProjector``, ``MirrorProjector``
``Ingestor``                  ``JsonIngestor``, ``MarkdownIngestor``
``Deriver``                   ``ConcatDeriver``, ``TimelineDeriver``
``ClaimVerifier``             ``KeywordClaimVerifier``
``ClaimStager``               ``FileClaimStager``
============================  ======================================

``Clock`` and ``IdGen`` already have two each in ``memfw.ports.clock``.

Nothing here is registered or enabled by default. ``register_reference``
puts the recall, projector, ingestor and deriver specs on a registry; each
stays off until an ``AdaptersConfig`` lists it under ``enabled``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registry import AdapterRegistry, AdapterSpec, default_registry
from .claims import FileClaimStager, KeywordClaimVerifier
from .derivers import ConcatDeriver, TimelineDeriver
from .ingestors import (
    JsonIngestor,
    MarkdownIngestor,
    items_from_json,
    items_from_json_file,
    items_from_markdown,
)
from .projectors import JsonlProjector, MirrorProjector
from .recall import InMemoryRecallProvider, tokenize

_SOURCE = "memfw.ports.reference"


def _recall(settings: Mapping[str, Any]) -> InMemoryRecallProvider:
    vault = settings.get("vault")
    if not isinstance(vault, str) or not vault:
        raise ValueError("the inmemory recall adapter needs a 'vault' setting")
    return InMemoryRecallProvider.from_vault(Path(vault))


def reference_specs() -> tuple[AdapterSpec, ...]:
    """Adapter specs for the registry-kind reference implementations.

    The ``inmemory`` recall adapter reads its vault from the ``vault`` setting
    (config only, as for every adapter); the others take no settings.
    """

    return (
        AdapterSpec("inmemory", "recall", _recall, source=_SOURCE),
        AdapterSpec("jsonl-index", "projector", lambda _s: JsonlProjector(), source=_SOURCE),
        AdapterSpec("markdown-mirror", "projector", lambda _s: MirrorProjector(), source=_SOURCE),
        AdapterSpec("json", "ingestor", lambda _s: JsonIngestor(), source=_SOURCE),
        AdapterSpec("markdown", "ingestor", lambda _s: MarkdownIngestor(), source=_SOURCE),
        AdapterSpec("concat", "deriver", lambda _s: ConcatDeriver(), source=_SOURCE),
        AdapterSpec("timeline", "deriver", lambda _s: TimelineDeriver(), source=_SOURCE),
    )


def register_reference(registry: AdapterRegistry | None = None) -> tuple[str, ...]:
    """Register every reference spec (default: the process-wide registry).

    Registering does not enable: an adapter runs only when the config lists
    it. A name that is already registered is left alone. Returns the names
    that were added.
    """

    target = registry if registry is not None else default_registry()
    added: list[str] = []
    for spec in reference_specs():
        if target.get(spec.name) is not None:
            continue
        try:
            target.register(spec)
        except ValueError:
            continue  # an installed entry point already uses the name
        added.append(spec.name)
    return tuple(added)


__all__ = [
    "ConcatDeriver",
    "FileClaimStager",
    "InMemoryRecallProvider",
    "JsonIngestor",
    "JsonlProjector",
    "KeywordClaimVerifier",
    "MarkdownIngestor",
    "MirrorProjector",
    "TimelineDeriver",
    "items_from_json",
    "items_from_json_file",
    "items_from_markdown",
    "reference_specs",
    "register_reference",
    "tokenize",
]
