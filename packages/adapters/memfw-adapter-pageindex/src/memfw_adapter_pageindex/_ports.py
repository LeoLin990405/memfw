"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports`` (the classes the
coordinator and the registry check). Without memfw (the standalone wheels)
they come from the ``memfw-ports`` wheel. Either way only L0 port modules are
imported, nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.models import (
        BackendBudgetExhausted,
        BackendUnavailable,
        ModelReply,
        render_tag_data,
    )
    from memfw.ports.privacy import PRIVACY_LEVELS, field_level, metadata_level, strictest
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.types import (
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
        validate_candidate,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.models import (  # type: ignore[no-redef,assignment,unused-ignore]
        BackendBudgetExhausted,
        BackendUnavailable,
        ModelReply,
        render_tag_data,
    )
    from memfw_ports.privacy import (  # type: ignore[no-redef,assignment,unused-ignore]
        PRIVACY_LEVELS,
        field_level,
        metadata_level,
        strictest,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        Availability,
        Candidate,
        ProviderUnavailable,
        RecallBudget,
        validate_candidate,
    )

__all__ = [
    "PRIVACY_LEVELS",
    "AdapterSpec",
    "Availability",
    "BackendBudgetExhausted",
    "BackendUnavailable",
    "Candidate",
    "ModelReply",
    "ProviderUnavailable",
    "RecallBudget",
    "field_level",
    "metadata_level",
    "render_tag_data",
    "strictest",
    "validate_candidate",
]
