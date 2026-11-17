"""The composition root (memfw-architecture section 5, layer L4).

``build_runtime(...)`` turns resolved settings into a ``Runtime``; interfaces
(L5) get capability objects from it instead of constructing implementations
themselves. A3a covers recall: ``Runtime.recall`` assembles the
``mode=two_stage`` providers, resolvers and filter policy and runs the
coordinator, with one ``InflightRegistry`` per Runtime (one Runtime per
Gateway); ``Runtime.ensure_index`` is the index staleness check. C1 adds the
``memory_context`` wiring: ``Runtime.context_v1`` and ``Runtime.context_pack``
build the ``memfw.context`` core slot with the visibility predicate
(``Runtime.context_keep``: tenant/source, or the recall strict policy). C2
composes the other slots (``Runtime.context_slot_provider``): ``handoff``
reads the conversation share as the calling app, ``topics`` the concept
catalogue (``context_sources``), so ``memfw.context`` imports neither.
C3 adds the ``core`` slot's id cache (``context_cache.ContextCache``, one per
Runtime unless injected or disabled), which never changes the output.

A3b moves the rest of the MCP server's wiring here: ``resolve_settings``
(explicit values, else ``MEMFW_*``, the embedder included),
``Runtime.open_repository`` (per call, never creating a vault), every
``memory_search`` mode with its row building (``Runtime.search``), and the
remember, related, status, conversation share, history and dream facades.
The capability modules behind them are imported on first use.

C5/C6 add the first ``runtime:`` switches in ``vault.yaml`` (``memfw.runtime.usage``):
``usage.enabled`` registers ``memory_used`` (``Runtime.memory_used``, a
``UsageSink``), ``search.record_access: false`` is the session-safe setting.
Without the section the default assembly is exactly what the MCP server built
before (byte-identical output except timing fields).

Importing this package loads ``memfw.recall`` (the coordinator) but not the
built-in providers; those are imported on the first ``recall`` call.
"""

from __future__ import annotations

from .index import ensure_search_index
from .runtime import (
    CONTEXT_SLOT_TIMEOUTS,
    RECALL_POLICIES,
    TWO_STAGE_PROVIDER_TIMEOUT,
    TWO_STAGE_TOTAL_TIMEOUT,
    Observed,
    RecallOutcome,
    Runtime,
    build_runtime,
)
from .settings import RuntimeSettings, history_refresh_seconds, resolve_settings

__all__ = [
    "CONTEXT_SLOT_TIMEOUTS",
    "RECALL_POLICIES",
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "RuntimeSettings",
    "build_runtime",
    "ensure_search_index",
    "history_refresh_seconds",
    "resolve_settings",
]
