"""Port protocols for the memfw compat framework.

memfw is the authoritative kernel plus a coordination layer; everything else
plugs in through one of four ports (see ``docs/ports-and-recall.md``):

- ``RecallProvider``: query → candidates (refs and scores, never body text)
- ``Projector``: vault → external format, with a manifest
- ``Ingestor``: external → staged candidates under ``_candidates/``
- ``Deriver``: vault → read-only view with citations and an epistemic cap

plus the ``Clock`` and ``IdGen`` ports the kernel takes time and ids from,
the ``ClaimVerifier`` / ``ClaimStager`` ports the conversation worker
verifies and stages claims through, the ``UsageSink`` port the optional
``memory_used`` signal goes through (``memfw.ports.usage``), and the
``ModelBackend`` port (``memfw.ports.models``) the worker summarises and tags
through, and the ``ProjectionStore`` port (``memfw.ports.projection_store``)
the K3 edge projection is stored behind, with the K4 bounded path patterns
over it (``memfw.ports.patterns``).

``ContextPack`` (``memfw.ports.context``) is the data contract for the
session-start context that ``memory_context`` v2 returns, and
``ProxyImport`` (``memfw.ports.proxies``) the file format an OCR or
transcription adapter writes for ``memfw attach proxy import``.

Ports 0.4.0 (W1-1, LLM Wiki) adds ``Citation.ref`` / ``ref_kind``
(``memfw.ports.refs``), the ``memfw-wiki-page/1`` and ``memfw-wiki-job/1``
formats (``memfw.ports.wiki``) and the ``EntailmentChecker`` port
(``memfw.ports.entailment``).
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
from .entailment import ENTAILMENT_PORT_VERSION, EntailmentChecker
from .models import (
    MODELS_PORT_VERSION,
    TAG_PROMPT_VERSION,
    BackendBudgetExhausted,
    BackendUnavailable,
    ModelBackend,
    ModelReply,
)
from .projection_store import (
    PROJECTION_STORE_PORT_VERSION,
    Edge,
    EdgeScope,
    ProjectionStore,
    RebuildResult,
    edge_set_version,
    edge_visible,
)
from .protocols import Deriver, Ingestor, Projector, RecallProvider
from .proxies import (
    PROXY_FORMAT,
    PROXY_FORMAT_VERSION,
    PROXY_KINDS,
    CoordinateSpace,
    Coverage,
    Locator,
    ProxyFormatError,
    ProxyImport,
    ProxySegment,
    SourceDimensions,
    load_proxy_import,
    parse_proxy_import,
)
from .refs import CITATION_REF_KINDS, CitationRefKind
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
from .versions import PORT_VERSIONS
from .wiki import (
    WIKI_JOB_FORMAT,
    WIKI_PAGE_FORMAT,
    WikiBuild,
    WikiClaim,
    WikiFormatError,
    WikiJob,
    WikiPage,
    WikiSource,
    parse_wiki_job,
    parse_wiki_page,
)

#: Version of the ``memfw.ports`` API as a whole (semver). The standalone
#: ``memfw-ports`` wheel carries exactly this version, and
#: ``tests/test_standalone_ports_wheel.py`` keeps the two equal: bump it (and
#: the wheel) with every change to the ports API, major for a breaking one.
PORTS_API_VERSION: Final = "0.4.0"

__all__ = [
    "ADAPTER_KINDS",
    "CITATION_REF_KINDS",
    "CLAIMS_PORT_VERSION",
    "ENTAILMENT_PORT_VERSION",
    "MAX_USAGE_IDS",
    "MODELS_PORT_VERSION",
    "TAG_PROMPT_VERSION",
    "PORTS_API_VERSION",
    "PORT_VERSIONS",
    "PROJECTION_STORE_PORT_VERSION",
    "POLARITIES",
    "PROXY_FORMAT",
    "PROXY_FORMAT_VERSION",
    "PROXY_KINDS",
    "WIKI_JOB_FORMAT",
    "WIKI_PAGE_FORMAT",
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
    "BackendBudgetExhausted",
    "BackendUnavailable",
    "Availability",
    "Candidate",
    "Citation",
    "CitationRefKind",
    "Clock",
    "ContextEntry",
    "ContextItem",
    "ContextPack",
    "ContextSlot",
    "ContextSlotProvider",
    "CoordinateSpace",
    "Coverage",
    "DeriveRequest",
    "DerivedView",
    "Deriver",
    "Edge",
    "EntailmentChecker",
    "EdgeScope",
    "ExtraValue",
    "FixedClock",
    "IdGen",
    "IngestItem",
    "Ingestor",
    "Locator",
    "Manifest",
    "ModelBackend",
    "ModelReply",
    "ProjectionPolicy",
    "ProjectionStore",
    "Projector",
    "ProviderUnavailable",
    "ProxyFormatError",
    "ProxyImport",
    "ProxySegment",
    "RecallBudget",
    "RebuildResult",
    "RecallProvider",
    "RefKind",
    "SeededIdGen",
    "SlotRequest",
    "SourceDimensions",
    "StageResult",
    "StagingError",
    "SystemClock",
    "UsageSignal",
    "UsageSink",
    "UuidIdGen",
    "WikiBuild",
    "WikiClaim",
    "WikiFormatError",
    "WikiJob",
    "WikiPage",
    "WikiSource",
    "candidate_directory",
    "candidate_id",
    "check_name",
    "check_view_citations",
    "compute_pack_version",
    "default_registry",
    "edge_set_version",
    "edge_visible",
    "item_content_hash",
    "load_proxy_import",
    "missing_modules",
    "pack_version_payload",
    "parse_proxy_import",
    "parse_wiki_job",
    "parse_wiki_page",
    "register",
    "stage_candidates",
    "validate_candidate",
]
