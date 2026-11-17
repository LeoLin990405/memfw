"""``memory_used`` (C5): a usage signal, off unless the vault or environment enables it."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.schema import MEMORY_USED_TOOL, TOOLS


class UsageTools(GatewayBase):
    """``memory_used`` and the tool list it may extend."""

    def usage_enabled(self) -> bool:
        """``memory_used`` is listed and callable (the Runtime decides, default off)."""

        return self.runtime.usage_enabled()

    def tools(self) -> list[dict[str, Any]]:
        """``tools/list``: exactly ``TOOLS`` unless the usage signal is enabled."""

        if not self.usage_enabled():
            return TOOLS
        return [*TOOLS, MEMORY_USED_TOOL]

    def memory_used(self, ids: object, session_ref: object) -> dict[str, Any]:
        """Record one signal (the Runtime checks and filters the ids); ``{"ok": true}``."""

        return self.runtime.memory_used(ids, session_ref, app=self.app)
