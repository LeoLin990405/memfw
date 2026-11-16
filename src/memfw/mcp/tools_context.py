"""``memory_context`` and the ``memfw://context/<slot>`` resources, built by the Runtime."""

from __future__ import annotations

import json
from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.schema import (
    CONTEXT_POLICIES,
    CONTEXT_RESOURCE_PREFIX,
    CONTEXT_SLOTS,
    RESOURCE_POLICIES,
    RESOURCES,
)


class ResourceQueryError(ValueError):
    """A ``resources/read`` URI with a query other than ``policy=<known policy>``."""


class ContextTools(GatewayBase):
    """``memory_context`` and the context resources."""

    def context(
        self,
        limit_tokens: int = 2000,
        *,
        v: int | None = 1,
        policy: str | None = "compat_fts",
        slots: Any = None,
    ) -> dict[str, Any]:
        """``memory_context``. v1 (default): the original payload, unchanged.

        ``v=2`` returns a ``ContextPack`` filtered with ``policy``
        (``compat_fts`` default, or ``strict``) with the requested ``slots``
        (default ``["core"]``, in the given order); the Runtime builds it.
        """

        version, policy_name = self.runtime.context_request(v, policy)
        names = self.runtime.context_slots_request(version, slots)
        repository = self._repo()
        if version == 1:
            return self.runtime.context_v1(repository, limit_tokens=limit_tokens)
        pack = self.runtime.context_pack(
            repository,
            policy_name=policy_name,
            limit_tokens=int(limit_tokens),
            slots=names,
            app=self.app,
        )
        return {"v": 2, "limit_tokens": limit_tokens, **pack.as_dict()}

    def resources(self) -> list[dict[str, Any]]:
        """``resources/list``."""

        return RESOURCES

    def read_resource(self, uri: str) -> dict[str, Any] | None:
        """``resources/read``: one slot's v=2 pack, or ``None`` for an unknown URI.

        ``memfw://context/<slot>`` or ``memfw://context/<slot>?policy=<name>``
        (``compat_fts`` or ``strict``; any other query raises
        ``ResourceQueryError``: fail closed). Without a query the policy is
        ``RESOURCE_POLICIES`` for the slot: ``strict`` for ``topics`` (its counts
        would otherwise include local-private memories), ``compat_fts`` for the
        others. The same caller as ``memory_context`` (app, tenant, sources)
        and the default budget.
        """

        if not uri.startswith(CONTEXT_RESOURCE_PREFIX):
            return None
        rest = uri[len(CONTEXT_RESOURCE_PREFIX) :]
        slot, has_query, query = rest.partition("?")
        if slot not in CONTEXT_SLOTS:
            return None
        policy = RESOURCE_POLICIES[slot]
        if has_query:
            key, has_value, value = query.partition("=")
            if key != "policy" or not has_value or value not in CONTEXT_POLICIES:
                raise ResourceQueryError("resource query must be policy=compat_fts or strict")
            policy = value
        payload = self.context(v=2, policy=policy, slots=[slot])
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }
