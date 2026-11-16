"""Port protocols for the memfw compat framework.

memfw is the authoritative kernel plus a coordination layer; everything else
plugs in through one of four ports (see ``docs/ports-and-recall.md``):

- ``RecallProvider``: query → candidates (refs and scores, never body text)
- ``Projector``: vault → external format, with a manifest
- ``Ingestor``: external → staged candidates under ``_candidates/``
- ``Deriver``: vault → read-only view with citations and an epistemic cap

plus the ``Clock`` and ``IdGen`` ports the kernel takes time and ids from.
"""

from __future__ import annotations

from .clock import Clock, FixedClock, IdGen, SeededIdGen, SystemClock, UuidIdGen
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

__all__ = [
    "ADAPTER_KINDS",
    "CANDIDATES_DIR",
    "ENTRY_POINT_GROUP",
    "ALLOWED_EXTRA_KEYS",
    "REF_KINDS",
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
    "StageResult",
    "StagingError",
    "SystemClock",
    "UuidIdGen",
    "candidate_directory",
    "candidate_id",
    "check_name",
    "check_view_citations",
    "default_registry",
    "missing_modules",
    "register",
    "stage_candidates",
    "validate_candidate",
]
