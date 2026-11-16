"""State every MCP tool group shares: the vault root, tenant, sources and Runtime."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from memfw.core.errors import MemfwError, NotFoundError
from memfw.core.models import Layer, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.notes import HttpEmbeddingProvider
from memfw.runtime import build_runtime

# Re-exported for the tool groups, so each import line stays in one module.
__all__ = [
    "GatewayBase",
    "HttpEmbeddingProvider",
    "Layer",
    "MemfwError",
    "MemoryDocument",
    "NotFoundError",
    "Repository",
    "Tier",
]


class GatewayBase:
    """The bound vault and caller; ``memfw.mcp.server.Gateway`` composes the tool groups."""

    def __init__(
        self,
        root: Path | str,
        *,
        embedder: HttpEmbeddingProvider | None = None,
        tenant: str | None = None,
        entity_enabled: bool | None = None,
        allowed_sources: tuple[str, ...] | None = None,
        app: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.app = (app if app is not None else os.environ.get("MEMFW_APP", "")).strip()
        self.tenant = tenant if tenant is not None else os.environ.get("MEMFW_TENANT", "")
        self.entity_enabled = (
            entity_enabled
            if entity_enabled is not None
            else os.environ.get("MEMFW_ENTITY_ENABLED", "").lower() in {"1", "true", "yes"}
        )
        configured_sources = os.environ.get("MEMFW_ALLOWED_SOURCES", "")
        self.allowed_sources = (
            allowed_sources
            if allowed_sources is not None
            else tuple(item.strip() for item in configured_sources.split(",") if item.strip())
        )
        if embedder is None:
            endpoint = os.environ.get("MEMFW_EMBEDDING_URL")
            if endpoint:
                embedder = HttpEmbeddingProvider(
                    endpoint,
                    model=os.environ.get("MEMFW_EMBEDDING_MODEL", "embedding-model"),
                    dimension=int(os.environ.get("MEMFW_EMBEDDING_DIMENSION", "1024")),
                )
        self.embedder = embedder
        # The composition root owns the recall wiring and the index check: one
        # Runtime per Gateway, one InflightRegistry per Runtime. Every
        # ``mode=two_stage`` call shares it, so a hung provider holds at most
        # ``max_inflight`` worker threads per Runtime, not one per call.
        self.runtime = build_runtime(
            self.root,
            tenant=self.tenant,
            allowed_sources=self.allowed_sources,
            embedder=self.embedder,
            entity_enabled=self.entity_enabled,
        )
        self.dream_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._last_staged: float | None = None
        self.history_refresh_seconds = float(
            os.environ.get("MEMFW_HISTORY_REFRESH_SECONDS", "30") or 0
        )

    def _repo(self) -> Repository:
        """Open an existing vault. A missing root is an error; never init one."""
        root = self.root
        if not root.is_dir() or not (root / "vault.yaml").is_file():
            raise ValueError("vault root does not exist")
        return Repository(root)

    def _visible(self, document: MemoryDocument) -> bool:
        """In the bound tenant and, when ``MEMFW_ALLOWED_SOURCES`` is set, an allowed source."""

        if document.tenant != self.tenant:
            return False
        return not self.allowed_sources or document.source in self.allowed_sources
