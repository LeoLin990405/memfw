"""``memory_get``, ``memory_remember``, ``memory_related`` and ``memory_status``."""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase


class MemoryTools(GatewayBase):
    """Single-memory tools and the vault summary (built by the Runtime)."""

    def get(self, memory_id: str) -> dict[str, Any]:
        document = self._repo().get(memory_id)
        if not self._visible(document):
            raise ValueError("memory is not visible in this tenant")
        return {
            "id": document.id,
            "title": document.title,
            "body": document.body,
            "layer": document.layer.value,
            "tier": document.tier.value,
            "type": document.type,
            "source": document.source,
            "provenance": document.provenance,
            "tags": list(document.tags),
            # So a caller can see that a causal claim is only a hypothesis.
            "epistemic": document.epistemic,
            "causal": document.causal,
            "causal_record": (
                document.causal_record.model_dump(mode="json")
                if document.causal_record is not None
                else None
            ),
            "limitations": list(document.limitations),
            "experience": (
                document.experience.model_dump(mode="json")
                if document.experience is not None
                else None
            ),
        }

    def remember(
        self,
        content: str,
        title: str = "memory",
        *,
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.runtime.remember(content, title, tags=tags, related=related)

    def related(self, memory_id: str, limit: int = 10) -> dict[str, Any]:
        return self.runtime.related(self._repo(), memory_id, limit)

    def status(self) -> dict[str, Any]:
        return self.runtime.status(self._repo())
