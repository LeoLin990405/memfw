"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports`` (the same
``Candidate`` class the coordinator validates). Without memfw (the standalone
wheels) they come from the ``memfw-ports`` wheel's ``memfw_ports``. Either way
only L0 port modules are imported: the value types, the privacy rules and the
stdlib-only vault reader of ``reference``; nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.privacy import (
        LEGACY_PRIVACY_FALLBACK,
        LEGACY_PRIVACY_KEY,
        admits,
        effective_level,
        legacy_default,
        parse_clearance,
    )
    from memfw.ports.reference._vault import MemoryFile, is_vault, iter_memories, memory_ids
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.types import (
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
        check_name,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import (  # type: ignore[no-redef,assignment,unused-ignore]
        LEGACY_PRIVACY_FALLBACK,
        LEGACY_PRIVACY_KEY,
        admits,
        effective_level,
        legacy_default,
        parse_clearance,
    )
    from memfw_ports.reference._vault import (  # type: ignore[no-redef,assignment,unused-ignore]
        MemoryFile,
        is_vault,
        iter_memories,
        memory_ids,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
        check_name,
    )

__all__ = [
    "LEGACY_PRIVACY_FALLBACK",
    "LEGACY_PRIVACY_KEY",
    "AdapterSpec",
    "Availability",
    "Candidate",
    "MemoryFile",
    "ProviderUnavailable",
    "RecallBudget",
    "admits",
    "check_name",
    "effective_level",
    "is_vault",
    "iter_memories",
    "legacy_default",
    "memory_ids",
    "parse_clearance",
]
