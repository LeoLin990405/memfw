"""The composition root (memfw-architecture section 5, layer L4).

``build_runtime(...)`` turns resolved settings into a ``Runtime``; interfaces
(L5) get capability objects from it instead of constructing implementations
themselves. A3a covers recall only: ``Runtime.recall`` assembles the
``mode=two_stage`` providers, resolvers and filter policy and runs the
coordinator, with one ``InflightRegistry`` per runtime.

There is no ``runtime:`` section in ``vault.yaml`` yet; the default assembly
is exactly what the MCP server built before (byte-identical output).

Importing this package loads ``memfw.recall`` (the coordinator) but not the
built-in providers; those are imported on the first ``recall`` call.
"""

from __future__ import annotations

from .runtime import (
    TWO_STAGE_PROVIDER_TIMEOUT,
    TWO_STAGE_TOTAL_TIMEOUT,
    Observed,
    RecallOutcome,
    Runtime,
    build_runtime,
)

__all__ = [
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "build_runtime",
]
