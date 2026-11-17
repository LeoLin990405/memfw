"""``memory_search``: argument checks; the Runtime runs every mode and builds the rows."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.protocol import _client_message
from memfw.mcp.schema import SEARCH_POLICIES

# Row building and freshness moved to ``memfw.runtime.search`` (A3b); these
# names stay importable here and from ``memfw.mcp.server`` (read-only aliases:
# a test patches ``memfw.runtime.search``).
from memfw.runtime.search import (
    _FRESHNESS_READ_ATTEMPTS,
    _RAW_HASH_CACHE,
    _frontmatter_version,
    _RawHashCache,
)

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


_MAX_SCOPE_CHARS = 512


def _checked_scope(scope: object) -> str:
    """Same rule as HTTP ``/search``: optional string of at most 512 characters."""
    if scope is None:
        return ""
    if not isinstance(scope, str):
        raise ValueError("scope must be a string")
    if len(scope) > _MAX_SCOPE_CHARS:
        raise ValueError("scope too long")
    return scope.strip()


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
        if not query.strip():
            raise ValueError("query required")
        if policy is None or policy == "":
            policy = "compat_fts"  # absent, as before this argument existed
        if policy not in SEARCH_POLICIES:
            raise ValueError("unknown policy")
        if mode != "two_stage" and policy != "compat_fts":
            raise ValueError("policy only applies to mode=two_stage")
        scope = _checked_scope(scope)
        if source and self.allowed_sources and source not in self.allowed_sources:
            raise ValueError("source is not allowed for this account")
        repository = self._repo()
        result: dict[str, Any] = self.runtime.search(
            repository,
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
        )
        return result
