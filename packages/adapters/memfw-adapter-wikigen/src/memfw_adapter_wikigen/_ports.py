"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw
(the standalone wheels) from the ``memfw-ports`` wheel's ``memfw_ports``.
Either way only L0 port modules are imported, nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.models import BackendBudgetExhausted, BackendUnavailable, ModelReply
    from memfw.ports.privacy import strictest
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.testing import ContractError, check_wiki_page
    from memfw.ports.types import (
        CAUSAL_VALUES,
        Availability,
        Citation,
        DerivedView,
        DeriveRequest,
        IngestItem,
    )
    from memfw.ports.wiki import (
        MAX_CITES_PER_CLAIM,
        MAX_CLAIM_CHARS,
        MAX_JOB_BYTES,
        WIKI_CAUSAL_CAP,
        WIKI_PAGE_FORMAT,
        WikiClaim,
        WikiFormatError,
        WikiJob,
        WikiPage,
        parse_wiki_job,
        parse_wiki_page,
        wiki_sources_digest,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.models import (  # type: ignore[no-redef,assignment,unused-ignore]
        BackendBudgetExhausted,
        BackendUnavailable,
        ModelReply,
    )
    from memfw_ports.privacy import strictest  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.testing import (  # type: ignore[no-redef,assignment,unused-ignore]
        ContractError,
        check_wiki_page,
    )
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        CAUSAL_VALUES,
        Availability,
        Citation,
        DerivedView,
        DeriveRequest,
        IngestItem,
    )
    from memfw_ports.wiki import (  # type: ignore[no-redef,assignment,unused-ignore]
        MAX_CITES_PER_CLAIM,
        MAX_CLAIM_CHARS,
        MAX_JOB_BYTES,
        WIKI_CAUSAL_CAP,
        WIKI_PAGE_FORMAT,
        WikiClaim,
        WikiFormatError,
        WikiJob,
        WikiPage,
        parse_wiki_job,
        parse_wiki_page,
        wiki_sources_digest,
    )

__all__ = [
    "CAUSAL_VALUES",
    "MAX_CITES_PER_CLAIM",
    "MAX_CLAIM_CHARS",
    "MAX_JOB_BYTES",
    "WIKI_CAUSAL_CAP",
    "WIKI_PAGE_FORMAT",
    "AdapterSpec",
    "Availability",
    "BackendBudgetExhausted",
    "BackendUnavailable",
    "Citation",
    "ContractError",
    "DeriveRequest",
    "DerivedView",
    "IngestItem",
    "ModelReply",
    "WikiClaim",
    "WikiFormatError",
    "WikiJob",
    "WikiPage",
    "check_wiki_page",
    "parse_wiki_job",
    "parse_wiki_page",
    "strictest",
    "wiki_sources_digest",
]
