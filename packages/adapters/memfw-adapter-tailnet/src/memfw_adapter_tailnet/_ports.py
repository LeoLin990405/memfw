"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw
(the standalone wheels) from the ``memfw-ports`` wheel. Either way only L0
port modules are imported, nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.privacy import PRIVACY_LEVELS, privacy_value
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.staging import StagingError, stage_candidates
    from memfw.ports.types import Availability, ExtraValue, IngestItem, StageResult
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import (  # type: ignore[no-redef,assignment,unused-ignore]
        PRIVACY_LEVELS,
        privacy_value,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.staging import (  # type: ignore[no-redef,assignment,unused-ignore]
        StagingError,
        stage_candidates,
    )
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        Availability,
        ExtraValue,
        IngestItem,
        StageResult,
    )

__all__ = [
    "PRIVACY_LEVELS",
    "AdapterSpec",
    "Availability",
    "ExtraValue",
    "IngestItem",
    "StageResult",
    "StagingError",
    "privacy_value",
    "stage_candidates",
]
