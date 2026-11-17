"""Port protocols for the memfw compat framework.

memfw is the authoritative kernel plus a coordination layer; everything else
plugs in through one of four ports (see ``docs/ports-and-recall.md``):

- ``RecallProvider``: query → candidates (refs and scores, never body text)
- ``Projector``: vault → external format, with a manifest
- ``Ingestor``: external → staged candidates under ``_candidates/``
- ``Deriver``: vault → read-only view with citations and an epistemic cap

plus the ``Clock`` and ``IdGen`` ports the kernel takes time and ids from,
the ``ClaimVerifier`` / ``ClaimStager`` ports the conversation worker
verifies and stages claims through, and the ``UsageSink`` port the optional
``memory_used`` signal goes through (``memfw.ports.usage``).

``ContextPack`` (``memfw.ports.context``) is the data contract for the
session-start context that ``memory_context`` v2 returns.
"""

from __future__ import annotations

from typing import Final

from .claims import (
    CLAIMS_PORT_VERSION,
    POLARITIES,
    ClaimEvidence,
    ClaimStager,
    ClaimVerifier,
    StagedClaim,
)
from .clock import Clock, FixedClock, IdGen, SeededIdGen, SystemClock, UuidIdGen
from .context import (
    ContextEntry,
    ContextItem,
    ContextPack,
    ContextSlot,
    ContextSlotProvider,
    SlotRequest,
    compute_pack_version,
    item_content_hash,
    pack_version_payload,
)
from .protocols import Deriver, Ingestor, Projector, RecallProvider
from .registry import (
    ADAPTER_KINDS,
    ENTRY_POINT_GROUP,
    AdapterInfo,
    AdapterKind,
    AdapterRegistry,
    AdaptersConfig,
    AdapterSpec,
    AdapterStatus,
    default_registry,
    missing_modules,
    register,
)
from .staging import (
    CANDIDATES_DIR,
    StagingError,
    candidate_directory,
    candidate_id,
    check_view_citations,
    stage_candidates,
)
from .types import (
    ALLOWED_EXTRA_KEYS,
    REF_KINDS,
    Availability,
    Candidate,
    Citation,
    DerivedView,
    DeriveRequest,
    ExtraValue,
    IngestItem,
    Manifest,
    ProjectionPolicy,
    ProviderUnavailable,
    RecallBudget,
    RefKind,
    StageResult,
    check_name,
    validate_candidate,
)
from .usage import (
    MAX_USAGE_IDS,
    SESSION_REF_RULE,
    USAGE_PORT_VERSION,
    UsageSignal,
    UsageSink,
)

#: Version of the ``memfw.ports`` API as a whole (semver). The standalone
#: ``memfw-ports`` wheel carries exactly this version, and
#: ``tests/test_standalone_ports_wheel.py`` keeps the two equal: bump it (and
#: the wheel) with every change to the ports API, major for a breaking one.
PORTS_API_VERSION: Final = "0.2.0"

__all__ = [
    "ADAPTER_KINDS",
    "CLAIMS_PORT_VERSION",
    "MAX_USAGE_IDS",
    "PORTS_API_VERSION",
    "POLARITIES",
    "ClaimEvidence",
    "ClaimStager",
    "ClaimVerifier",
    "StagedClaim",
    "CANDIDATES_DIR",
    "ENTRY_POINT_GROUP",
    "ALLOWED_EXTRA_KEYS",
    "REF_KINDS",
    "SESSION_REF_RULE",
    "USAGE_PORT_VERSION",
    "AdapterInfo",
    "AdapterKind",
    "AdapterRegistry",
    "AdapterSpec",
    "AdapterStatus",
    "AdaptersConfig",
    "Availability",
    "Candidate",
    "Citation",
    "Clock",
    "ContextEntry",
    "ContextItem",
    "ContextPack",
    "ContextSlot",
    "ContextSlotProvider",
    "DeriveRequest",
    "DerivedView",
    "Deriver",
    "ExtraValue",
    "FixedClock",
    "IdGen",
    "IngestItem",
    "Ingestor",
    "Manifest",
    "ProjectionPolicy",
    "Projector",
    "ProviderUnavailable",
    "RecallBudget",
    "RecallProvider",
    "RefKind",
    "SeededIdGen",
    "SlotRequest",
    "StageResult",
    "StagingError",
    "SystemClock",
    "UsageSignal",
    "UsageSink",
    "UuidIdGen",
    "candidate_directory",
    "candidate_id",
    "check_name",
    "check_view_citations",
    "compute_pack_version",
    "default_registry",
    "item_content_hash",
    "missing_modules",
    "pack_version_payload",
    "register",
    "stage_candidates",
    "validate_candidate",
]
