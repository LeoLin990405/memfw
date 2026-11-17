"""The one place ``memfw.recall`` takes names from the ports.

Inside memfw this module is ``memfw.recall._ports`` and re-exports
``memfw.ports``: the same objects the coordinator imported before, so memfw's
behaviour does not change. The standalone ``memfw-recall`` wheel ships these
same files as the top-level package ``memfw_recall``, next to the
``memfw-ports`` wheel's ``memfw_ports``; there the names come from
``memfw_ports``. The choice follows this module's own name, never what
happens to be installed, so a process with both memfw and the standalone
wheels never mixes the two copies of a dataclass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING or __name__.partition(".")[0] == "memfw":
    from memfw.ports.protocols import RecallProvider
    from memfw.ports.types import (
        MEMORY_ID_RE,
        Availability,
        Candidate,
        ExtraValue,
        ProviderUnavailable,
        RecallBudget,
        RefKind,
        check_name,
        validate_candidate,
    )
else:  # the standalone memfw-recall wheel (packages/memfw-recall)
    from memfw_ports.protocols import RecallProvider
    from memfw_ports.types import (
        MEMORY_ID_RE,
        Availability,
        Candidate,
        ExtraValue,
        ProviderUnavailable,
        RecallBudget,
        RefKind,
        check_name,
        validate_candidate,
    )

__all__ = [
    "MEMORY_ID_RE",
    "Availability",
    "Candidate",
    "ExtraValue",
    "ProviderUnavailable",
    "RecallBudget",
    "RecallProvider",
    "RefKind",
    "check_name",
    "validate_candidate",
]
