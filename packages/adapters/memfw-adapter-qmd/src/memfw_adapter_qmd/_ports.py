"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports`` (the classes the
recall coordinator checks by type, ``ProviderUnavailable`` included). Without
memfw (the standalone wheels) they come from the ``memfw-ports`` wheel's
``memfw_ports``. Either way only L0 port modules are imported, nothing else of
memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.privacy import PRIVACY_LEVELS, admits, metadata_level, strictest
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.types import (
        MAX_REF_CHARS,
        MEMORY_ID_RE,
        PATH_REF_RE,
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import (  # type: ignore[no-redef]
        PRIVACY_LEVELS,
        admits,
        metadata_level,
        strictest,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef]
    from memfw_ports.types import (  # type: ignore[no-redef]
        MAX_REF_CHARS,
        MEMORY_ID_RE,
        PATH_REF_RE,
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
    )

__all__ = [
    "MAX_REF_CHARS",
    "MEMORY_ID_RE",
    "PATH_REF_RE",
    "PRIVACY_LEVELS",
    "AdapterSpec",
    "Availability",
    "Candidate",
    "ProviderUnavailable",
    "RecallBudget",
    "admits",
    "metadata_level",
    "strictest",
]
