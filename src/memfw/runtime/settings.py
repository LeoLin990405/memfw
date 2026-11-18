"""Interface settings from arguments or the environment (moved from the MCP server, A3b).

``resolve_settings`` is what ``memfw.mcp`` used to do in ``Gateway.__init__``:
an explicit argument wins, otherwise the ``MEMFW_*`` variable is read. The
values (and the order they are read in, so the same bad variable fails the
same way) are unchanged; ``build_runtime`` takes the result.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from memfw.ports.privacy import parse_clearance

from .usage import env_switch

#: Privacy clearance of the MCP server (MM0): comma separated levels.
CLEARANCE_ENV = "MEMFW_MCP_CLEARANCE"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """What one interface instance runs with."""

    app: str
    tenant: str
    entity_enabled: bool
    allowed_sources: tuple[str, ...]
    #: An embedding provider, or None (no vectors: hybrid falls back to FTS).
    embedder: Any
    #: ``MEMFW_USAGE_SIGNAL`` (C5), or None: ``vault.yaml`` ``runtime.usage.enabled`` decides.
    usage_signal: bool | None = None
    #: ``MEMFW_RECORD_ACCESS`` (C6), or None: ``runtime.search.record_access`` decides.
    record_access: bool | None = None
    #: Privacy levels the interface may return; None filters nothing (MM0).
    clearance: frozenset[str] | None = None
    #: ``MEMFW_EMBEDDING_ALLOW_REMOTE``, or None: ``runtime.embedding.allow_remote`` decides.
    embedding_allow_remote: bool | None = None


def embedder_from_env(environ: Mapping[str, str]) -> Any:
    """``HttpEmbeddingProvider`` from ``MEMFW_EMBEDDING_*``, or None without a URL."""

    endpoint = environ.get("MEMFW_EMBEDDING_URL")
    if not endpoint:
        return None
    # Imported here: only a configured endpoint loads the notes package.
    from memfw.notes import HttpEmbeddingProvider

    return HttpEmbeddingProvider(
        endpoint,
        model=environ.get("MEMFW_EMBEDDING_MODEL", "embedding-model"),
        dimension=int(environ.get("MEMFW_EMBEDDING_DIMENSION", "1024")),
    )


def resolve_settings(
    *,
    embedder: Any = None,
    tenant: str | None = None,
    entity_enabled: bool | None = None,
    allowed_sources: tuple[str, ...] | None = None,
    app: str | None = None,
    usage_signal: bool | None = None,
    record_access: bool | None = None,
    environ: Mapping[str, str] | None = None,
    clearance: str | frozenset[str] | None = None,
    embedding_allow_remote: bool | None = None,
) -> RuntimeSettings:
    """Explicit values, else ``MEMFW_APP``, ``MEMFW_TENANT``, ``MEMFW_ENTITY_ENABLED``,
    ``MEMFW_ALLOWED_SOURCES`` (comma separated), ``MEMFW_EMBEDDING_*``,
    ``MEMFW_USAGE_SIGNAL`` and ``MEMFW_RECORD_ACCESS`` (booleans; unset or empty
    leaves the choice to ``vault.yaml``, see ``memfw.runtime.usage``) and
    ``MEMFW_MCP_CLEARANCE`` (comma separated privacy levels; unset filters
    nothing, a malformed value narrows to ``public``) and
    ``MEMFW_EMBEDDING_ALLOW_REMOTE`` (a boolean; unset leaves it to
    ``vault.yaml`` ``runtime.embedding.allow_remote``, default off)."""

    env = os.environ if environ is None else environ
    resolved_app = (app if app is not None else env.get("MEMFW_APP", "")).strip()
    resolved_tenant = tenant if tenant is not None else env.get("MEMFW_TENANT", "")
    resolved_entity = (
        entity_enabled
        if entity_enabled is not None
        else env.get("MEMFW_ENTITY_ENABLED", "").lower() in {"1", "true", "yes"}
    )
    configured_sources = env.get("MEMFW_ALLOWED_SOURCES", "")
    resolved_sources = (
        allowed_sources
        if allowed_sources is not None
        else tuple(item.strip() for item in configured_sources.split(",") if item.strip())
    )
    if embedder is None:
        embedder = embedder_from_env(env)
    if usage_signal is None:
        usage_signal = env_switch(env, "MEMFW_USAGE_SIGNAL")
    if record_access is None:
        record_access = env_switch(env, "MEMFW_RECORD_ACCESS")
    if embedding_allow_remote is None:
        embedding_allow_remote = env_switch(env, "MEMFW_EMBEDDING_ALLOW_REMOTE")
    if clearance is None:
        resolved_clearance = parse_clearance(env.get(CLEARANCE_ENV))
    elif isinstance(clearance, frozenset):
        resolved_clearance = clearance
    else:
        resolved_clearance = parse_clearance(clearance)
    return RuntimeSettings(
        app=resolved_app,
        tenant=resolved_tenant,
        entity_enabled=resolved_entity,
        allowed_sources=resolved_sources,
        embedder=embedder,
        usage_signal=usage_signal,
        record_access=record_access,
        clearance=resolved_clearance,
        embedding_allow_remote=embedding_allow_remote,
    )


def history_refresh_seconds(environ: Mapping[str, str] | None = None) -> float:
    """``MEMFW_HISTORY_REFRESH_SECONDS`` (default 30; empty means 0)."""

    env = os.environ if environ is None else environ
    return float(env.get("MEMFW_HISTORY_REFRESH_SECONDS", "30") or 0)


__all__ = ["RuntimeSettings", "embedder_from_env", "history_refresh_seconds", "resolve_settings"]
