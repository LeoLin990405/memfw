"""The one place ``memfw.recall`` takes names from the ports.

When this package sits inside a parent package (``memfw.recall``, or a
vendored ``<vendor>.memfw.recall``), the names come from the sibling
``ports`` package by a relative import: inside memfw that is
``memfw.ports``, the same objects the coordinator imported before, so memfw's
behaviour does not change. When it is a top-level package (the standalone
``memfw-recall`` wheel ships these files as ``memfw_recall``), the names come
from the ``memfw-ports`` wheel's ``memfw_ports``. The choice follows this
module's own name, never what happens to be installed, so a process with both
memfw and the standalone wheels never mixes the two copies of a dataclass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING or "." in (__package__ or ""):
    from ..ports.protocols import RecallProvider
    from ..ports.types import (
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
