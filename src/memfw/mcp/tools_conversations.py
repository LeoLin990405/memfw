"""The shared conversation store the ``conversations_*`` tools read."""

from __future__ import annotations

from memfw.conversations import OPERATOR, ConversationShare, SharePolicy, open_share
from memfw.mcp.base import GatewayBase

# Re-exported for the history tools (same visibility rules).
__all__ = ["OPERATOR", "ConversationShare", "ConversationTools", "SharePolicy"]


class ConversationTools(GatewayBase):
    """``conversations_search``, ``_list``, ``_get`` and ``_handoff`` (read-only)."""

    def _conversations(self) -> ConversationShare:
        """The shared conversation store as this server's app sees it."""

        if not self.app:
            raise ValueError("conversation tools need the calling app: pass --app or MEMFW_APP")
        if self.app.strip().lower() == OPERATOR:
            raise ValueError(f"'{OPERATOR}' is reserved for the memfw CLI; name the app instead")
        self._repo()
        return open_share(self.root, self.app)
