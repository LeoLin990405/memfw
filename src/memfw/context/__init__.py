"""Session-start context (CAG): the capability behind MCP ``memory_context``.

Layer L3. It reads the vault through ``memfw.core``, filters with the recall
``FilterPolicy`` (``memfw.recall``) and returns the L0 ``ContextPack``
contract (``memfw.ports``). It never imports an interface layer (MCP, HTTP,
CLI); those only parse arguments and serialise what this returns.
"""

from __future__ import annotations

from .core import (
    CONTEXT_POLICIES,
    CONTEXT_VERSIONS,
    CORE_SLOT,
    build_core_pack,
    context_request,
    core_context_v1,
    estimate_tokens,
    filter_policy,
)

__all__ = [
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "build_core_pack",
    "context_request",
    "core_context_v1",
    "estimate_tokens",
    "filter_policy",
]
