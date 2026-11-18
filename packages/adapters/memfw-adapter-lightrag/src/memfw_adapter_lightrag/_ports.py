"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw
(the standalone wheels) from the ``memfw-ports`` wheel's ``memfw_ports``.
Either way only L0 port modules are imported, nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.privacy import metadata_level
    from memfw.ports.reference._vault import MemoryFile, is_vault, read_memory
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.types import (
        Availability,
        Citation,
        DerivedView,
        DeriveRequest,
        IngestItem,
        ProviderUnavailable,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import metadata_level  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.reference._vault import (  # type: ignore[no-redef,assignment,unused-ignore]
        MemoryFile,
        is_vault,
        read_memory,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        Availability,
        Citation,
        DerivedView,
        DeriveRequest,
        IngestItem,
        ProviderUnavailable,
    )

__all__ = [
    "AdapterSpec",
    "Availability",
    "Citation",
    "DeriveRequest",
    "DerivedView",
    "IngestItem",
    "MemoryFile",
    "ProviderUnavailable",
    "is_vault",
    "metadata_level",
    "read_memory",
]
