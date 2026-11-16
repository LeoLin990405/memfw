"""The memfw recall coordination layer (compat framework §3, R6 v4 PR-2a).

Not wired into MCP, HTTP or the CLI yet: today's search paths are unchanged.
See ``docs/ports-and-recall.md``.

Importing this package does not import memfw internals. The built-in
providers and the vault resolver live in ``memfw.recall.builtin``, the only
module here that reaches the vault and retrievers.
"""

from __future__ import annotations

from .coordinator import (
    DEFAULT_TOTAL_TIMEOUT,
    Coordinator,
    InflightRegistry,
    ProviderConfig,
    ProviderReport,
    RecallHit,
    RecallLabels,
    RecallMetrics,
    RecallResult,
    Router,
)
from .fusion import DEFAULT_RRF_K, FusedHit, rrf_fuse
from .labels import (
    CONFLICTED_LABEL,
    OPEN_QUESTION_LABEL_PREFIX,
    OpenQuestionIndex,
    open_question_labels,
)
from .policy import (
    DEFAULT_CLEARANCE,
    DROP_REASONS,
    FilterPolicy,
    MemoryMeta,
    Verdict,
    classify_privacy,
    evaluate,
    parse_clearance,
)
from .resolve import MemoryPathResolver, PathResolver, Resolution, Resolver
from .routing import AllProvidersRouter, DefaultRouter, RouteDecision, classify_query

__all__ = [
    "CONFLICTED_LABEL",
    "DEFAULT_CLEARANCE",
    "DEFAULT_RRF_K",
    "DEFAULT_TOTAL_TIMEOUT",
    "DROP_REASONS",
    "AllProvidersRouter",
    "Coordinator",
    "DefaultRouter",
    "FilterPolicy",
    "FusedHit",
    "InflightRegistry",
    "MemoryMeta",
    "MemoryPathResolver",
    "OPEN_QUESTION_LABEL_PREFIX",
    "OpenQuestionIndex",
    "PathResolver",
    "ProviderConfig",
    "ProviderReport",
    "RecallHit",
    "RecallLabels",
    "RecallMetrics",
    "RecallResult",
    "Resolution",
    "Resolver",
    "RouteDecision",
    "Router",
    "Verdict",
    "classify_privacy",
    "classify_query",
    "evaluate",
    "open_question_labels",
    "parse_clearance",
    "rrf_fuse",
]
