"""Session-start context (CAG): the capability behind MCP ``memory_context``.

Layer L3. It reads the vault through ``memfw.core`` and returns the L0
``ContextPack`` contract (``memfw.ports``). It imports no other capability
(the visibility predicate is injected) and no interface layer (MCP, HTTP,
CLI). C2: several slots per pack (``slots``), each from a
``ContextSlotProvider``; providers that need another capability get it
injected by ``memfw.runtime``. U6: the optional ``current`` slot
(``memfw.context.current``), short-lived context with a lifetime, only when
the vault enables it.
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
from .current import (
    CLOCK_SKEW,
    CURRENT_SLOT,
    CURRENT_TAG,
    DEFAULT_CURRENT_TTL_DAYS,
    MAX_CURRENT_TTL_DAYS,
    CurrentSlotProvider,
    is_current,
    select_current,
)
from .slots import (
    DEFAULT_SLOTS,
    HANDOFF_SLOT,
    OPTIONAL_SLOTS,
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
    "CLOCK_SKEW",
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "CURRENT_SLOT",
    "CURRENT_TAG",
    "DEFAULT_CURRENT_TTL_DAYS",
    "DEFAULT_SLOTS",
    "HANDOFF_SLOT",
    "MAX_CURRENT_TTL_DAYS",
    "OPTIONAL_SLOTS",
    "SLOT_NAMES",
    "TOPICS_SLOT",
    "CoreCandidates",
    "CoreSlotProvider",
    "CurrentSlotProvider",
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
    "is_current",
    "select_core",
    "select_current",
    "slots_request",
]
