"""Session-start context (CAG): the capability behind MCP ``memory_context``.

Layer L3. It reads the vault through ``memfw.core`` and returns the L0
``ContextPack`` contract (``memfw.ports``). It imports no other capability
(the visibility predicate is injected) and no interface layer (MCP, HTTP,
CLI). C2: several slots per pack (``slots``), each from a
``ContextSlotProvider``; providers that need another capability get it
injected by ``memfw.runtime``.
"""

from __future__ import annotations

from .core import (
    CONTEXT_POLICIES,
    CONTEXT_VERSIONS,
    CORE_SLOT,
    CoreCandidates,
    Keep,
    build_core_pack,
    build_core_slot,
    context_request,
    core_candidates,
    core_context_v1,
    estimate_tokens,
    if_version_request,
    is_core_shaped,
    select_core,
)
from .slots import (
    DEFAULT_SLOTS,
    HANDOFF_SLOT,
    SLOT_NAMES,
    TOPICS_SLOT,
    CoreSlotProvider,
    HandoffSlotProvider,
    SessionFeed,
    SessionSummary,
    SlotDegraded,
    SlotInflight,
    SlotUnavailable,
    TopicCatalogue,
    TopicsSlotProvider,
    assemble_pack,
    cut_items,
    slots_request,
)

__all__ = [
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "DEFAULT_SLOTS",
    "HANDOFF_SLOT",
    "SLOT_NAMES",
    "TOPICS_SLOT",
    "CoreCandidates",
    "CoreSlotProvider",
    "HandoffSlotProvider",
    "Keep",
    "SessionFeed",
    "SessionSummary",
    "SlotDegraded",
    "SlotInflight",
    "SlotUnavailable",
    "TopicCatalogue",
    "TopicsSlotProvider",
    "assemble_pack",
    "build_core_pack",
    "build_core_slot",
    "context_request",
    "core_candidates",
    "core_context_v1",
    "cut_items",
    "estimate_tokens",
    "if_version_request",
    "is_core_shaped",
    "select_core",
    "slots_request",
]
