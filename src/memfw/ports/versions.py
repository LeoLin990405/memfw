"""Every port's contract version (``PORT_VERSION``), in one table. Stdlib only.

Architecture section 4: each protocol carries a contract version; an
implementation declares which one it was checked against and the composition
root checks they agree. ``PORT_VERSIONS`` names them all. A breaking change to
one port bumps its number here (and ``PORTS_API_VERSION``'s major); the
package compatibility table in ``docs/ports-and-recall.md`` section 1.1 is
tested against this mapping and the package metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .claims import CLAIMS_PORT_VERSION
from .entailment import ENTAILMENT_PORT_VERSION
from .models import MODELS_PORT_VERSION
from .projection_store import PROJECTION_STORE_PORT_VERSION
from .usage import USAGE_PORT_VERSION

PORT_VERSIONS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "RecallProvider": 1,
        "Projector": 1,
        "Ingestor": 1,
        "Deriver": 1,
        "Clock": 1,
        "IdGen": 1,
        "ClaimVerifier": CLAIMS_PORT_VERSION,
        "ClaimStager": CLAIMS_PORT_VERSION,
        "ContextSlotProvider": 1,
        "UsageSink": USAGE_PORT_VERSION,
        "ModelBackend": MODELS_PORT_VERSION,
        "ProjectionStore": PROJECTION_STORE_PORT_VERSION,
        "EntailmentChecker": ENTAILMENT_PORT_VERSION,
    }
)

__all__ = ["PORT_VERSIONS"]
