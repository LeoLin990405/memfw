"""The shared conversation store the ``conversations_*`` tools read."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.protocol import _flag

__all__ = ["ConversationTools", "conversation_search_options"]

#: Optional ``conversations_search`` arguments (M3c); an absent one is not passed.
_TEXT_OPTIONS = ("mode", "task_type", "outcome", "project", "component", "intent_like")
_INT_OPTIONS = ("windows", "window_turns", "max_bytes")
_FLAG_OPTIONS = ("archived_only", "with_archive_state")


def conversation_search_options(args: dict[str, Any]) -> dict[str, Any]:
    """The M3c options a ``conversations_search`` call names (none: the call as before)."""

    out: dict[str, Any] = {}
    for key in _TEXT_OPTIONS:
        if args.get(key):
            out[key] = str(args[key])
    for key in _INT_OPTIONS:
        if args.get(key) is not None:
            out[key] = int(args[key])
    for key in _FLAG_OPTIONS:
        if _flag(args, key, False):
            out[key] = True
    return out


class ConversationTools(GatewayBase):
    """``conversations_search``, ``_list``, ``_get`` and ``_handoff`` (read-only)."""

    def _conversations(self) -> Any:
        """The shared conversation store as this server's app sees it (``Runtime``)."""

        return self.runtime.conversation_share(self.app)
