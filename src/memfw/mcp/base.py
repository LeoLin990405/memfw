"""State every MCP tool group shares: the vault root, tenant, sources and Runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memfw.runtime import build_runtime, history_refresh_seconds, resolve_settings

__all__ = ["GatewayBase"]


class GatewayBase:
    """The bound vault and caller; ``memfw.mcp.server.Gateway`` composes the tool groups.

    Settings (explicit arguments, else ``MEMFW_*``) are resolved by the
    runtime (``memfw.runtime.resolve_settings``); ``tenant``,
    ``allowed_sources``, ``embedder`` and ``entity_enabled`` are read-only
    views of ``self.runtime``'s values. Every capability, the vault opener
    included, comes from ``self.runtime``.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        embedder: Any = None,
        tenant: str | None = None,
        entity_enabled: bool | None = None,
        allowed_sources: tuple[str, ...] | None = None,
        app: str | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        settings = resolve_settings(
            embedder=embedder,
            tenant=tenant,
            entity_enabled=entity_enabled,
            allowed_sources=allowed_sources,
            app=app,
        )
        self.app = settings.app
        # The composition root owns the recall wiring and the index check: one
        # Runtime per Gateway, one InflightRegistry per Runtime. Every
        # ``mode=two_stage`` call shares it, so a hung provider holds at most
        # ``max_inflight`` worker threads per Runtime, not one per call.
        self.runtime = build_runtime(
            self.root,
            tenant=settings.tenant,
            allowed_sources=settings.allowed_sources,
            embedder=settings.embedder,
            entity_enabled=settings.entity_enabled,
            usage_signal=settings.usage_signal,
            record_access=settings.record_access,
        )
        self.dream_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._last_staged: float | None = None
        self.history_refresh_seconds = history_refresh_seconds()

    # Read-only views of the Runtime's settings: one source of truth.

    @property
    def tenant(self) -> str:
        return self.runtime.tenant

    @property
    def allowed_sources(self) -> tuple[str, ...]:
        return self.runtime.allowed_sources

    @property
    def embedder(self) -> Any:
        return self.runtime.embedder

    @property
    def entity_enabled(self) -> bool:
        return self.runtime.entity_enabled

    def _repo(self) -> Any:
        """Open an existing vault (``Runtime.open_repository``): never init one.

        Opened per call from the configured root, so a symlinked or relative
        root is followed per call.
        """
        return self.runtime.open_repository()

    def _visible(self, document: Any) -> bool:
        """In the bound tenant and, when ``MEMFW_ALLOWED_SOURCES`` is set, an allowed source."""

        return self.runtime.visible(document)
