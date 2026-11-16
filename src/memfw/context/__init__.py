"""Session-start context (CAG): the capability behind MCP ``memory_context``.

Layer L3. It reads the vault through ``memfw.core`` and returns the L0
``ContextPack`` contract (``memfw.ports``). It imports no other capability
(the visibility predicate is injected) and no interface layer (MCP, HTTP,
CLI).
"""

from __future__ import annotations

from .core import (
    CONTEXT_POLICIES,
    CONTEXT_VERSIONS,
    CORE_SLOT,
    Keep,
    build_core_pack,
    context_request,
    core_context_v1,
    estimate_tokens,
)

__all__ = [
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "build_core_pack",
    "context_request",
    "core_context_v1",
    "Keep",
    "estimate_tokens",
]
