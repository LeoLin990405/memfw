"""``memory_context``: v1 payload or a v2 ``ContextPack``, built by the Runtime."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase


class ContextTools(GatewayBase):
    """``memory_context``."""

    def context(
        self, limit_tokens: int = 2000, *, v: int | None = 1, policy: str | None = "compat_fts"
    ) -> dict[str, Any]:
        """``memory_context``. v1 (default): the original payload, unchanged.

        ``v=2`` returns a ``ContextPack`` filtered with ``policy``
        (``compat_fts`` default, or ``strict``); the Runtime builds it.
        """

        version, policy_name = self.runtime.context_request(v, policy)
        repository = self._repo()
        if version == 1:
            return self.runtime.context_v1(repository, limit_tokens=limit_tokens)
        pack = self.runtime.context_pack(
            repository, policy_name=policy_name, limit_tokens=int(limit_tokens)
        )
        return {"v": 2, "limit_tokens": limit_tokens, **pack.as_dict()}
