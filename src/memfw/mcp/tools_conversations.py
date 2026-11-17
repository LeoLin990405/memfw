"""The shared conversation store the ``conversations_*`` tools read."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase

__all__ = ["ConversationTools"]


class ConversationTools(GatewayBase):
    """``conversations_search``, ``_list``, ``_get`` and ``_handoff`` (read-only)."""

    def _conversations(self) -> Any:
        """The shared conversation store as this server's app sees it (``Runtime``)."""

        return self.runtime.conversation_share(self.app)
