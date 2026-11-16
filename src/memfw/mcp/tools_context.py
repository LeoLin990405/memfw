"""``memory_context`` and the ``memfw://context/<slot>`` resources, built by the Runtime."""

from __future__ import annotations

import json
from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.schema import CONTEXT_RESOURCE_PREFIX, CONTEXT_SLOTS, RESOURCES


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

        The same caller as ``memory_context`` (app, tenant, sources), the
        default policy (``compat_fts``) and the default budget.
        """

        if not uri.startswith(CONTEXT_RESOURCE_PREFIX):
            return None
        slot = uri[len(CONTEXT_RESOURCE_PREFIX) :]
        if slot not in CONTEXT_SLOTS:
            return None
        payload = self.context(v=2, slots=[slot])
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }
