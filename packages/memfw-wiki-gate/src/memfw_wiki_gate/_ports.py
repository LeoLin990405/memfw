"""The one place this package takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw (the
standalone wheels) from the ``memfw-ports`` wheel's ``memfw_ports``. Only L0
port modules are imported: the wiki formats, citation refs and privacy levels.
"""

from __future__ import annotations

try:
    from memfw.ports.privacy import PRIVACY_LEVELS, strictest
    from memfw.ports.types import CAUSAL_VALUES, Citation
    from memfw.ports.wiki import (
        RELEASABLE_PRIVACY,
        WIKI_CAUSAL_CAP,
        WIKI_SECTIONS,
        WikiBuild,
        WikiClaim,
        WikiFormatError,
        WikiJob,
        WikiPage,
        WikiSource,
        canonical_json,
        parse_wiki_job,
        wiki_sources_digest,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import (  # type: ignore[no-redef,assignment,import-not-found,unused-ignore]
        PRIVACY_LEVELS,
        strictest,
    )
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,import-not-found,unused-ignore]
        CAUSAL_VALUES,
        Citation,
    )
    from memfw_ports.wiki import (  # type: ignore[no-redef,assignment,import-not-found,unused-ignore]
        RELEASABLE_PRIVACY,
        WIKI_CAUSAL_CAP,
        WIKI_SECTIONS,
        WikiBuild,
        WikiClaim,
        WikiFormatError,
        WikiJob,
        WikiPage,
        WikiSource,
        canonical_json,
        parse_wiki_job,
        wiki_sources_digest,
    )

__all__ = [
    "CAUSAL_VALUES",
    "PRIVACY_LEVELS",
    "RELEASABLE_PRIVACY",
    "WIKI_CAUSAL_CAP",
    "WIKI_SECTIONS",
    "Citation",
    "WikiBuild",
    "WikiClaim",
    "WikiFormatError",
    "WikiJob",
    "WikiPage",
    "WikiSource",
    "canonical_json",
    "parse_wiki_job",
    "strictest",
    "wiki_sources_digest",
]
