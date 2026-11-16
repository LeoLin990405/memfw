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

There is no ``runtime:`` section in ``vault.yaml`` yet; the default assembly
is exactly what the MCP server built before (byte-identical output except
timing fields).

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

__all__ = [
    "CONTEXT_SLOT_TIMEOUTS",
    "RECALL_POLICIES",
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "build_runtime",
    "ensure_search_index",
]
