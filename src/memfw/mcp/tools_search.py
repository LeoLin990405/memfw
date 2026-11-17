"""``memory_search``: argument parsing; the Runtime checks them, runs the mode, builds rows."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.protocol import _client_message

# Row building and freshness moved to ``memfw.runtime.search`` (A3b). The
# underscore names are deprecated aliases of its public names, kept importable
# here and from ``memfw.mcp.server``; a test patches ``memfw.runtime.search``.
from memfw.runtime.search import FRESHNESS_READ_ATTEMPTS as _FRESHNESS_READ_ATTEMPTS
from memfw.runtime.search import RAW_HASH_CACHE as _RAW_HASH_CACHE
from memfw.runtime.search import RawHashCache as _RawHashCache
from memfw.runtime.search import frontmatter_version as _frontmatter_version

__all__ = [
    "_FRESHNESS_READ_ATTEMPTS",
    "_RAW_HASH_CACHE",
    "SearchTools",
    "_frontmatter_version",
    "_policy_argument",
    "_RawHashCache",
]


def _policy_argument(arguments: dict[str, Any]) -> str:
    """``memory_search`` ``policy``: absent, null or ``""`` means ``compat_fts``.

    Any non-string is an error.
    """

    value = arguments.get("policy")
    if value is None or value == "":
        return "compat_fts"
    if not isinstance(value, str):
        raise ValueError("policy must be a string")
    return value


class SearchTools(GatewayBase):
    """``memory_search`` (fts, hybrid, vector, multihop, two_stage)."""

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        mode: str = "hybrid",
        source: str = "",
        include_history: bool = False,
        as_of: str | None = None,
        rounds: int | None = None,
        judge: object | None = None,
        scope: str = "",
        policy: str = "compat_fts",
        labels: bool = False,
    ) -> dict[str, Any]:
        # This delivery does not score hits. An injected judge is ignored.
        del judge
        # The Runtime checks the arguments (same messages) before it opens the vault.
        result: dict[str, Any] = self.runtime.search(
            query,
            limit,
            mode=mode,
            source=source,
            include_history=include_history,
            as_of=as_of,
            rounds=rounds,
            scope=scope,
            policy=policy,
            labels=labels,
            client_message=_client_message,
            app=self.app,
        )
        return result
